"""Daily server digest: price movers, most actively traded commodities, Marketplace
trending items, and sellability insights. It is auto-posted once a day to an
admin-configured channel/time. /digest-now builds and posts the exact same content on
demand, so nobody has to wait for the scheduled time just to see it.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

import discord
from discord import app_commands
from discord.ext import commands, tasks

from bot.uex.exceptions import UexApiError
from bot.uex.marketplace import marketplace_item_url
from bot.uex.trends import compute_movers

logger = logging.getLogger("uexbot.digest")

# How often the background loop checks whether it's "time" to post for any configured guild.
# A daily post only needs to land within its target hour, so this doesn't need to be tight -
# a shorter interval just means less drift between the configured hour and the actual post.
CHECK_INTERVAL_MINUTES = 30


class Digest(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.post_scheduled_digests.start()

    def cog_unload(self) -> None:
        self.post_scheduled_digests.cancel()

    @app_commands.command(
        name="set-digest-channel",
        description="(Admin) Configure the channel and UTC hour for the daily trading digest.",
    )
    @app_commands.describe(
        channel="Channel to post the daily digest in",
        hour_utc="Hour of day (UTC, 0-23) to post it",
    )
    @app_commands.checks.has_permissions(manage_guild=True)
    async def set_digest_channel(
        self,
        interaction: discord.Interaction,
        channel: discord.TextChannel,
        hour_utc: app_commands.Range[int, 0, 23],
    ) -> None:
        await self.bot.db.set_guild_digest_config(
            guild_id=interaction.guild_id, channel_id=channel.id, hour_utc=hour_utc
        )
        await interaction.response.send_message(
            f"Daily digest will post in {channel.mention} at **{hour_utc:02d}:00 UTC** every day "
            f"(checked every {CHECK_INTERVAL_MINUTES} min, so it may land up to that long after the exact hour). "
            "Use /digest-now to post one immediately, or /digest-disable to turn it off.",
            ephemeral=True,
        )

    @app_commands.command(name="digest-disable", description="(Admin) Turn off the daily digest for this server.")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def digest_disable(self, interaction: discord.Interaction) -> None:
        disabled = await self.bot.db.disable_guild_digest(interaction.guild_id)
        msg = "Daily digest disabled." if disabled else "No digest was configured for this server."
        await interaction.response.send_message(msg, ephemeral=True)

    @app_commands.command(
        name="digest-now",
        description="Post the trading digest (prices, trends, Marketplace sellability) right now.",
    )
    async def digest_now(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer()
        embed = await self._build_digest_embed()
        await interaction.followup.send(embed=embed)

    async def _build_digest_embed(self) -> discord.Embed:
        embed = discord.Embed(
            title="Daily Trading Digest",
            color=discord.Color.blurple(),
            timestamp=datetime.now(timezone.utc),
        )

        try:
            price_rows = await self.bot.uex.get_commodities_prices_all()
            gainers, losers = compute_movers(price_rows, limit=5)
        except UexApiError as exc:
            gainers, losers = [], []
            logger.info("Digest: movers unavailable: %s", exc)
        movers_lines = []
        if gainers:
            movers_lines.append("**Up:** " + ", ".join(f"{m.commodity_name} +{m.pct_change:.1f}%" for m in gainers))
        if losers:
            movers_lines.append("**Down:** " + ", ".join(f"{m.commodity_name} {m.pct_change:.1f}%" for m in losers))
        embed.add_field(
            name="Price Movers",
            value="\n".join(movers_lines) if movers_lines else "No notable movers right now.",
            inline=False,
        )

        trends_cog = self.bot.get_cog("Trends")
        trending_entries = trends_cog.get_trending_snapshot() if trends_cog else []
        if trending_entries:
            lines = [f"{i}. {e.commodity_name} ({e.total_trips_15d} trips)" for i, e in enumerate(trending_entries[:5], start=1)]
            embed.add_field(name="Most Actively Traded", value="\n".join(lines), inline=False)
        else:
            embed.add_field(name="Most Actively Traded", value="Still gathering data - check back later.", inline=False)

        try:
            marketplace_trends = await self.bot.uex.get_marketplace_trends()
        except UexApiError as exc:
            marketplace_trends = []
            logger.info("Digest: marketplace trends unavailable: %s", exc)
        if marketplace_trends:
            lines = [
                f"{i}. {r.get('item_name', 'Unknown')} ({r.get('negotiations_count', 0)} negotiations)"
                for i, r in enumerate(marketplace_trends[:5], start=1)
            ]
            embed.add_field(name="Marketplace Trending", value="\n".join(lines), inline=False)
        else:
            embed.add_field(name="Marketplace Trending", value="No marketplace trend data right now.", inline=False)

        sellability_rows = await self.bot.db.get_top_liquidity_items(limit=3)
        liquidity_movers = await self.bot.db.get_liquidity_movers(limit=3)
        embed.add_field(
            name="Marketplace Sellability",
            value=_format_sellability_digest(sellability_rows, liquidity_movers),
            inline=False,
        )

        embed.set_footer(text="UEX Corp data · /liquidity-rank, /best-route, and /price for full detail")
        return embed

    @tasks.loop(minutes=CHECK_INTERVAL_MINUTES)
    async def post_scheduled_digests(self) -> None:
        configs = await self.bot.db.list_enabled_guild_digest_configs()
        if not configs:
            return

        now = datetime.now(timezone.utc)
        today_str = now.strftime("%Y-%m-%d")
        due = [c for c in configs if c["hour_utc"] == now.hour and c["last_posted_date"] != today_str]
        if not due:
            return

        embed = await self._build_digest_embed()
        for config in due:
            channel = self.bot.get_channel(config["channel_id"])
            if channel is None:
                logger.warning("Digest channel %s not found for guild %s", config["channel_id"], config["guild_id"])
                continue
            try:
                await channel.send(embed=embed)
            except discord.HTTPException as exc:
                logger.warning("Failed to post digest for guild %s: %s", config["guild_id"], exc)
                continue
            await self.bot.db.mark_guild_digest_posted(config["guild_id"], today_str)

    @post_scheduled_digests.before_loop
    async def before_post_scheduled_digests(self) -> None:
        await self.bot.wait_until_ready()

    async def cog_app_command_error(self, interaction: discord.Interaction, error: app_commands.AppCommandError) -> None:
        if isinstance(error, app_commands.MissingPermissions):
            await interaction.response.send_message(
                "You need the Manage Server permission to configure the digest.", ephemeral=True
            )
            return
        raise error


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Digest(bot))


def _format_sellability_digest(rows: list[dict], movers: list[dict]) -> str:
    """Create one compact daily-digest field from locally tracked liquidity data."""
    if not rows:
        return "Still collecting Marketplace sellability data."

    lines = ["**Best to list now**"]
    for index, row in enumerate(rows, start=1):
        item_name = row["item_name"]
        id_item = row.get("id_item")
        item = f"[{item_name}]({marketplace_item_url(id_item)})" if id_item is not None else item_name
        lines.append(f"{index}. {item} — **{float(row['score']):,.0f}/100**")

    if movers:
        lines.append("\n**Biggest rating shifts**")
        for mover in movers:
            previous = float(mover["previous_score"])
            current = float(mover["current_score"])
            change = current - previous
            arrow = "📈" if change > 0 else "📉" if change < 0 else "➖"
            direction = "up" if change > 0 else "down" if change < 0 else "unchanged"
            item_name = mover["item_name"]
            id_item = mover.get("id_item")
            item = f"[{item_name}]({marketplace_item_url(id_item)})" if id_item is not None else item_name
            if change:
                lines.append(f"{arrow} {item} {direction} {abs(change):,.0f} pts ({previous:,.0f} → {current:,.0f})")
            else:
                lines.append(f"{arrow} {item} unchanged at {current:,.0f}/100")
    else:
        lines.append("\n*Rating shifts appear after another hourly snapshot.*")
    return "\n".join(lines)
