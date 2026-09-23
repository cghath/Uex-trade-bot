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
    location_label: str
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


def location_breadcrumb(row: dict[str, Any]) -> str:
    """Planet/moon/orbit -> city/outpost/space station -> terminal, skipping whichever
    levels a given /items_prices row doesn't carry. Deliberately excludes star system -
    results are grouped by system as their own section (see the cog), not repeated on
    every line, which got noisy fast on real data (a widely-stocked item can list a dozen+
    shops in the same system)."""
    mid = row.get("planet_name") or row.get("moon_name") or row.get("orbit_name")
    local = row.get("city_name") or row.get("outpost_name") or row.get("space_station_name")
    breadcrumb = " → ".join(str(part) for part in (mid, local) if part)
    terminal_name = str(row.get("terminal_name") or "Unknown terminal")
    return f"{breadcrumb} → {terminal_name}" if breadcrumb else terminal_name


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
        result.append(ItemListing(
            id_terminal=id_terminal,
            terminal_name=str(row.get("terminal_name") or "Unknown"),
            location_label=location_breadcrumb(row),
            star_system_name=row.get("star_system_name"),
            price_buy=price,
            distance_gm=distances.get(id_terminal),
        ))

    def sort_key(listing: ItemListing) -> tuple[int, bool, float]:
        same_system = origin_star_system is not None and listing.star_system_name == origin_star_system
        return (0 if same_system else 1, listing.distance_gm is None, listing.distance_gm or 0.0)

    result.sort(key=sort_key)
    return result


def format_item_listing_line(listing: ItemListing, *, origin_star_system: str | None = None) -> str:
    if listing.distance_gm is None:
        if origin_star_system is not None and listing.star_system_name == origin_star_system:
            distance = "same system, exact distance unknown"
        else:
            distance = "distance unknown"
    elif listing.distance_gm == 0:
        distance = "you're already here"
    else:
        distance = f"{listing.distance_gm:.1f} Gm away"
    return f"**{listing.location_label}** — {listing.price_buy:,.0f} aUEC · {distance}"
