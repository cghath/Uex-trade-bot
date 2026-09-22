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

from bot.uex.backup_routes import find_backup_routes
from bot.uex.mixed_routes import find_hedge_cargo
from bot.uex.route_presentation import cargo_item_line
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
            except Exception:
                # Failed BEFORE handle_leg_outcome ever ran - nothing was persisted, so the
                # claim this modal just took must be released, not left stranding the leg
                # as "already reported" forever. See LegOutcomeView.release_claim. Any
                # failure counts, not just discord.HTTPException - a timeout or other
                # transport error leaves persistence just as un-run.
                self.parent_view.release_claim()
                raise
            outcome_line = describe_leg_outcome(outcome=outcome, actual_price=actual_price, actual_scu=actual_scu)
            await self.parent_view.disable_in_background(outcome_line)
            handled = await self.cog._record_leg_outcome_durably(
                interaction.channel, self.thread_id, self.leg_index, self.leg,
                outcome=outcome, actual_price=actual_price, actual_scu=actual_scu,
            )
            if not handled:
                # Carry-forward audit defect: neither the outcome nor its durable
                # recovery could be saved - restore the PARENT view's real reportability
                # (this modal's own ephemeral response already served its purpose).
                self.parent_view.release_claim()
                await self.parent_view.reenable_in_background()
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
        except Exception:
            # Any failure here (not just discord.HTTPException) means the claim was
            # never followed by a persisted outcome - see _claim's own release path.
            self._release()
            raise
        outcome_line = describe_leg_outcome(
            outcome="more", actual_price=self.actual_price, actual_scu=self.actual_scu, precision="exact",
        )
        await self.parent_view.disable_in_background(outcome_line)
        handled = await self.cog._record_leg_outcome_durably(
            interaction.channel, self.thread_id, self.leg_index, self.leg,
            outcome="more", actual_price=self.actual_price, actual_scu=self.actual_scu, precision="exact",
        )
        if not handled:
            # Carry-forward audit defect: neither the outcome nor its durable recovery
            # could be saved - restore the PARENT view's real reportability (this
            # follow-up view's own ephemeral message already served its purpose).
            self.parent_view.release_claim()
            await self.parent_view.reenable_in_background()

    @discord.ui.button(label="I was capped, more was there", style=discord.ButtonStyle.gray)
    async def capacity_limited(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if not await self._claim(interaction):
            return
        try:
            await interaction.response.edit_message(view=self)
        except Exception:
            # Same reasoning as MoreOutcomeFollowupView.drained above - any failure,
            # not just discord.HTTPException, leaves the outcome unpersisted.
            self._release()
            raise
        outcome_line = describe_leg_outcome(
            outcome="more", actual_price=self.actual_price, actual_scu=self.actual_scu, precision="floor",
        )
        await self.parent_view.disable_in_background(outcome_line)
        handled = await self.cog._record_leg_outcome_durably(
            interaction.channel, self.thread_id, self.leg_index, self.leg,
            outcome="more", actual_price=self.actual_price, actual_scu=self.actual_scu, precision="floor",
        )
        if not handled:
            self.parent_view.release_claim()
            await self.parent_view.reenable_in_background()


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
        # Audit-confirmed carry-forward defect: every caller of this (the "less"/"more"
        # outcome flows and AbandonConfirmView.confirm) invokes it AFTER their own
        # acknowledgement succeeds but BEFORE the real durable write - so an exception
        # escaping this purely cosmetic edit doesn't just fail the edit, it skips that
        # durable call entirely. This used to catch only discord.HTTPException, on the
        # (correct as far as it went, but incomplete) reasoning that this method itself
        # holds no claim to release on failure - true, but irrelevant to whether letting a
        # DIFFERENT exception type escape blocks a REQUIRED next step in every caller.
        # There's nothing durable riding on this edit actually landing (the real state is
        # the DB write right after it, and self.resolved is already set by claim() before
        # this ever runs), so any failure here is safe to just swallow - not just
        # discord.HTTPException.
        if self.message is not None:
            try:
                edit_kwargs: dict = {"view": self}
                if outcome_line is not None and self.message.embeds:
                    edit_kwargs["embed"] = _embed_with_outcome(self.message.embeds[0], outcome_line)
                await self.message.edit(**edit_kwargs)
            except Exception:
                pass

    async def reenable_in_background(self) -> None:
        """Carry-forward audit defect: when NEITHER the outcome/abandon write nor its
        durable recovery could be saved (_record_leg_outcome_durably/_abandon_thread_
        durably returning False), every caller already calls release_claim() to make the
        leg reportable again internally - but release_claim() only flips this view's own
        in-memory state (self.resolved, each item.disabled). The real Discord message
        still shows whatever disable_in_background last sent it: disabled buttons, since
        that's what claim() had already set before disable_in_background ever ran. A
        disabled component on the actual message doesn't dispatch a click at all,
        regardless of this process's own view state - so telling the user to "report this
        leg again" was false: the button they'd need to click still looked, and behaved,
        disabled. Re-sends the view (not the embed - the "Reported: ..."/"Route
        abandoned." label staying visible alongside newly-clickable buttons is a much
        smaller, more defensible imperfection than the alternative of guessing whether
        this Message object's own .embeds has been mutated by an earlier edit, which real
        discord.py Message objects never do locally but a test double easily could)."""
        if self.message is not None:
            try:
                await self.message.edit(view=self)
            except Exception:
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
        except Exception:
            # The ack failed BEFORE handle_leg_outcome ran, so nothing was persisted -
            # release the claim so a retry can still record the outcome. See
            # release_claim's own docstring for why this is only safe pre-persistence.
            # Any exception counts here, not just discord.HTTPException - a timeout or
            # other transport failure leaves persistence just as un-run.
            self.release_claim()
            raise
        handled = await self.cog._record_leg_outcome_durably(
            interaction.channel, self.thread_id, self.leg_index, self.leg, outcome="matched"
        )
        if not handled:
            # Carry-forward audit defect: neither the outcome nor its durable recovery
            # could be saved - restore reportability for real, not just internally, or
            # the recovery notice's own "report this leg again" is a dead-end.
            self.release_claim()
            await self.reenable_in_background()

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
        except Exception:
            # Failed BEFORE abandon_thread's own DB write ever ran - release the claim
            # (and re-enable this confirmation view's own buttons) so a retry can still
            # go through, rather than stranding the leg as claimed with nothing recorded.
            # Any exception counts, not just discord.HTTPException - a timeout or other
            # transport failure leaves the DB write just as un-run.
            self.parent_view.release_claim()
            for item in self.children:
                item.disabled = False
            raise
        await self.parent_view.disable_in_background("**Reported:** Route abandoned.")
        handled = await self.cog._abandon_thread_durably(
            interaction.channel, self.thread_id, reason="you asked to stop tracking it"
        )
        if not handled:
            # Carry-forward audit defect: neither the abandon nor its durable recovery
            # could be saved - restore the PARENT view's real reportability (this
            # confirm view's own ephemeral message already served its purpose).
            self.parent_view.release_claim()
            await self.parent_view.reenable_in_background()

    @discord.ui.button(label="No, keep going", style=discord.ButtonStyle.gray)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        for item in self.children:
            item.disabled = True
        await interaction.response.edit_message(content="Continuing to track this route.", view=self)


class HedgeReportModal(discord.ui.Modal):
    """A single number, not the three-button matched/less/more flow LegOutcomeView uses -
    a hedge is a best-effort side note, not the route the player explicitly asked to
    track, so it doesn't need that flow's precision (e.g. an over-quote here is always
    treated as a conservative 'floor', never asserted as an exact drain - see
    RouteProgression._record_hedge_report)."""
    scu_input = discord.ui.TextInput(label="Actual SCU", placeholder="e.g. 16", required=True, max_length=10)
    price_input = discord.ui.TextInput(
        label="Actual price per unit (optional)", required=False, max_length=12
    )

    def __init__(self, *, cog: "RouteProgression", hedge_id: int, side: str, view: "HedgeReportView") -> None:
        verb = "buy" if side == "buy" else "sell"
        super().__init__(title=f"How much did you {verb}?")
        self.cog = cog
        self.hedge_id = hedge_id
        self.side = side
        self.parent_view = view

    async def on_submit(self, interaction: discord.Interaction) -> None:
        try:
            actual_scu = float(str(self.scu_input.value).strip())
        except ValueError:
            await interaction.response.send_message("That SCU value isn't a number - try again.", ephemeral=True)
            return
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

        recorded = await self.cog._record_hedge_report(
            hedge_id=self.hedge_id, side=self.side, actual_price=actual_price, actual_scu=actual_scu,
        )
        if not recorded:
            await interaction.response.send_message("This was already reported.", ephemeral=True)
            return
        await interaction.response.send_message("Got it, thanks for reporting.", ephemeral=True)
        await self.parent_view.disable_in_background()


class HedgeReportView(discord.ui.View):
    """Deliberately has no claim()/release_claim() dance like LegOutcomeView - a hedge
    report is a best-effort side note, never part of the tracked route's own
    idempotency-critical sequence, and Database.record_hedge_side_outcome's own
    WHERE-guarded UPDATE already makes a double-submit a harmless no-op at the DB level.
    The button is still disabled after a successful report so the UI doesn't invite a
    pointless second click. No timeout (unlike LegOutcomeView's implicit default) since a
    hedge's sell side may only become reportable much later, when the player reaches the
    destination leg."""
    message: discord.Message | None = None

    def __init__(self, *, cog: "RouteProgression", hedge_id: int, side: str) -> None:
        super().__init__(timeout=None)
        self.cog = cog
        self.hedge_id = hedge_id
        self.side = side
        verb = "bought" if side == "buy" else "sold"
        self.report_button.label = f"Report what I {verb}"

    @discord.ui.button(label="Report", style=discord.ButtonStyle.blurple)
    async def report_button(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await interaction.response.send_modal(
            HedgeReportModal(cog=self.cog, hedge_id=self.hedge_id, side=self.side, view=self)
        )

    async def disable_in_background(self) -> None:
        for item in self.children:
            item.disabled = True
        if self.message is not None:
            try:
                await self.message.edit(view=self)
            except Exception:
                # Purely cosmetic - by now the report is already recorded, so nothing durable rides
                # on this edit landing. Not just HTTPException: LegOutcomeView's own copy of this
                # was widened for the same reason by the 2026-09-13 audit.
                logger.info("Could not disable a hedge report button (non-critical)", exc_info=True)


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

        # Audit-confirmed defect #4: route startup has partial-failure gaps after the
        # Discord thread and/or database row are created. Everything from create_thread
        # through the first leg prompt is one try block, not several, so ANY failure past
        # thread creation rolls back through the SAME path below - a failure at add_user,
        # the DB write, either intro message, or the first leg prompt used to either
        # propagate uncaught (no handling at all) or get misreported as "couldn't create a
        # thread" when the thread had, in fact, already been created. Nothing posted up to
        # any of these points is unique/valuable - the recommendation embed this button
        # was attached to is still visible in the original channel either way - so a full
        # rollback (delete the DB row if created, delete the orphaned thread if created)
        # plus an honest, immediately-retryable message is simpler and safer than trying
        # to resume a half-built thread.
        thread: discord.Thread | None = None
        thread_row_created = False
        try:
            thread = await channel.create_thread(
                name=f"Route: {route.title}"[:100],
                type=discord.ChannelType.private_thread,
                invitable=False,
                auto_archive_duration=1440,
            )
            await thread.add_user(interaction.user)

            route_snapshot = {
                "title": route.title,
                "legs": [
                    {
                        "side": leg.side, "id_terminal": leg.id_terminal, "id_commodity": leg.id_commodity,
                        "terminal_name": leg.terminal_name, "commodity_name": leg.commodity_name,
                        "display_label": leg.display_label, "quoted_price": leg.quoted_price,
                        "quoted_scu": leg.quoted_scu, "quoted_status": leg.quoted_status,
                        # Kept here (not just in route_progression_legs, which has no
                        # column for it) so a leg can be fully reconstructed from the DB
                        # alone - needed by _get_leg's restart-safe fallback and by the
                        # recovery queue's reconstruction. See terminal_state_update_for_
                        # outcome's own docstring for why this must stay separate from
                        # quoted_scu.
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
            thread_row_created = True
            self._active_legs[thread.id] = route.legs

            # Post the full route breakdown the user actually picked - interaction.message
            # is the message the "Track this route" button was attached to, carrying the
            # same embed /best-route just sent (price, cargo, confidence, warnings,
            # everything) - before the leg-by-leg flow starts, not just a bare title.
            if interaction.message is not None and interaction.message.embeds:
                await thread.send(embed=interaction.message.embeds[0])
            await thread.send(
                f"Tracking **{route.title}** - report each leg as you complete it. This thread "
                "closes automatically once every leg is reported (or after "
                f"{ABANDONMENT_HOURS:g}h of inactivity)."
            )
            await self._post_leg_prompt(thread, thread.id, 0, route.legs[0])
        except Exception as exc:
            logger.warning(
                "Failed to fully start tracking a route (thread=%s): %s",
                getattr(thread, "id", None), exc,
            )
            if thread is None:
                # create_thread itself failed - nothing was ever created, so there's
                # nothing to roll back. This is also the single most common real failure
                # (a missing Manage Threads permission), worth a more specific hint than
                # the generic rollback message below.
                await interaction.followup.send(
                    "Couldn't create a private thread for tracking (missing permissions?).", ephemeral=True
                )
                return
            self._active_legs.pop(thread.id, None)
            db_cleaned = True
            if thread_row_created:
                try:
                    await self.bot.db.delete_route_progression_thread(thread.id)
                except Exception:
                    db_cleaned = False
                    logger.exception(
                        "Failed to clean up the DB row for a partially-started route (thread=%s)", thread.id
                    )
            thread_cleaned = True
            try:
                await thread.delete()
            except Exception:
                thread_cleaned = False
                logger.warning("Failed to clean up the orphaned thread %s", thread.id)
            if db_cleaned and thread_cleaned:
                await interaction.followup.send(
                    "Couldn't fully set up route tracking (a step failed partway through) - "
                    "nothing was left behind to get stuck; try tracking the route again.",
                    ephemeral=True,
                )
            else:
                await interaction.followup.send(
                    "Couldn't fully set up route tracking, and the automatic cleanup "
                    "afterward didn't fully succeed either - a leftover thread or "
                    "tracking record may still exist. Please let an admin know before "
                    "trying again, rather than retrying immediately.",
                    ephemeral=True,
                )
            return
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
        if leg.quoted_price is not None and leg.quoted_scu is not None:
            quoted_line = f"Quoted: {leg.quoted_price:,.2f} aUEC/unit · {leg.quoted_scu:,.0f} SCU"
        else:
            quoted_line = "No quoted figures were available for this leg."
        title = f"Leg {leg_index + 1}: {leg.display_label}"
        embed = discord.Embed(title=title, description=quoted_line, color=discord.Color.blurple())
        view = LegOutcomeView(cog=self, thread_id=thread_id, leg_index=leg_index, leg=leg)

        # Idempotent: only the caller that wins the DB claim for this leg_index actually
        # sends anything - see claim_route_progression_advance. A second call for the
        # same leg_index (a retried handle_leg_outcome, or the recovery poller redoing a
        # step) normally means an earlier attempt already succeeded and there's nothing
        # to do.
        #
        # Follow-up audit-confirmed defect (2026-09-15, finding #3): that was only true
        # when the earlier attempt actually resolved to success - a held claim can ALSO
        # mean an earlier attempt ended in genuine, still-unresolved ambiguity (both its
        # send AND its own reconciliation failed, see the except Exception branch below).
        # Blindly trusting "claim held" as proof of success there let a retry return
        # normally with nothing ever sent and nothing ever queued for recovery - this
        # function's own ambiguous-failure safety net (raise, so the caller's
        # retry-then-durable-queue logic actually engages) never got the chance to run.
        # Re-verify against Discord's own history every time the claim is already held,
        # exactly like the send-failure path below does, instead of trusting it on faith.
        if not await self.bot.db.claim_route_progression_advance(thread_id, to_index=leg_index):
            checked_ok, found = await self._find_sent_leg_prompt(thread, title)
            if found is not None:
                view.message = found
                self.bot.add_view(view, message_id=found.id)
                return
            if not checked_ok:
                logger.error(
                    "Leg prompt claim for thread %s leg %d is held from an earlier "
                    "attempt whose outcome was never resolved, and reconciling against "
                    "thread history failed again just now - still unresolved; this "
                    "route needs manual review", thread_id, leg_index,
                )
                raise RuntimeError(
                    f"leg prompt for thread {thread_id} leg {leg_index} is still "
                    "unresolved (claim held, history unreachable)"
                )
            # Confirmed genuinely absent: the earlier attempt's send never actually
            # landed. The claim is already correctly held at this leg_index (nothing to
            # re-claim), so fall through and perform the send now instead of returning.

        try:
            view.message = await thread.send(embed=embed, view=view)
        except discord.HTTPException:
            # A real HTTP response came back rejecting the request - Discord never
            # created the message, so it's safe to release the claim and let a retry
            # resend. See release_route_progression_advance_claim's own docstring.
            await self.bot.db.release_route_progression_advance_claim(
                thread_id, claimed_index=leg_index, revert_to=leg_index - 1
            )
            raise
        except Exception as exc:
            # No confirmed response at all (a timeout or other transport failure) - unlike
            # discord.HTTPException above, this does NOT prove the send never happened.
            # Audit-confirmed defect #3: blindly releasing here (as this used to) risks a
            # genuine duplicate live prompt if the message actually sent and only its
            # confirmation was lost. Reconcile against Discord's own message history
            # instead of guessing either way - see _find_sent_leg_prompt's docstring.
            checked_ok, found = await self._find_sent_leg_prompt(thread, title)
            if found is not None:
                # It really did send - only the confirmation was lost. thread.send()
                # never returned, so discord.py never registered this view's buttons
                # against the real message - re-attach them explicitly, and treat this
                # as success: the claim stays held, nothing is released or re-raised.
                view.message = found
                self.bot.add_view(view, message_id=found.id)
                return
            if checked_ok:
                # Confirmed genuinely absent from recent history - safe to release and
                # let a retry resend, exactly like the discord.HTTPException case above.
                await self.bot.db.release_route_progression_advance_claim(
                    thread_id, claimed_index=leg_index, revert_to=leg_index - 1
                )
                raise
            # Reconciliation itself couldn't get an answer (thread unreachable,
            # permissions lost - a second, independent failure on top of the first).
            # Genuinely ambiguous, so per this project's own "quarantine ambiguous
            # outcomes, never blindly retry" convention: hold the claim rather than risk
            # a duplicate. Unlike simply never releasing on every ambiguous failure, this
            # is the rare case that's actually unresolvable - logged loudly rather than
            # left as a silent stall, since every retry from here on will otherwise see
            # the claim already held and quietly do nothing.
            logger.error(
                "Could not confirm whether the leg prompt for thread %s leg %d actually "
                "sent (send failed with %s, and reconciling against thread history also "
                "failed) - holding its claim rather than risking a duplicate; this route "
                "needs manual review", thread_id, leg_index, type(exc).__name__,
            )
            raise

    async def _find_sent_leg_prompt(
        self, thread: discord.Thread, title: str
    ) -> tuple[bool, discord.Message | None]:
        """Reconciles an ambiguous thread.send() failure against Discord's own message
        history - the only real way to know whether a leg prompt actually sent, rather
        than guessing. Returns (True, message) if a matching message is found, (True,
        None) if recent history was checked and it's genuinely absent, or (False, None)
        if the check itself couldn't get an answer. Matches on author + exact embed title
        only, no time window - a leg's title (which encodes its leg_index) is only ever
        posted once per thread's lifetime, so any match in recent history is unambiguous
        regardless of age."""
        try:
            async for message in thread.history(limit=10):
                if (
                    message.author.id == self.bot.user.id
                    and message.embeds
                    and message.embeds[0].title == title
                ):
                    return True, message
            return True, None
        except Exception:
            return False, None

    async def _suggest_shortfall_hedge(
        self, channel: discord.abc.MessageableChannel, thread_id: int, leg_index: int,
        leg: RouteLegInput, shortfall_scu: float,
    ) -> None:
        """Best-effort only - never allowed to affect handle_leg_outcome's own critical
        path (leg persistence, market update, suppression, next-leg advance are all
        already committed by the time this runs). Suggests a commodity to fill the
        cargo space a buy-side shortfall just opened up, at the SAME origin/destination
        pair the player is already partway through - the anchored counterpart to
        stock_headroom_warning's pre-trip nudge (bot.uex.route_presentation), surfaced
        reactively once a shortfall has actually happened instead of only warned about
        in advance. The paired sell leg is read fresh from route_progression_legs (not
        self._active_legs) so this still works after a bot restart, unlike most of this
        file's in-memory state - see the module docstring's own known limitation.

        max_commodities=1 on the search below caps this at a single suggestion - a real
        UX finding (not just a simplification): showing the hedge as plain text with no
        way to actually track it left the player's confirmed-empty commodity as the only
        thing the thread would ever ask about again, silently dropping the hedge's own
        data back into the void. Recording it as a route_progression_hedges row (see that
        table's schema comment) and attaching a HedgeReportView lets the player confirm
        what actually happened, feeding real data back the same way a tracked leg does -
        capping it at one suggestion keeps that trackable via a single button instead of
        needing a button per commodity.

        Says so explicitly when no complementary commodity exists, rather than staying
        silent - a prior version returned with nothing sent, which read identically to
        "nothing happened" whether the search genuinely found nothing or silently failed;
        a player who just reported a shortfall deserves to know the space was considered
        and nothing usable was found for it, not to wonder if the report registered."""
        if shortfall_scu <= 0 or not isinstance(channel, discord.Thread):
            return
        try:
            legs = await self.bot.db.get_route_progression_legs(thread_id)
            paired_sell = next(
                (row for row in legs if row["side"] == "sell" and row["id_commodity"] == leg.id_commodity), None,
            )
            if paired_sell is None:
                return
            remaining_budget = (leg.quoted_price * shortfall_scu) if leg.quoted_price else None
            market_rows = await self.bot.db.get_mixed_route_market_rows()
            hedge = find_hedge_cargo(
                market_rows,
                origin_terminal_id=leg.id_terminal,
                destination_terminal_id=paired_sell["id_terminal"],
                exclude_commodity_id=leg.id_commodity,
                remaining_capacity_scu=shortfall_scu,
                remaining_budget=remaining_budget,
                max_commodities=1,
            )
            if not hedge:
                await channel.send(
                    f"That shortfall left ~{shortfall_scu:,.0f} SCU of cargo space unused - nothing else "
                    f"trades between **{leg.terminal_name}** and your destination right now."
                )
                return
            item = hedge[0]
            hedge_id = await self.bot.db.create_route_progression_hedge(
                thread_id=thread_id, origin_leg_index=leg_index, destination_leg_index=paired_sell["leg_index"],
                id_commodity=item.id_commodity, commodity_name=item.commodity_name,
                id_terminal_origin=leg.id_terminal, terminal_name_origin=leg.terminal_name,
                id_terminal_destination=paired_sell["id_terminal"],
                terminal_name_destination=str(item.destination.get("terminal_name") or "Unknown"),
                quoted_price_buy=item.buy_price, quoted_scu=item.quantity_scu, quoted_price_sell=item.sell_price,
                market_scu_buy=item.source.get("scu_buy"), market_scu_sell=item.destination.get("scu_sell"),
                status_buy=item.source.get("status_buy"), status_sell=item.destination.get("status_sell"),
            )
            view = HedgeReportView(cog=self, hedge_id=hedge_id, side="buy")
            view.message = await channel.send(
                f"That shortfall left ~{shortfall_scu:,.0f} SCU of cargo space unused - while "
                f"you're still at **{leg.terminal_name}**, this could fill it:\n{cargo_item_line(item)}",
                view=view,
            )
        except Exception:
            logger.info(
                "Shortfall-hedge suggestion failed for thread %s (non-critical, leg outcome already recorded)",
                thread_id, exc_info=True,
            )

    async def _suggest_sell_shortfall_reroute(
        self, channel: discord.abc.MessageableChannel, thread_id: int, leg_index: int,
        leg: RouteLegInput, shortfall_scu: float,
    ) -> None:
        """Best-effort only - same non-critical-path guarantee as _suggest_shortfall_hedge.

        A sell-side shortfall means demand at THIS terminal ran short, so the player is left
        physically holding shortfall_scu of unsold cargo. Answers "where else can I sell this"
        by reusing bot.uex.backup_routes.find_backup_routes' other_destination search, with
        the CURRENT terminal passed as both origin_terminal_id and destination_terminal_id:
        origin because that is where the held cargo would travel from, and destination because
        passing the same terminal for both makes find_backup_routes' own origin-exclusion
        (anchor_destinations never includes origin_terminal_id) correctly rule out "sell the
        rest right back here" as a candidate, while its baseline naturally prices as
        unavailable - there is no "continue as planned" once the shortfall already happened,
        so any profitable reroute qualifies rather than needing to clear MIN_DETOUR_GAIN_PCT
        over a real baseline (see find_backup_routes' own _clearly_better).

        ship_capacity_scu is deliberately capped to exactly shortfall_scu, not the player's
        real ship - the player is standing at a sell location holding a fixed amount of cargo,
        not shopping for fillers, so there is no real "capacity" question here beyond what
        they are already holding; this also means fuller_hold and without_anchor come back
        empty by construction (zero room and zero origin stock respectively), which is exactly
        right for this use - only other_destination is ever shown.

        Unlike the buy-side hedge, this is a plain suggestion, never tracked with a button or
        a route_progression_hedges row: that table's confirmation flow piggybacks onto the
        anchor route's own NEXT leg, and a sell-side reroute has no such next leg to attach a
        confirmation to - the tracked route already ends at (or continues past) this shortfall."""
        if shortfall_scu <= 0 or not isinstance(channel, discord.Thread):
            return
        try:
            legs = await self.bot.db.get_route_progression_legs(thread_id)
            paired_buy = next(
                (row for row in legs if row["side"] == "buy" and row["id_commodity"] == leg.id_commodity), None,
            )
            if paired_buy is None:
                return
            buy_price = (
                paired_buy["actual_price"] if paired_buy["actual_price"] is not None else paired_buy["quoted_price"]
            )
            if not buy_price:
                return
            market_rows = await self.bot.db.get_mixed_route_market_rows()
            result = find_backup_routes(
                market_rows,
                origin_terminal_id=leg.id_terminal, destination_terminal_id=leg.id_terminal,
                anchor_commodity_id=leg.id_commodity, anchor_scu=shortfall_scu, anchor_buy_price=float(buy_price),
                ship_capacity_scu=shortfall_scu,
            )
            if result.other_destination is None:
                await channel.send(
                    f"That left ~{shortfall_scu:,.0f} SCU of **{leg.commodity_name}** unsold - no better "
                    f"buyer was found from **{leg.terminal_name}** right now."
                )
                return
            load = result.other_destination
            item = load.cargo[0]
            unsold_note = (
                f" (only {item.quantity_scu:,.0f} of the {shortfall_scu:,.0f} SCU you're holding - the rest "
                f"still won't sell there)" if load.anchor_unsold_scu > 0 else ""
            )
            await channel.send(
                f"That left ~{shortfall_scu:,.0f} SCU of **{leg.commodity_name}** unsold - "
                f"**{load.destination_name}** buys it{unsold_note}:\n{cargo_item_line(item)}"
            )
        except Exception:
            logger.info(
                "Sell-shortfall reroute suggestion failed for thread %s (non-critical, leg outcome already recorded)",
                thread_id, exc_info=True,
            )

    async def _post_pending_hedge_sell_prompt(
        self, channel: discord.abc.MessageableChannel, thread_id: int, leg_index: int,
    ) -> None:
        """Best-effort, additive only - same guarantee as _suggest_shortfall_hedge.
        Piggybacks a hedge's sell-side confirmation onto the anchor route's OWN next leg
        prompt, once that leg is the hedge's recorded destination and its buy side is
        already confirmed - deliberately a separate message with its own view, never
        merged into LegOutcomeView itself, so this can't affect that view's own claim
        logic."""
        if not isinstance(channel, discord.Thread):
            return
        try:
            hedge = await self.bot.db.get_pending_hedge_sell_for_leg(thread_id, leg_index)
            if hedge is None:
                return
            view = HedgeReportView(cog=self, hedge_id=hedge["id"], side="sell")
            view.message = await channel.send(
                f"Also - while you're at **{hedge['terminal_name_destination']}**, did you sell the "
                f"**{hedge['commodity_name']}** from that earlier hedge?",
                view=view,
            )
        except Exception:
            logger.info(
                "Pending hedge-sell prompt failed for thread %s leg %d (non-critical)",
                thread_id, leg_index, exc_info=True,
            )

    async def _record_hedge_report(
        self, *, hedge_id: int, side: str, actual_price: float | None, actual_scu: float,
    ) -> bool:
        """The hedge counterpart to handle_leg_outcome's outcome handling - infers
        matched/less/more/missing from a single reported SCU figure (see
        HedgeReportModal's own docstring for why this doesn't need LegOutcomeView's
        three-button precision), then writes back through record_hedge_report_market_update
        (never record_player_report_market_update - a hedge has no real route_progression_legs
        row to mark). Returns False when this side was already reported (mirrors
        record_route_progression_leg_outcome's own idempotency), true otherwise."""
        hedge = await self.bot.db.get_route_progression_hedge(hedge_id)
        if hedge is None:
            return False
        if hedge[f"{side}_outcome"] is not None:
            return False  # already reported - never write the same side's market data a second time
        quoted_scu = hedge["quoted_scu"] or 0
        precision: str | None = None
        if actual_scu <= 0:
            outcome = "missing"
        elif actual_scu < quoted_scu:
            outcome = "less"
        elif actual_scu == quoted_scu:
            outcome = "matched"
        else:
            # Conservative by construction: a single reported number can't distinguish
            # "the terminal was fully drained" from "I just stopped there" the way
            # MoreOutcomeFollowupView's two buttons do for a tracked leg - 'floor' never
            # overwrites terminal_market_state with a claimed-exact figure (see
            # terminal_state_update_for_outcome's own docstring).
            outcome, precision = "more", "floor"
        if side == "buy":
            id_terminal, terminal_name = hedge["id_terminal_origin"], hedge["terminal_name_origin"]
            quoted_price, quoted_status, market_scu = (
                hedge["quoted_price_buy"], hedge["status_buy"], hedge["market_scu_buy"],
            )
        else:
            id_terminal, terminal_name = hedge["id_terminal_destination"], hedge["terminal_name_destination"]
            quoted_price, quoted_status, market_scu = (
                hedge["quoted_price_sell"], hedge["status_sell"], hedge["market_scu_sell"],
            )
        update_row = terminal_state_update_for_outcome(
            id_commodity=hedge["id_commodity"], id_terminal=id_terminal,
            commodity_name=hedge["commodity_name"], terminal_name=terminal_name,
            side=side, outcome=outcome,
            quoted_price=quoted_price, quoted_scu=quoted_scu, quoted_status=quoted_status,
            actual_price=actual_price, actual_scu=actual_scu, precision=precision,
            market_scu=market_scu,
        )
        if update_row is not None:
            await self.bot.db.record_hedge_report_market_update(update_row)
            if update_confirms_depletion(update_row, side=side):
                until = (
                    datetime.now(timezone.utc) + timedelta(hours=SUPPRESSION_HOURS)
                ).strftime("%Y-%m-%d %H:%M:%S")
                await self.bot.db.suppress_hedge_market_side(
                    id_commodity=hedge["id_commodity"], id_terminal=id_terminal, side=side, until=until,
                )
        # Marked reported LAST, after the market write has landed. The other order (mark first,
        # then write) meant one transient failure in the market write - a database lock, say -
        # left this side permanently "already reported" with the player's data never recorded,
        # and every retry just said so. Both market writes are idempotent upserts, so a retry
        # after a failure here simply repeats them harmlessly.
        return await self.bot.db.record_hedge_side_outcome(
            hedge_id, side, outcome=outcome, actual_price=actual_price, actual_scu=actual_scu,
        )

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
        # False here (the default) whenever `recorded` is True too - the leg's outcome was
        # never written before THIS call, so nothing could have marked its market update
        # (or suppression - follow-up audit finding #2) applied yet either. Only the
        # same-report-retried fallthrough below can set either True, from the leg row it
        # already had to fetch anyway.
        market_update_already_applied = False
        suppression_already_applied = False
        if not recorded:
            # record_route_progression_leg_outcome's UPDATE can be rejected for two
            # structurally different reasons (audit-confirmed defect #3's fix added the
            # second): outcome IS NOT NULL (this leg was already recorded by someone), or
            # the thread's own status is no longer 'in_progress' (e.g. abandoned by a
            # racing action) - the SQL ANDs both conditions together, so a False return
            # doesn't say which one failed. But the two are still distinguishable from
            # what's actually stored: the status check can only be what rejected this
            # write if outcome is STILL NULL here (had it been reachable with a real
            # in_progress status, the UPDATE would have simply succeeded) - so
            # stored["outcome"] being None, on its own, proves the thread must have left
            # in_progress. This must be checked BEFORE is_same_report below, not folded
            # into "not is_same_report" - a stored outcome of None trivially never equals
            # this call's own outcome string, so treating it as "a different report won"
            # would misreport an abandonment race as a duplicate-report conflict.
            stored = await self.bot.db.get_route_progression_leg(thread_id, leg_index)
            if stored is None or stored["outcome"] is None:
                if isinstance(channel, discord.Thread):
                    try:
                        await channel.send(
                            "This route is no longer being tracked (completed or abandoned) - "
                            "this report was not recorded."
                        )
                    except discord.HTTPException:
                        pass
                return
            is_same_report = (
                stored["outcome"] == outcome
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
            # Same report, retried - fall through. The next-leg/completion step below is
            # separately guarded by claim_route_progression_advance, so this finishes
            # whichever part of the earlier attempt never completed - including when that
            # earlier attempt already carried the thread all the way to completion
            # (claim_route_progression_advance/set_route_progression_thread_status simply
            # no-op again in that case, exactly as before this fix).
            #
            # Audit-confirmed carry-forward defect: the market-state re-merge just below is
            # NOT actually a safe no-op to blindly repeat on a replay, despite reapplying
            # "the identical values" - terminal_market_state is mutable SHARED state, and
            # something else (a fresh UEX collector snapshot, a different leg touching the
            # same commodity/terminal) may have written a genuinely newer value to it in
            # the time between the original attempt and this replay. Reapplying this
            # report's own (now possibly stale) values would silently clobber that newer
            # data. market_update_applied_at is how record_player_report_market_update
            # marks that it has already run once for this leg - checked here so a replay
            # skips re-running it.
            #
            # Follow-up audit finding #2 (2026-09-15): suppression used to share that same
            # marker, gated behind the same "not market_update_already_applied" check
            # below - so if suppress_terminal_market_side alone failed AFTER the market
            # update had already committed (and marked itself applied), no later replay
            # ever retried suppression again, since market_update_already_applied being
            # True skipped that whole block, suppression call included. Tracked with its
            # own independent marker (suppression_applied_at) instead - a repeated
            # depletion report can still safely refresh the suppression window's expiry
            # on a genuine same-report replay, since suppress_terminal_market_side's own
            # marker check only blocks a SECOND replay once suppression has actually
            # succeeded once, not every retry of a failed one.
            market_update_already_applied = stored["market_update_applied_at"] is not None
            suppression_already_applied = stored["suppression_applied_at"] is not None

        update_row = terminal_state_update_for_outcome(
            id_commodity=leg.id_commodity, id_terminal=leg.id_terminal,
            commodity_name=leg.commodity_name, terminal_name=leg.terminal_name,
            side=leg.side, outcome=outcome,
            quoted_price=leg.quoted_price, quoted_scu=leg.quoted_scu, quoted_status=leg.quoted_status,
            actual_price=actual_price, actual_scu=actual_scu, precision=precision,
            market_scu=leg.market_scu,
        )
        if update_row is not None and not market_update_already_applied:
            await self.bot.db.record_player_report_market_update(
                update_row, thread_id=thread_id, leg_index=leg_index
            )
        # Independently gated from the market-state write above (finding #2) - a replay
        # whose market update already landed must still retry suppression if IT is the
        # part that previously failed.
        if update_row is not None and not suppression_already_applied and update_confirms_depletion(
            update_row, side=leg.side
        ):
            until = (
                datetime.now(timezone.utc) + timedelta(hours=SUPPRESSION_HOURS)
            ).strftime("%Y-%m-%d %H:%M:%S")
            await self.bot.db.suppress_terminal_market_side(
                id_commodity=leg.id_commodity, id_terminal=leg.id_terminal, side=leg.side, until=until,
                thread_id=thread_id, leg_index=leg_index,
            )

        # Additive only, never on the critical path above (leg persistence, market update,
        # suppression already committed by this point) - gated on market_update_already_applied
        # the same way suppression is, so a replay of an already-recorded report doesn't post a
        # duplicate suggestion. A buy-side shortfall means cargo space opened up (fill it, same
        # origin/destination pair - _suggest_shortfall_hedge); a sell-side shortfall means
        # demand ran short, so the player is left holding unsold cargo (find it a different
        # buyer - _suggest_sell_shortfall_reroute). Different problems, different searches.
        if not market_update_already_applied and outcome in ("missing", "less"):
            shortfall_scu = (leg.quoted_scu or 0) - (actual_scu or 0)
            if leg.side == "buy":
                await self._suggest_shortfall_hedge(channel, thread_id, leg_index, leg, shortfall_scu)
            else:
                await self._suggest_sell_shortfall_reroute(channel, thread_id, leg_index, leg, shortfall_scu)

        thread_row = await self.bot.db.get_route_progression_thread(thread_id)
        if thread_row is None:
            return
        next_index = leg_index + 1
        total_legs = thread_row["total_legs"]
        if next_index >= total_legs:
            if not await self.bot.db.claim_route_progression_advance(thread_id, to_index=total_legs):
                return  # completion already handled by an earlier attempt, or the thread
                        # left in_progress (e.g. abandoned) since claim_route_progression_
                        # advance now also requires it - either way, nothing to do here.
            status_set = False
            try:
                status_set = await self.bot.db.set_route_progression_thread_status(thread_id, "completed")
            except Exception:
                # The claim is not proof the status write happened - release it so a
                # retry's own claim attempt can still finish completion, instead of
                # seeing this index as already (falsely) advanced and leaving the
                # thread stuck in_progress forever. Mirrors _post_leg_prompt's own
                # release-on-definite-failure handling above.
                await self.bot.db.release_route_progression_advance_claim(
                    thread_id, claimed_index=total_legs, revert_to=leg_index
                )
                raise
            self._active_legs.pop(thread_id, None)
            if not status_set:
                # Audit-confirmed defect #3: something else (an abandonment racing this
                # same completion) already moved the thread off 'in_progress' between our
                # claim above and this write - that other action already sent its own
                # message and archived the thread. Sending "Route complete!" now on top of
                # it would be a visible, confusing contradiction for a route that isn't
                # actually being tracked anymore either way, so there's nothing further to
                # do here.
                return
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
            await self._post_pending_hedge_sell_prompt(channel, thread_id, next_index)

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
    ) -> bool:
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
        dependency on this process's _active_legs cache.

        Returns whether the outcome ended up handled at all - either recorded outright,
        durably queued for the recovery poller to keep retrying, or (follow-up audit
        finding #4, 2026-09-15) already saved despite the queue write also failing - see
        the already_saved check below for why that last case must NOT be treated the
        same as "nothing was saved." False only when the report was genuinely never
        saved anywhere: the caller must release the view's claim and restore its buttons
        in that case, or the "please report this leg again" notice below would be
        telling the user to do something the button no longer lets them do."""
        last_exc: BaseException | None = None
        for attempt in range(1, POST_ACK_RETRY_ATTEMPTS + 1):
            try:
                await self.handle_leg_outcome(
                    channel, thread_id, leg_index, leg,
                    outcome=outcome, actual_price=actual_price, actual_scu=actual_scu, precision=precision,
                )
                return True
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
        queued = False
        try:
            await self.bot.db.queue_route_progression_leg_recovery(
                thread_id=thread_id, leg_index=leg_index, side=leg.side,
                id_terminal=leg.id_terminal, id_commodity=leg.id_commodity,
                terminal_name=leg.terminal_name, commodity_name=leg.commodity_name,
                display_label=leg.display_label, quoted_price=leg.quoted_price,
                quoted_scu=leg.quoted_scu, quoted_status=leg.quoted_status, market_scu=leg.market_scu,
                outcome=outcome, actual_price=actual_price, actual_scu=actual_scu, precision=precision,
            )
            queued = True
        except Exception:
            logger.exception("Failed to queue durable recovery for thread %s leg %d", thread_id, leg_index)

        already_saved = False
        if not queued:
            # Audit-confirmed follow-up defect (finding #4, 2026-09-15): every retry
            # above re-ran handle_leg_outcome's WHOLE call, but that call's own outcome
            # write is only its FIRST step - a later step (the next-leg prompt, the
            # market/suppression writes) can keep failing on every attempt even though
            # the outcome itself was durably saved on attempt 1 (handle_leg_outcome's
            # is_same_report fall-through is exactly what makes replaying the whole call
            # safe to retry in the first place). If the queue write also failed, the
            # caller must not treat this as "nothing was saved" - reopening the view
            # would let a second, different report through, which record_route_
            # progression_leg_outcome correctly rejects as conflicting, permanently
            # abandoning the real, already-saved report's own unfinished downstream
            # work with no path left to resume it. route_progression_legs is the source
            # of truth here, not this call's own success/failure.
            stored = await self.bot.db.get_route_progression_leg(thread_id, leg_index)
            already_saved = (
                stored is not None
                and stored["outcome"] == outcome
                and stored["actual_price"] == actual_price
                and stored["actual_scu"] == actual_scu
                and stored["precision"] == precision
            )

        if isinstance(channel, discord.Thread):
            # The notice must reflect whether the queue write actually succeeded - telling
            # the user "no further action is needed" when the insert itself just raised
            # (the exact outage this queue exists for) would hide a report that's now in
            # neither route_progression_legs nor the recovery table.
            if queued:
                message = (
                    "Something went wrong saving that report - it's been queued to retry "
                    "automatically in the background, so no further action is needed."
                )
            elif already_saved:
                message = (
                    "Your report was saved, but we couldn't confirm the next step went "
                    "through, and automatic recovery could not be scheduled either - "
                    "please let an admin know so the route can be checked. No need to "
                    "report this leg again."
                )
            else:
                message = (
                    "Something went wrong saving that report, and automatic recovery could "
                    "not be scheduled either - please report this leg again, or contact an "
                    "admin if it keeps failing."
                )
            try:
                await channel.send(message)
            except discord.HTTPException:
                pass
        return queued or already_saved

    async def abandon_thread(
        self, channel: discord.abc.MessageableChannel | None, thread_id: int, *, reason: str
    ) -> None:
        """Single close-and-archive path for both a user-initiated 'Abandon route' and the
        48h inactivity poller - the DB status is set first either way, so a channel this
        bot can no longer reach (kicked, thread deleted) still stops being tracked."""
        status_set = await self.bot.db.set_route_progression_thread_status(thread_id, "abandoned")
        self._active_legs.pop(thread_id, None)
        if not status_set:
            # Audit-confirmed defect #3: the thread had already left 'in_progress' by the
            # time this ran (e.g. the final leg's outcome completed the route in a race
            # against this same abandon attempt) - that other action already sent its own
            # message and archived the thread. Sending "was abandoned" now on top of a
            # completed route would be a visible, confusing contradiction, so there's
            # nothing further to do here.
            return
        if not isinstance(channel, discord.Thread):
            return
        try:
            await channel.send(f"This route-tracking thread was abandoned ({reason}).")
            await channel.edit(archived=True, locked=False)
        except discord.HTTPException as exc:
            logger.warning("Failed to close abandoned thread %s: %s", thread_id, exc)

    async def _abandon_thread_durably(
        self, channel: discord.abc.MessageableChannel | None, thread_id: int, *, reason: str
    ) -> bool:
        """Same post-ack retry discipline as _record_leg_outcome_durably, for
        AbandonConfirmView.confirm's own commit point - abandon_thread's DB write is a
        plain idempotent status update, safe to repeat. Exhausted retries queue a durable
        recovery action the same way (queue_route_progression_abandon_recovery).

        Returns whether the abandon ended up handled at all - either recorded outright,
        or durably queued. False only when BOTH fail - see _record_leg_outcome_durably's
        own docstring for why the caller must release the claim and restore the view in
        that case."""
        last_exc: BaseException | None = None
        for attempt in range(1, POST_ACK_RETRY_ATTEMPTS + 1):
            try:
                await self.abandon_thread(channel, thread_id, reason=reason)
                return True
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
        queued = False
        try:
            await self.bot.db.queue_route_progression_abandon_recovery(thread_id=thread_id, reason=reason)
            queued = True
        except Exception:
            logger.exception("Failed to queue durable recovery for abandoning thread %s", thread_id)
        if isinstance(channel, discord.Thread):
            if queued:
                message = (
                    "Something went wrong abandoning this route - it's been queued to retry "
                    "automatically in the background."
                )
            else:
                message = (
                    "Something went wrong abandoning this route, and automatic recovery could "
                    "not be scheduled either - please try again, or contact an admin if it "
                    "keeps failing."
                )
            try:
                await channel.send(message)
            except discord.HTTPException:
                pass
        return queued

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
        try:
            pending = await self.bot.db.get_pending_route_progression_actions()
        except Exception:
            # A transient DB failure here must not escape - discord.ext.tasks.Loop only
            # auto-reconnects for a fixed network/timeout exception tuple that doesn't
            # include SQLite errors, so an uncaught exception would permanently kill this
            # loop rather than simply skipping to the next scheduled tick.
            logger.exception("Recovery: failed to load pending route-progression actions this cycle")
            return
        for row in pending:
            action_id = row["id"]
            thread_id = row["thread_id"]
            try:
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
                if row["action_kind"] == "leg_outcome":
                    next_index = row["leg_index"] + 1
                    is_final_leg = next_index >= thread_row["total_legs"]
                    if not is_final_leg and not isinstance(channel, discord.Thread):
                        # A non-final leg's outcome isn't safe to record yet: handle_leg_
                        # outcome would save it but then silently skip the next-leg prompt
                        # (the isinstance(channel, discord.Thread) gate it already has),
                        # and this poller would then delete the only durable record telling
                        # a future tick to deliver that prompt. Leave the row queued and
                        # try again once the thread is reachable - completion doesn't need
                        # this same guard, since a missing channel there only skips a nice-
                        # to-have closing message, not route continuation.
                        logger.info(
                            "Recovery: thread %s not reachable yet for leg %s, leaving queued",
                            thread_id, row["leg_index"],
                        )
                        await self.bot.db.mark_route_progression_pending_action_attempted(action_id)
                        continue
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
                try:
                    await self.bot.db.mark_route_progression_pending_action_attempted(action_id)
                except Exception:
                    # Failure accounting itself must not be able to escape and kill the
                    # loop either - see the same reasoning at the top of this method.
                    logger.exception(
                        "Recovery: failed to record a failed attempt for pending action %s", action_id
                    )


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(RouteProgression(bot))
