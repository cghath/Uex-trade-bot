"""Pure helpers for the In-game Item Finder (/ingame-item-finder): for one catalogued
item (weapons, armor, ammo, and more - the same /items catalog Marketplace already
searches), which shops actually sell it right now, closest to a given location first.
Dependency-free like the rest of bot/uex/, so it's easy to unit test against synthetic
/items_prices rows.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

@dataclass(frozen=True)
class ItemListing:
    id_terminal: int
    terminal_name: str
    place_label: str
    vendor_label: str | None
    star_system_name: str | None
    price_buy: float
    # None when /terminals_distances couldn't price this pair. Confirmed on real data this
    # is a genuine UEX gap for SOME pairs (not just a network hiccup or a cross-system
    # pair) - e.g. Admin - Seraphim -> Skutters - GrimHEX, both in Crusader orbit and
    # effectively neighbors, returns a bare `false` - never fabricated, and never allowed
    # to sort ahead of a genuinely closer, successfully-measured option.
    distance_gm: float | None


def _positive_float(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


def split_place_and_vendor(row: dict[str, Any]) -> tuple[str, str | None]:
    """UEX terminal names consistently follow a 'Vendor - Place' convention (e.g.
    'Skutters - GrimHEX', 'Cubby Blast - Area 18') - splitting on the LAST ' - ' and
    taking the place (the part after it) gives a shorter, more commonly-recognized name
    than the separate city_name/outpost_name/space_station_name field in every case a
    real /items_prices pull disagreed (e.g. 'Checkmate' vs 'Checkmate Station', and
    'GrimHEX' vs the formal 'Green Imperial Housing Exchange' from space_station_name) -
    confirmed against live UEX data (18/22 identical, 4/22 differ, every difference an
    improvement), not assumed. A terminal name with no ' - ' separator (e.g. 'Equipment
    Contested Zone Checkmate') falls back to the structured location field instead of the
    whole raw name, with no separate vendor."""
    terminal_name = str(row.get("terminal_name") or "")
    if " - " in terminal_name:
        vendor, _, place = terminal_name.rpartition(" - ")
        return place.strip(), (vendor.strip() or None)
    fallback = row.get("city_name") or row.get("outpost_name") or row.get("space_station_name")
    return str(fallback or terminal_name or "Unknown"), None


def place_and_vendor_text(place: str, vendor: str | None, *, bold: bool = False) -> str:
    """How every shop command names a shop (audit MSG-19): 'Place (Vendor)', or 'Vendor at
    Place' when the place already ends in its own parentheses ('Ship Weapons at Pyro
    Gateway (Stanton)') rather than stacking two. `bold` bolds the place."""
    shown = f"**{place}**" if bold else place
    if not vendor:
        return shown
    if place.rstrip().endswith(")"):
        return f"{vendor} at {shown}"
    return f"{shown} ({vendor})"


def rank_item_listings(
    listings: list[dict[str, Any]], distances: dict[int, float | None], *,
    origin_star_system: str | None = None,
) -> list[ItemListing]:
    """Every terminal /items_prices reports a real buy price for, closest to the player's
    given location first. `distances` is id_terminal -> gigameters (or None when unknown) -
    a caller's job to supply, since pricing each pair needs a live per-pair UEX call this
    module deliberately stays free of.

    A listing in the player's OWN star system (origin_star_system) is preferred as a
    fallback tier whenever its distance is unknown - confirmed on real data that
    /terminals_distances has real gaps for SOME pairs even within the same system (see
    ItemListing.distance_gm's own docstring), and without this a genuinely nearby neighbor
    sorts dead last behind every successfully-measured but truly cross-system option, and
    can fall off the display entirely on a widely-stocked item. Within a tier, a known
    distance still sorts before an unknown one, and known distances sort by their real
    value - this only changes which TIER an unknown-distance listing competes in, never
    fabricates a number for it."""
    result = []
    for row in listings:
        price = _positive_float(row.get("price_buy"))
        raw_terminal_id = row.get("id_terminal")
        if price is None or raw_terminal_id is None:
            continue
        try:
            id_terminal = int(raw_terminal_id)
        except (TypeError, ValueError):
            continue
        place_label, vendor_label = split_place_and_vendor(row)
        result.append(ItemListing(
            id_terminal=id_terminal,
            terminal_name=str(row.get("terminal_name") or "Unknown"),
            place_label=place_label,
            vendor_label=vendor_label,
            star_system_name=row.get("star_system_name"),
            price_buy=price,
            distance_gm=distances.get(id_terminal),
        ))

    def sort_key(listing: ItemListing) -> tuple[int, bool, float]:
        same_system = origin_star_system is not None and listing.star_system_name == origin_star_system
        return (0 if same_system else 1, listing.distance_gm is None, listing.distance_gm or 0.0)

    result.sort(key=sort_key)
    return result


def distance_text(listing: ItemListing, *, origin_star_system: str | None = None) -> str:
    """'3 Gm', 'here', 'same system' (unknown, but in the player's own system) or 'distance unknown'."""
    if listing.distance_gm is None:
        if origin_star_system is not None and listing.star_system_name == origin_star_system:
            return "same system"
        return "distance unknown"
    if listing.distance_gm == 0:
        return "here"
    return f"{round(listing.distance_gm, 1):g} Gm"


def _price(listing: ItemListing) -> str:
    return f"{listing.price_buy:,.0f}"


# The reply is built from these blocks, one text block each in the layout (the owner's pick,
# 2026-10-03). Every shop line is plain proportional text, never a monospace table: a fixed
# column truncated two different real places ('People's Service Station Alpha' vs '...Lambda')
# to the same text, and once widened, Discord wrapped the rows and broke the columns anyway.


def item_header(item_name: str, shop_count: int, origin: str) -> str:
    shops = f"{shop_count} shop sells it" if shop_count == 1 else f"{shop_count} shops sell it"
    return f"## {item_name}\n-# {shops} · prices in aUEC · from {origin}"


def highlights_block(ranked: list[ItemListing], *, origin_star_system: str | None = None) -> str:
    """The answer first: the nearest shop, and the cheapest (the nearest of those at the lowest
    price), naming the star system when it isn't the player's own. One line when they're the same."""
    nearest = ranked[0]
    low = min(listing.price_buy for listing in ranked)
    cheapest = next(listing for listing in ranked if listing.price_buy == low)

    def line(label: str, listing: ItemListing) -> str:
        where = place_and_vendor_text(listing.place_label, listing.vendor_label)
        if listing.star_system_name and listing.star_system_name != origin_star_system:
            where += f", {listing.star_system_name}"
        return (f"**{label}** · {where} · {_price(listing)} · "
                f"{distance_text(listing, origin_star_system=origin_star_system)}")

    if cheapest is nearest:
        return line("Nearest and cheapest", nearest)
    return line("Nearest", nearest) + "\n" + line("Cheapest", cheapest)


def system_block(system: str, listings: list[ItemListing], *, origin_star_system: str | None = None) -> str:
    """One star system's shops, closest first. When every shop there charges the same, the
    price (and the vendor, if they share one) goes in the heading once and the shops share a line."""
    def distance(listing: ItemListing) -> str:
        return distance_text(listing, origin_star_system=origin_star_system)

    if len(listings) > 1 and len({listing.price_buy for listing in listings}) == 1:
        vendors = {listing.vendor_label for listing in listings}
        vendor = next(iter(vendors)) if len(vendors) == 1 else None
        heading = f"### {system} · all {_price(listings[0])}" + (f" ({vendor})" if vendor else "")
        shops = []
        for listing in listings:
            name = listing.place_label if vendor else place_and_vendor_text(listing.place_label, listing.vendor_label)
            gap = distance(listing)
            shops.append(f"{name} {gap}" if gap.endswith(" Gm") else f"{name} ({gap})")
        return heading + "\n" + " · ".join(shops)
    lines = [f"### {system}"]
    lines += [f"- {place_and_vendor_text(listing.place_label, listing.vendor_label, bold=True)} · "
              f"{_price(listing)} · {distance(listing)}" for listing in listings]
    return "\n".join(lines)


def item_footer(omitted: int) -> str:
    more = f" · {omitted} more shop{'' if omitted == 1 else 's'} not shown" if omitted else ""
    return f"-# UEX prices, up to 24h old{more}"
