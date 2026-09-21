"""Tests for the pure trend/volume/route-scoring helpers in bot/uex/trends.py."""
from __future__ import annotations

from bot.uex.trends import (
    SELL_SIDE_NO_DEMAND_CODE,
    ScoredRouteEntry,
    TrendingEntry,
    aggregate_commodity_trips,
    compute_movers,
    rank_by_achievable_profit,
    rank_top_scored_routes,
    rank_trending,
    select_available_routes,
    select_in_stock_routes,
)
from bot.uex.data_health import classify_terminal_health
from bot.uex.route_confidence import coalesce_report_count, compute_route_confidence, track_record_modifier


def _price_row(**overrides) -> dict:
    """A /commodities_prices_all row for one commodity at one terminal."""
    row = {
        "commodity_name": "Laranite",
        "price_sell": 0,
        "price_sell_avg": 0,
        "scu_buy_users_rows": 0,
        "scu_sell_users_rows": 0,
        "volatility_price_buy": None,
        "volatility_price_sell": None,
    }
    row.update(overrides)
    return row


def _route_row(**overrides) -> dict:
    """A /commodities_routes row."""
    row = {
        "origin_terminal_name": "Mine",
        "destination_terminal_name": "City",
        "id_terminal_origin": 10,
        "id_terminal_destination": 20,
        "price_origin": 100,
        "price_destination": 200,
        "price_margin": 100,
        "price_roi": 100.0,
        "distance": 42.0,
        "score": 50,
        "profit": 1000.0,
        "investment": 10000.0,
        "scu_origin": 500,
        "scu_destination": 800,
        "status_origin": 2,
        "status_destination": 1,
    }
    row.update(overrides)
    return row


def _trending(name: str, trips: int, volatility: float | None = None) -> TrendingEntry:
    return TrendingEntry(
        commodity_name=name,
        total_trips_15d=trips,
        avg_volatility=volatility,
        best_sell_price=100.0,
        best_buy_price=50.0,
    )


def _scored(name: str, profit: float, price_roi: float = 100.0) -> ScoredRouteEntry:
    return ScoredRouteEntry(
        commodity_name=name,
        id_commodity=1,
        origin_terminal_name="Mine",
        destination_terminal_name="City",
        price_origin=100,
        price_destination=200,
        price_margin=100,
        price_roi=price_roi,
        distance=42.0,
        score=None,
        scu_origin=500,
        scu_destination=800,
        status_origin=2,
        status_destination=1,
        profit=profit,
    )


# --- aggregate_commodity_trips ---


def test_trips_summed_across_terminals_and_both_sides():
    rows = [
        _price_row(scu_buy_users_rows=3, scu_sell_users_rows=5),
        _price_row(scu_buy_users_rows=2, scu_sell_users_rows=0),
    ]
    trips, volatility = aggregate_commodity_trips(rows)
    assert trips == 10
    assert volatility is None


def test_trips_handles_null_counts():
    trips, _ = aggregate_commodity_trips([_price_row(scu_buy_users_rows=None, scu_sell_users_rows=None)])
    assert trips == 0


def test_volatility_averages_buy_and_sell_samples():
    rows = [
        _price_row(volatility_price_buy=1.0, volatility_price_sell=2.0),
        _price_row(volatility_price_buy=3.0),
    ]
    _, volatility = aggregate_commodity_trips(rows)
    assert volatility == 2.0


# --- rank_trending ---


def test_rank_trending_orders_by_trips_then_volatility():
    entries = [
        _trending("Quiet", 5),
        _trending("BusyVolatile", 20, volatility=9.0),
        _trending("BusySteady", 20, volatility=1.0),
        _trending("BusyUnknown", 20, volatility=None),
    ]
    ranked = rank_trending(entries)
    # Same trip count: lower volatility wins, unknown volatility ranks last.
    assert [e.commodity_name for e in ranked] == ["BusySteady", "BusyVolatile", "BusyUnknown", "Quiet"]


def test_rank_trending_respects_limit():
    entries = [_trending(f"C{i}", i) for i in range(20)]
    assert len(rank_trending(entries, limit=10)) == 10


# --- compute_movers ---


