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
    price_buy: float
    # None when /terminals_distances couldn't price this pair (e.g. across star systems,
    # or a transient lookup failure) - never fabricated, and never allowed to sort ahead
    # of a genuinely closer, successfully-measured option.
    distance_gm: float | None


def _positive_float(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


def location_breadcrumb(row: dict[str, Any]) -> str:
    """Star system -> planet/moon/orbit -> city/outpost/space station -> terminal,
    skipping whichever levels a given /items_prices row doesn't carry."""
    system = row.get("star_system_name")
    mid = row.get("planet_name") or row.get("moon_name") or row.get("orbit_name")
    local = row.get("city_name") or row.get("outpost_name") or row.get("space_station_name")
    breadcrumb = " → ".join(str(part) for part in (system, mid, local) if part)
    terminal_name = str(row.get("terminal_name") or "Unknown terminal")
    return f"{breadcrumb} → {terminal_name}" if breadcrumb else terminal_name


def rank_item_listings(
    listings: list[dict[str, Any]], distances: dict[int, float | None],
) -> list[ItemListing]:
    """Every terminal /items_prices reports a real buy price for, closest to the player's
    given location first. `distances` is id_terminal -> gigameters (or None when unknown) -
    a caller's job to supply, since pricing each pair needs a live per-pair UEX call this
    module deliberately stays free of. Unknown-distance listings sort LAST, never first, so
    a genuinely close option is never buried behind one the bot simply couldn't measure."""
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
            price_buy=price,
            distance_gm=distances.get(id_terminal),
        ))
    result.sort(key=lambda listing: (listing.distance_gm is None, listing.distance_gm or 0.0))
    return result


def format_item_listing_line(listing: ItemListing) -> str:
    if listing.distance_gm is None:
        distance = "distance unknown"
    elif listing.distance_gm == 0:
        distance = "you're already here"
    else:
        distance = f"{listing.distance_gm:.1f} Gm away"
    return f"**{listing.location_label}** — {listing.price_buy:,.0f} aUEC · {distance}"
