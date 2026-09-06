"""Saved per-user route-filter defaults, applied by /best-route, /top-routes,
/mixed-routes, and /multi-stop-route whenever their matching option is left unset."""
from __future__ import annotations

import discord
from discord import app_commands
from discord.ext import commands

from bot.cogs.prices import SYSTEM_CHOICES
from bot.uex.trading_preferences import UNSET, format_trading_preferences

SYSTEM_PREFERENCE_CHOICES = [*SYSTEM_CHOICES, app_commands.Choice(name="Any (no restriction)", value="any")]

RISK_TOLERANCE_CHOICES = [
    app_commands.Choice(name="Low - avoid illegal/explosive/buggy/volatile goods", value="low"),
    app_commands.Choice(name="Medium - avoid illegal or buggy goods only", value="medium"),
    app_commands.Choice(name="High - no restriction (default)", value="high"),
]


class TradingPreferences(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    @app_commands.command(
        name="set-trading-preferences",
        description="Save route-filter defaults so you don't have to repeat them every call.",
    )
    @app_commands.describe(
        space_only="mixed-routes/multi-stop-route default: require confirmed space stations only",
        capital_ship_access="mixed-routes/multi-stop-route default: force XL-hangar/freight-elevator filtering, any ship",
        auto_load_only="Default auto-load-only for all 4 route commands",
        system="Default star-system restriction for all 4 route commands ('Any' clears it)",
        risk_tolerance="Illegal/volatile/explosive/buggy tolerance - stored now, not yet enforced by routes",
    )
    @app_commands.rename(
        space_only="space-only",
        capital_ship_access="capital-ship-access",
        auto_load_only="auto-load-only",
        risk_tolerance="risk-tolerance",
    )
    @app_commands.choices(system=SYSTEM_PREFERENCE_CHOICES, risk_tolerance=RISK_TOLERANCE_CHOICES)
    async def set_trading_preferences(
        self,
        interaction: discord.Interaction,
        space_only: bool | None = None,
        capital_ship_access: bool | None = None,
        auto_load_only: bool | None = None,
        system: app_commands.Choice[str] | None = None,
        risk_tolerance: app_commands.Choice[str] | None = None,
    ) -> None:
        if (
            space_only is None
            and capital_ship_access is None
            and auto_load_only is None
            and system is None
            and risk_tolerance is None
        ):
            await interaction.response.send_message(
                "Pass at least one option to change. Use /my-trading-preferences to see your "
                "current settings.",
                ephemeral=True,
            )
            return

        prefs = await self.bot.db.set_trading_preferences(
            interaction.user.id,
            space_only=space_only if space_only is not None else UNSET,
            capital_ship_access=capital_ship_access if capital_ship_access is not None else UNSET,
            auto_load_only=auto_load_only if auto_load_only is not None else UNSET,
            preferred_system=(
                UNSET if system is None else (None if system.value == "any" else system.value)
            ),
            risk_tolerance=UNSET if risk_tolerance is None else risk_tolerance.value,
        )
        await interaction.response.send_message(
            f"Trading preferences updated.\n{format_trading_preferences(prefs)}",
            ephemeral=True,
        )

    @app_commands.command(
        name="clear-trading-preferences",
        description="Reset all saved trading preferences back to their defaults.",
    )
    async def clear_trading_preferences(self, interaction: discord.Interaction) -> None:
        removed = await self.bot.db.clear_trading_preferences(interaction.user.id)
        msg = "Trading preferences cleared." if removed else "You don't have any saved trading preferences."
        await interaction.response.send_message(msg, ephemeral=True)

    @app_commands.command(
        name="my-trading-preferences",
        description="Show your saved trading-preference defaults.",
    )
    async def my_trading_preferences(self, interaction: discord.Interaction) -> None:
        prefs = await self.bot.db.get_trading_preferences(interaction.user.id)
        await interaction.response.send_message(
            "**Your trading preferences**\n"
            f"{format_trading_preferences(prefs)}\n\n"
            "Applied automatically whenever you don't pass the matching option yourself. "
            "Set with /set-trading-preferences, reset with /clear-trading-preferences.",
            ephemeral=True,
        )


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(TradingPreferences(bot))