def test_movers_split_into_gainers_and_losers_by_magnitude():
    rows = [
        _price_row(commodity_name="Up", price_sell=110, price_sell_avg=100),
        _price_row(commodity_name="Down", price_sell=90, price_sell_avg=100),
        _price_row(commodity_name="Flat", price_sell=100, price_sell_avg=100),
    ]
    gainers, losers = compute_movers(rows)
    assert [m.commodity_name for m in gainers] == ["Up"]
    assert gainers[0].pct_change == 10.0
    assert [m.commodity_name for m in losers] == ["Down"]
    assert losers[0].pct_change == -10.0


def test_movers_average_across_terminals_per_commodity():
    # One noisy terminal shouldn't dominate: 100->110 and 100->90 average out flat.
    rows = [
        _price_row(commodity_name="Mixed", price_sell=110, price_sell_avg=100),
        _price_row(commodity_name="Mixed", price_sell=90, price_sell_avg=100),
    ]
    gainers, losers = compute_movers(rows)
    assert gainers == [] and losers == []


def test_movers_ignore_noise_below_half_percent():
    rows = [_price_row(commodity_name="Barely", price_sell=100.4, price_sell_avg=100)]
    gainers, losers = compute_movers(rows)
    assert gainers == [] and losers == []


def test_movers_skip_rows_without_usable_prices():
    rows = [
        _price_row(commodity_name="NoBaseline", price_sell=100, price_sell_avg=0),
        _price_row(commodity_name="NoCurrent", price_sell=0, price_sell_avg=100),
        _price_row(commodity_name=None, price_sell=100, price_sell_avg=100),
    ]
    gainers, losers = compute_movers(rows)
    assert gainers == [] and losers == []


# --- select_available_routes ---


def test_select_available_routes_returns_every_qualifying_route_sorted_by_profit():
    """Not just the single most-profitable one - a later auto-load-only/system filter
    needs a same-commodity alternative to fall back to if the top route gets excluded.
    Ranked by profit (not UEX's own `score`, which turned out to be an undocumented
    "higher is better" black box with no published formula)."""
    rows = [
        _route_row(profit=9000, scu_origin=0),  # most profitable, but nothing to buy
        _route_row(profit=8000, price_origin=0),  # origin doesn't sell it
        _route_row(profit=700),
        _route_row(profit=600, origin_terminal_name="Backup"),
    ]
    routes = select_available_routes("Laranite", 1, rows)
    assert [r.profit for r in routes] == [700, 600]
    assert (routes[0].origin_terminal_id, routes[0].destination_terminal_id) == (10, 20)


def test_select_available_routes_uses_roi_as_a_tie_breaker():
    rows = [
        _route_row(profit=700, price_roi=10.0, origin_terminal_name="LowROI"),
        _route_row(profit=700, price_roi=50.0, origin_terminal_name="HighROI"),
    ]
    routes = select_available_routes("Laranite", 1, rows)
    assert [r.origin_terminal_name for r in routes] == ["HighROI", "LowROI"]


def test_select_available_routes_excludes_rows_with_no_profit_figure():
    rows = [_route_row(profit=None), _route_row(profit=10, origin_terminal_name="HasProfit")]
    routes = select_available_routes("Laranite", 1, rows)
    assert len(routes) == 1
    assert routes[0].origin_terminal_name == "HasProfit"


def test_select_available_routes_returns_empty_when_nothing_qualifies():
    assert select_available_routes("Laranite", 1, [_route_row(scu_origin=0)]) == []
    assert select_available_routes("Laranite", 1, []) == []


def test_select_available_routes_excludes_a_negative_or_zero_profit_route():
    """Real defect a user hit live: this only checked that `profit` was PRESENT, not that
    it was actually positive - UEX's own /commodities_routes can include a route where
    the destination price is below the origin's (a genuine money-losing pairing), and
    without this check it could still fill a remaining "top N" slot once genuinely
    profitable candidates ran out, showing a route that loses money as a
    recommendation."""
    rows = [
        _route_row(profit=-92160, price_origin=1760, price_destination=1600, origin_terminal_name="Losing"),
        _route_row(profit=0, origin_terminal_name="Breakeven"),
        _route_row(profit=700, origin_terminal_name="Profitable"),
    ]
    routes = select_available_routes("Laranite", 1, rows)
    assert len(routes) == 1
    assert routes[0].origin_terminal_name == "Profitable"


