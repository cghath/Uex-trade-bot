"""Pure helpers for marketplace search/formatting - kept dependency-free for easy testing."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any


def find_item_id_by_name(items: list[dict], query: str) -> int | None:
    """Resolve a typed query to a single catalog item's id, if there's an unambiguous match.

    Prefers an exact (case-insensitive) name match; falls back to a substring match only
    if it's unique, so a vague query doesn't silently pick the wrong item.
    """
    query_lower = query.strip().lower()
    if not query_lower:
        return None

    for item in items:
        if (item.get("name") or "").strip().lower() == query_lower:
            return item.get("id")

    substring_matches = [item for item in items if query_lower in (item.get("name") or "").lower()]
    if len(substring_matches) == 1:
        return substring_matches[0].get("id")
    return None


def filter_listings_by_keyword(listings: list[dict], keyword: str) -> list[dict]:
    """Client-side fallback search when a query doesn't resolve to one catalog item -
    matches free-text listings (services/contracts, custom titles) that /marketplace_listings
    itself can't filter by keyword server-side."""
    keyword_lower = keyword.strip().lower()
    if not keyword_lower:
        return listings
    return [
        listing
        for listing in listings
        if keyword_lower in (listing.get("title") or "").lower()
        or keyword_lower in (listing.get("description") or "").lower()
    ]


def exclude_sold_out(listings: list[dict]) -> list[dict]:
    return [listing for listing in listings if not listing.get("is_sold_out")]


def parse_uex_number(raw: Any) -> float | None:
    """UEX's Marketplace endpoints (listings, trends, negotiations) inconsistently send
    numeric-looking fields as JSON strings rather than numbers - e.g. a listing's `price` has
    been observed as the string "15000000", not the int 15000000, and /marketplace_trends'
    price_avg_sell the same way (unlike the commodities/prices endpoints, which do send real
    numbers). Formatting a raw string value with `:,.0f` raises ValueError, and comparing one
    to a float with `>`/`<` raises TypeError, so every place this bot does either to a
    Marketplace numeric field should go through this first. Returns None for null/missing/
    unparsable input instead of raising.
    """
    if raw is None:
        return None
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


def parse_listing_quality(raw: Any) -> float | None:
    """UEX documents a listing's `quality` as a 0-100 *string|null*, set by the seller when
    posting (this bot can't set it yet - UEX's POST /marketplace_advertise doesn't expose it
    as a field, even though it's readable on listings). Returns None for null/unset/unparsable
    values, and also for 0 - a seller who never set a quality and one who explicitly reported
    the ore as worthless read the same in this API, and treating 0 as "no data" avoids either
    silently excluding real low-quality listings from an unfiltered search or silently matching
    them against a quality filter that was never meant to include "no data" listings.
    """
    value = parse_uex_number(raw)
    return value if value is not None and value > 0 else None


@dataclass
class MarketplaceMoverEntry:
    item_name: str
    current_avg_sell: float
    baseline_avg_sell: float
    pct_change: float
    listings_count_sell: int | None


def compute_marketplace_movers(rows: list[dict], limit: int = 5) -> tuple[list[MarketplaceMoverEntry], list[MarketplaceMoverEntry]]:
    """From /marketplace_trends rows (already one row per item, unlike /commodities_prices_all
    which needs grouping across terminals), rank items by how far their current average sell
    price has drifted from their own trailing-month average sell price
    (price_avg_sell vs. price_avg_month_sell). Both fields come back as JSON strings from UEX,
    not numbers, so they're coerced with parse_uex_number first - the same string-typing quirk
    that broke /marketplace-trending's formatting. Returns (top gainers, top losers), each
    sorted by magnitude, dropping noise-level moves under 0.5%.
    """
    movers: list[MarketplaceMoverEntry] = []
    for row in rows:
        name = row.get("item_name")
        if not name:
            continue
        current = parse_uex_number(row.get("price_avg_sell"))
        baseline = parse_uex_number(row.get("price_avg_month_sell"))
        if current is None or baseline is None or baseline <= 0:
            continue

        pct_change = round(((current - baseline) / baseline) * 100, 2)
        if abs(pct_change) < 0.5:
            continue

        movers.append(
            MarketplaceMoverEntry(
                item_name=name,
                current_avg_sell=round(current, 2),
                baseline_avg_sell=round(baseline, 2),
                pct_change=pct_change,
                listings_count_sell=row.get("listings_count_sell"),
            )
        )

    gainers = sorted((m for m in movers if m.pct_change > 0), key=lambda m: m.pct_change, reverse=True)[:limit]
    losers = sorted((m for m in movers if m.pct_change < 0), key=lambda m: m.pct_change)[:limit]
    return gainers, losers


# Display order for average-price tables: sell listings (what sellers ask) before buy
# listings (what buyers offer); anything unexpected sorts last rather than crashing.
_OPERATION_SORT_ORDER = {"sell": 0, "buy": 1}


@dataclass
class MarketplaceAverageEntry:
    item_name: str
    quality_tier: int | None
    operation: str
    currency: str
    unit: str
    listings_count: int
    price_avg: float | None
    price_avg_week: float | None
    price_avg_month: float | None


