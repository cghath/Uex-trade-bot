"""Recommendation Outcome Tracking (Phase 1, local-only): a "Track this route" button on a
route recommendation opens a private thread, walks the user leg-by-leg asking whether each
one matched what was quoted, and writes confirmed outcomes back into the same
terminal_market_state/terminal_market_observations tables the intelligence collector uses
(tagged source='player_report' - see bot/uex/route_progression.py) so evidence classification
and route confidence benefit immediately, without ever submitting anything to UEX itself.

Known limitation, accepted for this phase: leg-outcome views are NOT Discord persistent
views (matching this codebase's existing ConfirmListingView/ConfirmDeleteListingView, which
aren't either). A bot restart while a thread has an unclicked leg prompt breaks those
specific buttons until the 48h abandonment poller eventually sweeps the thread - a real gap,
not a silent one, and one Phase 1.5 (persistent views with custom_ids) would close.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import json
import logging

import discord
from discord.ext import commands, tasks

from bot.uex.route_progression import (
    SUPPRESSION_HOURS,
    describe_leg_outcome,
    is_reportable_amount,
    terminal_state_update_for_outcome,
    update_confirms_depletion,
)

logger = logging.getLogger("uexbot.route_progression")

# Checked every ABANDONMENT_POLL_HOURS for any in_progress thread idle longer than
# ABANDONMENT_HOURS - matches this codebase's usual "check periodically, act on a coarser
# threshold" pollers (e.g. intelligence.py's data-health snapshot cadence).
ABANDONMENT_POLL_HOURS = 6
ABANDONMENT_HOURS = 48

MAX_TRACKABLE_ROUTES = 5

# A leg-outcome button/modal's real commit point (claim() + the Discord acknowledgement)
# happens BEFORE handle_leg_outcome/abandon_thread ever runs - by the time either of those
# raises, the user has already been told "reported" and the leg is locked. Retrying the
# whole call is safe (every step it performs - the outcome/market-state upserts, the
# thread-status update - is idempotent), so a transient DB lock or Discord hiccup gets a
# few automatic chances to resolve itself instead of stranding the thread on the very
# first blip. See _record_leg_outcome_durably/_abandon_thread_durably.
POST_ACK_RETRY_ATTEMPTS = 3
POST_ACK_RETRY_DELAY_SECONDS = 2.0

# How often the durable recovery queue is retried once POST_ACK_RETRY_ATTEMPTS are
# exhausted for an action - short enough that a transient outage recovers well before the
# 48h abandonment poller would otherwise be the only thing that ever touches the thread
# again, matching this codebase's other short-interval background pollers (e.g.
# negotiation_alerts.py's 5 min, personal_inventory.py's 5 min).
RECOVERY_POLL_MINUTES = 15


def _embed_with_outcome(embed: discord.Embed, outcome_line: str) -> discord.Embed:
    """Append a reported-outcome line under a leg prompt's existing 'Quoted: ...' text,
    rather than replacing it - so the box keeps showing both what was quoted and what
    actually happened, in the same message it's always lived in."""
    new_embed = embed.copy()
    new_embed.description = f"{embed.description}\n\n{outcome_line}" if embed.description else outcome_line
    return new_embed


@dataclass
class RouteLegInput:
    """What a route-sending cog (prices.py, trends.py, ...) supplies per leg to start
    tracking. quoted_status is UEX's own 1-7 band at recommendation time, if known."""
    side: str  # 'buy' | 'sell'
    id_terminal: int
    id_commodity: int
    terminal_name: str
    commodity_name: str
    display_label: str
    quoted_price: float | None
    quoted_scu: float | None
    quoted_status: int | None
    # The terminal's real quoted stock/demand at recommendation time, when it differs from
    # quoted_scu - only set by callers whose quoted_scu is a PLANNED cargo allocation
    # rather than genuine market data (/mixed-routes, /multi-stop-route; see
    # terminal_state_update_for_outcome's own docstring). None for callers where
    # quoted_scu already IS the real market figure directly (/best-route, /top-routes).
    market_scu: float | None = None


def _leg_input_from_snapshot(leg_dict: dict) -> RouteLegInput:
    """Rebuilds a RouteLegInput from one entry of a persisted route_snapshot (or an
    equally-shaped dict from the recovery queue) - the DB-only path used whenever
    RouteProgression._active_legs doesn't have the thread anymore (a bot restart, or a
    recovery-queue retry running well after the original interaction)."""
    return RouteLegInput(
        side=leg_dict["side"], id_terminal=leg_dict["id_terminal"], id_commodity=leg_dict["id_commodity"],
        terminal_name=leg_dict["terminal_name"], commodity_name=leg_dict["commodity_name"],
        display_label=leg_dict["display_label"], quoted_price=leg_dict.get("quoted_price"),
        quoted_scu=leg_dict.get("quoted_scu"), quoted_status=leg_dict.get("quoted_status"),
        market_scu=leg_dict.get("market_scu"),
    )


