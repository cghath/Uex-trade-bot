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


def _distance_label(listing: ItemListing, *, origin_star_system: str | None = None) -> str:
    if listing.distance_gm is None:
        if origin_star_system is not None and listing.star_system_name == origin_star_system:
            return "same system"
        return "unknown"
    if listing.distance_gm == 0:
        return "here"
    return f"{listing.distance_gm:.1f} Gm"


def format_item_listing_line(listing: ItemListing, *, origin_star_system: str | None = None) -> str:
    """'**Place** (Vendor) — Price aUEC · Distance' - deliberately plain text, not a
    monospace table. A fixed-width table column looked clean for the SHORT names it was
    designed against, but two real problems surfaced live once real name-length variance
    showed up: (1) a narrow fixed width truncated two DIFFERENT real places (e.g.
    'People's Service Station Alpha' vs. '...Lambda') down to identical displayed text -
    Discord has no hover/tooltip to recover the rest, since no JS runs in a message/embed;
    (2) widening the column to fix that made rows wide enough that Discord WRAPS them
    inside an embed field instead of scrolling horizontally (confirmed live - the earlier
    assumption that a code block scrolls was wrong), breaking column alignment entirely.
    Plain proportional text sidesteps both: it never collapses two different names to the
    same string, and a long name just wraps gracefully like any other sentence instead of
    misaligning a column."""
    vendor_part = f" ({listing.vendor_label})" if listing.vendor_label else ""
    distance = _distance_label(listing, origin_star_system=origin_star_system)
    return f"**{listing.place_label}**{vendor_part} — {listing.price_buy:,.0f} aUEC · {distance}"
