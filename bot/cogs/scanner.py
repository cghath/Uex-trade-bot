"""Undervalued Scanner: proactively finds UEX Marketplace sell listings priced well
below their item's own 30-day average ("steals"), and notifies each user who's set a
scanner channel with /set-scanner-channel.

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

from bot.uex.exceptions import UexApiError
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
        description="Set the channel where I'll post Undervalued Scanner steal alerts for you.",
    )
    @app_commands.describe(channel="The channel to post steal alerts in")
    async def set_scanner_channel(self, interaction: discord.Interaction, channel: discord.TextChannel) -> None:
        await self.bot.db.set_scanner_channel(interaction.user.id, channel.id)
        await interaction.response.send_message(
            f"Scanner alerts will now be posted in {channel.mention} (checked every "
            f"{POLL_INTERVAL_MINUTES} min, threshold {self.bot.config.scanner_steal_threshold:.0%} off "
            "the 30-day average). Run /set-scanner-channel again anytime to change it.",
            ephemeral=True,
        )

    @app_commands.command(name="scanner-status", description="Check whether the Undervalued Scanner is set up for you.")
    async def scanner_status(self, interaction: discord.Interaction) -> None:
        channel_id = await self.bot.db.get_scanner_channel(interaction.user.id)
        if channel_id is None:
            await interaction.response.send_message(
                "Scanner not set up yet. Run /set-scanner-channel to start getting proactive steal alerts.",
                ephemeral=True,
            )
            return
        await interaction.response.send_message(
            f"Scanner active: alerts post to <#{channel_id}>, checked every {POLL_INTERVAL_MINUTES} min "
            f"for sell listings at least {self.bot.config.scanner_steal_threshold:.0%} below their 30-day average.",
            ephemeral=True,
        )

    @app_commands.command(name="scan-now", description="Manually scan the Marketplace for undervalued sell listings right now.")
    async def scan_now(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        try:
            steals = await self._find_current_steals()
        except UexApiError as exc:
            await interaction.followup.send(f"UEX API error: {exc}")
            return

        if not steals:
            await interaction.followup.send(
                f"No sell listings currently at least {self.bot.config.scanner_steal_threshold:.0%} "
                "below their 30-day average."
            )
            return

        embed = discord.Embed(title="Undervalued Scanner — manual scan", color=discord.Color.green())
        for steal in steals[:5]:
            embed.add_field(
                name=f"{steal.item_name} — {steal.listing_title}"[:256],
                value=(
                    f"**{steal.listing_price:,.0f} {steal.currency}** vs 30d avg "
                    f"**{steal.fair_price:,.0f} {steal.currency}** — **{steal.discount_pct:.0f}%** off "
                    f"· by {steal.seller}"
                ),
                inline=False,
            )
        if len(steals) > 5:
            embed.set_footer(text=f"...and {len(steals) - 5} more not shown.")
        await interaction.followup.send(embed=embed)

    @tasks.loop(minutes=POLL_INTERVAL_MINUTES)
    async def poll_scanner(self) -> None:
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
            user_id = watcher["user_id"]
            seen_ids = await self.bot.db.get_seen_scanner_listing_ids(user_id)
            fresh = [s for s in steals if s.listing_id not in seen_ids]
            if not fresh:
                continue

            channel = self.bot.get_channel(watcher["channel_id"])
            if channel is None:
                logger.warning("Scanner channel %s not resolvable for user %s.", watcher["channel_id"], user_id)
                continue

            for steal in fresh[:MAX_NOTIFY_PER_USER_PER_POLL]:
                await self._notify(channel, user_id, steal)
                await self.bot.db.mark_scanner_listing_seen(user_id, steal.listing_id)

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

    async def _notify(self, channel: discord.abc.Messageable, user_id: int, steal: StealEntry) -> None:
        embed = discord.Embed(title="Undervalued item found!", color=discord.Color.green())
        embed.description = f"**{steal.item_name}** — {steal.listing_title}"
        embed.add_field(name="Listing price", value=f"{steal.listing_price:,.0f} {steal.currency}")
        embed.add_field(name="30-day average", value=f"{steal.fair_price:,.0f} {steal.currency}")
        embed.add_field(name="Discount", value=f"{steal.discount_pct:.0f}%")
        embed.set_footer(text=f"by {steal.seller} · Undervalued Scanner")
        try:
            await channel.send(content=f"<@{user_id}>", embed=embed)
        except discord.HTTPException as exc:
            logger.warning("Failed to post scanner alert to channel for user %s: %s", user_id, exc)

    @poll_scanner.before_loop
    async def before_poll_scanner(self) -> None:
        await self.bot.wait_until_ready()


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Scanner(bot))