@dataclass
class TrackableRoute:
    route_kind: str  # 'best_route' | 'top_routes' | 'mixed_routes' | 'multi_stop_route'
    title: str
    legs: list[RouteLegInput]


class ActualAmountModal(discord.ui.Modal):
    scu_input = discord.ui.TextInput(label="Actual SCU", placeholder="e.g. 40", required=True, max_length=10)
    price_input = discord.ui.TextInput(
        label="Actual price per unit (optional)", required=False, max_length=12
    )

    def __init__(
        self, *, cog: "RouteProgression", thread_id: int, leg_index: int, leg: RouteLegInput, flow: str,
        parent_view: "LegOutcomeView",
    ) -> None:
        title = "How much was actually there?" if flow == "less" else "How much did you take?"
        super().__init__(title=title)
        self.cog = cog
        self.thread_id = thread_id
        self.leg_index = leg_index
        self.leg = leg
        self.flow = flow
        self.parent_view = parent_view

    async def on_submit(self, interaction: discord.Interaction) -> None:
        # The button that opened this modal never locked the leg (see LegOutcomeView) -
        # cancelling out of a modal with no submission must leave the leg reportable, not
        # stuck. This is the first real check of whether the leg is still open.
        if self.parent_view.resolved:
            await interaction.response.send_message("This leg was already reported.", ephemeral=True)
            return
        try:
            actual_scu = float(str(self.scu_input.value).strip())
        except ValueError:
            await interaction.response.send_message("That SCU value isn't a number - try again.", ephemeral=True)
            return
        # float() happily parses "inf"/"nan" (not a ValueError) - reject those and any
        # negative SCU here, before the leg is ever claimed, rather than letting a
        # malformed report reach shared terminal_market_state. Zero stays valid (a
        # legitimate "nothing was there" report).
        if not is_reportable_amount(actual_scu):
            await interaction.response.send_message(
                "That SCU value has to be a real, non-negative number - try again.", ephemeral=True
            )
            return
        actual_price: float | None = None
        price_text = str(self.price_input.value).strip()
        if price_text:
            try:
                actual_price = float(price_text)
            except ValueError:
                await interaction.response.send_message("That price isn't a number - try again.", ephemeral=True)
                return
            if not is_reportable_amount(actual_price):
                await interaction.response.send_message(
                    "That price has to be a real, non-negative number - try again.", ephemeral=True
                )
                return

        if self.flow == "less":
            # The true commit point for this flow - claim the leg now, not when the button
            # that opened this modal was clicked.
            if not self.parent_view.claim():
                await interaction.response.send_message("This leg was already reported.", ephemeral=True)
                return
            outcome = "missing" if actual_scu <= 0 else "less"
            try:
                await interaction.response.send_message("Got it, thanks for reporting.", ephemeral=True)
            except discord.HTTPException:
                # Failed BEFORE handle_leg_outcome ever ran - nothing was persisted, so the
                # claim this modal just took must be released, not left stranding the leg
                # as "already reported" forever. See LegOutcomeView.release_claim.
                self.parent_view.release_claim()
                raise
            outcome_line = describe_leg_outcome(outcome=outcome, actual_price=actual_price, actual_scu=actual_scu)
            await self.parent_view.disable_in_background(outcome_line)
            await self.cog._record_leg_outcome_durably(
                interaction.channel, self.thread_id, self.leg_index, self.leg,
                outcome=outcome, actual_price=actual_price, actual_scu=actual_scu,
            )
        else:
            # "more" still doesn't commit here - MoreOutcomeFollowupView's own buttons are
            # the real commit point for this flow, same reasoning as this modal itself.
            await interaction.response.send_message(
                "One more thing - did you take everything there was, or did something else "
                "stop you first (your cargo hold, or the terminal itself)?",
                view=MoreOutcomeFollowupView(
                    cog=self.cog, thread_id=self.thread_id, leg_index=self.leg_index, leg=self.leg,
                    actual_price=actual_price, actual_scu=actual_scu, parent_view=self.parent_view,
                ),
                ephemeral=True,
            )


