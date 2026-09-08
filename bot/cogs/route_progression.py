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

from dataclasses import dataclass
import logging

import discord
from discord.ext import commands, tasks

from bot.uex.route_progression import terminal_state_update_for_outcome

logger = logging.getLogger("uexbot.route_progression")

# Checked every ABANDONMENT_POLL_HOURS for any in_progress thread idle longer than
# ABANDONMENT_HOURS - matches this codebase's usual "check periodically, act on a coarser
# threshold" pollers (e.g. intelligence.py's data-health snapshot cadence).
ABANDONMENT_POLL_HOURS = 6
ABANDONMENT_HOURS = 48

MAX_TRACKABLE_ROUTES = 5


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
        self, *, cog: "RouteProgression", thread_id: int, leg_index: int, leg: RouteLegInput, flow: str
    ) -> None:
        title = "How much was actually there?" if flow == "less" else "How much did you take?"
        super().__init__(title=title)
        self.cog = cog
        self.thread_id = thread_id
        self.leg_index = leg_index
        self.leg = leg
        self.flow = flow

    async def on_submit(self, interaction: discord.Interaction) -> None:
        try:
            actual_scu = float(str(self.scu_input.value).strip())
        except ValueError:
            await interaction.response.send_message("That SCU value isn't a number - try again.", ephemeral=True)
            return
        actual_price: float | None = None
        price_text = str(self.price_input.value).strip()
        if price_text:
            try:
                actual_price = float(price_text)
            except ValueError:
                await interaction.response.send_message("That price isn't a number - try again.", ephemeral=True)
                return

        if self.flow == "less":
            outcome = "missing" if actual_scu <= 0 else "less"
            await interaction.response.send_message("Got it, thanks for reporting.", ephemeral=True)
            await self.cog.handle_leg_outcome(
                interaction.channel, self.thread_id, self.leg_index, self.leg,
                outcome=outcome, actual_price=actual_price, actual_scu=actual_scu,
            )
        else:
            await interaction.response.send_message(
                "One more thing - did you take everything there was, or did something else "
                "stop you first (your cargo hold, or the terminal itself)?",
                view=MoreOutcomeFollowupView(
                    cog=self.cog, thread_id=self.thread_id, leg_index=self.leg_index, leg=self.leg,
                    actual_price=actual_price, actual_scu=actual_scu,
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
        actual_price: float | None, actual_scu: float,
    ) -> None:
        super().__init__(timeout=300)
        self.cog = cog
        self.thread_id = thread_id
        self.leg_index = leg_index
        self.leg = leg
        self.actual_price = actual_price
        self.actual_scu = actual_scu
        self.resolved = False

    async def _lock(self, interaction: discord.Interaction) -> bool:
        if self.resolved:
            await interaction.response.send_message("This leg was already reported.", ephemeral=True)
            return False
        self.resolved = True
        for item in self.children:
            item.disabled = True
        return True

    @discord.ui.button(label="Terminal was drained", style=discord.ButtonStyle.red)
    async def drained(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if not await self._lock(interaction):
            return
        await interaction.response.edit_message(view=self)
        await self.cog.handle_leg_outcome(
            interaction.channel, self.thread_id, self.leg_index, self.leg,
            outcome="more", actual_price=self.actual_price, actual_scu=self.actual_scu, precision="exact",
        )

    @discord.ui.button(label="I was capped, more was there", style=discord.ButtonStyle.gray)
    async def capacity_limited(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if not await self._lock(interaction):
            return
        await interaction.response.edit_message(view=self)
        await self.cog.handle_leg_outcome(
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

    async def _lock(self, interaction: discord.Interaction) -> bool:
        # Same check-then-set double-click guard as ConfirmListingView/ConfirmDeleteListingView
        # in bot/cogs/marketplace.py - asyncio is single-threaded and nothing awaits between
        # the check and the set, so the second of two racing callbacks always sees the first's
        # write. Only guards against double-PROCESSING, not the visual button state for the
        # 'less'/'more' modal paths - opening a modal consumes the interaction's one allowed
        # response, so those buttons can't also be disabled-and-edited in the same round trip.
        if self.resolved:
            await interaction.response.send_message("This leg was already reported.", ephemeral=True)
            return False
        self.resolved = True
        return True

    @discord.ui.button(label="Matched quote", style=discord.ButtonStyle.green)
    async def matched(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if not await self._lock(interaction):
            return
        for item in self.children:
            item.disabled = True
        await interaction.response.edit_message(view=self)
        await self.cog.handle_leg_outcome(
            interaction.channel, self.thread_id, self.leg_index, self.leg, outcome="matched"
        )

    @discord.ui.button(label="Less / not there", style=discord.ButtonStyle.red)
    async def less(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if not await self._lock(interaction):
            return
        await interaction.response.send_modal(
            ActualAmountModal(
                cog=self.cog, thread_id=self.thread_id, leg_index=self.leg_index, leg=self.leg, flow="less"
            )
        )

    @discord.ui.button(label="More than quoted", style=discord.ButtonStyle.blurple)
    async def more(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if not await self._lock(interaction):
            return
        await interaction.response.send_modal(
            ActualAmountModal(
                cog=self.cog, thread_id=self.thread_id, leg_index=self.leg_index, leg=self.leg, flow="more"
            )
        )


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

    def cog_unload(self) -> None:
        self.poll_abandoned_threads.cancel()

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

    async def _post_leg_prompt(
        self, thread: discord.Thread, thread_id: int, leg_index: int, leg: RouteLegInput
    ) -> None:
        if leg.quoted_price is not None and leg.quoted_scu is not None:
            quoted_line = f"Quoted: {leg.quoted_price:,.2f} aUEC/unit · {leg.quoted_scu:,.0f} SCU"
        else:
            quoted_line = "No quoted figures were available for this leg."
        embed = discord.Embed(
            title=f"Leg {leg_index + 1}: {leg.display_label}",
            description=quoted_line,
            color=discord.Color.blurple(),
        )
        await thread.send(
            embed=embed, view=LegOutcomeView(cog=self, thread_id=thread_id, leg_index=leg_index, leg=leg)
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
        await self.bot.db.record_route_progression_leg_outcome(
            thread_id=thread_id, leg_index=leg_index, outcome=outcome,
            actual_price=actual_price, actual_scu=actual_scu, precision=precision,
        )
        update_row = terminal_state_update_for_outcome(
            id_commodity=leg.id_commodity, id_terminal=leg.id_terminal,
            commodity_name=leg.commodity_name, terminal_name=leg.terminal_name,
            side=leg.side, outcome=outcome,
            quoted_price=leg.quoted_price, quoted_scu=leg.quoted_scu, quoted_status=leg.quoted_status,
            actual_price=actual_price, actual_scu=actual_scu, precision=precision,
        )
        if update_row is not None:
            await self.bot.db.record_terminal_market_snapshot([update_row], source="player_report")

        thread_row = await self.bot.db.get_route_progression_thread(thread_id)
        if thread_row is None:
            return
        legs = self._active_legs.get(thread_id, [])
        next_index = leg_index + 1
        if next_index >= thread_row["total_legs"] or next_index >= len(legs):
            await self.bot.db.set_route_progression_thread_status(thread_id, "completed")
            self._active_legs.pop(thread_id, None)
            if isinstance(channel, discord.Thread):
                try:
                    await channel.send("Route complete - thanks for reporting! This thread will archive now.")
                    await channel.edit(archived=True, locked=False)
                except discord.HTTPException as exc:
                    logger.warning("Failed to close completed thread %s: %s", thread_id, exc)
            return
        if isinstance(channel, discord.Thread):
            await self._post_leg_prompt(channel, thread_id, next_index, legs[next_index])

    @tasks.loop(hours=ABANDONMENT_POLL_HOURS)
    async def poll_abandoned_threads(self) -> None:
        stale = await self.bot.db.get_stale_route_progression_threads(older_than_hours=ABANDONMENT_HOURS)
        for row in stale:
            thread_id = row["thread_id"]
            await self.bot.db.set_route_progression_thread_status(thread_id, "abandoned")
            self._active_legs.pop(thread_id, None)
            try:
                channel = self.bot.get_channel(thread_id) or await self.bot.fetch_channel(thread_id)
            except discord.HTTPException as exc:
                logger.info("Couldn't reach abandoned thread %s to close it: %s", thread_id, exc)
                continue
            if not isinstance(channel, discord.Thread):
                continue
            try:
                await channel.send(
                    f"This route-tracking thread was inactive for over {ABANDONMENT_HOURS:g}h "
                    "and has been marked abandoned."
                )
                await channel.edit(archived=True, locked=False)
            except discord.HTTPException as exc:
                logger.warning("Failed to close abandoned thread %s: %s", thread_id, exc)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(RouteProgression(bot))
