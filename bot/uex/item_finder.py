"""Pure helpers for the In-game Item Finder (/ingame-item-finder): for one catalogued
item (weapons, armor, ammo, and more - the same /items catalog Marketplace already
searches), which shops actually sell it right now, closest to a given location first.
Dependency-free like the rest of bot/uex/, so it's easy to unit test against synthetic
/items_prices rows.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

# Monospace table columns (see build_item_listing_table) - Discord has no real <table>, so
# a ```code block``` with padded columns is the only way to get aligned rows, the same
# pattern /command-usage's aggregate report already uses. Place/vendor width is DYNAMIC,
# sized to the longest real name in the results being shown (see build_item_listing_table) -
# a fixed narrow width previously truncated two DIFFERENT real places down to identical
# displayed text (confirmed live: "People's Service Station Alpha" and "...Lambda" both
# became "People's Servi…"), silently making them indistinguishable. Discord has no
# hover/tooltip mechanism for message content (no JS runs in a message/embed), so the only
# way to make a name recoverable is to not cut it off in the first place - these MAX
# constants are a generous cap (based on real observed UEX place/vendor name lengths) that
# only kicks in for a genuine outlier, which then relies on Discord's own horizontal-scroll
# behavior for an overflowing code-block line instead of a fabricated "see more" control.
PLACE_LABEL = "Place"
VENDOR_LABEL = "Vendor"
PRICE_LABEL = "Price"
DISTANCE_LABEL = "Distance"
PLACE_COL_MAX_WIDTH = 34
VENDOR_COL_MAX_WIDTH = 22
PRICE_COL_WIDTH = 9
DISTANCE_COL_WIDTH = 12
# Target ceiling for one fenced code-block's char count (header + fences + rows) - kept
# comfortably under Discord's 1024-char field-chunk limit so add_chunked_fields' own
# chunk_lines packing never has to hard-split a block mid-row, which would break its
# fences. A system with more rows than fit in one block gets multiple self-contained
# fenced blocks instead (see build_item_listing_table), each independently well-formed.
TABLE_BLOCK_CHAR_BUDGET = 900


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


def _truncate(text: str, width: int) -> str:
    return text if len(text) <= width else text[: width - 1] + "…"


def _distance_label(listing: ItemListing, *, origin_star_system: str | None = None) -> str:
    if listing.distance_gm is None:
        if origin_star_system is not None and listing.star_system_name == origin_star_system:
            return "same system"
        return "unknown"
    if listing.distance_gm == 0:
        return "here"
    return f"{listing.distance_gm:.1f} Gm"


def format_item_listing_header(place_width: int, vendor_width: int) -> str:
    """Column header for format_item_listing_row's table - both take the same
    (place_width, vendor_width) pair so a header always lines up with its own rows."""
    return (
        f"{PLACE_LABEL:<{place_width}} {VENDOR_LABEL:<{vendor_width}} "
        f"{PRICE_LABEL:>{PRICE_COL_WIDTH}} {DISTANCE_LABEL:>{DISTANCE_COL_WIDTH}}"
    )


def format_item_listing_row(
    listing: ItemListing, place_width: int, vendor_width: int, *, origin_star_system: str | None = None,
) -> str:
    """One monospace row at the given column widths, meant to sit inside a ```code
    block``` alongside format_item_listing_header()'s header row - Discord embeds have no
    real <table>, and a code block is the only way to get real column alignment (the same
    pattern /command-usage's aggregate report already uses). Callers building a real table
    should get place_width/vendor_width from build_item_listing_table rather than picking
    their own - a width smaller than a given name still truncates it via _truncate."""
    place = _truncate(listing.place_label, place_width)
    vendor = _truncate(listing.vendor_label or "-", vendor_width)
    price = f"{listing.price_buy:,.0f}"
    distance = _distance_label(listing, origin_star_system=origin_star_system)
    return (
        f"{place:<{place_width}} {vendor:<{vendor_width}} "
        f"{price:>{PRICE_COL_WIDTH}} {distance:>{DISTANCE_COL_WIDTH}}"
    )


def _wrap_table_block(header: str, rows: list[str]) -> str:
    return "```\n" + "\n".join([header, *rows]) + "\n```"


def build_item_listing_table(
    listings: list[ItemListing], *, origin_star_system: str | None = None,
    block_char_budget: int = TABLE_BLOCK_CHAR_BUDGET,
) -> list[str]:
    """One or more self-contained ```fenced code blocks```, each a header + as many rows as
    fit under `block_char_budget` - splitting into multiple blocks (rather than handing a
    caller one giant string that a downstream character-count-based chunker might hard-split
    mid-row) whenever a result set's full row count wouldn't otherwise fit safely under
    Discord's ~1024-char field-chunk limit. Column widths are computed ONCE from every
    listing passed in (not a fixed constant) - see the module-level comment above
    PLACE_COL_MAX_WIDTH for why a fixed narrow width was a real bug, not just cosmetic."""
    if not listings:
        return []
    place_width = min(PLACE_COL_MAX_WIDTH, max(len(PLACE_LABEL), max(len(l.place_label) for l in listings)))
    vendor_width = min(
        VENDOR_COL_MAX_WIDTH, max(len(VENDOR_LABEL), max(len(l.vendor_label or "-") for l in listings))
    )
    header = format_item_listing_header(place_width, vendor_width)
    rows = [
        format_item_listing_row(listing, place_width, vendor_width, origin_star_system=origin_star_system)
        for listing in listings
    ]

    overhead = len("```\n") + len(header) + len("\n") + len("\n```")
    blocks: list[str] = []
    current_rows: list[str] = []
    current_len = overhead
    for row in rows:
        added = len(row) + 1
        if current_rows and current_len + added > block_char_budget:
            blocks.append(_wrap_table_block(header, current_rows))
            current_rows = []
            current_len = overhead
        current_rows.append(row)
        current_len += added
    if current_rows:
        blocks.append(_wrap_table_block(header, current_rows))
    return blocks
