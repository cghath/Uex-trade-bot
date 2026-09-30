"""Where to Buy a Ship (/where-to-buy-ship): every in-game terminal that sells one ship
for aUEC, and every terminal that rents it, cheapest first. A lookup like
/ingame-item-finder (bot/cogs/item_finder.py), but with no location option or distance
sort - ships are sold at only 7 terminals in-game. In-game prices only, never pledge-store.
"""
from __future__ import annotations

import asyncio
import logging
import sqlite3

import discord
from discord import app_commands
from discord.ext import commands

from bot.autocomplete import gather_within
from bot.uex.exceptions import UexApiError, describe_uex_api_error
from bot.uex.route_presentation import add_chunked_fields, chunk_lines
from bot.uex.ship_shops import (
    build_ship_shop_sections,
    rank_purchase_listings,
    rank_rental_listings,
    ship_autocomplete_names,
    ship_shop_description,
    terminal_ids_missing_star_system,
    vehicle_ids_with_listings,
)
from bot.uex.ships import resolve_ship

logger = logging.getLogger(__name__)

# Discord's own autocomplete choice cap.
MAX_AUTOCOMPLETE_CHOICES = 25
# Ship and terminal names are echoed back verbatim (including whatever the user typed for
# an unresolvable ship), so nothing this command sends is ever allowed to ping anyone.
NO_MENTIONS = discord.AllowedMentions.none()
FOOTER_TEXT = "Prices from UEX Corp datarunner reports, cached up to 12h."


def _raise_unexpected(*results: object, expected: tuple[type[BaseException], ...] = (UexApiError,)) -> None:
    """gather(return_exceptions=True) hands back every exception as a value - only the
    `expected` types are reportable failures; anything else is a bug and re-raised."""
    for result in results:
        if isinstance(result, BaseException) and not isinstance(result, expected):
            raise result


_AUTOCOMPLETE_FAILURES = (UexApiError, TimeoutError)


