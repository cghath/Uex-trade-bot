"""Tests for mixed-commodity cargo allocation and ranking."""
from bot.uex.mixed_routes import (
    build_mixed_routes,
    is_space_terminal,
    requires_capital_cargo_access,
    supports_capital_cargo_access,
)
from bot.cogs.prices import _chunk_lines


def _row(commodity_id, terminal_id, name, terminal, **values):
    return {
        "id_commodity": commodity_id,
        "id_terminal": terminal_id,
        "commodity_name": name,
        "terminal_name": terminal,
        "price_buy": None,
        "price_sell": None,
        "scu_buy": None,
        "scu_sell": None,
        **values,
    }


def test_builds_mixed_load_with_stock_demand_ship_and_profit_limits():
    rows = [
        _row(1, 10, "Stileron", "Bueno", price_buy=100, scu_buy=4),
        _row(1, 20, "Stileron", "Levski", price_sell=200, scu_sell=10),
        _row(2, 10, "Cobalt", "Bueno", price_buy=20, scu_buy=95),
        _row(2, 20, "Cobalt", "Levski", price_sell=50, scu_sell=80),
        _row(3, 10, "Diamond", "Bueno", price_buy=10, scu_buy=20),
        _row(3, 20, "Diamond", "Levski", price_sell=20, scu_sell=7),
    ]
    (route,) = build_mixed_routes(rows, ship_capacity_scu=100)
    assert [(item.commodity_name, item.quantity_scu) for item in route.cargo] == [
        ("Stileron", 4), ("Cobalt", 80), ("Diamond", 7)
    ]
    assert route.cargo_scu == 91
    assert route.investment == 2070
    assert route.profit == 2870


def test_budget_is_a_hard_limit_and_requires_two_allocated_commodities():
    rows = [
        _row(1, 1, "A", "Origin", price_buy=100, scu_buy=4),
        _row(1, 2, "A", "Destination", price_sell=200, scu_sell=4),
        _row(2, 1, "B", "Origin", price_buy=10, scu_buy=10),
        _row(2, 2, "B", "Destination", price_sell=15, scu_sell=10),
    ]
    assert build_mixed_routes(rows, ship_capacity_scu=10, budget=400) == []
    (route,) = build_mixed_routes(rows, ship_capacity_scu=10, budget=420)
    assert route.investment <= 420
    assert len(route.cargo) == 2


def test_routes_rank_by_ship_adjusted_profit_and_return_only_five():
    rows = []
    for destination_id in range(2, 9):
        rows.extend([
            _row(1, 1, "A", "Origin", price_buy=10, scu_buy=1),
            _row(1, destination_id, "A", f"D{destination_id}", price_sell=10 + destination_id, scu_sell=1),
            _row(2, 1, "B", "Origin", price_buy=10, scu_buy=1),
            _row(2, destination_id, "B", f"D{destination_id}", price_sell=11 + destination_id, scu_sell=1),
        ])
    routes = build_mixed_routes(rows, ship_capacity_scu=10)
    assert len(routes) == 5
    assert [route.profit for route in routes] == sorted((route.profit for route in routes), reverse=True)


def test_single_commodity_and_unprofitable_pairs_are_excluded():
    rows = [
        _row(1, 1, "A", "Origin", price_buy=100, scu_buy=10),
        _row(1, 2, "A", "Destination", price_sell=200, scu_sell=10),
        _row(2, 1, "B", "Origin", price_buy=100, scu_buy=10),
        _row(2, 2, "B", "Destination", price_sell=90, scu_sell=10),
    ]
    assert build_mixed_routes(rows, ship_capacity_scu=10) == []


def test_warning_lines_are_split_without_being_dropped():
    lines = ["warning one", "warning two is longer", "warning three"]
    chunks = _chunk_lines(lines, max_length=25)
    assert all(len(chunk) <= 25 for chunk in chunks)
    assert "\n".join(chunks).splitlines() == lines


def test_space_terminal_uses_explicit_uex_relationship_not_planet_name():
    assert is_space_terminal({"id_space_station": 13, "planet_name": "ArcCorp"})
    assert is_space_terminal({"space_station_name": "Baijini Point", "planet_name": "ArcCorp"})
    assert not is_space_terminal({"id_space_station": 0, "id_outpost": 7})
    assert not is_space_terminal({"id_outpost": 7, "moon_name": "Arial"})
    assert not is_space_terminal({"planet_name": "Hurston"})
    assert not is_space_terminal({})


def test_space_only_excludes_route_when_either_end_is_on_the_surface():
    rows = [
        _row(1, 1, "A", "Station", price_buy=10, scu_buy=2, id_space_station=10),
        _row(1, 2, "A", "Outpost", price_sell=20, scu_sell=2, id_outpost=20),
        _row(2, 1, "B", "Station", price_buy=10, scu_buy=2, id_space_station=10),
        _row(2, 2, "B", "Outpost", price_sell=20, scu_sell=2, id_outpost=20),
    ]
    assert build_mixed_routes(rows, ship_capacity_scu=10)
    assert build_mixed_routes(rows, ship_capacity_scu=10, space_only=True) == []


def test_capital_ship_and_terminal_access_use_explicit_uex_metadata():
    assert requires_capital_cargo_access({"name": "Polaris", "pad_type": "XL"})
    assert requires_capital_cargo_access({"name": "Hull C", "is_loading_dock": 1})
    assert not requires_capital_cargo_access({"name": "Freelancer", "pad_type": "M"})
    assert supports_capital_cargo_access({"has_loading_dock": 1})
    assert supports_capital_cargo_access({"station_pad_types": "S|M|L|XL"})
    assert not supports_capital_cargo_access({"station_pad_types": "S|M|L"})
    assert not supports_capital_cargo_access({})


def test_capital_access_filter_fails_closed_for_unverified_terminal():
    rows = [
        _row(1, 1, "A", "XL Station", price_buy=10, scu_buy=2, station_pad_types="XL"),
        _row(1, 2, "A", "Unknown", price_sell=20, scu_sell=2),
        _row(2, 1, "B", "XL Station", price_buy=10, scu_buy=2, station_pad_types="XL"),
        _row(2, 2, "B", "Unknown", price_sell=20, scu_sell=2),
    ]
    assert build_mixed_routes(rows, ship_capacity_scu=10)
    assert build_mixed_routes(rows, ship_capacity_scu=10, capital_access_only=True) == []
