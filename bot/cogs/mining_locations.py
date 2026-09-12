"""Where to Mine (/where-to-mine): for one raw material, shows which star systems, planets,
moons, and named mining sites (asteroid belts/rings) it's actually found at - a lookup, not
a route between multiple spots, since a mining run generally sticks to one location.
"""
from __future__ import annotations

import asyncio

import discord
from discord import app_commands
from discord.ext import commands

from bot.uex.exceptions import UexApiError, describe_uex_api_error
from bot.uex.mining_locations import describe_mining_locations, resolve_mineable_commodity


async def mineable_commodity_autocomplete(
    interaction: discord.Interaction, current: str
) -> list[app_commands.Choice[str]]:
    """Same pattern as commodity_name_autocomplete (bot/cogs/prices.py), scoped to is_raw
    only - not is_refinable, so hand-mined materials with no refinery pathway (e.g.
    Jaclium) are still suggested."""
    try:
        commodities = await interaction.client.uex.get_commodities()
    except UexApiError:
        return []
    mineable = [c for c in commodities if c.get("is_raw")]
    current_lower = current.lower()
    matches = [c for c in mineable if current_lower in (c.get("name") or "").lower()][:25]
    return [app_commands.Choice(name=(c.get("name") or "")[:100], value=c.get("name") or "") for c in matches]


class MiningLocations(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    @app_commands.command(
        name="where-to-mine",
        description="Show which star systems, planets, moons, and mining sites a raw material is found at.",
    )
    @app_commands.describe(ore="Raw/mineable commodity name, e.g. 'Ouratite (Raw)' or 'Jaclium (Ore)'")
    @app_commands.autocomplete(ore=mineable_commodity_autocomplete)
    async def where_to_mine(self, interaction: discord.Interaction, ore: str) -> None:
        await interaction.response.defer()
        try:
            commodities = await self.bot.uex.get_commodities()
        except UexApiError as exc:
            await interaction.followup.send(describe_uex_api_error(exc))
            return

        commodity = resolve_mineable_commodity(commodities, ore)
        if commodity is None:
            await interaction.followup.send(
                f"Couldn't find a raw material matching '{ore}' - pick from the autocomplete suggestions."
            )
            return

        try:
            star_systems, planets, moons, pois = await asyncio.gather(
                self.bot.uex.get_star_systems(),
                self.bot.uex.get_planets(),
                self.bot.uex.get_moons(),
                self.bot.uex.get_poi(),
            )
        except UexApiError as exc:
            await interaction.followup.send(describe_uex_api_error(exc))
            return

        info = describe_mining_locations(
            commodity,
            star_systems_by_id={s["id"]: s["name"] for s in star_systems},
            planets_by_id={p["id"]: p["name"] for p in planets},
            moons_by_id={m["id"]: m["name"] for m in moons},
            poi_rows_by_id={p["id"]: p for p in pois},
        )

        embed = discord.Embed(title=f"{info.commodity_name} — Where to Mine", color=discord.Color.blurple())
        if info.star_systems:
            embed.add_field(name="Star system(s)", value=", ".join(info.star_systems), inline=True)
        if info.planets:
            embed.add_field(name="Planet(s)", value=", ".join(info.planets), inline=True)
        if info.moons:
            embed.add_field(name="Moon(s)", value=", ".join(info.moons), inline=True)
        if info.mining_pois:
            embed.add_field(name="Named mining sites", value="\n".join(info.mining_pois), inline=False)
        if info.hotspots:
            lines = [f"**{spot.location}** — {spot.concentration_pct:g}%" for spot in info.hotspots]
            embed.add_field(name="Richest known concentration", value="\n".join(lines), inline=False)
        if not (info.star_systems or info.planets or info.moons or info.mining_pois or info.hotspots):
            embed.description = "No location data available for this material yet."

        footer = "Location data from UEX Corp, cached up to 24h."
        if info.difficulty is not None and info.mining_profile is not None:
            profile = info.mining_profile
            lines = [
                f"**{info.difficulty.capitalize()}** "
                f"(resistance {profile.resistance:g}, instability {profile.instability:g})",
                f"Optimal charge window: {profile.optimal_window:g} "
                "(side note only, not part of the rating above)",
            ]
            embed.add_field(name="Mining difficulty", value="\n".join(lines), inline=False)
        if info.difficulty is not None or info.hotspots:
            footer += (
                " Difficulty rating and concentration hotspots are community-sourced game data, "
                "not from UEX - may not reflect the current game balance."
            )

        embed.set_footer(text=footer)
        await interaction.followup.send(embed=embed)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(MiningLocations(bot))
