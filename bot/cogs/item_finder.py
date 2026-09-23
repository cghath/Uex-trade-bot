"""In-game Item Finder (/ingame-item-finder): for one catalogued item (weapons, armor,
ammo, and more - the same /items catalog Marketplace already searches), shows which shops
actually sell it right now, closest to a given location first. A lookup, not a route -
same spirit as /where-to-mine (bot/cogs/mining_locations.py), just for buyable gear
instead of mineable ore.
"""
from __future__ import annotations

import asyncio

import discord
from discord import app_commands
from discord.ext import commands

from bot.cogs.marketplace import item_name_autocomplete
from bot.cogs.prices import terminal_name_autocomplete
from bot.uex.exceptions import UexApiError, describe_uex_api_error
from bot.uex.item_finder import format_item_listing_line, rank_item_listings
from bot.uex.marketplace import find_item_id_by_name
from bot.uex.route_presentation import add_chunked_fields, chunk_lines

# A finder result list longer than this stops being a quick lookup - the closest matches
# are always the most useful ones anyway, since results are already distance-sorted.
MAX_RESULTS_SHOWN = 15
# /terminals_distances is one live call per candidate terminal (no batch endpoint) -
# batched to stay well under UEX's 120/min ceiling even for a widely-stocked item, the
# same batch_size UexClient.get_item_catalog already uses for its own per-category fetches.
DISTANCE_BATCH_SIZE = 8


class ItemFinder(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    @app_commands.command(
        name="ingame-item-finder",
        description="Find shops selling a weapon, armor, ammo, or other in-game item, closest to you first.",
    )
    @app_commands.describe(
        item="Item name (weapons, armor, ammo, and more) - autocompletes.",
        location="Terminal you're at, e.g. 'Area18' or 'Port Tressler' - autocompletes.",
    )
    @app_commands.autocomplete(item=item_name_autocomplete, location=terminal_name_autocomplete)
    async def ingame_item_finder(self, interaction: discord.Interaction, item: str, location: str) -> None:
        await interaction.response.defer()

        resolved = await self.bot.db.resolve_terminal_id_by_name(location)
        if resolved is None:
            await interaction.followup.send(
                f"Couldn't find a single terminal matching '{location}' - pick one from the "
                "autocomplete list to make sure it's unambiguous."
            )
            return
        origin_id, origin_name = resolved

        try:
            catalog = await self.bot.uex.get_item_catalog()
        except UexApiError as exc:
            await interaction.followup.send(describe_uex_api_error(exc))
            return
        id_item = find_item_id_by_name(catalog, item)
        if id_item is None:
            await interaction.followup.send(
                f"Couldn't find a single item matching '{item}' - pick one from the autocomplete list."
            )
            return

        try:
            listings = await self.bot.uex.get_items_prices(id_item=id_item)
        except UexApiError as exc:
            await interaction.followup.send(describe_uex_api_error(exc))
            return
        if not listings:
            await interaction.followup.send(f"No shop currently lists **{item}** for sale.")
            return

        item_display = listings[0].get("item_name") or item
        candidate_ids = sorted({
            int(row["id_terminal"]) for row in listings if row.get("id_terminal") is not None
        })
        distances = await self._fetch_distances(origin_id, candidate_ids)
        ranked = rank_item_listings(listings, distances)
        if not ranked:
            await interaction.followup.send(f"No shop currently lists **{item_display}** for sale.")
            return

        shown = ranked[:MAX_RESULTS_SHOWN]
        lines = [format_item_listing_line(listing) for listing in shown]

        # Footer set BEFORE add_chunked_fields runs (not after), matching this codebase's
        # established convention (see /price's and /where-to-mine's identical ordering) -
        # its len(embed) budget check needs the footer's real length already counted.
        footer = "Prices from UEX Corp, cached up to 24h · distances cached up to 12h."
        if len(ranked) > len(shown):
            footer += f" · {len(ranked) - len(shown)} more shop(s) omitted, showing the closest {len(shown)}."

        embed = discord.Embed(
            title=f"{item_display} — Where to Buy",
            description=f"Closest to **{origin_name}** first.",
            color=discord.Color.blurple(),
        )
        embed.set_footer(text=footer)

        if add_chunked_fields(embed, name="Shops", lines=lines):
            await interaction.followup.send(embed=embed)
            return

        # Plain-text fallback for a result list too large for one embed - same
        # disclose-don't-drop pattern as /mixed-routes'/multi-stop-route's own fallbacks.
        fallback_lines = [
            f"**{item_display} — Where to Buy**", f"Closest to **{origin_name}** first.", "", *lines, "", footer,
        ]
        for chunk in chunk_lines(fallback_lines, max_length=1900):
            await interaction.followup.send(content=chunk)

    async def _fetch_distances(self, origin_id: int, terminal_ids: list[int]) -> dict[int, float | None]:
        """id_terminal -> gigameters from origin_id, batched via asyncio.gather so a
        widely-stocked item doesn't serialize dozens of live /terminals_distances calls.
        The origin terminal itself (if it's also a candidate) never needs a live call -
        distance to itself is trivially 0."""
        distances: dict[int, float | None] = {}
        to_fetch = [tid for tid in terminal_ids if tid != origin_id]
        if origin_id in terminal_ids:
            distances[origin_id] = 0.0
        for start in range(0, len(to_fetch), DISTANCE_BATCH_SIZE):
            batch = to_fetch[start:start + DISTANCE_BATCH_SIZE]
            results = await asyncio.gather(
                *(self.bot.uex.get_terminal_distance(origin_id, tid) for tid in batch),
                return_exceptions=True,
            )
            for tid, result in zip(batch, results):
                if isinstance(result, Exception) or not result:
                    distances[tid] = None
                    continue
                try:
                    distances[tid] = float(result.get("distance"))
                except (TypeError, ValueError):
                    distances[tid] = None
        return distances


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(ItemFinder(bot))