class MoreOutcomeFollowupView(discord.ui.View):
    """Only shown after 'More than quoted' - resolves whether the confirmed SCU is an exact
    post-leg figure (terminal drained) or just a lower bound (the player's own cargo hold,
    or the terminal's demand, capped them first). See terminal_state_update_for_outcome for
    why a floor is never written back to terminal_market_state as if it were exact."""

    def __init__(
        self, *, cog: "RouteProgression", thread_id: int, leg_index: int, leg: RouteLegInput,
        actual_price: float | None, actual_scu: float, parent_view: "LegOutcomeView",
    ) -> None:
        super().__init__(timeout=300)
        self.cog = cog
        self.thread_id = thread_id
        self.leg_index = leg_index
        self.leg = leg
        self.actual_price = actual_price
        self.actual_scu = actual_scu
        self.parent_view = parent_view

    async def _claim(self, interaction: discord.Interaction) -> bool:
        # Delegates to the ORIGINAL LegOutcomeView's claim - that's the one true "is this
        # leg resolved yet" flag, not a second independent one on this follow-up view.
        if not self.parent_view.claim():
            await interaction.response.send_message("This leg was already reported.", ephemeral=True)
            return False
        for item in self.children:
            item.disabled = True
        return True

    def _release(self) -> None:
        # Mirror of LegOutcomeView.release_claim for this follow-up view's OWN buttons -
        # the parent's claim also needs releasing, since _claim() delegated to it above.
        self.parent_view.release_claim()
        for item in self.children:
            item.disabled = False

    @discord.ui.button(label="Terminal was drained", style=discord.ButtonStyle.red)
    async def drained(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if not await self._claim(interaction):
            return
        try:
            await interaction.response.edit_message(view=self)
        except discord.HTTPException:
            self._release()
            raise
        outcome_line = describe_leg_outcome(
            outcome="more", actual_price=self.actual_price, actual_scu=self.actual_scu, precision="exact",
        )
        await self.parent_view.disable_in_background(outcome_line)
        await self.cog._record_leg_outcome_durably(
            interaction.channel, self.thread_id, self.leg_index, self.leg,
            outcome="more", actual_price=self.actual_price, actual_scu=self.actual_scu, precision="exact",
        )

    @discord.ui.button(label="I was capped, more was there", style=discord.ButtonStyle.gray)
    async def capacity_limited(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if not await self._claim(interaction):
            return
        try:
            await interaction.response.edit_message(view=self)
        except discord.HTTPException:
            self._release()
            raise
        outcome_line = describe_leg_outcome(
            outcome="more", actual_price=self.actual_price, actual_scu=self.actual_scu, precision="floor",
        )
        await self.parent_view.disable_in_background(outcome_line)
        await self.cog._record_leg_outcome_durably(
            interaction.channel, self.thread_id, self.leg_index, self.leg,
            outcome="more", actual_price=self.actual_price, actual_scu=self.actual_scu, precision="floor",
        )


class LegOutcomeView(discord.ui.View):
    """Posted for the CURRENT leg only. 'Less'/'More' open a modal for the actual figure -
    'missing' is derived from a 'Less' report of exactly 0, not a separate button, since the
    two are otherwise identical (see ActualAmountModal.on_submit)."""

    def __init__(self, *, cog: "RouteProgression", thread_id: int, leg_index: int, leg: RouteLegInput) -> None:
        super().__init__(timeout=None)
        self.cog = cog
        self.thread_id = thread_id
        self.leg_index = leg_index
        self.leg = leg
        self.resolved = False
        # Set by RouteProgression._post_leg_prompt right after sending, so a commit that
        # happens via a DIFFERENT interaction (a modal submit, or the More-outcome
        # follow-up view's own buttons) can still push the disabled-button state here -
        # those interactions are tied to their own ephemeral message, not this one.
        self.message: discord.Message | None = None

    def claim(self) -> bool:
        # Check-then-set, matching ConfirmListingView/ConfirmDeleteListingView in
        # bot/cogs/marketplace.py - asyncio is single-threaded with no await between the
        # check and the set, so a second racing commit always observes the first one's
        # write. Only ever called at a TRUE commit point (this button directly writes the
        # outcome, or a later modal/follow-up interaction does) - never when merely
        # opening a modal, which is what caused a cancelled-without-submitting modal to
        # permanently lock the leg with no outcome ever recorded.
        if self.resolved:
            return False
        self.resolved = True
        for item in self.children:
            item.disabled = True
        return True

    def release_claim(self) -> None:
        # Undo an in-flight claim() when the acknowledgement meant to make it durable
        # failed BEFORE persistence (handle_leg_outcome/abandon_thread never ran) - the
        # leg must stay reportable, not permanently "already reported" with nothing ever
        # recorded. Never call this once persistence has actually happened; there is no
        # DB-side undo, and releasing a claim after the fact would let a retry duplicate
        # an outcome that's already saved.
        self.resolved = False
        for item in self.children:
            item.disabled = False

    async def disable_in_background(self, outcome_line: str | None = None) -> None:
        if self.message is not None:
            try:
                edit_kwargs: dict = {"view": self}
                if outcome_line is not None and self.message.embeds:
                    edit_kwargs["embed"] = _embed_with_outcome(self.message.embeds[0], outcome_line)
                await self.message.edit(**edit_kwargs)
            except discord.HTTPException:
                pass

    @discord.ui.button(label="Matched quote", style=discord.ButtonStyle.green)
    async def matched(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if not self.claim():
            await interaction.response.send_message("This leg was already reported.", ephemeral=True)
            return
        edit_kwargs: dict = {"view": self}
        if self.message is not None and self.message.embeds:
            edit_kwargs["embed"] = _embed_with_outcome(
                self.message.embeds[0], describe_leg_outcome(outcome="matched")
            )
        try:
            await interaction.response.edit_message(**edit_kwargs)
        except discord.HTTPException:
            # The ack failed BEFORE handle_leg_outcome ran, so nothing was persisted -
            # release the claim so a retry can still record the outcome. See
            # release_claim's own docstring for why this is only safe pre-persistence.
            self.release_claim()
            raise
        await self.cog._record_leg_outcome_durably(
            interaction.channel, self.thread_id, self.leg_index, self.leg, outcome="matched"
        )

    @discord.ui.button(label="Less / not there", style=discord.ButtonStyle.red)
    async def less(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        # Deliberately does NOT claim the leg - opening the modal isn't a commitment, and
        # claiming here is exactly what left a cancelled-without-submitting modal stuck.
        if self.resolved:
            await interaction.response.send_message("This leg was already reported.", ephemeral=True)
            return
        await interaction.response.send_modal(
            ActualAmountModal(
                cog=self.cog, thread_id=self.thread_id, leg_index=self.leg_index, leg=self.leg,
                flow="less", parent_view=self,
            )
        )

    @discord.ui.button(label="More than quoted", style=discord.ButtonStyle.blurple)
    async def more(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if self.resolved:
            await interaction.response.send_message("This leg was already reported.", ephemeral=True)
            return
        await interaction.response.send_modal(
            ActualAmountModal(
                cog=self.cog, thread_id=self.thread_id, leg_index=self.leg_index, leg=self.leg,
                flow="more", parent_view=self,
            )
        )

    @discord.ui.button(label="Abandon route", style=discord.ButtonStyle.gray, row=1)
    async def abandon(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        # Same pattern as opening a Less/More modal: this only asks for confirmation, it
        # doesn't claim the leg - AbandonConfirmView's own confirm button is the real
        # commit point, via the same parent_view.claim() every other commit path uses.
        if self.resolved:
            await interaction.response.send_message("This leg was already reported.", ephemeral=True)
            return
        await interaction.response.send_message(
            "Abandon tracking this route? This closes the thread and can't be undone.",
            view=AbandonConfirmView(cog=self.cog, thread_id=self.thread_id, parent_view=self),
            ephemeral=True,
        )


class AbandonConfirmView(discord.ui.View):
    """Confirm/cancel gate in front of a real, irreversible action - same pattern as
    ConfirmDeleteListingView in bot/cogs/marketplace.py."""

    def __init__(self, *, cog: "RouteProgression", thread_id: int, parent_view: "LegOutcomeView") -> None:
        super().__init__(timeout=60)
        self.cog = cog
        self.thread_id = thread_id
        self.parent_view = parent_view

    @discord.ui.button(label="Yes, abandon this route", style=discord.ButtonStyle.red)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if not self.parent_view.claim():
            await interaction.response.send_message(
                "This leg was already reported - the route can't be abandoned anymore.", ephemeral=True
            )
            return
        for item in self.children:
            item.disabled = True
        try:
            await interaction.response.edit_message(view=self)
        except discord.HTTPException:
            # Failed BEFORE abandon_thread's own DB write ever ran - release the claim
            # (and re-enable this confirmation view's own buttons) so a retry can still
            # go through, rather than stranding the leg as claimed with nothing recorded.
            self.parent_view.release_claim()
            for item in self.children:
                item.disabled = False
            raise
        await self.parent_view.disable_in_background("**Reported:** Route abandoned.")
        await self.cog._abandon_thread_durably(interaction.channel, self.thread_id, reason="you asked to stop tracking it")

    @discord.ui.button(label="No, keep going", style=discord.ButtonStyle.gray)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        for item in self.children:
            item.disabled = True
        await interaction.response.edit_message(content="Continuing to track this route.", view=self)


class RouteTrackingView(discord.ui.View):
    """Attach to a route-recommendation embed - one 'Track' button per route (up to
    MAX_TRACKABLE_ROUTES), each opening its own private thread. Built with plain
    discord.ui.Button instances (not the @discord.ui.button decorator) since the number of
    routes shown varies per command call."""

    def __init__(self, cog: "RouteProgression", routes: list[TrackableRoute]) -> None:
        super().__init__(timeout=900)
        self.cog = cog
        routes = routes[:MAX_TRACKABLE_ROUTES]
        self.routes = routes
        for index, route in enumerate(routes):
            label = "Track this route" if len(routes) == 1 else f"Track route #{index + 1}"
            button: discord.ui.Button = discord.ui.Button(
                label=label, style=discord.ButtonStyle.blurple, row=0,
            )
            button.callback = self._make_callback(route)
            self.add_item(button)

    def _make_callback(self, route: TrackableRoute):
        async def callback(interaction: discord.Interaction) -> None:
            await self.cog.start_tracking(interaction, route)
        return callback


class RouteProgression(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        # Legs for an in-progress thread, keyed by thread_id - populated at thread creation,
        # not reconstructed from route_snapshot after a restart (the same non-persistence
        # limitation as the Views themselves; see the module docstring).
        self._active_legs: dict[int, list[RouteLegInput]] = {}
        self.poll_abandoned_threads.start()
        self.retry_pending_route_progression_actions.start()

    def cog_unload(self) -> None:
        self.poll_abandoned_threads.cancel()
        self.retry_pending_route_progression_actions.cancel()

    async def start_tracking(self, interaction: discord.Interaction, route: TrackableRoute) -> None:
        channel = interaction.channel
        if not isinstance(channel, discord.TextChannel):
            await interaction.response.send_message(
                "Route tracking only works from a regular server text channel.", ephemeral=True
            )
            return
        await interaction.response.defer(ephemeral=True)
        try:
            thread = await channel.create_thread(
                name=f"Route: {route.title}"[:100],
                type=discord.ChannelType.private_thread,
                invitable=False,
                auto_archive_duration=1440,
            )
            await thread.add_user(interaction.user)
        except discord.HTTPException as exc:
            logger.warning("Failed to create route-tracking thread: %s", exc)
            await interaction.followup.send(
                "Couldn't create a private thread for tracking (missing permissions?).", ephemeral=True
            )
            return

        route_snapshot = {
            "title": route.title,
            "legs": [
                {
                    "side": leg.side, "id_terminal": leg.id_terminal, "id_commodity": leg.id_commodity,
                    "terminal_name": leg.terminal_name, "commodity_name": leg.commodity_name,
                    "display_label": leg.display_label, "quoted_price": leg.quoted_price,
                    "quoted_scu": leg.quoted_scu, "quoted_status": leg.quoted_status,
                    # Kept here (not just in route_progression_legs, which has no column
                    # for it) so a leg can be fully reconstructed from the DB alone -
                    # needed by _get_leg's restart-safe fallback and by the recovery
                    # queue's reconstruction. See terminal_state_update_for_outcome's own
                    # docstring for why this must stay separate from quoted_scu.
                    "market_scu": leg.market_scu,
                }
                for leg in route.legs
            ],
        }
        await self.bot.db.create_route_progression_thread(
            thread_id=thread.id, user_id=interaction.user.id, guild_id=interaction.guild_id,
            route_kind=route.route_kind, route_snapshot=route_snapshot,
            legs=[
                {
                    "side": leg.side, "id_terminal": leg.id_terminal, "id_commodity": leg.id_commodity,
                    "quoted_price": leg.quoted_price, "quoted_scu": leg.quoted_scu,
                    "quoted_status": leg.quoted_status,
                }
                for leg in route.legs
            ],
        )
        self._active_legs[thread.id] = route.legs

        # Post the full route breakdown the user actually picked - interaction.message is
        # the message the "Track this route" button was attached to, carrying the same
        # embed /best-route just sent (price, cargo, confidence, warnings, everything) -
        # before the leg-by-leg flow starts, not just a bare title.
        if interaction.message is not None and interaction.message.embeds:
            await thread.send(embed=interaction.message.embeds[0])
        await thread.send(
            f"Tracking **{route.title}** - report each leg as you complete it. This thread "
            "closes automatically once every leg is reported (or after "
            f"{ABANDONMENT_HOURS:g}h of inactivity)."
        )
        await self._post_leg_prompt(thread, thread.id, 0, route.legs[0])
        await interaction.followup.send(f"Started tracking in {thread.mention}.", ephemeral=True)

    async def _get_leg(self, thread_id: int, leg_index: int) -> RouteLegInput | None:
        """Looks up a leg's full input, preferring the in-memory cache (populated at
        thread creation) but falling back to the persisted route_snapshot when it isn't
        there - which is what lets a retry, or a recovery-queue poll tick, still complete
        correctly after a bot restart wipes _active_legs. This closes that gap only for
        the ALREADY-answered legs' downstream steps; a currently-unclicked prompt's own
        buttons are still dead after a restart (see the module docstring)."""
        legs = self._active_legs.get(thread_id)
        if legs is not None and leg_index < len(legs):
            return legs[leg_index]
        thread_row = await self.bot.db.get_route_progression_thread(thread_id)
        if thread_row is None:
            return None
        snapshot_legs = json.loads(thread_row["route_snapshot"]).get("legs", [])
        if leg_index >= len(snapshot_legs):
            return None
        return _leg_input_from_snapshot(snapshot_legs[leg_index])

    async def _post_leg_prompt(
        self, thread: discord.Thread, thread_id: int, leg_index: int, leg: RouteLegInput
    ) -> None:
        # Idempotent: only the caller that wins the DB claim for this leg_index actually
        # sends anything - see claim_route_progression_advance. Makes a second call for
        # the same leg_index (a retried handle_leg_outcome, or the recovery poller redoing
        # a step whose earlier Discord send actually succeeded but whose success response
        # never reached us) a silent no-op instead of a duplicate live prompt.
        if not await self.bot.db.claim_route_progression_advance(thread_id, to_index=leg_index):
            return
        if leg.quoted_price is not None and leg.quoted_scu is not None:
            quoted_line = f"Quoted: {leg.quoted_price:,.2f} aUEC/unit · {leg.quoted_scu:,.0f} SCU"
        else:
            quoted_line = "No quoted figures were available for this leg."
        embed = discord.Embed(
            title=f"Leg {leg_index + 1}: {leg.display_label}",
            description=quoted_line,
            color=discord.Color.blurple(),
        )
        view = LegOutcomeView(cog=self, thread_id=thread_id, leg_index=leg_index, leg=leg)
        view.message = await thread.send(embed=embed, view=view)

    async def handle_leg_outcome(
        self,
        channel: discord.abc.MessageableChannel,
        thread_id: int,
        leg_index: int,
        leg: RouteLegInput,
        *,
        outcome: str,
        actual_price: float | None = None,
        actual_scu: float | None = None,
        precision: str | None = None,
    ) -> None:
        recorded = await self.bot.db.record_route_progression_leg_outcome(
            thread_id=thread_id, leg_index=leg_index, outcome=outcome,
            actual_price=actual_price, actual_scu=actual_scu, precision=precision,
        )
        if not recorded:
            # The outcome UPDATE's own WHERE outcome IS NULL guard means this leg was
            # already recorded - either by a genuinely different report racing this one
            # (two live prompts for the same leg), or by an EARLIER attempt of this exact
            # retry chain that got this far before failing on a later step. Only the
            # second case is safe to continue past - compare against what's actually
            # stored to tell them apart.
            stored = await self.bot.db.get_route_progression_leg(thread_id, leg_index)
            is_same_report = (
                stored is not None
                and stored["outcome"] == outcome
                and stored["actual_price"] == actual_price
                and stored["actual_scu"] == actual_scu
                and stored["precision"] == precision
            )
            if not is_same_report:
                # A different report won the race. It already drove the market-state
                # write and the next-leg/completion step - writing THIS report's values
                # now would silently contradict what route_progression_legs actually
                # recorded, which is exactly the "conflicting reports overwrite each
                # other" failure mode this guard exists to prevent.
                if isinstance(channel, discord.Thread):
                    try:
                        await channel.send(
                            "This leg was already reported (likely from a duplicate prompt) - "
                            "this report was not recorded, to avoid overwriting the earlier one."
                        )
                    except discord.HTTPException:
                        pass
                return
            # Same report, retried - fall through. The market-state re-merge below is a
            # safe no-op/idempotent re-write of the identical values; the next-leg/
            # completion step is separately guarded by claim_route_progression_advance,
            # so this finishes whichever part of the earlier attempt never completed.

        update_row = terminal_state_update_for_outcome(
            id_commodity=leg.id_commodity, id_terminal=leg.id_terminal,
            commodity_name=leg.commodity_name, terminal_name=leg.terminal_name,
            side=leg.side, outcome=outcome,
            quoted_price=leg.quoted_price, quoted_scu=leg.quoted_scu, quoted_status=leg.quoted_status,
            actual_price=actual_price, actual_scu=actual_scu, precision=precision,
            market_scu=leg.market_scu,
        )
        if update_row is not None:
            await self.bot.db.record_player_report_market_update(update_row)
        if update_confirms_depletion(update_row, side=leg.side):
            until = (datetime.now(timezone.utc) + timedelta(hours=SUPPRESSION_HOURS)).strftime("%Y-%m-%d %H:%M:%S")
            await self.bot.db.suppress_terminal_market_side(
                id_commodity=leg.id_commodity, id_terminal=leg.id_terminal, side=leg.side, until=until,
            )

        thread_row = await self.bot.db.get_route_progression_thread(thread_id)
        if thread_row is None:
            return
        next_index = leg_index + 1
        total_legs = thread_row["total_legs"]
        if next_index >= total_legs:
            if not await self.bot.db.claim_route_progression_advance(thread_id, to_index=total_legs):
                return  # completion already handled by an earlier attempt
            await self.bot.db.set_route_progression_thread_status(thread_id, "completed")
            self._active_legs.pop(thread_id, None)
            if isinstance(channel, discord.Thread):
                try:
                    await channel.send("Route complete - thanks for reporting! This thread will archive now.")
                    await channel.edit(archived=True, locked=False)
                except discord.HTTPException as exc:
                    logger.warning("Failed to close completed thread %s: %s", thread_id, exc)
            return
        next_leg = await self._get_leg(thread_id, next_index)
        if next_leg is not None and isinstance(channel, discord.Thread):
            await self._post_leg_prompt(channel, thread_id, next_index, next_leg)

    async def _record_leg_outcome_durably(
        self,
        channel: discord.abc.MessageableChannel,
        thread_id: int,
        leg_index: int,
        leg: RouteLegInput,
        *,
        outcome: str,
        actual_price: float | None = None,
        actual_scu: float | None = None,
        precision: str | None = None,
    ) -> None:
        """The real commit point every leg-outcome button/modal calls, in place of
        handle_leg_outcome directly - by the time this runs, the user has already seen
        the leg acknowledged as "reported" (claim() + the Discord edit already
        succeeded), so a failure here can no longer fall back on "nothing happened yet,
        let them retry the click." handle_leg_outcome's own steps are all safe to repeat
        (record_route_progression_leg_outcome only ever commits the FIRST write for a
        leg, and the next-leg/completion step is separately guarded by
        claim_route_progression_advance - see both), so retrying the whole call gives a
        transient DB lock or Discord hiccup a few chances to resolve before giving up.
        If every attempt fails, the action is durably queued
        (queue_route_progression_leg_recovery) rather than left to the 48h abandonment
        poller as the only recourse - retry_pending_route_progression_actions keeps
        retrying it on its own short cadence, fully reconstructed from the DB, with no
        dependency on this process's _active_legs cache."""
        last_exc: BaseException | None = None
        for attempt in range(1, POST_ACK_RETRY_ATTEMPTS + 1):
            try:
                await self.handle_leg_outcome(
                    channel, thread_id, leg_index, leg,
                    outcome=outcome, actual_price=actual_price, actual_scu=actual_scu, precision=precision,
                )
                return
            except Exception as exc:
                last_exc = exc
                logger.warning(
                    "handle_leg_outcome attempt %d/%d failed for thread %s leg %d: %s",
                    attempt, POST_ACK_RETRY_ATTEMPTS, thread_id, leg_index, exc,
                )
                if attempt < POST_ACK_RETRY_ATTEMPTS:
                    await asyncio.sleep(POST_ACK_RETRY_DELAY_SECONDS)
        logger.error(
            "handle_leg_outcome permanently failed for thread %s leg %d after %d attempts",
            thread_id, leg_index, POST_ACK_RETRY_ATTEMPTS, exc_info=last_exc,
        )
        try:
            await self.bot.db.queue_route_progression_leg_recovery(
                thread_id=thread_id, leg_index=leg_index, side=leg.side,
                id_terminal=leg.id_terminal, id_commodity=leg.id_commodity,
                terminal_name=leg.terminal_name, commodity_name=leg.commodity_name,
                display_label=leg.display_label, quoted_price=leg.quoted_price,
                quoted_scu=leg.quoted_scu, quoted_status=leg.quoted_status, market_scu=leg.market_scu,
                outcome=outcome, actual_price=actual_price, actual_scu=actual_scu, precision=precision,
            )
        except Exception:
            logger.exception("Failed to queue durable recovery for thread %s leg %d", thread_id, leg_index)
        if isinstance(channel, discord.Thread):
            try:
                await channel.send(
                    "Something went wrong saving that report - it's been queued to retry "
                    "automatically in the background, so no further action is needed."
                )
            except discord.HTTPException:
                pass

    async def abandon_thread(
        self, channel: discord.abc.MessageableChannel | None, thread_id: int, *, reason: str
    ) -> None:
        """Single close-and-archive path for both a user-initiated 'Abandon route' and the
        48h inactivity poller - the DB status is set first either way, so a channel this
        bot can no longer reach (kicked, thread deleted) still stops being tracked."""
        await self.bot.db.set_route_progression_thread_status(thread_id, "abandoned")
        self._active_legs.pop(thread_id, None)
        if not isinstance(channel, discord.Thread):
            return
        try:
            await channel.send(f"This route-tracking thread was abandoned ({reason}).")
            await channel.edit(archived=True, locked=False)
        except discord.HTTPException as exc:
            logger.warning("Failed to close abandoned thread %s: %s", thread_id, exc)

    async def _abandon_thread_durably(
        self, channel: discord.abc.MessageableChannel | None, thread_id: int, *, reason: str
    ) -> None:
        """Same post-ack retry discipline as _record_leg_outcome_durably, for
        AbandonConfirmView.confirm's own commit point - abandon_thread's DB write is a
        plain idempotent status update, safe to repeat. Exhausted retries queue a durable
        recovery action the same way (queue_route_progression_abandon_recovery)."""
        last_exc: BaseException | None = None
        for attempt in range(1, POST_ACK_RETRY_ATTEMPTS + 1):
            try:
                await self.abandon_thread(channel, thread_id, reason=reason)
                return
            except Exception as exc:
                last_exc = exc
                logger.warning(
                    "abandon_thread attempt %d/%d failed for thread %s: %s",
                    attempt, POST_ACK_RETRY_ATTEMPTS, thread_id, exc,
                )
                if attempt < POST_ACK_RETRY_ATTEMPTS:
                    await asyncio.sleep(POST_ACK_RETRY_DELAY_SECONDS)
        logger.error(
            "abandon_thread permanently failed for thread %s after %d attempts",
            thread_id, POST_ACK_RETRY_ATTEMPTS, exc_info=last_exc,
        )
        try:
            await self.bot.db.queue_route_progression_abandon_recovery(thread_id=thread_id, reason=reason)
        except Exception:
            logger.exception("Failed to queue durable recovery for abandoning thread %s", thread_id)
        if isinstance(channel, discord.Thread):
            try:
                await channel.send(
                    "Something went wrong abandoning this route - it's been queued to retry "
                    "automatically in the background."
                )
            except discord.HTTPException:
                pass

    @tasks.loop(hours=ABANDONMENT_POLL_HOURS)
    async def poll_abandoned_threads(self) -> None:
        stale = await self.bot.db.get_stale_route_progression_threads(older_than_hours=ABANDONMENT_HOURS)
        for row in stale:
            thread_id = row["thread_id"]
            channel: discord.abc.MessageableChannel | None
            try:
                channel = self.bot.get_channel(thread_id) or await self.bot.fetch_channel(thread_id)
            except discord.HTTPException as exc:
                logger.info("Couldn't reach thread %s to close it: %s", thread_id, exc)
                channel = None
            await self.abandon_thread(channel, thread_id, reason=f"inactive for over {ABANDONMENT_HOURS:g}h")

    @tasks.loop(minutes=RECOVERY_POLL_MINUTES)
    async def retry_pending_route_progression_actions(self) -> None:
        """Retries durably-queued actions left by an exhausted post-ack retry (see
        _record_leg_outcome_durably/_abandon_thread_durably) - each row is fully
        self-contained, so this never touches self._active_legs and keeps working even
        for a thread whose in-memory cache a bot restart wiped out. A single failed
        attempt just bumps attempts/last_attempt_at and leaves the row for the next poll
        tick; the 48h abandonment poller remains the last-resort backstop if a
        thread/channel is permanently unreachable."""
        pending = await self.bot.db.get_pending_route_progression_actions()
        for row in pending:
            action_id = row["id"]
            thread_id = row["thread_id"]
            thread_row = await self.bot.db.get_route_progression_thread(thread_id)
            if thread_row is None or thread_row["status"] != "in_progress":
                # The thread's fate was already decided some other way (completed,
                # abandoned, or its row is gone) - this queued action no longer applies.
                await self.bot.db.delete_route_progression_pending_action(action_id)
                continue
            channel: discord.abc.MessageableChannel | None
            try:
                channel = self.bot.get_channel(thread_id) or await self.bot.fetch_channel(thread_id)
            except discord.HTTPException as exc:
                logger.info("Recovery: couldn't reach thread %s: %s", thread_id, exc)
                channel = None
            try:
                if row["action_kind"] == "leg_outcome":
                    leg = _leg_input_from_snapshot(row)
                    await self.handle_leg_outcome(
                        channel, thread_id, row["leg_index"], leg,
                        outcome=row["outcome"], actual_price=row["actual_price"],
                        actual_scu=row["actual_scu"], precision=row["precision"],
                    )
                else:
                    await self.abandon_thread(channel, thread_id, reason=row["reason"])
                await self.bot.db.delete_route_progression_pending_action(action_id)
            except Exception as exc:
                logger.warning(
                    "Recovery attempt failed for pending action %s (thread %s): %s",
                    action_id, thread_id, exc,
                )
                await self.bot.db.mark_route_progression_pending_action_attempted(action_id)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(RouteProgression(bot))
