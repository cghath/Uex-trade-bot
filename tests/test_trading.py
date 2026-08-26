"""Tests for the local buy/sell/route ranking in bot/uex/trading.py, against synthetic
/commodities_prices rows (one row per terminal for a single commodity)."""
from __future__ import annotations

from bot.uex.trading import TradeRoute, best_buy_locations, best_routes, best_sell_locations


def _row(**overrides) -> dict:
    """A realistic /commodities_prices row for one terminal."""
    row = {
        "id_terminal": 1,
        "terminal_name": "Terminal A",
        "commodity_name": "Laranite",
        "price_buy": 0,
        "price_sell": 0,
        "scu_buy": None,
        "scu_sell": None,
        "status_buy": None,
        "status_sell": None,
    }
    row.update(overrides)
    return row


def test_best_sell_locations_sorts_highest_first_and_drops_non_buying_terminals():
    rows = [
        _row(id_terminal=1, terminal_name="Low", price_sell=100),
        _row(id_terminal=2, terminal_name="None", price_sell=0),
        _row(id_terminal=3, terminal_name="High", price_sell=300),
        _row(id_terminal=4, terminal_name="Null", price_sell=None),
        _row(id_terminal=5, terminal_name="Mid", price_sell=200),
    ]
    result = best_sell_locations(rows)
    assert [r["terminal_name"] for r in result] == ["High", "Mid", "Low"]


def test_best_buy_locations_sorts_cheapest_first_and_drops_non_selling_terminals():
    rows = [
        _row(id_terminal=1, terminal_name="Mid", price_buy=200),
        _row(id_terminal=2, terminal_name="None", price_buy=0),
        _row(id_terminal=3, terminal_name="Cheap", price_buy=100),
        _row(id_terminal=4, terminal_name="Null", price_buy=None),
    ]
    result = best_buy_locations(rows)
    assert [r["terminal_name"] for r in result] == ["Cheap", "Mid"]


def test_locations_respect_limit():
    rows = [_row(id_terminal=i, price_sell=i * 10, price_buy=i * 10) for i in range(1, 10)]
    assert len(best_sell_locations(rows, limit=3)) == 3
    assert len(best_buy_locations(rows, limit=3)) == 3


def test_best_routes_pairs_cheapest_buy_with_best_sell():
    rows = [
        _row(id_terminal=1, terminal_name="Mine", price_buy=100, price_sell=0),
        _row(id_terminal=2, terminal_name="Outpost", price_buy=150, price_sell=0),
        _row(id_terminal=3, terminal_name="City", price_buy=0, price_sell=250),
        _row(id_terminal=4, terminal_name="Station", price_buy=0, price_sell=200),
    ]
    routes = best_routes(rows)
    assert routes, "expected at least one profitable route"
    top = routes[0]
    assert top.buy_terminal == "Mine"
    assert top.sell_terminal == "City"
    assert top.profit_per_unit == 150
    # Sorted by profit, descending.
    profits = [r.profit_per_unit for r in routes]
    assert profits == sorted(profits, reverse=True)


def test_best_routes_excludes_same_terminal_pairs():
    # One terminal both sells cheap and buys high: profitable on paper, but not a route.
    rows = [_row(id_terminal=1, terminal_name="Both", price_buy=100, price_sell=200)]
    assert best_routes(rows) == []


def test_best_routes_excludes_unprofitable_pairs():
    rows = [
        _row(id_terminal=1, terminal_name="Expensive", price_buy=300, price_sell=0),
        _row(id_terminal=2, terminal_name="LowBall", price_buy=0, price_sell=250),
    ]
    assert best_routes(rows) == []


def test_best_routes_carries_stock_and_status_fields_through():
    rows = [
        _row(id_terminal=1, terminal_name="Mine", price_buy=100, scu_buy=500, status_buy=2),
        _row(id_terminal=2, terminal_name="City", price_sell=200, scu_sell=800, status_sell=1),
    ]
    (route,) = best_routes(rows)
    assert route.scu_buy_available == 500
    assert route.scu_sell_wanted == 800
    assert route.status_buy_code == 2
    assert route.status_sell_code == 1
    assert route.buy_terminal_id == 1
    assert route.sell_terminal_id == 2


def test_trade_route_profit_and_margin():
    route = TradeRoute(
        commodity_name="Laranite",
        buy_terminal="A",
        buy_price=100,
        sell_terminal="B",
        sell_price=175.567,
    )
    assert route.profit_per_unit == 75.57
    assert route.margin_pct == 75.6


def test_trade_route_margin_is_zero_when_buy_price_is_zero():
    route = TradeRoute(
        commodity_name="Laranite",
        buy_terminal="A",
        buy_price=0,
        sell_terminal="B",
        sell_price=100,
    )
    assert route.margin_pct == 0.0
