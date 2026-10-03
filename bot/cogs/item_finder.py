"""In-game Item Finder (/ingame-item-finder): for one catalogued item (weapons, armor,
ammo, and more - the same /items catalog Marketplace already searches), shows which shops
actually sell it right now, closest to a given location first. A lookup, not a route -
same spirit as /where-to-mine (bot/cogs/mining_locations.py), just for buyable gear
instead of mineable ore.
"""
from __future__ import annotations

import asyncio
import logging

import discord
from discord import app_commands
from discord.ext import commands

from bot.autocomplete import gather_within
from bot.cogs.prices import terminal_name_autocomplete
from bot.uex.exceptions import UexApiError, describe_uex_api_error
from bot.uex.item_finder import (
    highlights_block,
    item_footer,
    item_header,
    place_and_vendor_text,
    rank_item_listings,
    split_place_and_vendor,
    system_block,
)
from bot.uex.client import fetch_terminal_distances
from bot.uex.marketplace import find_item_id_by_name
from bot.uex.route_presentation import chunk_lines

logger = logging.getLogger(__name__)

# A finder result list longer than this stops being a quick lookup - the closest matches
# are always the most useful ones anyway, since results are already distance-sorted.
MAX_RESULTS_SHOWN = 15
# /terminals_distances is one live call per candidate terminal (no batch endpoint) -
# batched to stay well under UEX's 120/min ceiling even for a widely-stocked item, the
# same batch_size UexClient.get_item_catalog already uses for its own per-category fetches.
DISTANCE_BATCH_SIZE = 8
# Discord's own autocomplete choice cap.
MAX_AUTOCOMPLETE_CHOICES = 25


async def sold_item_name_autocomplete(interaction: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
    """Scoped to items UEX currently reports at least one real shop price for
    (`/items_prices_all`), not the full item catalog (`item_name_autocomplete` in
    bot/cogs/marketplace.py, used by Marketplace commands where any catalogued item can
    legitimately be listed). Confirmed live that most catalogued items - cosmetics, ship
    paint, and similar - have no shop listing at all: of 7,769 distinct catalog names,
    only 2,829 ever appear in `/items_prices_all`. Suggesting the other ~5,000 just
    guarantees "No shop currently lists X for sale" the instant a player picks one -
    exactly the "autocomplete feels pointlessly bloated" complaint this fixes. Unlike
    Marketplace's `traded_item_autocomplete` (which falls back to the full catalog for a
    coverage gap in the BOT'S OWN activity tracking, not the item's real availability),
    this deliberately does NOT fall back to the full catalog - an item genuinely absent
    from a real shop-price pull should stay unreachable from here, not resurface as a
    dead-end suggestion."""
    (rows,) = await gather_within(interaction.client.uex.get_items_prices_all())
    if isinstance(rows, (UexApiError, TimeoutError)):
        return []
    if isinstance(rows, BaseException):
        raise rows
    current_lower = current.lower()
    seen: set[str] = set()
    matches: list[str] = []
    for row in rows:
        name = row.get("item_name")
        if not name or name in seen:
            continue
        seen.add(name)
        if current_lower in name.lower():
            matches.append(name)
            if len(matches) >= MAX_AUTOCOMPLETE_CHOICES:
                break
    return [app_commands.Choice(name=name[:100], value=name) for name in matches]


class ItemFinder(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self._warm_task: asyncio.Task | None = None

    async def cog_load(self) -> None:
        # Fill the autocomplete's 12h cache at startup, so the first person to type after a
        # restart isn't the one who pays for the cold fetch (see bot/autocomplete.py).
        self._warm_task = asyncio.create_task(self._warm_autocomplete_cache())

    async def cog_unload(self) -> None:
        if self._warm_task is not None:
            self._warm_task.cancel()

    async def _warm_autocomplete_cache(self) -> None:
        try:
            await self.bot.uex.get_items_prices_all()
        except UexApiError as exc:
            logger.warning("Couldn't pre-load /ingame-item-finder autocomplete data: %s", exc)

    @app_commands.command(
        name="ingame-item-finder",
        description="Find shops selling a weapon, armor, ammo, or other in-game item, closest to you first.",
    )
    @app_commands.describe(
        item="Item name (weapons, armor, ammo, and more) - autocompletes.",
        location="Terminal you're at, e.g. 'Area18' or 'Port Tressler' - autocompletes.",
    )
    @app_commands.autocomplete(item=sold_item_name_autocomplete, location=terminal_name_autocomplete)
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
        # Grouped by star system instead of one flat list - repeating "Pyro" on every line got
        # noisy fast on real data, where a widely-stocked item lists a dozen+ shops in one
        # system. dict keeps insertion order, and `shown` is already closest-first (the origin's
        # own system sorts first per rank_item_listings), so its group comes first. Each shop
        # names its place (not the raw "Vendor - Place" terminal name) with the vendor beside
        # it, so two shops at the same place stay distinguishable.
        by_system: dict[str, list] = {}
        for listing in shown:
            by_system.setdefault(listing.star_system_name or "Unknown system", []).append(listing)
        origin = place_and_vendor_text(*split_place_and_vendor({"terminal_name": origin_name}))
        blocks = [
            item_header(item_display, len(ranked), origin),
            highlights_block(ranked, origin_star_system=origin_star_system),
            "\n\n".join(system_block(system, listings, origin_star_system=origin_star_system)
                         for system, listings in by_system.items()),
            item_footer(len(ranked) - len(shown)),
        ]

        # One layout (the owner's pick, 2026-10-03): the shops shown are capped at
        # MAX_RESULTS_SHOWN, well inside Discord's 4,000 characters. If Discord refuses it
        # anyway, the same blocks go as text - the same facts, nothing dropped.
        layout = discord.ui.LayoutView()
        container = discord.ui.Container(accent_colour=discord.Colour.blurple())
        for index, block in enumerate(blocks):
            if index:
                container.add_item(discord.ui.Separator())
            container.add_item(discord.ui.TextDisplay(block))
        layout.add_item(container)
        try:
            await interaction.followup.send(view=layout)
            return
        except discord.HTTPException as exc:
            logger.warning("Item finder layout send failed (%s); falling back to text", exc)
        for chunk in chunk_lines("\n\n".join(blocks).split("\n"), max_length=1900):
            await interaction.followup.send(content=chunk)

    async def _fetch_distances(self, origin_id: int, terminal_ids: list[int]) -> dict[int, float | None]:
        """id_terminal -> gigameters from origin_id, batched via asyncio.gather so a
        widely-stocked item doesn't serialize dozens of live /terminals_distances calls.
        The origin terminal itself (if it's also a candidate) never needs a live call -
        distance to itself is trivially 0."""
        return await fetch_terminal_distances(self.bot.uex, origin_id, terminal_ids, batch_size=DISTANCE_BATCH_SIZE)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(ItemFinder(bot))
