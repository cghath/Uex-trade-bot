"""Tests for the pure trend/volume/route-scoring helpers in bot/uex/trends.py."""
from __future__ import annotations

from bot.uex.trends import (
    SELL_SIDE_NO_DEMAND_CODE,
    MoverEntry,
    ScoredRouteEntry,
    TrendingEntry,
    aggregate_commodity_trips,
    compute_movers,
    rank_top_scored_routes,
    rank_trending,
    select_best_available_route,
    select_best_in_stock_route,
)


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
        "price_origin": 100,
        "price_destination": 200,
        "price_margin": 100,
        "price_roi": 100.0,
        "distance": 42.0,
        "score": 50,
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


def _scored(name: str, score: float) -> ScoredRouteEntry:
    return ScoredRouteEntry(
        commodity_name=name,
        id_commodity=1,
        origin_terminal_name="Mine",
        destination_terminal_name="City",
        price_origin=100,
        price_destination=200,
        price_margin=100,
        price_roi=100.0,
        distance=42.0,
        score=score,
        scu_origin=500,
        scu_destination=800,
        status_origin=2,
        status_destination=1,
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


# --- select_best_available_route ---


def test_select_best_available_route_picks_highest_score_with_origin_stock():
    rows = [
        _route_row(score=90, scu_origin=0),  # best score, but nothing to buy
        _route_row(score=80, price_origin=0),  # origin doesn't sell it
        _route_row(score=70),
        _route_row(score=60, origin_terminal_name="Backup"),
    ]
    best = select_best_available_route("Laranite", 1, rows)
    assert best is not None
    assert best.score == 70


def test_select_best_available_route_excludes_unscored_rows():
    rows = [_route_row(score=None), _route_row(score=10, origin_terminal_name="Scored")]
    best = select_best_available_route("Laranite", 1, rows)
    assert best is not None
    assert best.origin_terminal_name == "Scored"


def test_select_best_available_route_returns_none_when_nothing_qualifies():
    assert select_best_available_route("Laranite", 1, [_route_row(scu_origin=0)]) is None
    assert select_best_available_route("Laranite", 1, []) is None


# --- select_best_in_stock_route ---


def test_select_best_in_stock_route_requires_live_destination_demand():
    rows = [
        _route_row(score=90, status_destination=SELL_SIDE_NO_DEMAND_CODE),  # full, no demand
        _route_row(score=80, status_destination=0),  # destination doesn't buy it
        _route_row(score=70, status_destination=None),
        _route_row(score=60, price_destination=0),
        _route_row(score=50, scu_destination=0),
        _route_row(score=40, destination_terminal_name="LiveDemand"),
    ]
    best = select_best_in_stock_route("Laranite", 1, rows)
    assert best is not None
    assert best.destination_terminal_name == "LiveDemand"
    assert best.score == 40


def test_select_best_in_stock_route_still_requires_origin_stock():
    assert select_best_in_stock_route("Laranite", 1, [_route_row(scu_origin=0)]) is None


# --- rank_top_scored_routes ---


def test_rank_top_scored_routes_orders_by_score_and_caps():
    entries = [_scored(f"C{i}", score=i) for i in range(15)]
    ranked = rank_top_scored_routes(entries, limit=10)
    assert len(ranked) == 10
    assert [e.score for e in ranked] == list(range(14, 4, -1))
