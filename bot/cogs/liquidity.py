"""Discord commands for the Liquidity Score feature.
Allows users to see which items are moving the fastest in the marketplace.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

import discord
from discord import app_commands
from discord.ext import commands

from bot.uex.charts import render_liquidity_history_chart
from bot.uex.marketplace import marketplace_item_url

logger = logging.getLogger("uexbot.liquidity")


async def liquidity_item_autocomplete(
    interaction: discord.Interaction, current: str
) -> list[app_commands.Choice[str]]:
    rows = await interaction.client.db.find_liquidity_items(current)
    return [app_commands.Choice(name=row["item_name"][:100], value=row["item_name"]) for row in rows]


def _marketplace_name(item_name: str, id_item: int | None) -> str:
    return f"[{item_name}]({marketplace_item_url(id_item)})" if id_item is not None else item_name


def _relative_timestamp(raw: str | None) -> str | None:
    if not raw:
        return None
    try:
        when = datetime.fromisoformat(raw).replace(tzinfo=timezone.utc)
    except ValueError:
        return None
    return f"<t:{int(when.timestamp())}:R>"


def _format_rating_change(previous: float, current: float) -> str:
    """Render a compact, plain-language before/after sellability rating."""
    change = current - previous
    points = abs(round(change))
    point_word = "point" if points == 1 else "points"
    if change > 0:
        direction = f"📈 **Up {points} {point_word}**"
    elif change < 0:
        direction = f"📉 **Down {points} {point_word}**"
    else:
        direction = "➖ **No change**"
    return f"{direction} · {previous:,.0f} → {current:,.0f} / 100"


class LiquidityCog(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.db = bot.db

    @app_commands.command(
        name="liquidity-rank",
        description="The bot's own sellability score: top 10 items ranked by how fast they sell (not UEX's raw activity).",
    )
    async def liquidity_rank(
        self, interaction: discord.Interaction
    ) -> None:
        try:
            rows = await self.db.get_top_liquidity_items(limit=10)
            
            if not rows:
                await interaction.response.send_message(
                    "No liquidity data available yet. The bot is still calculating scores based on recent trends.",
                    ephemeral=True,
                )
                return

            embed = discord.Embed(
                title="🔥 Marketplace Liquidity Leaderboard",
                description="Items ranked by estimated sellability, rated from 0 to 100.",
                color=discord.Color.orange()
            )
            updated = _relative_timestamp(rows[0].get("last_updated"))
            if updated:
                embed.description += f" Last refreshed {updated}."

            for i, row in enumerate(rows, start=1):
                item_name = row["item_name"]
                score = row["score"]
                detail = (
                    f"Sellability Rating: **{score:,.0f}/100**\n"
                    f"{row.get('negotiations_success', 0):,} completed · "
                    f"{row.get('negotiations_open', 0):,} open\n"
                    f"{row.get('listings_count_sell', 0):,} sell listings · "
                    f"{row.get('listings_count_buy', 0):,} buy postings"
                )
                if row.get("id_item") is not None:
                    detail += f"\n🔗 [Open in UEX Marketplace]({marketplace_item_url(row['id_item'])})"
                embed.add_field(
                    name=f"{i}. {item_name}",
                    value=detail,
                    inline=False
                )

            footer = "Deals count fully · open negotiations count half · each buy posting adds a small demand bonus."
            embed.set_footer(text=footer)
            await interaction.response.send_message(embed=embed)

        except Exception as e:
            logger.error("Error fetching liquidity rank: %s", e)
            await interaction.response.send_message(
                "An error occurred while fetching the liquidity rank. Please try again later.",
                ephemeral=True,
            )

    @app_commands.command(
        name="liquidity-trends",
        description="Sellability-score history for one item, or its biggest movers (the bot's score, not raw activity).",
    )
    @app_commands.describe(item="Optional Marketplace item to chart")
    @app_commands.autocomplete(item=liquidity_item_autocomplete)
    async def liquidity_trends(self, interaction: discord.Interaction, item: str | None = None) -> None:
        await interaction.response.defer()
        if item:
            history = await self.db.get_liquidity_history(item)
            if len(history) < 2:
                await interaction.followup.send(
                    f"Still collecting liquidity history for **{item}**. Check back after another hourly refresh."
                )
                return
            first, latest = history[0], history[-1]
            change = float(latest["score"]) - float(first["score"])
            pct_change = (change / float(first["score"]) * 100) if float(first["score"]) else None
            chart = render_liquidity_history_chart(item_name=latest["item_name"], history_rows=history)
            embed = discord.Embed(
                title=f"{latest['item_name']} — Liquidity trends",
                description=f"{len(history)} hourly observations over the last 7 days.",
                color=discord.Color.orange(),
            )
            embed.add_field(name="Current sellability rating", value=f"**{float(latest['score']):,.0f}/100**")
            change_text = _format_rating_change(float(first["score"]), float(latest["score"]))
            if pct_change is not None:
                change_text += f" ({pct_change:+.1f}%)"
            embed.add_field(name="Change over recorded history", value=change_text)
            embed.add_field(
                name="Latest activity",
                value=(f"{latest['negotiations_success']:,} completed · {latest['negotiations_open']:,} open\n"
                       f"{latest.get('listings_count_sell', 0):,} sell listings · "
                       f"{latest.get('listings_count_buy', 0):,} buy postings"),
                inline=False,
            )
            id_item = latest.get("id_item")
            if id_item is not None:
                embed.url = marketplace_item_url(id_item)
            embed.set_footer(text="Deals count fully · open negotiations count half · each buy posting adds a small demand bonus.")
            if chart is None:
                await interaction.followup.send(embed=embed)
            else:
                file = discord.File(chart, filename="liquidity_trend.png")
                embed.set_image(url="attachment://liquidity_trend.png")
                await interaction.followup.send(embed=embed, file=file)
            return

        movers = await self.db.get_liquidity_movers()
        if not movers:
            await interaction.followup.send(
                "Still collecting liquidity history. This list appears after at least two hourly snapshots."
            )
            return
        embed = discord.Embed(
            title="📈 Marketplace Liquidity Movers",
            description="Biggest sellability-rating changes over the available history from the last 24 hours.",
            color=discord.Color.gold(),
        )
        lines = []
        for i, mover in enumerate(movers, start=1):
            previous = float(mover["previous_score"])
            current = float(mover["current_score"])
            lines.append(
                f"**{i}. {_marketplace_name(mover['item_name'], mover.get('id_item'))}**\n"
                f"{_format_rating_change(previous, current)}"
            )
        embed.description += "\n\n" + "\n".join(lines)
        embed.set_footer(text="A full 24-hour comparison becomes available after one day of tracking.")
        await interaction.followup.send(embed=embed)

async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(LiquidityCog(bot))
