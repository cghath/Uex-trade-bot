"""Cross-terminal price-outlier detection within one market snapshot.

Born from a real incident: UEX's own data showed Rayari Kaltag Research Outpost buying
Fresh Food at 2,614 aUEC/SCU while a neighboring terminal (Rayari Anvik) showed 21,614 for
the same commodity in the same snapshot - roughly an 8x gap, later confirmed in-game to be
a genuine UEX data-entry error (the real price was 21,614; Kaltag's own live price matched
it once checked directly). Nothing in the existing route-confidence scoring
(bot/uex/route_confidence.py) would have caught this - that scoring rewards freshness,
report-count corroboration, and availability, none of which flags a single wrong number
that's otherwise "fresh" and "available."

A corroboration-count gate modeled on the scanner's MIN_LISTINGS_FOR_FAIR_PRICE precedent
was considered and dropped for this specific pipeline: /mixed-routes and /multi-stop-route
read from terminal_market_state, fed by the bulk /commodities_prices_all endpoint, which
UEX documents WITHOUT the price_buy_users_rows/price_sell_users_rows report-count fields
the single-commodity /commodities_prices endpoint carries - confirmed every row in the live
snapshot has these NULL. A "near-zero reports" gate would fire on literally every route,
carrying no signal. This module is the check that's actually available instead: compare one
terminal's price for a commodity against every OTHER terminal's price for that SAME
commodity, in the SAME snapshot - no external threshold or corroboration data needed, and
it directly targets the failure mode that actually happened (one terminal's number badly
out of line with everyone else's for the identical item).
"""
from __future__ import annotations

from dataclasses import dataclass
from statistics import median
from typing import Any

# Require at least this many OTHER terminals with a real price for the same commodity
# before trusting a comparison - same "don't trust a lone data point" reasoning as
# bot/uex/scanner.py's MIN_LISTINGS_FOR_FAIR_PRICE and route_confidence.py's
# MIN_REPORTS_FOR_TRACK_RECORD.
MIN_SIBLINGS_FOR_COMPARISON = 3

# A price more than this many times its siblings' median (or less than 1/this) is flagged.
# Chosen well above ordinary terminal-to-terminal price variance for the same commodity
# (typically well under 2x even across star systems) but comfortably below the ~8x gap the
# real Fresh Food data error showed - a deliberate, documented judgment call, not derived
# from a full statistical study of the live data; revisit if it proves too noisy or too lax
# once it's been watched against real snapshots for a while.
OUTLIER_RATIO_THRESHOLD = 4.0

# (id_commodity, "buy"|"sell") -> [(id_terminal, price), ...] for every terminal with a
# real positive price for that commodity/side in one snapshot.
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
    """Group every terminal's positive buy/sell price for each commodity, once per
    snapshot, so a later per-item outlier check doesn't rescan the whole snapshot for
    every cargo item. Build this once per command call from the same market_rows already
    fetched for route building, not per item."""
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
    id_commodity: int,
    id_terminal: int,
    side: str,
    price: float,
) -> PriceOutlier | None:
    """None unless `price` is a confirmed outlier against every OTHER terminal's own price
    for this same commodity/side in the same snapshot - the terminal being checked is
    always excluded from its own comparison pool, and too few siblings means "can't tell,"
    not "not an outlier.\""""
    if price is None or price <= 0:
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
    return PriceOutlier(
        price=price, sibling_median=sibling_median, sibling_count=len(siblings), ratio=effective_ratio
    )


def format_price_outlier_warning(outlier: PriceOutlier, *, label: str) -> str:
    """'{label} price P is N.Nx above/below the median of M other terminals (X) for this
    commodity' - deliberately doesn't claim which number is right, since this check alone
    can't tell (see the module docstring): it flags disagreement, not a known-correct
    value."""
    direction = "below" if outlier.price < outlier.sibling_median else "above"
    return (
        f"{label} price {outlier.price:,.0f} is {outlier.ratio:.1f}x {direction} the median "
        f"of {outlier.sibling_count} other terminals ({outlier.sibling_median:,.0f}) for this "
        f"commodity - could be a real deal or a data error, verify before committing"
    )