async def listed_ship_autocomplete(interaction: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
    """Only ships UEX has at least one in-game buy OR rent row for - the same lesson as
    /ingame-item-finder's sold_item_name_autocomplete: suggesting every one of /vehicles'
    ~280 ships (only 174 are buyable or rentable) just guarantees a dead-end "no location
    on record" answer for the rest. If one *_all call fails or runs past the autocomplete
    deadline, the other one's ships are still offered; if both (or /vehicles itself) do,
    no suggestions rather than an error."""
    uex = interaction.client.uex
    vehicles, purchase_rows, rental_rows = await gather_within(
        uex.get_vehicles(),
        uex.get_vehicle_purchase_prices_all(),
        uex.get_vehicle_rental_prices_all(),
    )
    _raise_unexpected(vehicles, purchase_rows, rental_rows, expected=_AUTOCOMPLETE_FAILURES)
    if isinstance(vehicles, _AUTOCOMPLETE_FAILURES):
        return []
    loaded = [rows for rows in (purchase_rows, rental_rows) if not isinstance(rows, _AUTOCOMPLETE_FAILURES)]
    if not loaded:
        return []
    names = ship_autocomplete_names(
        vehicles, vehicle_ids_with_listings(*loaded), current, limit=MAX_AUTOCOMPLETE_CHOICES,
    )
    return [app_commands.Choice(name=name[:100], value=name[:100]) for name in names]


class ShipShops(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self._warm_task: asyncio.Task | None = None

    async def cog_load(self) -> None:
        # Fill the autocomplete's three 12h caches at startup, so the first person to type
        # after a restart isn't the one who pays for the cold fetches (see bot/autocomplete.py).
        self._warm_task = asyncio.create_task(self._warm_autocomplete_cache())

    async def cog_unload(self) -> None:
        if self._warm_task is not None:
            self._warm_task.cancel()

    async def _warm_autocomplete_cache(self) -> None:
        results = await asyncio.gather(
            self.bot.uex.get_vehicles(),
            self.bot.uex.get_vehicle_purchase_prices_all(),
            self.bot.uex.get_vehicle_rental_prices_all(),
            return_exceptions=True,
        )
        for result in results:
            if isinstance(result, BaseException):
                logger.warning("Couldn't pre-load /where-to-buy-ship autocomplete data: %r", result)

    async def _star_systems_for(self, id_terminals: set[int]) -> dict[int, str]:
        """System per terminal from the cached terminal_reference table, for the few rows
        UEX sent with no star_system_name - without this, e.g. MOTH's Nyx rentals split
        across 'Rent — Nyx' and 'Rent — Unknown system'. Presentation only: a failed or
        empty lookup just leaves that row under 'Unknown system'."""
        systems: dict[int, str] = {}
        for id_terminal in sorted(id_terminals):
            try:
                system = await self.bot.db.get_terminal_star_system(id_terminal)
            except sqlite3.Error:
                logger.warning("terminal_reference lookup failed for terminal %s", id_terminal, exc_info=True)
                continue
            if system:
                systems[id_terminal] = system
        return systems

    @app_commands.command(
        name="where-to-buy-ship",
        description="Find where to buy or rent a ship in-game, with aUEC prices, cheapest first.",
    )
    @app_commands.describe(ship="Ship name, e.g. 'Cutlass Black' - autocompletes ships sold or rented in-game.")
    @app_commands.autocomplete(ship=listed_ship_autocomplete)
    async def where_to_buy_ship(self, interaction: discord.Interaction, ship: str) -> None:
        await interaction.response.defer()

        async def send(**kwargs) -> None:
            await interaction.followup.send(allowed_mentions=NO_MENTIONS, **kwargs)

        try:
            vehicles = await self.bot.uex.get_vehicles()
        except UexApiError as exc:
            await send(content=describe_uex_api_error(exc))
            return
        vehicle = resolve_ship(vehicles, ship)
        try:
            id_vehicle = int(vehicle["id"]) if vehicle is not None else None
        except (KeyError, TypeError, ValueError):
            id_vehicle = None
        if id_vehicle is None:
            await send(content=(
                f"Couldn't find a single ship matching '{ship}' - pick one from the autocomplete list."
            ))
            return
        ship_name = vehicle.get("name") or ship

        purchase_rows, rental_rows = await asyncio.gather(
            self.bot.uex.get_vehicle_purchase_prices(id_vehicle),
            self.bot.uex.get_vehicle_rental_prices(id_vehicle),
            return_exceptions=True,
        )
        _raise_unexpected(purchase_rows, rental_rows)
        purchases_failed = isinstance(purchase_rows, UexApiError)
        rentals_failed = isinstance(rental_rows, UexApiError)
        if purchases_failed and rentals_failed:
            await send(content=describe_uex_api_error(purchase_rows))
            return

        purchase_rows = [] if purchases_failed else purchase_rows
        rental_rows = [] if rentals_failed else rental_rows
        system_by_terminal = await self._star_systems_for(
            terminal_ids_missing_star_system(purchase_rows, rental_rows)
        )
        purchases = rank_purchase_listings(purchase_rows, system_by_terminal)
        rentals = rank_rental_listings(rental_rows, system_by_terminal)
        if not purchases and not rentals and not purchases_failed and not rentals_failed:
            await send(content=f"UEX has no in-game purchase or rental location on record for **{ship_name}**.")
            return

        title = f"{ship_name} — Where to Buy"
        description = ship_shop_description(has_rentals=bool(rentals))
        sections = build_ship_shop_sections(
            purchases, rentals, purchases_failed=purchases_failed, rentals_failed=rentals_failed,
        )

        embed = discord.Embed(title=title, description=description, color=discord.Color.blurple())
        # Footer set BEFORE add_chunked_fields, so its len(embed) budget check counts it.
        embed.set_footer(text=FOOTER_TEXT)
        # All-or-nothing across every section (see /ingame-item-finder): never send an
        # embed with some sections shown and others silently missing.
        if all(add_chunked_fields(embed, name=name, lines=lines) for name, lines in sections):
            await send(embed=embed)
            return

        fallback_lines = [f"**{title}**", *description.splitlines(), ""]
        for name, lines in sections:
            fallback_lines.append(f"**{name}**")
            fallback_lines.extend(lines)
            fallback_lines.append("")
        fallback_lines.append(FOOTER_TEXT)
        for chunk in chunk_lines(fallback_lines, max_length=1900):
            await send(content=chunk)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(ShipShops(bot))
