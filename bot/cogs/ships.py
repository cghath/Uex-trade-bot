"""The shared ship-name autocomplete, used by every command with a `ship` option.

This was also the Ships cog, with /set-default-ship and /clear-default-ship, until audit
UX-15 folded both into /set-trading-preferences' `ship` option: the same saved setting,
which had been settable two ways with a description that named only /best-route. The ship
is stored in user_trading_preferences.ship_name (Database.get_default_ship reads it).
"""
from __future__ import annotations

import discord
from discord import app_commands

from bot.autocomplete import fetch_within


async def ship_name_autocomplete(interaction: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
    vehicles = await fetch_within(interaction.client.uex.get_vehicles())
    if vehicles is None:
        return []
    current_lower = current.lower()
    matches = [v for v in vehicles if current_lower in (v.get("name") or "").lower()][:25]
    return [app_commands.Choice(name=(v.get("name") or "")[:100], value=v.get("name") or "") for v in matches]
