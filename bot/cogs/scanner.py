"""Raw Materials Deal Scanner: proactively finds quality-matched raw-material Marketplace
sell listings priced well below their own 30-day average, and notifies each user who has
set a scanner channel with /set-scanner-channel.

Persistent, like marketplace_alerts.py and stock_alerts.py (not one-shot) - there's no
"remove" command because there's nothing to remove, just one channel per user; setting a
new channel replaces the old one. Unlike those two, there's no dedicated averages-cache
table: UexClient already caches /marketplace_prices_averages_all for 1h client-side
(bot/uex/client.py) - a second cache layer on top of that would just duplicate it.

The comparison logic itself lives in bot/uex/scanner.py (pure, unit-tested) - see that
module's docstring for why this only ever looks at sell-side listings.
"""
from __future__ import annotations

import logging

import discord
from discord import app_commands
from discord.ext import commands, tasks

from bot.delivery import Delivery, send_to_channel_or_dm
from bot.uex.exceptions import UexApiError, describe_uex_api_error
from bot.uex.marketplace import marketplace_item_link, marketplace_item_url
from bot.uex.scanner import StealEntry, build_fair_price_index, find_steals

logger = logging.getLogger("uexbot.scanner")

# /marketplace_listings has a 5-minute client-side cache (bot/uex/client.py's default TTL -
# it's not in _ENDPOINT_CACHE_TTL's explicit list) and averages refresh hourly, so polling
# faster than the listings cache wouldn't see fresher data. Matched to marketplace_alerts.py's
# interval since it watches the same underlying endpoint.
POLL_INTERVAL_MINUTES = 15
MAX_NOTIFY_PER_USER_PER_POLL = 5  # cap notification spam if many steals appear between polls


