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
from bot.uex.item_finder import ItemListing, build_item_listing_table, rank_item_listings
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
        origin_star_system = await self.bot.db.get_terminal_star_system(origin_id)

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
        ranked = rank_item_listings(listings, distances, origin_star_system=origin_star_system)
        if not ranked:
            await interaction.followup.send(f"No shop currently lists **{item_display}** for sale.")
            return

        shown = ranked[:MAX_RESULTS_SHOWN]
        # Grouped by star system as separate fields instead of one flat list - repeating
        # "Pyro →" (or any system) on every single line got noisy fast on real data, where
        # a widely-stocked item can list a dozen+ shops in the same system. dict preserves
        # insertion order, and `shown` is already closest-first (same-system-as-origin
        # listings sort first per rank_item_listings), so the origin's own system - if it
        # has any results - is naturally the first group shown.
        #
        # Each group renders as a monospace table (place, vendor, price, distance) inside
        # one or more ```code blocks``` - Discord embeds have no real <table>, and a code
        # block is the only way to get real column alignment. Place (not the raw
        # "Vendor - Place" terminal name) is the primary label so results are actually
        # navigable, and a separate vendor column keeps two shops at the same place (e.g.
        # two different gun stores both at Checkmate Station) distinguishable.
        listings_by_system: dict[str, list[ItemListing]] = {}
        for listing in shown:
            system_label = listing.star_system_name or "Unknown system"
            listings_by_system.setdefault(system_label, []).append(listing)

        # Footer set BEFORE add_chunked_fields runs (not after), matching this codebase's
        # established convention (see /price's and /where-to-mine's identical ordering) -
        # its len(embed) budget check needs the footer's real length already counted.
        footer = "Prices from UEX Corp, cached up to 24h · distances cached up to 12h."
        if len(ranked) > len(shown):
            footer += f" · {len(ranked) - len(shown)} more shop(s) omitted, showing the closest {len(shown)}."

        embed = discord.Embed(
            title=f"{item_display} — Where to Buy",
            description=f"Closest to **{origin_name}** first. Prices in aUEC.",
            color=discord.Color.blurple(),
        )
        embed.set_footer(text=footer)

        # Each system's table is built as a LIST of already-fenced blocks (usually one,
        # more if the row count needs splitting - see build_item_listing_table) so
        # add_chunked_fields/chunk_lines only ever has to pack whole blocks together, never
        # hard-split one mid-row, which would break its fences.
        table_by_system: dict[str, list[str]] = {
            system_label: build_item_listing_table(group, origin_star_system=origin_star_system)
            for system_label, group in listings_by_system.items()
        }

        # All-or-nothing across every system group, not per group - the same
        # disclose-don't-drop guarantee add_chunked_fields already gives one field, applied
        # here so a result list that doesn't fully fit never sends with some systems shown
        # and others silently missing.
        all_fit = all(
            add_chunked_fields(embed, name=system_label, lines=blocks)
            for system_label, blocks in table_by_system.items()
        )
        if all_fit:
            await interaction.followup.send(embed=embed)
            return

        # Plain-text fallback for a result list too large for one embed - same
        # disclose-don't-drop pattern as /mixed-routes'/multi-stop-route's own fallbacks.
        fallback_lines = [
            f"**{item_display} — Where to Buy**",
            f"Closest to **{origin_name}** first. Prices in aUEC.",
            "",
        ]
        for system_label, blocks in table_by_system.items():
            fallback_lines.append(f"**{system_label}**")
            fallback_lines.extend(blocks)
            fallback_lines.append("")
        fallback_lines.append(footer)
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
