"""Trade log / inventory-adjacent commands.

Important nuance: UEX's API does not expose a live in-game cargo hold — there's no
endpoint that reports "you are currently carrying 40 SCU of Laranite." What it *does*
have is /user_trades, a history of trades a player has logged via UEX's own tools
(website, DataRunner app, etc.), scoped by that player's personal secret_key.

So "inventory management" here means two complementary things:
  1. /trade-log-add and /trade-log — a local ledger you fill in from Discord, good for
     quick tracking without needing a secret_key.
  2. /uex-trades — pulls your actual logged trades from UEX itself, using *your own*
     linked UEX account (see bot/cogs/account.py: /link-uex-account). Each Discord user
     in the server links their own account, so everyone's /uex-trades shows their own data.
"""
from __future__ import annotations

import asyncio
import logging

import discord
from discord import app_commands
from discord.ext import commands

from bot.uex.exceptions import UexApiError
from bot.uex.leaderboard import LeaderboardEntry, rank_leaderboard, sum_sell_revenue

logger = logging.getLogger("uexbot.trades")

OPERATION_CHOICES = [
    app_commands.Choice(name="Buy", value="buy"),
    app_commands.Choice(name="Sell", value="sell"),
]

# Small pacing delay between per-user /user_trades calls in /leaderboard, so a server with
# many linked accounts doesn't burst well past what's reasonable against UEX's rate limit.
_LEADERBOARD_CALL_DELAY = 0.3
LEADERBOARD_LIMIT = 10


class Trades(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    @app_commands.command(name="trade-log-add", description="Log a buy/sell to your personal trade ledger.")
    @app_commands.describe(
        operation="Buy or sell",
        commodity="Commodity name",
        quantity_scu="Quantity in SCU",
        unit_price="Price per unit (aUEC)",
        terminal="Where the trade happened (optional)",
    )
    @app_commands.choices(operation=OPERATION_CHOICES)
    async def trade_log_add(
        self,
        interaction: discord.Interaction,
        operation: app_commands.Choice[str],
        commodity: str,
        quantity_scu: float,
        unit_price: float,
        terminal: str | None = None,
    ) -> None:
        entry_id = await self.bot.db.log_trade(
            user_id=interaction.user.id,
            commodity_name=commodity,
            operation=operation.value,
            terminal_name=terminal,
            quantity_scu=quantity_scu,
            unit_price=unit_price,
        )
        total = quantity_scu * unit_price
        await interaction.response.send_message(
            f"Logged #{entry_id}: {operation.value} {quantity_scu} SCU of {commodity} "
            f"@ {unit_price:.2f} aUEC (total {total:,.0f} aUEC)"
        )

    @app_commands.command(name="trade-log", description="Show your recent logged trades.")
    async def trade_log(self, interaction: discord.Interaction, limit: int = 10) -> None:
        entries = await self.bot.db.get_trade_log(interaction.user.id, limit=limit)
        if not entries:
            await interaction.response.send_message("No trades logged yet. Use /trade-log-add.")
            return
        lines = []
        for e in entries:
            terminal = f" @ {e['terminal_name']}" if e["terminal_name"] else ""
            lines.append(
                f"#{e['id']} [{e['logged_at']}] {e['operation'].upper()} "
                f"{e['quantity_scu']} SCU {e['commodity_name']}{terminal} "
                f"({e['unit_price']:.2f} aUEC/unit)"
            )
        await interaction.response.send_message("\n".join(lines))

    @app_commands.command(name="uex-trades", description="Show your trade history as logged on UEX itself (requires a linked account).")
    async def uex_trades(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)

        secret_key = await self.bot.db.get_user_secret_key(interaction.user.id)
        if not secret_key:
            await interaction.followup.send(
                "You haven't linked a UEX account yet. Run /link-uex-account first "
                "(it opens a private form, nothing is posted in the channel)."
            )
            return

        try:
            rows = await self.bot.uex.get_user_trades(secret_key=secret_key)
        except UexApiError as exc:
            await interaction.followup.send(
                f"Couldn't fetch UEX trade history: {exc}\n"
                "Your linked secret key may be invalid or expired — try /unlink-uex-account "
                "then /link-uex-account again with a fresh key."
            )
            return

        if not rows:
            await interaction.followup.send("No trades found on your UEX account.")
            return

        lines = [
            f"{r.get('operation', '?')} {r.get('scu', '?')} SCU {r.get('commodity_name', '?')} "
            f"@ {r.get('price', '?')} aUEC/unit ({r.get('date_added', '?')})"
            for r in rows[:15]
        ]
        await interaction.followup.send("\n".join(lines))

    @app_commands.command(
        name="leaderboard",
        description="Top traders in this server by verified UEX sell revenue (from /uex-trades data only).",
    )
    async def leaderboard(self, interaction: discord.Interaction) -> None:
        if interaction.guild is None:
            await interaction.response.send_message("This command only works in a server.", ephemeral=True)
            return

        await interaction.response.defer()

        linked_user_ids = await self.bot.db.list_linked_user_ids()
        if not linked_user_ids:
            await interaction.followup.send(
                "Nobody has linked a UEX account yet. Use /link-uex-account to be eligible for the leaderboard."
            )
            return

        entries: list[LeaderboardEntry] = []
        for user_id in linked_user_ids:
            try:
                member = await interaction.guild.fetch_member(user_id)
            except discord.NotFound:
                continue  # linked account belongs to someone not in this server
            except discord.HTTPException as exc:
                logger.info("Couldn't check membership for user %s: %s", user_id, exc)
                continue

            secret_key = await self.bot.db.get_user_secret_key(user_id)
            if not secret_key:
                continue

            try:
                trades = await self.bot.uex.get_user_trades(secret_key=secret_key)
            except UexApiError as exc:
                logger.info("Skipping %s in leaderboard: %s", member.display_name, exc)
                await asyncio.sleep(_LEADERBOARD_CALL_DELAY)
                continue

            await asyncio.sleep(_LEADERBOARD_CALL_DELAY)

            revenue, count = sum_sell_revenue(trades)
            if count > 0:
                entries.append(LeaderboardEntry(user_id=user_id, total_sell_revenue=revenue, sell_trade_count=count))

        if not entries:
            await interaction.followup.send(
                "No verified sell trades found yet among linked accounts in this server."
            )
            return

        ranked = rank_leaderboard(entries, limit=LEADERBOARD_LIMIT)
        embed = discord.Embed(title="Trading Leaderboard — Verified Sell Revenue", color=discord.Color.gold())
        medals = {0: "🥇", 1: "🥈", 2: "🥉"}
        lines = []
        for i, e in enumerate(ranked):
            prefix = medals.get(i, f"{i + 1}.")
            lines.append(
                f"{prefix} <@{e.user_id}> — **{e.total_sell_revenue:,.0f} aUEC** ({e.sell_trade_count} sells)"
            )
        embed.description = "\n".join(lines)
        embed.set_footer(
            text="Counts gross sell revenue from each player's own verified UEX trade history only — "
            "/trade-log-add entries never count toward this."
        )
        await interaction.followup.send(embed=embed)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Trades(bot))
