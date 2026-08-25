"""/intro - a compact, self-maintaining command guide.

Discord shows a command's description and options when a player starts typing it,
so /intro acts as a readable map rather than repeating every long description.
Command names are still read from the live command tree, and an uncategorized
command still appears under "Other" instead of becoming invisible.
"""
from __future__ import annotations

import discord
from discord import app_commands
from discord.ext import commands

# Display order + grouping. A command name not listed here still appears, under "Other".
# These remain available as slash commands but are implementation health checks rather than
# normal player tools, so they do not add noise to /intro.
HIDDEN_COMMANDS = {"marketplace-index-status"}

CATEGORIES: list[tuple[str, str, list[str]]] = [
    ("💰 Prices & Routes", "Terminal prices, profitable hauls, and ranked live routes.", [
        "price", "best-route", "top-routes", "terminal-history",
    ]),
    ("📊 Commodity Trends", "Trade volume, price movement, and commodity price charts.", [
        "trending", "movers", "commodity-history",
    ]),
    ("🚀 Ship & Cargo", "Save a ship once to calculate cargo limits and per-run profit.", [
        "set-default-ship", "clear-default-ship", "my-ship",
    ]),
    ("🔔 Alerts & Notifications", "Price targets, Marketplace matches, restocks, and DM delivery checks.", [
        "alert-add", "alert-list", "alert-remove",
        "marketplace-alert-add", "marketplace-alert-list", "marketplace-alert-remove",
        "stock-alert-add", "stock-alert-list", "stock-alert-remove", "test-dm",
    ]),
    ("🧾 Trade Log & Leaderboard", "Your personal ledger, UEX-recorded history, and server standings.", [
        "trade-log-add", "trade-log", "uex-trades", "leaderboard",
    ]),
    ("🛒 UEX Marketplace", "Search and analyze listings, or manage your own Marketplace activity.", [
        "marketplace-search", "marketplace-trending", "marketplace-movers", "marketplace-average",
        "marketplace-history", "my-negotiations", "my-favorites", "marketplace-post",
        "marketplace-delete-listing",
    ]),
    ("🔥 Marketplace Intelligence", "Sellability rankings and history for all items, plus quality-matched raw-material deal scans.", [
        "liquidity-rank", "liquidity-trends", "scan-now", "scanner-status", "set-scanner-channel",
    ]),
    ("🗓️ Daily Digest", "Post a snapshot now or configure the server's scheduled digest.", [
        "digest-now", "set-digest-channel", "digest-disable",
    ]),
    ("🔗 Account Linking", "Connect UEX securely to use personal Marketplace and trade-history tools.", [
        "link-uex-account", "unlink-uex-account", "uex-account-status",
    ]),
    ("❓ Help", "Return to this guide whenever you need a quick map.", ["intro"]),
]


class Help(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    @app_commands.command(name="intro", description="Show what this bot can do and list every command.")
    async def intro(self, interaction: discord.Interaction) -> None:
        # Acknowledge before assembling the guide so a busy background refresh can never
        # let Discord's short interaction-response window expire.
        await interaction.response.defer()
        commands_by_name = {c.name: c for c in self.bot.tree.get_commands()}
        categorized_names: set[str] = set()

        embed = discord.Embed(
            title="UEX Trading Bot — Commands",
            description=(
                "Your quick map to live UEX Corp trading tools.\n\n"
                "**Start here:** `/price` for terminal prices · `/best-route` for a haul · "
                "`/liquidity-rank` for Marketplace sellability.\n\n"
                "Start typing any command to see its options and autocomplete."
            ),
            color=discord.Color.blurple(),
        )

        for category_name, summary, command_names in CATEGORIES:
            available_commands = []
            for name in command_names:
                cmd = commands_by_name.get(name)
                if cmd is None:
                    continue  # command was renamed/removed - don't show a dead entry
                categorized_names.add(name)
                available_commands.append(cmd)
            if available_commands:
                value = f"{summary}\n{_format_command_list(available_commands)}"
                _add_command_fields(embed, category_name, [value])

        leftover = [
            c for n, c in commands_by_name.items()
            if n not in categorized_names and n not in HIDDEN_COMMANDS
        ]
        if leftover:
            _add_command_fields(embed, "Other", [_format_command_list(sorted(leftover, key=lambda c: c.name))])

        embed.set_footer(text="Tip: commands with a text option usually support autocomplete — start typing and pick from the dropdown.")
        await interaction.followup.send(embed=embed)


def _format_command_list(commands: list[app_commands.Command]) -> str:
    return " · ".join(f"`/{cmd.name}`" for cmd in commands)


def _add_command_fields(embed: discord.Embed, category_name: str, lines: list[str]) -> None:
    """Add one or more Discord-safe fields for a command category.

    Discord limits a field value to 1,024 characters. Command descriptions are pulled
    from the live command tree, so a category can legitimately outgrow that limit as
    commands become more helpful. Split only between whole command lines so no command
    description is truncated.
    """
    chunks: list[list[str]] = []
    current: list[str] = []
    current_length = 0
    for line in lines:
        added_length = len(line) + (1 if current else 0)  # newline between entries
        if current and current_length + added_length > 1024:
            chunks.append(current)
            current = []
            current_length = 0
        current.append(line)
        current_length += len(line) + (1 if len(current) > 1 else 0)
    if current:
        chunks.append(current)

    for index, chunk in enumerate(chunks):
        name = category_name if index == 0 else f"{category_name} (continued)"
        embed.add_field(name=name, value="\n".join(chunk), inline=False)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Help(bot))