# --- select_in_stock_routes ---


def test_select_in_stock_routes_requires_live_destination_demand():
    rows = [
        _route_row(profit=900, status_destination=SELL_SIDE_NO_DEMAND_CODE),  # full, no demand
        _route_row(profit=800, status_destination=0),  # destination doesn't buy it
        _route_row(profit=700, status_destination=None),
        _route_row(profit=600, price_destination=0),
        _route_row(profit=500, scu_destination=0),
        _route_row(profit=400, destination_terminal_name="LiveDemand"),
    ]
    routes = select_in_stock_routes("Laranite", 1, rows)
    assert len(routes) == 1
    assert routes[0].destination_terminal_name == "LiveDemand"
    assert routes[0].profit == 400


def test_select_in_stock_routes_still_requires_origin_stock():
    assert select_in_stock_routes("Laranite", 1, [_route_row(scu_origin=0)]) == []


def test_select_in_stock_routes_excludes_a_negative_or_zero_profit_route():
    """Same real defect as select_available_routes's own test - this shares the identical
    profit-present-but-not-positive gap."""
    rows = [
        _route_row(profit=-92160, price_origin=1760, price_destination=1600, origin_terminal_name="Losing"),
        _route_row(profit=0, origin_terminal_name="Breakeven"),
        _route_row(profit=700, origin_terminal_name="Profitable"),
    ]
    routes = select_in_stock_routes("Laranite", 1, rows)
    assert len(routes) == 1
    assert routes[0].origin_terminal_name == "Profitable"


# --- rank_top_scored_routes ---


def test_rank_top_scored_routes_orders_by_profit_and_caps():
    entries = [_scored(f"C{i}", profit=i) for i in range(15)]
    ranked = rank_top_scored_routes(entries, limit=10)
    assert len(ranked) == 10
    assert [e.profit for e in ranked] == list(range(14, 4, -1))


def test_rank_top_scored_routes_uses_roi_as_a_tie_breaker():
    entries = [
        _scored("LowROI", profit=500, price_roi=10.0),
        _scored("HighROI", profit=500, price_roi=50.0),
    ]
    ranked = rank_top_scored_routes(entries, limit=10)
    assert [e.commodity_name for e in ranked] == ["HighROI", "LowROI"]


# --- rank_by_achievable_profit ---
# Fixture numbers are modeled on real live UEX data: a Waste route UEX itself reports
# at 54,500,000 aUEC theoretical profit (250,000 SCU, needing a 58,000,000 aUEC
# investment no real player has) outranked a Corundum route UEX reports at only
# 1,385,120 aUEC (787 SCU) - purely because of UEX's own unlimited-cargo/budget basis.


def _real_route(*, commodity_name: str, price_origin: float, price_destination: float,
                 scu_origin: float, scu_destination: float, profit: float, price_roi: float = 0.0) -> ScoredRouteEntry:
    return ScoredRouteEntry(
        commodity_name=commodity_name,
        id_commodity=hash(commodity_name) % 1000,
        origin_terminal_name="Origin",
        destination_terminal_name="Destination",
        price_origin=price_origin,
        price_destination=price_destination,
        price_margin=None,
        price_roi=price_roi,
        distance=None,
        score=None,
        scu_origin=scu_origin,
        scu_destination=scu_destination,
        status_origin=None,
        status_destination=None,
        profit=profit,
    )


def _waste_and_corundum() -> list[ScoredRouteEntry]:
    waste = _real_route(
        commodity_name="Waste", price_origin=232, price_destination=450,
        scu_origin=250_000, scu_destination=250_000, profit=54_500_000,
    )
    corundum = _real_route(
        commodity_name="Corundum", price_origin=2640, price_destination=4400,
        scu_origin=787, scu_destination=787, profit=1_385_120,
    )
    return [waste, corundum]


def test_rank_by_achievable_profit_is_a_noop_without_ship_or_budget():
    entries = _waste_and_corundum()
    assert rank_by_achievable_profit(entries, ship_cargo_scu=None, budget=None) == entries


