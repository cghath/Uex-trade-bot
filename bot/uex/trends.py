"""Pure aggregation helpers for trend/volume features.

Kept dependency-free (no Discord, no I/O) so it's easy to unit test against
synthetic data - the actual API calls live in the cogs that use these.
"""
from __future__ import annotations

from dataclasses import dataclass

from bot.uex.supply_demand import SELL_SIDE_NO_DEMAND_CODE, has_sell_side_demand


@dataclass
class TrendingEntry:
    commodity_name: str
    total_trips_15d: int
    avg_volatility: float | None
    best_sell_price: float
    best_buy_price: float | None


@dataclass
class MoverEntry:
    commodity_name: str
    current_avg_sell: float
    baseline_avg_sell: float
    pct_change: float


@dataclass
class ScoredRouteEntry:
    commodity_name: str
    id_commodity: int
    origin_terminal_name: str
    destination_terminal_name: str
    price_origin: float
    price_destination: float
    price_margin: float | None
    price_roi: float | None
    distance: float | None
    score: float
    scu_origin: float | None
    scu_destination: float | None
    status_origin: int | None
    status_destination: int | None
    volatility_origin: float | None = None
    volatility_destination: float | None = None
    origin_terminal_id: int | None = None
    destination_terminal_id: int | None = None


def _positive_id(value: object) -> int | None:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def aggregate_commodity_trips(rows: list[dict]) -> tuple[int, float | None]:
    """Given /commodities_prices rows for ONE commodity across many terminals, return
    (total real player-submitted trade trips in the last 15 days, average price volatility).

    scu_buy_users_rows / scu_sell_users_rows are UEX's own count of user-submitted trade
    trips per terminal in the last 15 days - the closest thing to real trade volume the
    API exposes. Summing across terminals gives a system-wide activity count.
    """
    total_trips = 0
    volatility_samples: list[float] = []

    for row in rows:
        total_trips += int(row.get("scu_buy_users_rows") or 0)
        total_trips += int(row.get("scu_sell_users_rows") or 0)

        for key in ("volatility_price_buy", "volatility_price_sell"):
            value = row.get(key)
            if value is not None:
                volatility_samples.append(float(value))

    avg_volatility = round(sum(volatility_samples) / len(volatility_samples), 3) if volatility_samples else None
    return total_trips, avg_volatility


def rank_trending(entries: list[TrendingEntry], limit: int = 10) -> list[TrendingEntry]:
    """Highest real trade-trip count first; ties broken by lower volatility (more reliable)."""
    return sorted(
        entries,
        key=lambda e: (-e.total_trips_15d, e.avg_volatility if e.avg_volatility is not None else float("inf")),
    )[:limit]


def compute_movers(rows: list[dict], limit: int = 5) -> tuple[list[MoverEntry], list[MoverEntry]]:
    """From bulk /commodities_prices_all rows (all commodities, all terminals), rank
    commodities by how far their current sell price has drifted from their own recent
    average sell price. Returns (top gainers, top losers), each sorted by magnitude.

    Rows are grouped by commodity first (averaging price_sell and price_sell_avg across
    terminals) so one noisy terminal doesn't dominate - this reflects a market-wide trend,
    not a single-location blip.
    """
    by_commodity: dict[str, list[dict]] = {}
    for row in rows:
        name = row.get("commodity_name")
        if not name:
            continue
        by_commodity.setdefault(name, []).append(row)

    movers: list[MoverEntry] = []
    for name, commodity_rows in by_commodity.items():
        current_values = [r["price_sell"] for r in commodity_rows if (r.get("price_sell") or 0) > 0]
        baseline_values = [r["price_sell_avg"] for r in commodity_rows if (r.get("price_sell_avg") or 0) > 0]
        if not current_values or not baseline_values:
            continue

        current_avg = sum(current_values) / len(current_values)
        baseline_avg = sum(baseline_values) / len(baseline_values)
        if baseline_avg <= 0:
            continue

        pct_change = round(((current_avg - baseline_avg) / baseline_avg) * 100, 2)
        # Ignore noise-level moves so the list stays meaningful.
        if abs(pct_change) < 0.5:
            continue

        movers.append(
            MoverEntry(
                commodity_name=name,
                current_avg_sell=round(current_avg, 2),
                baseline_avg_sell=round(baseline_avg, 2),
                pct_change=pct_change,
            )
        )

    gainers = sorted((m for m in movers if m.pct_change > 0), key=lambda m: m.pct_change, reverse=True)[:limit]
    losers = sorted((m for m in movers if m.pct_change < 0), key=lambda m: m.pct_change)[:limit]
    return gainers, losers


