"""Discord commands for the Liquidity Score feature.
Allows users to see which items are moving the fastest in the marketplace.
"""
from __future__ import annotations

import logging
import discord
from discord.ext import commands
from discord import app_commands
from bot.db.database import Database

logger = logging.getLogger("uexbot.liquidity")

class LiquidityCog(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.db = bot.db

    @app_commands.command(
        name="liquidity-rank",
        description="Show the top 10 most liquid items in the marketplace (ranked by how fast they sell)."
    )
    async def liquidity_rank(
        self, interaction: discord.Interaction
    ) -> None:
        try:
            # Fetch the top 10 items from our liquidity scores table
            # This table is updated periodically by the background worker
            rows = await self.db.get_top_liquidity_items(limit=10)
            
            if not rows:
                await interaction.response.send_message(
                    "No liquidity data available yet. The bot is still calculating scores based on recent trends.",
                    ephemeral=True,
                )
                return

            embed = discord.Embed(
                title="🔥 Marketplace Liquidity Leaderboard",
                description="Items ranked by how quickly they move (Negotiation Activity / Listing Count).",
                color=discord.Color.orange()
            )

            # Build the list of items
            # We can also pull the current price if we want, but for a rank,
            # the score is the primary metric.
            for i, row in enumerate(rows, start=1):
                item_name = row["item_name"]
                score = row["score"]
                # We can also pull the current price if we want, but for a rank,
                # the score is the primary metric.
                embed.add_field(
                    name=f"{i}. {item_name}",
                    value=f"Liquidity Score: **{score:,.0f}**",
                    inline=False
                )

            embed.set_footer(text="Data updated periodically based on marketplace trends.")
            await interaction.response.send_message(embed=embed)

        except Exception as e:
            logger.error("Error fetching liquidity rank: %s", e)
            await interaction.response.send_message(
                "An error occurred while fetching the liquidity rank. Please try again later.",
                ephemeral=True,
            )

async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(LiquidityCog(bot))
