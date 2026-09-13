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


def test_best_sell_locations_excludes_confirmed_no_demand_even_with_a_stale_positive_price():
    """Audit finding: UEX status 7 ('Maximum Inventory, No Demand') means the terminal is
    CONFIRMED not buying, even when price_sell itself is still a stale positive number -
    the same inversion has_sell_side_demand/effective_sell_scu already exist for
    elsewhere, now applied to the ranking itself, not just the displayed SCU figure."""
    rows = [
        _row(id_terminal=1, terminal_name="Maxed", price_sell=300, status_sell=7),
        _row(id_terminal=2, terminal_name="RealDemand", price_sell=200, status_sell=3),
    ]
    result = best_sell_locations(rows)
    assert [r["terminal_name"] for r in result] == ["RealDemand"]


def test_best_sell_locations_keeps_a_real_out_of_stock_terminal_with_no_live_scu_figure():
    """A real Out-of-Stock (status 1, the LOW end) sell-side terminal genuinely wants to
    buy - excluding it for having no live scu_sell figure would be exactly backwards,
    the same lesson SELL_SIDE_STATUS_CLARIFIER exists to spell out."""
    rows = [_row(id_terminal=1, terminal_name="Empty", price_sell=100, scu_sell=0, status_sell=1)]
    assert [r["terminal_name"] for r in best_sell_locations(rows)] == ["Empty"]


def test_best_buy_locations_excludes_confirmed_empty_stock_even_with_a_stale_positive_price():
    """Audit finding: UEX status 1 ('Out of Stock (Empty)') on the BUY side means the
    terminal has nothing to sell you, even when price_buy itself is still a stale positive
    number - confirmed against real live UEX data (every real status_buy==1 row has
    scu_buy==0 across every commodity checked)."""
    rows = [
        _row(id_terminal=1, terminal_name="Empty", price_buy=100, scu_buy=0, status_buy=1),
        _row(id_terminal=2, terminal_name="RealStock", price_buy=150, scu_buy=200, status_buy=3),
    ]
    result = best_buy_locations(rows)
    assert [r["terminal_name"] for r in result] == ["RealStock"]


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
