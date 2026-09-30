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

from bot.cogs.prices import commodity_name_autocomplete, terminal_name_autocomplete
from bot.delivery import MAX_MESSAGE_CHARS, fit_lines
from bot.discord_ui import AlertRemovePickerView
from bot.uex.exceptions import UexApiError, describe_uex_api_error
from bot.uex.leaderboard import LeaderboardEntry, rank_leaderboard, sum_sell_revenue

logger = logging.getLogger("uexbot.trades")

OPERATION_CHOICES = [
    app_commands.Choice(name="Buy", value="buy"),
    app_commands.Choice(name="Sell", value="sell"),
]

# Small pacing delay between per-user /user_trades calls in /leaderboard, so a server with
# many linked accounts doesn't burst well past what's reasonable against UEX's rate limit.
_LEADERBOARD_CALL_DELAY = 0.3


def _trade_time(value) -> str:
    """A UEX trade's `date_added` (a unix timestamp) as a Discord timestamp, shown in each
    viewer's own time zone - it used to print as a raw number like 1790000000 (audit MSG-9)."""
    try:
        return f"<t:{int(value)}:f>"
    except (TypeError, ValueError):
        return str(value) if value not in (None, "") else "date unknown"


def _trade_line(entry: dict) -> str:
    terminal = f" @ {entry['terminal_name']}" if entry["terminal_name"] else ""
    return (
        f"#{entry['id']} [{entry['logged_at']}] {entry['operation'].upper()} "
        f"{entry['quantity_scu']} SCU {entry['commodity_name']}{terminal} "
        f"({entry['unit_price']:.2f} aUEC/unit)"
    )


def _trade_choice(entry: dict) -> dict:
    """One /trade-log entry as a row of the remove menu."""
    terminal = f" @ {entry['terminal_name']}" if entry["terminal_name"] else ""
    return {
        "id": entry["id"],
        "label": f"#{entry['id']} {entry['operation'].upper()} {entry['quantity_scu']} SCU {entry['commodity_name']}",
        "description": f"{entry['unit_price']:.2f} aUEC/unit{terminal} · {entry['logged_at']}",
    }


LEADERBOARD_LIMIT = 10


class Trades(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    @app_commands.command(name="trade-log-add", description="Log a buy/sell to your personal trade ledger.")
    @app_commands.describe(
        operation="Buy or sell",
        commodity="Commodity name - autocompletes",
        quantity_scu="Quantity in SCU",
        unit_price="Price per unit (aUEC)",
        terminal="Where the trade happened (optional) - autocompletes",
    )
    @app_commands.choices(operation=OPERATION_CHOICES)
    @app_commands.autocomplete(commodity=commodity_name_autocomplete, terminal=terminal_name_autocomplete)
    async def trade_log_add(
        self,
        interaction: discord.Interaction,
        operation: app_commands.Choice[str],
        commodity: str,
        quantity_scu: float,
        unit_price: float,
        terminal: str | None = None,
    ) -> None:
        # Deferred before any DB write: a write can wait on a lock past Discord's
        # 3-second window, and a player who sees "did not respond" retries into a
        # duplicate (audit REL-8).
        await interaction.response.defer(ephemeral=True)
        entry_id = await self.bot.db.log_trade(
            user_id=interaction.user.id,
            commodity_name=commodity,
            operation=operation.value,
            terminal_name=terminal,
            quantity_scu=quantity_scu,
            unit_price=unit_price,
        )
        total = quantity_scu * unit_price
        await interaction.followup.send(
            f"Logged #{entry_id}: {operation.value} {quantity_scu} SCU of {commodity} "
            f"@ {unit_price:.2f} aUEC (total {total:,.0f} aUEC). A wrong entry can be removed "
            "from /trade-log.",
            ephemeral=True,
        )

    @app_commands.command(name="trade-log", description="Show your recent logged trades, and remove a wrong one.")
    @app_commands.describe(limit="How many recent trades to show (1-50, default 10)")
    async def trade_log(
        self, interaction: discord.Interaction, limit: app_commands.Range[int, 1, 50] = 10,
    ) -> None:
        entries = await self.bot.db.get_trade_log(interaction.user.id, limit=limit)
        if not entries:
            await interaction.response.send_message("No trades logged yet. Use /trade-log-add.", ephemeral=True)
            return
        # There was no way to fix a mistyped entry (audit UX-16). The menu removes one; the
        # list above it is redrawn without it, so a correction is remove, then re-add.
        shown = list(entries)

        async def remove(_: discord.Interaction, entry_id: int) -> str:
            removed = await self.bot.db.delete_trade_log_entry(interaction.user.id, int(entry_id))
            shown[:] = [e for e in shown if e["id"] != entry_id]
            head = f"Removed trade #{entry_id}." if removed else f"Trade #{entry_id} was already gone."
            if not shown:
                return f"{head} No trades left in this list."
            return head + "\n" + fit_lines([_trade_line(e) for e in shown], limit=MAX_MESSAGE_CHARS - len(head) - 1)

        view = AlertRemovePickerView(
            alerts=[_trade_choice(e) for e in entries], author_id=interaction.user.id,
            remove_callback=remove, placeholder_noun="trade",
        )
        await interaction.response.send_message(fit_lines([_trade_line(e) for e in entries]), view=view,
                                                ephemeral=True)
        view.origin = interaction

    @app_commands.command(name="uex-trades", description="Show your trade history as logged on UEX itself (requires a linked account).")
    async def uex_trades(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)

        secret_key = await self.bot.db.get_user_secret_key(interaction.user.id)
        if not secret_key:
            await interaction.followup.send(
                "You haven't linked a UEX account yet. Run /link-uex-account first "
                "(it opens a private form, nothing is posted in the channel).",
                ephemeral=True,
            )
            return

        try:
            rows = await self.bot.uex.get_user_trades(secret_key=secret_key)
        except UexApiError as exc:
            # Only an auth failure is the key's fault; this used to tell the player to
            # re-link for any failure, a UEX outage included (audit MSG-10).
            await interaction.followup.send(
                f"Couldn't fetch UEX trade history. {describe_uex_api_error(exc)}", ephemeral=True,
            )
            return

        if not rows:
            await interaction.followup.send("No trades found on your UEX account.", ephemeral=True)
            return

        lines = [
            f"{r.get('operation', '?')} {r.get('scu', '?')} SCU {r.get('commodity_name', '?')} "
            f"@ {r.get('price', '?')} aUEC/unit ({_trade_time(r.get('date_added'))})"
            for r in rows[:15]
        ]
        await interaction.followup.send(fit_lines(lines), ephemeral=True)

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