def test_rank_by_achievable_profit_favors_what_a_real_ship_and_budget_can_realize():
    # A 1,440 SCU ship and a 2,000,000 aUEC budget can only ever haul ~313,920 aUEC of
    # profit out of Waste (ship-capped at 1,440 SCU) but ~1,333,333 aUEC out of
    # Corundum (budget-capped at ~757.6 SCU, still under its own 787 SCU stock) - over
    # 4x more, the reverse of UEX's own raw-profit ordering.
    ranked = rank_by_achievable_profit(_waste_and_corundum(), ship_cargo_scu=1440, budget=2_000_000)
    assert [e.commodity_name for e in ranked] == ["Corundum", "Waste"]


def test_rank_by_achievable_profit_uses_roi_as_a_tie_breaker():
    low_roi = _real_route(
        commodity_name="LowROI", price_origin=100, price_destination=200,
        scu_origin=500, scu_destination=500, profit=1000, price_roi=10.0,
    )
    high_roi = _real_route(
        commodity_name="HighROI", price_origin=100, price_destination=200,
        scu_origin=500, scu_destination=500, profit=1000, price_roi=50.0,
    )
    ranked = rank_by_achievable_profit([low_roi, high_roi], ship_cargo_scu=100, budget=None)
    assert [e.commodity_name for e in ranked] == ["HighROI", "LowROI"]


def test_route_confidence_rewards_fresh_reports_availability_and_stable_prices():
    fresh = classify_terminal_health(
        {"terminal_name": "A", "has_recent_reports": 1, "last_update_days": 0,
         "last_update_days_limit": 1, "last_update_days_percentage": 100,
         "prices_updated_percentage": 100}
    )
    high = compute_route_confidence(
        origin_health=fresh, destination_health=fresh,
        origin_report_count=5, destination_report_count=5,
        volatility_origin=0.1, volatility_destination=0.1,
        origin_available=True, destination_available=True,
    )
    low = compute_route_confidence(
        origin_health=None, destination_health=None,
        origin_report_count=0, destination_report_count=0,
        volatility_origin=1.0, volatility_destination=1.0,
        origin_available=True, destination_available=False,
    )
    assert high.label == "High"
    assert high.score > low.score
    assert low.label == "Low"


def test_route_confidence_is_not_a_profit_score():
    stale = classify_terminal_health(
        {"terminal_name": "A", "has_recent_reports": 0, "last_update_days": 14,
         "last_update_days_limit": 1, "last_update_days_percentage": 0,
         "prices_updated_percentage": 100}
    )
    confidence = compute_route_confidence(
        origin_health=stale, destination_health=stale,
        origin_report_count=0, destination_report_count=0,
        volatility_origin=None, volatility_destination=None,
        origin_available=True, destination_available=True,
    )
    assert confidence.score < 50


def test_report_count_coalescing_preserves_valid_zero():
    assert coalesce_report_count(0, 9) == 0
    assert coalesce_report_count(None, 9) == 9


def test_track_record_modifier_is_neutral_below_the_minimum_sample_size():
    """A single report saying 'matched' would otherwise look like 100% confidence from
    one data point - no adjustment below MIN_REPORTS_FOR_TRACK_RECORD."""
    assert track_record_modifier(1, 1) == 0
    assert track_record_modifier(0, 2) == 0


def test_track_record_modifier_rewards_a_strong_match_rate():
    assert track_record_modifier(10, 10) == 10


def test_track_record_modifier_penalizes_a_poor_match_rate():
    assert track_record_modifier(0, 10) == -10


def test_track_record_modifier_is_neutral_at_a_fifty_percent_rate():
    assert track_record_modifier(5, 10) == 0


def test_compute_route_confidence_applies_the_track_record_modifier_and_stays_clamped():
    fresh = classify_terminal_health(
        {"terminal_name": "A", "has_recent_reports": 1, "last_update_days": 0,
         "last_update_days_limit": 1, "last_update_days_percentage": 100,
         "prices_updated_percentage": 100}
    )
    kwargs = dict(
        origin_health=fresh, destination_health=fresh,
        origin_report_count=5, destination_report_count=5,
        volatility_origin=0.1, volatility_destination=0.1,
        origin_available=True, destination_available=True,
    )
    base = compute_route_confidence(**kwargs)
    boosted = compute_route_confidence(**kwargs, track_record_modifier=10)
    penalized = compute_route_confidence(**kwargs, track_record_modifier=-10)
    assert boosted.score == min(100, base.score + 10)
    assert penalized.score == max(0, base.score - 10)