def select_best_available_route(
    commodity_name: str, id_commodity: int, route_rows: list[dict]
) -> ScoredRouteEntry | None:
    """From one commodity's /commodities_routes rows, pick the single highest-`score` route
    whose origin terminal has real buy-side stock right now: price_origin > 0 (the terminal
    actually sells it at all) AND scu_origin > 0 (real stock, not just a price with nothing to
    sell) - the same "available to buy" bar bot/uex/stock_alerts.py uses. A route with no
    `score` at all can't be ranked by this feature's whole premise (UEX's own route-quality
    metric), so it's excluded rather than sorted arbitrarily. Returns None if nothing on this
    commodity qualifies.
    """
    candidates = [
        r
        for r in route_rows
        if (r.get("price_origin") or 0) > 0 and (r.get("scu_origin") or 0) > 0 and r.get("score") is not None
    ]
    if not candidates:
        return None

    best = max(candidates, key=lambda r: r["score"])
    return ScoredRouteEntry(
        commodity_name=commodity_name,
        id_commodity=id_commodity,
        origin_terminal_name=best.get("origin_terminal_name", "Unknown"),
        destination_terminal_name=best.get("destination_terminal_name", "Unknown"),
        price_origin=best.get("price_origin") or 0,
        price_destination=best.get("price_destination") or 0,
        price_margin=best.get("price_margin"),
        price_roi=best.get("price_roi"),
        distance=best.get("distance"),
        score=best["score"],
        scu_origin=best.get("scu_origin"),
        scu_destination=best.get("scu_destination"),
        status_origin=best.get("status_origin"),
        status_destination=best.get("status_destination"),
        volatility_origin=best.get("volatility_origin"),
        volatility_destination=best.get("volatility_destination"),
        origin_terminal_id=_positive_id(best.get("id_terminal_origin")),
        destination_terminal_id=_positive_id(best.get("id_terminal_destination")),
    )


def rank_top_scored_routes(entries: list[ScoredRouteEntry], limit: int = 10) -> list[ScoredRouteEntry]:
    """Entries are already one-per-commodity (see select_best_available_route, called once per
    commodity in the background refresh) - this just ranks across every commodity by UEX's own
    score, highest first, and caps the list."""
    return sorted(entries, key=lambda e: e.score, reverse=True)[:limit]


# UEX's own /commodities_status defines sell-side code 7 (86-100% inventory band) as
# "Maximum Inventory (No Demand)" - confirmed by directly querying the live endpoint
# (see scripts/dump_status_codes.py) and cross-checked against real /commodities_prices rows: every
# terminal actively paying for a commodity was sitting at the LOW end of this scale (code 1,
# "Out of Stock (Empty)" - i.e. the terminal's own stock is depleted, so it wants to buy),
# while terminals not buying at all showed price 0. The sell side runs in the opposite
# direction from the buy side: low inventory = high demand (good to sell into), full
# inventory = UEX's own explicit "no demand" (bad). This is the one sell-side code UEX itself
# flags as unambiguously bad. The shared demand check also fails closed for code 0/None
# ("not applicable" or unknown) and requires a positive destination SCU value.
def select_best_in_stock_route(
    commodity_name: str, id_commodity: int, route_rows: list[dict]
) -> ScoredRouteEntry | None:
    """Like select_best_available_route, but stricter: also requires the DESTINATION side to
    have real, currently-live sell-side demand, not just the origin having real buy-side stock.
    The default /top-routes view only checks the buy side, which means a route can rank highly and
    still be practically dead - great buy-side stock but the destination has UEX's own
    explicit "no demand" status. The shared demand check requires positive destination SCU
    and a known, applicable status other than SELL_SIDE_NO_DEMAND_CODE. SCU alone doesn't
    catch this since it is a much larger, closer-to-static figure that doesn't reflect live
    status the way the categorical code does. Same
    one-highest-score-per-commodity selection as select_best_available_route otherwise; returns
    None if nothing on this commodity qualifies.
    """
    candidates = [
        r
        for r in route_rows
        if (r.get("price_origin") or 0) > 0
        and (r.get("scu_origin") or 0) > 0
        and (r.get("price_destination") or 0) > 0
        and has_sell_side_demand(r.get("scu_destination"), r.get("status_destination"))
        and r.get("score") is not None
    ]
    if not candidates:
        return None

    best = max(candidates, key=lambda r: r["score"])
    return ScoredRouteEntry(
        commodity_name=commodity_name,
        id_commodity=id_commodity,
        origin_terminal_name=best.get("origin_terminal_name", "Unknown"),
        destination_terminal_name=best.get("destination_terminal_name", "Unknown"),
        price_origin=best.get("price_origin") or 0,
        price_destination=best.get("price_destination") or 0,
        price_margin=best.get("price_margin"),
        price_roi=best.get("price_roi"),
        distance=best.get("distance"),
        score=best["score"],
        scu_origin=best.get("scu_origin"),
        scu_destination=best.get("scu_destination"),
        status_origin=best.get("status_origin"),
        status_destination=best.get("status_destination"),
        volatility_origin=best.get("volatility_origin"),
        volatility_destination=best.get("volatility_destination"),
        origin_terminal_id=_positive_id(best.get("id_terminal_origin")),
        destination_terminal_id=_positive_id(best.get("id_terminal_destination")),
    )
