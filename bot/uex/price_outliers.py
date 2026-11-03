"""Cross-terminal price-outlier detection within one market snapshot.

Born from a real incident: UEX's own data showed Rayari Kaltag Research Outpost buying Fresh
Food at 2,614 aUEC/SCU while a neighboring terminal (Rayari Anvik) showed 21,614 for the
same commodity in the same snapshot - roughly an 8x gap, later confirmed in-game to be a
genuine UEX data-entry error (the real price was 21,614). Nothing in the route-confidence
scoring (bot/uex/route_confidence.py) would have caught it: that scoring rewards freshness,
report-count corroboration and availability, none of which flags a single wrong number
that's otherwise "fresh" and "available."

A corroboration-count gate like the scanner's MIN_LISTINGS_FOR_FAIR_PRICE was considered and
dropped for /mixed-routes and /multi-stop-route specifically: they read terminal_market_state,
fed by the bulk /commodities_prices_all endpoint, which carries none of the
price_buy_users_rows/price_sell_users_rows report counts the single-commodity
/commodities_prices endpoint does - every row in the live snapshot has them NULL, so such a
gate would fire on every route. This is the check that's available instead: one terminal's
price for a commodity against every OTHER terminal's price for that same commodity, in the
same snapshot. It needs no outside data and targets exactly the failure that happened.

First built on production's feature/price-outlier-detection branch (c63ca73, 2026-09-22),
which was never merged; ported to aiv2 (a6bd024) the next day and back from there, onto
today's code, as PROJECT_CONTEXT.md entry 121.
"""
from __future__ import annotations

from dataclasses import dataclass
from statistics import median
from typing import Any

# At least this many OTHER terminals with a real price for the same commodity before a
# comparison is trusted - the same "don't trust a lone data point" reasoning as the
# scanner's MIN_LISTINGS_FOR_FAIR_PRICE and route_confidence.py's MIN_REPORTS_FOR_TRACK_RECORD.
MIN_SIBLINGS_FOR_COMPARISON = 3

# A price more than this many times its siblings' median (or less than 1/this) is flagged.
# Well above ordinary terminal-to-terminal variance for one commodity (typically under 2x,
# even across star systems) and well below the ~8x gap of the real Fresh Food error. A
# judgment call, not a statistical fit; revisit if it proves too noisy or too lax.
OUTLIER_RATIO_THRESHOLD = 4.0

# (id_commodity, "buy"|"sell") -> [(id_terminal, price), ...] for every terminal with a real
# positive price for that commodity and side in one snapshot.
PriceOutlierIndex = dict[tuple[int, str], list[tuple[int, float]]]


@dataclass(frozen=True)
class PriceOutlier:
    price: float
    sibling_median: float
    sibling_count: int
    ratio: float  # Always >= OUTLIER_RATIO_THRESHOLD: price/median if high, median/price if low.


def _positive_float(value: Any) -> float | None:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def _integer(value: Any) -> int | None:
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def index_commodity_prices(market_rows: list[dict[str, Any]]) -> PriceOutlierIndex:
    """Every terminal's positive buy/sell price per commodity, grouped once per snapshot so
    the per-item check doesn't rescan the snapshot for every cargo item. Build it once per
    command from rows it already fetched, never per item."""
    index: PriceOutlierIndex = {}
    for row in market_rows:
        commodity_id = _integer(row.get("id_commodity"))
        terminal_id = _integer(row.get("id_terminal"))
        if commodity_id is None or terminal_id is None:
            continue
        for side, field in (("buy", "price_buy"), ("sell", "price_sell")):
            price = _positive_float(row.get(field))
            if price is not None:
                index.setdefault((commodity_id, side), []).append((terminal_id, price))
    return index


def find_price_outlier(
    index: PriceOutlierIndex,
    *,
    id_commodity: int | None,
    id_terminal: int,
    side: str,
    price: float | None,
) -> PriceOutlier | None:
    """None unless `price` is a confirmed outlier against every OTHER terminal's price for
    the same commodity and side. The terminal being checked never counts toward its own
    comparison, and too few siblings means "can't tell", not "not an outlier"."""
    if id_commodity is None or price is None or price <= 0:
        return None
    siblings = [p for terminal_id, p in index.get((id_commodity, side), []) if terminal_id != id_terminal]
    if len(siblings) < MIN_SIBLINGS_FOR_COMPARISON:
        return None
    sibling_median = median(siblings)
    if sibling_median <= 0:
        return None
    ratio = price / sibling_median
    effective_ratio = ratio if ratio >= 1 else 1 / ratio
    if effective_ratio < OUTLIER_RATIO_THRESHOLD:
        return None
    return PriceOutlier(price=price, sibling_median=sibling_median, sibling_count=len(siblings),
                        ratio=effective_ratio)


def format_price_outlier_warning(outlier: PriceOutlier, *, label: str) -> str:
    """Says the price disagrees with the others, never which number is right: this check
    alone can't tell a real deal from a data error."""
    direction = "below" if outlier.price < outlier.sibling_median else "above"
    return (
        f"{label} price {outlier.price:,.0f} is {outlier.ratio:.1f}x {direction} the median "
        f"of {outlier.sibling_count} other terminals ({outlier.sibling_median:,.0f}) for this "
        f"commodity - could be a real deal or a data error, verify before committing"
    )
