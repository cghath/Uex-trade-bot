"""Per-user default ship, used by /best-route to show real haulable cargo (SCU)."""
from __future__ import annotations

import discord
from discord import app_commands
from discord.ext import commands

from bot.uex.exceptions import UexApiError
from bot.uex.ships import resolve_ship


async def ship_name_autocomplete(interaction: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
    try:
        vehicles = await interaction.client.uex.get_vehicles()
    except UexApiError:
        return []
    current_lower = current.lower()
    matches = [v for v in vehicles if current_lower in (v.get("name") or "").lower()][:25]
    return [app_commands.Choice(name=(v.get("name") or "")[:100], value=v.get("name") or "") for v in matches]


class Ships(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    @app_commands.command(name="set-default-ship", description="Set your default cargo ship, used by /best-route to show how much you can actually haul.")
    @app_commands.describe(ship="Ship name, e.g. 'Cutlass Black' or 'Caterpillar'")
    @app_commands.autocomplete(ship=ship_name_autocomplete)
    async def set_default_ship(self, interaction: discord.Interaction, ship: str) -> None:
        try:
            vehicles = await self.bot.uex.get_vehicles()
        except UexApiError as exc:
            await interaction.response.send_message(f"UEX API error: {exc}", ephemeral=True)
            return

        vehicle = resolve_ship(vehicles, ship)
        if vehicle is None:
            await interaction.response.send_message(
                f"Couldn't find a single unambiguous match for '{ship}'. Try the full ship name "
                "and pick from the autocomplete suggestions.",
                ephemeral=True,
            )
            return

        await self.bot.db.set_default_ship(interaction.user.id, vehicle.get("name"))
        scu = vehicle.get("scu")
        scu_text = f"{scu:,.0f} SCU" if scu else "unknown cargo capacity"
        await interaction.response.send_message(
            f"Default ship set to **{vehicle.get('name')}** ({scu_text}). "
            "/best-route will now show how much of a run you can actually haul.",
            ephemeral=True,
        )

    @app_commands.command(name="clear-default-ship", description="Clear your default ship.")
    async def clear_default_ship(self, interaction: discord.Interaction) -> None:
        removed = await self.bot.db.clear_default_ship(interaction.user.id)
        msg = "Default ship cleared." if removed else "You don't have a default ship set."
        await interaction.response.send_message(msg, ephemeral=True)

    @app_commands.command(name="my-ship", description="Show your current default ship.")
    async def my_ship(self, interaction: discord.Interaction) -> None:
        ship_name = await self.bot.db.get_default_ship(interaction.user.id)
        if not ship_name:
            await interaction.response.send_message("No default ship set. Use /set-default-ship.", ephemeral=True)
            return

        try:
            vehicles = await self.bot.uex.get_vehicles()
            vehicle = resolve_ship(vehicles, ship_name)
        except UexApiError:
            vehicle = None

        if vehicle is None:
            await interaction.response.send_message(
                f"Default ship is set to '{ship_name}', but it couldn't be matched against UEX's current "
                "ship list (maybe renamed) - try /set-default-ship again.",
                ephemeral=True,
            )
            return

        scu = vehicle.get("scu")
        scu_text = f"{scu:,.0f} SCU" if scu else "unknown cargo capacity"
        await interaction.response.send_message(f"Default ship: **{vehicle.get('name')}** ({scu_text})", ephemeral=True)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Ships(bot))