class Scanner(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.poll_scanner.start()

    def cog_unload(self) -> None:
        self.poll_scanner.cancel()

    @app_commands.command(
        name="set-scanner-channel",
        description="Set the channel for quality-matched raw-material deal alerts.",
    )
    @app_commands.describe(channel="The channel to post steal alerts in")
    async def set_scanner_channel(self, interaction: discord.Interaction, channel: discord.TextChannel) -> None:
        await self.bot.db.set_scanner_channel(interaction.user.id, channel.id)
        await interaction.response.send_message(
            f"Raw-material deal alerts will now be posted in {channel.mention} (checked every "
            f"{POLL_INTERVAL_MINUTES} min, threshold {self.bot.config.scanner_steal_threshold:.0%} off "
            "the quality-matched 30-day average). Run /set-scanner-channel again anytime to change it, "
            "or turn it off from /scanner-status.",
            ephemeral=True,
        )

    @app_commands.command(
        name="scanner-status",
        description="Check your Raw Materials Deal Scanner alerts and their quality-matched scope.",
    )
    async def scanner_status(self, interaction: discord.Interaction) -> None:
        channel_id = await self.bot.db.get_scanner_channel(interaction.user.id)
        if channel_id is None:
            await interaction.response.send_message(
                "Raw Materials Deal Scanner not set up yet. Run /set-scanner-channel to start getting "
                "quality-matched material deal alerts.",
                ephemeral=True,
            )
            return
        view = ScannerOffView(self.bot.db, interaction.user.id)
        await interaction.response.send_message(
            f"Raw Materials Deal Scanner active: alerts post to <#{channel_id}>, checked every "
            f"{POLL_INTERVAL_MINUTES} min for Commodities and Harvestables with reported quality, at least "
            f"{self.bot.config.scanner_steal_threshold:.0%} below their quality-matched 30-day average.",
            view=view,
            ephemeral=True,
        )
        view.origin = interaction

    @app_commands.command(
        name="scan-now",
        description="Scan raw-material sell listings with reported quality for underpriced deals now.",
    )
    async def scan_now(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        try:
            steals = await self._find_current_steals()
        except UexApiError as exc:
            await interaction.followup.send(describe_uex_api_error(exc))
            return

        if not steals:
            await interaction.followup.send(
                f"No quality-matched Commodities or Harvestables are currently at least "
                f"{self.bot.config.scanner_steal_threshold:.0%} below their 30-day average."
            )
            return

        embed = discord.Embed(title="Raw Materials Deal Scanner", color=discord.Color.green())
        embed.description = (
            "Quality-matched Commodities and Harvestables only · "
            "crafted gear is excluded because its modifiers are not structured in UEX data."
        )
        for steal in steals[:5]:
            embed.add_field(
                name=f"{steal.item_name} — {steal.listing_title}"[:256],
                value=(
                    f"**{steal.listing_price:,.0f} {steal.currency}** vs 30d avg "
                    f"**{steal.fair_price:,.0f} {steal.currency}** — **{steal.discount_pct:.0f}%** off "
                    f"· by {steal.seller}\n🔗 [Open in UEX Marketplace]({marketplace_item_url(steal.id_item)})"
                ),
                inline=False,
            )
        if len(steals) > 5:
            embed.set_footer(text=f"...and {len(steals) - 5} more not shown.")
        await interaction.followup.send(embed=embed)

    @tasks.loop(minutes=POLL_INTERVAL_MINUTES)
    async def poll_scanner(self) -> None:
        # Nothing may escape a tasks.loop body: it only restarts itself after a narrow set
        # of network errors, so anything else would stop the scanner until a restart.
        try:
            await self._poll_scanner_once()
        except Exception:
            logger.exception("Scanner poll failed; retrying next cycle")

    async def _poll_scanner_once(self) -> None:
        watchers = await self.bot.db.list_scanner_watchers()
        if not watchers:
            return

        try:
            steals = await self._find_current_steals()
        except UexApiError as exc:
            logger.warning("Scanner poll failed: %s", exc)
            return
        if not steals:
            return

        # Listings/averages are global, not per-user - fetched and matched once above,
        # then fanned out to every watcher's own dedup state, same pattern as
        # marketplace_alerts.py grouping by (keyword, operation) to share one API call.
        for watcher in watchers:
            # Per watcher, so one user's failure can't block everyone after them.
            try:
                await self._notify_watcher(watcher, steals)
            except Exception:
                logger.exception("Scanner notifications failed for user %s this cycle", watcher.get("user_id"))

    async def _notify_watcher(self, watcher: dict, steals: list[StealEntry]) -> None:
        user_id = watcher["user_id"]
        seen_ids = await self.bot.db.get_seen_scanner_listing_ids(user_id)
        fresh = [s for s in steals if s.listing_id not in seen_ids]
        if not fresh:
            return

        for steal in fresh[:MAX_NOTIFY_PER_USER_PER_POLL]:
            outcome = await self._notify(watcher["channel_id"], user_id, steal)
            if not outcome.settled:
                break  # a temporary failure: this deal (and the rest) stay unseen for the next poll
            await self.bot.db.mark_scanner_listing_seen(user_id, steal.listing_id)
            if outcome is Delivery.UNDELIVERABLE:
                break  # neither the channel nor a DM will take it; the rest would fail the same way

    async def _find_current_steals(self) -> list[StealEntry]:
        """Fetch live sell listings + averages and return every current steal - shared by
        both /scan-now and the background poll (each computes it fresh; UexClient's own
        client-side caching, not this method, is what keeps repeat calls cheap)."""
        listings = await self.bot.uex.get_marketplace_listings(operation="sell")
        if not listings:
            return []
        averages = await self.bot.uex.get_marketplace_prices_averages_all()
        fair_prices = build_fair_price_index(averages)
        return find_steals(listings, fair_prices, self.bot.config.scanner_steal_threshold)

    async def _notify(self, channel_id: int, user_id: int, steal: StealEntry) -> Delivery:
        """Posts in the watcher's channel, falling back to a DM when the channel is gone or
        refuses the post - any channel post can 403, see CLAUDE.md."""
        embed = discord.Embed(title="Raw-material deal found!", color=discord.Color.green())
        embed.description = f"**{marketplace_item_link(steal.item_name, steal.id_item)}** — {steal.listing_title}"
        embed.add_field(name="Listing price", value=f"{steal.listing_price:,.0f} {steal.currency}")
        embed.add_field(name="30-day average", value=f"{steal.fair_price:,.0f} {steal.currency}")
        embed.add_field(name="Discount", value=f"{steal.discount_pct:.0f}%")
        embed.set_footer(text=f"by {steal.seller} · Raw Materials Deal Scanner · quality-matched 30-day average")
        return await send_to_channel_or_dm(
            self.bot, channel_id, user_id, label=f"scanner deal {steal.listing_id}", content=f"<@{user_id}>", embed=embed,
        )

    @poll_scanner.before_loop
    async def before_poll_scanner(self) -> None:
        await self.bot.wait_until_ready()


class ScannerOffView(discord.ui.View):
    """A "Turn off" button on /scanner-status: before it there was no way to stop the
    scanner at all (audit UX-3), short of pointing it at a channel nobody reads. Lives on
    the status reply rather than as its own command, to keep the command list short."""

    def __init__(self, db, user_id: int) -> None:
        super().__init__(timeout=300)
        self.db = db
        self.user_id = user_id
        self.origin: discord.Interaction | None = None  # the /scanner-status call, set once sent

    @discord.ui.button(label="Turn off", style=discord.ButtonStyle.danger)
    async def turn_off(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if interaction.user.id != self.user_id:
            await interaction.response.send_message("This button isn't for you.", ephemeral=True)
            return
        was_on = await self.db.clear_scanner_channel(self.user_id)
        button.disabled = True
        await interaction.response.edit_message(
            content=("Raw Materials Deal Scanner turned off. Run /set-scanner-channel to turn it back on."
                     if was_on else "The Raw Materials Deal Scanner was already off."),
            view=self,
        )
        self.stop()

    async def on_timeout(self) -> None:
        # Grey the button out so it doesn't look usable after it stops working. An
        # ephemeral reply can still be edited through its interaction for 15 minutes.
        if self.origin is None:
            return
        for child in self.children:
            child.disabled = True
        try:
            await self.origin.edit_original_response(view=self)
        except discord.HTTPException:
            pass


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Scanner(bot))
