"""/intro - a self-documenting command list.

Descriptions are pulled live from each command's registered `description=`, not
hand-copied here, so this can't silently drift out of sync as commands change.
Only the grouping (which category each command name belongs in) is maintained by
hand; anything not explicitly grouped still shows up under "Other" rather than
being silently dropped, so a new command is never invisible even before someone
remembers to categorize it.
"""
from __future__ import annotations

import discord
from discord import app_commands
from discord.ext import commands

# Display order + grouping. A command name not listed here still appears, under "Other".
CATEGORIES: list[tuple[str, list[str]]] = [
    ("Prices & Routes", ["price", "best-route"]),
    ("Trends & Movers", ["trending", "movers", "commodity-history", "top-scored-routes", "top-in-stock-routes"]),
    ("Ship & Cargo", ["set-default-ship", "clear-default-ship", "my-ship"]),
    ("Price Alerts", ["alert-add", "alert-list", "alert-remove"]),
    ("Trade Log & Leaderboard", ["trade-log-add", "trade-log", "uex-trades", "leaderboard"]),
    ("UEX Marketplace", [
        "marketplace-search", "marketplace-trending", "marketplace-movers", "marketplace-history",
        "my-negotiations", "my-favorites", "marketplace-post", "marketplace-delete-listing",
        "marketplace-index-status",
    ]),
    ("Marketplace Alerts", ["marketplace-alert-add", "marketplace-alert-list", "marketplace-alert-remove"]),
    ("Stock Alerts", ["stock-alert-add", "stock-alert-list", "stock-alert-remove"]),
    ("Daily Digest", ["digest-now", "set-digest-channel", "digest-disable"]),
    ("Account Linking", ["link-uex-account", "unlink-uex-account", "uex-account-status"]),
    ("Diagnostics", ["test-dm"]),
    ("Help", ["intro"]),
]


class Help(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    @app_commands.command(name="intro", description="Show what this bot can do and list every command.")
    async def intro(self, interaction: discord.Interaction) -> None:
        commands_by_name = {c.name: c for c in self.bot.tree.get_commands()}
        categorized_names: set[str] = set()

        embed = discord.Embed(
            title="UEX Trading Bot — Commands",
            description=(
                "Star Citizen trading tools backed by live UEX Corp data: commodity prices and "
                "trade routes, trend/volume tracking, price alerts, a personal trade ledger, "
                "cargo math for your ship, and the player-to-player UEX Marketplace.\n\n"
                "Options in *italics* are optional."
            ),
            color=discord.Color.blurple(),
        )

        for category_name, command_names in CATEGORIES:
            lines = []
            for name in command_names:
                cmd = commands_by_name.get(name)
                if cmd is None:
                    continue  # command was renamed/removed - don't show a dead entry
                categorized_names.add(name)
                lines.append(_format_command_line(cmd))
            if lines:
                embed.add_field(name=category_name, value="\n".join(lines), inline=False)

        leftover = [c for n, c in commands_by_name.items() if n not in categorized_names]
        if leftover:
            lines = [_format_command_line(c) for c in sorted(leftover, key=lambda c: c.name)]
            embed.add_field(name="Other", value="\n".join(lines), inline=False)

        embed.set_footer(text="Tip: most commands with a text option support autocomplete - start typing and pick from the dropdown.")
        await interaction.response.send_message(embed=embed)


def _format_command_line(cmd: app_commands.Command) -> str:
    params = "".join(
        f" *[{p.display_name}]*" if not p.required else f" <{p.display_name}>"
        for p in cmd.parameters
    )
    return f"`/{cmd.name}{params}` — {cmd.description}"


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Help(bot))