def parse_marketplace_average_rows(rows: list[dict]) -> list[MarketplaceAverageEntry]:
    """Coerce raw /marketplace_prices_averages rows (one per item x quality tier x operation
    x currency x unit combo) into typed entries, ready to display. All numeric fields go
    through parse_uex_number since Marketplace endpoints send numbers as JSON strings (see
    parse_uex_number's docstring); quality_tier 0 is a real tier (Q0), so unlike
    parse_listing_quality it is preserved, not treated as unset. A row where none of the
    three averages parse has nothing to show and is dropped. Output is sorted for display:
    sell side before buy side, then quality tier ascending (rows without a tier last), then
    currency, then unit - so the cheapest-to-describe grouping (operation) is contiguous.
    """
    entries: list[MarketplaceAverageEntry] = []
    for row in rows:
        price_avg = parse_uex_number(row.get("price_avg"))
        price_avg_week = parse_uex_number(row.get("price_avg_week"))
        price_avg_month = parse_uex_number(row.get("price_avg_month"))
        if price_avg is None and price_avg_week is None and price_avg_month is None:
            continue
        tier = parse_uex_number(row.get("quality_tier"))
        listings_count = parse_uex_number(row.get("listings_count"))
        entries.append(
            MarketplaceAverageEntry(
                item_name=row.get("item_name") or "",
                quality_tier=int(tier) if tier is not None else None,
                operation=(row.get("operation") or "").strip().lower(),
                currency=row.get("currency") or "UEC",
                unit=row.get("unit") or "unit",
                listings_count=int(listings_count) if listings_count is not None else 0,
                price_avg=price_avg,
                price_avg_week=price_avg_week,
                price_avg_month=price_avg_month,
            )
        )
    entries.sort(
        key=lambda e: (
            _OPERATION_SORT_ORDER.get(e.operation, len(_OPERATION_SORT_ORDER)),
            e.quality_tier if e.quality_tier is not None else float("inf"),
            e.currency,
            e.unit,
        )
    )
    return entries


def extract_item_activity(trend_rows: list[dict]) -> list[dict]:
    """Distill /marketplace_trends rows down to just what the accumulating traded-items index
    (bot/db/database.py: marketplace_item_activity) needs to store. A row missing id_item or
    item_name can't be tracked and is skipped. negotiations_count/total_listings_count come
    back as real numbers (unlike the price_* fields on this same endpoint - see
    parse_uex_number), but `or 0` still guards against a null/missing value.
    """
    result = []
    for row in trend_rows:
        id_item = row.get("id_item")
        item_name = row.get("item_name")
        if id_item is None or not item_name:
            continue
        result.append(
            {
                "id_item": id_item,
                "item_name": item_name,
                "negotiations_count": row.get("negotiations_count") or 0,
                "listings_count": row.get("total_listings_count") or 0,
            }
        )
    return result


def rank_traded_items(activity_rows: list[dict], limit: int = 1000) -> list[dict]:
    """Rank the accumulating traded-items index by a combined activity score - negotiations_count
    (historical deal activity) plus listings_count (current supply/visibility), added together
    rather than weighted or normalized. This is only ever used for ordering, never displayed as
    a literal number, so a straight sum is the simplest thing that does the right thing: an item
    that's both actively negotiated AND currently well-stocked ranks above one that's only
    strong on a single axis.
    """
    ranked = sorted(
        activity_rows,
        key=lambda r: (r.get("negotiations_count") or 0) + (r.get("listings_count") or 0),
        reverse=True,
    )
    return ranked[:limit]


def match_traded_items(ranked_items: list[dict], query: str, limit: int = 25) -> list[dict]:
    """Substring-match a query against an already-ranked traded-items list, preserving rank
    order - so typing a partial name surfaces the most heavily-traded matching item first."""
    query_lower = query.strip().lower()
    if not query_lower:
        return ranked_items[:limit]
    matches = [item for item in ranked_items if query_lower in item["item_name"].lower()]
    return matches[:limit]


def reshape_marketplace_history_rows(rows: list[dict]) -> list[dict]:
    """Convert /marketplace_prices_history rows into the {date_added, price_buy, price_sell}
    shape bot/uex/charts.py's render_price_history_chart() expects.

    Unlike /commodities_prices_history (which already has separate price_buy/price_sell
    columns per row), each Marketplace history row records a single price change for a
    single operation ('buy' or 'sell'), in a `price` field - so this splits that single
    price into whichever of price_buy/price_sell matches the row's operation, leaving the
    other None (the chart renderer already tolerates gaps, since not every terminal has
    both sides on /commodities_prices_history either). Rows missing date_added or a
    parsable price are dropped. Sorted ascending by date_added, since UEX doesn't
    guarantee ordering on this endpoint.
    """
    reshaped = []
    for row in rows:
        date_added = row.get("date_added")
        price = parse_uex_number(row.get("price"))
        if not date_added or price is None:
            continue
        operation = (row.get("operation") or "").strip().lower()
        reshaped.append(
            {
                "date_added": date_added,
                "price_buy": price if operation == "buy" else None,
                "price_sell": price if operation == "sell" else None,
            }
        )
    reshaped.sort(key=lambda r: r["date_added"])
    return reshaped


def filter_listings_by_quality(
    listings: list[dict], min_quality: float | None, max_quality: float | None
) -> list[dict]:
    """Keep only listings whose quality falls within [min_quality, max_quality] (inclusive,
    either bound optional/independent). A listing with no usable quality value is dropped
    whenever a quality filter is active - it's neither a match nor a mismatch, so surfacing it
    under a quality-scoped search would be misleading. With no bounds given, returns listings
    unchanged (quality filtering is opt-in).
    """
    if min_quality is None and max_quality is None:
        return listings
    kept = []
    for listing in listings:
        quality = parse_listing_quality(listing.get("quality"))
        if quality is None:
            continue
        if min_quality is not None and quality < min_quality:
            continue
        if max_quality is not None and quality > max_quality:
            continue
        kept.append(listing)
    return kept
