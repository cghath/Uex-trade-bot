"""Tests for mixed-commodity cargo allocation and ranking."""
import discord

from bot.uex.mixed_routes import (
    allocate_pair_cargo,
    build_mixed_routes,
    is_space_terminal,
    requires_capital_cargo_access,
    supports_capital_cargo_access,
)
from bot.cogs.prices import _add_chunked_fields, _chunk_lines


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
        "status_sell": 1,
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
    # The cheapest possible 2-commodity load is 1 unit of each (100 + 10 = 110) - below
    # that, no 2-item combination is affordable at all, regardless of allocation strategy.
    assert build_mixed_routes(rows, ship_capacity_scu=10, budget=100) == []
    (route,) = build_mixed_routes(rows, ship_capacity_scu=10, budget=420)
    assert route.investment <= 420
    assert len(route.cargo) == 2


def test_allocate_pair_cargo_finds_the_true_optimum_within_the_exact_search_thresholds():
    """The two-ordering greedy approach is still a real approximation, not a solver - a
    random search over small scenarios (capacity 9, budget 51) found this case where
    neither ordering comes close: margin-first (equal per-unit margins, so stable order
    keeps the first-listed commodity first) gets 3xA + 1xB = 76 profit; the true optimum
    is 1xA + 8xB = 171 (cost 15 + 32 = 47, within the 51 budget, using all 9 capacity).
    Within EXACT_SEARCH_MAX_CANDIDATES/EXACT_SEARCH_MAX_CAPACITY, allocate_pair_cargo
    must find this exactly, not approximate it."""
    source_a = _row(1, 1, "A", "Origin", price_buy=15, scu_buy=11)
    dest_a = _row(1, 2, "A", "Destination", price_sell=34, scu_sell=11)
    source_b = _row(2, 1, "B", "Origin", price_buy=4, scu_buy=9)
    dest_b = _row(2, 2, "B", "Destination", price_sell=23, scu_sell=9)
    pairs = [(source_a, dest_a), (source_b, dest_b)]
    cargo = allocate_pair_cargo(pairs, capacity=9, budget=51, max_commodities=3, min_commodities=2)
    assert {item.commodity_name: item.quantity_scu for item in cargo} == {"A": 1.0, "B": 8.0}
    assert sum(item.profit for item in cargo) == 171


def test_allocate_pair_cargo_still_returns_a_qualifying_result_past_the_exact_thresholds():
    """Past EXACT_SEARCH_MAX_CANDIDATES/EXACT_SEARCH_MAX_CAPACITY, allocation falls back
    to the two-ordering heuristic rather than brute-forcing an unbounded search space -
    this must still return a valid, budget/capacity-respecting, qualifying result, not
    error or hang."""
    pairs = []
    for i in range(10):  # past EXACT_SEARCH_MAX_CANDIDATES (8)
        # Capped stock (5 units each) so no single commodity can consume all 30
        # capacity alone - otherwise a degenerate greedy pick could land on 1 item
        # regardless of ordering, which isn't what this test is checking.
        buy, sell = 10, 10 + (i + 1)
        source = _row(i, 1, f"C{i}", "Origin", price_buy=buy, scu_buy=5)
        dest = _row(i, 2, f"C{i}", "Destination", price_sell=sell, scu_sell=5)
        pairs.append((source, dest))
    cargo = allocate_pair_cargo(pairs, capacity=30, budget=500, max_commodities=3, min_commodities=2)
    assert len(cargo) >= 2
    assert sum(item.investment for item in cargo) <= 500
    assert sum(item.quantity_scu for item in cargo) <= 30


def test_a_bigger_ship_never_scores_worse_than_a_smaller_one_at_the_exact_threshold():
    """Regression: capacity above EXACT_SEARCH_MAX_CAPACITY (25) used to fall straight to
    the two-ordering heuristic, with no exact solve at all. On this data (equal per-unit
    margins for A and B, so a tie-broken greedy commits fully to one commodity before
    ever trying the other) BOTH greedy orderings land on a single commodity alone -
    filling all 26 capacity with just one of them - which fails the 2-commodity minimum,
    so a real, valid 475-profit two-commodity load found at 25 SCU would have vanished
    entirely at 26 SCU. allocate_pair_cargo must still try a capped exact solve (as if
    capacity were the threshold) and prefer it over a worse or absent heuristic result -
    a bigger ship must never do worse than a smaller one would on identical data."""
    source_a = _row(1, 1, "A", "Origin", price_buy=15, scu_buy=1000)
    dest_a = _row(1, 2, "A", "Destination", price_sell=34, scu_sell=1000)
    source_b = _row(2, 1, "B", "Origin", price_buy=4, scu_buy=1000)
    dest_b = _row(2, 2, "B", "Destination", price_sell=23, scu_sell=1000)
    pairs = [(source_a, dest_a), (source_b, dest_b)]
    at_threshold = allocate_pair_cargo(pairs, capacity=25, budget=1_000_000, max_commodities=3, min_commodities=2)
    past_threshold = allocate_pair_cargo(pairs, capacity=26, budget=1_000_000, max_commodities=3, min_commodities=2)
    assert len(past_threshold) >= 2
    assert sum(item.profit for item in past_threshold) >= sum(item.profit for item in at_threshold)


def test_allocate_pair_cargo_prefers_efficiency_over_raw_margin_when_budget_binds():
    """Greedy-by-per-unit-margin alone can pick badly under a binding budget: A's per-unit
    margin (50) beats B's (9), but A is so expensive that the budget barely affords 1
    unit, wasting potential that spending the same budget entirely on cheap-but-lower-
    margin B would have captured. Trying profit-per-aUEC-invested too (B: 9/10 = 0.9 beats
    A: 50/90 = ~0.56) and keeping whichever ordering performs better fixes this without a
    full knapsack search over commodity combinations."""
    source_a = _row(1, 1, "A", "Origin", price_buy=90, scu_buy=1000)
    dest_a = _row(1, 2, "A", "Destination", price_sell=140, scu_sell=1000)
    source_b = _row(2, 1, "B", "Origin", price_buy=10, scu_buy=1000)
    dest_b = _row(2, 2, "B", "Destination", price_sell=19, scu_sell=1000)
    pairs = [(source_a, dest_a), (source_b, dest_b)]
    cargo = allocate_pair_cargo(pairs, capacity=10, budget=100, max_commodities=3)
    assert [item.commodity_name for item in cargo] == ["B"]
    assert sum(item.profit for item in cargo) == 90


def test_build_mixed_routes_never_drops_a_valid_load_for_a_higher_profit_single_item():
    """Regression: picking allocate_pair_cargo's ordering purely by total profit (see the
    test above) can pick a 1-item allocation over a 2-item one with lower profit - fine
    for a single leg of a multi-stop chain, but /mixed-routes requires at least 2
    commodities, so that same A/B data must still produce the 2-item (A+B, profit 59)
    load here, not disappear just because the 1-item (B-only, profit 90) allocation
    scores higher and doesn't meet the 2-commodity minimum."""
    rows = [
        _row(1, 1, "A", "Origin", price_buy=90, scu_buy=1000),
        _row(1, 2, "A", "Destination", price_sell=140, scu_sell=1000),
        _row(2, 1, "B", "Origin", price_buy=10, scu_buy=1000),
        _row(2, 2, "B", "Destination", price_sell=19, scu_sell=1000),
    ]
    (route,) = build_mixed_routes(rows, ship_capacity_scu=10, budget=100)
    assert [item.commodity_name for item in route.cargo] == ["A", "B"]
    assert route.profit == 59


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


def test_destination_with_no_demand_status_is_a_hard_exclusion():
    rows = [
        _row(1, 1, "A", "Origin", price_buy=10, scu_buy=2),
        _row(1, 2, "A", "Destination", price_sell=20, scu_sell=2, status_sell=7),
        _row(2, 1, "B", "Origin", price_buy=10, scu_buy=2),
        _row(2, 2, "B", "Destination", price_sell=20, scu_sell=2, status_sell="7"),
    ]
    assert build_mixed_routes(rows, ship_capacity_scu=10) == []


def test_destination_with_unknown_status_is_not_treated_as_confirmed_demand():
    rows = [
        _row(1, 1, "A", "Origin", price_buy=10, scu_buy=2),
        _row(1, 2, "A", "Destination", price_sell=20, scu_sell=2, status_sell=None),
        _row(2, 1, "B", "Origin", price_buy=10, scu_buy=2),
        _row(2, 2, "B", "Destination", price_sell=20, scu_sell=2, status_sell=0),
    ]
    assert build_mixed_routes(rows, ship_capacity_scu=10) == []


def test_warning_lines_are_split_without_being_dropped():
    lines = ["warning one", "warning two is longer", "warning three"]
    chunks = _chunk_lines(lines, max_length=25)
    assert all(len(chunk) <= 25 for chunk in chunks)
    assert "\n".join(chunks).splitlines() == lines


def test_oversized_embed_fields_are_split_without_dropping_text():
    oversized = "x" * 2200
    chunks = _chunk_lines([oversized])
    assert all(len(chunk) <= 1024 for chunk in chunks)
    assert "".join(chunks) == oversized

    embed = discord.Embed(title="Routes")
    _add_chunked_fields(embed, name="n" * 300, lines=[oversized])
    assert all(len(field.name) <= 256 for field in embed.fields)
    assert all(len(field.value) <= 1024 for field in embed.fields)
    assert "".join(field.value for field in embed.fields) == oversized


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


def test_auto_load_only_excludes_a_route_when_the_origin_lacks_auto_load():
    rows = [
        _row(1, 1, "A", "Origin", price_buy=10, scu_buy=2, is_auto_load=0),
        _row(1, 2, "A", "Destination", price_sell=20, scu_sell=2),
        _row(2, 1, "B", "Origin", price_buy=10, scu_buy=2, is_auto_load=0),
        _row(2, 2, "B", "Destination", price_sell=20, scu_sell=2),
    ]
    assert build_mixed_routes(rows, ship_capacity_scu=10)
    assert build_mixed_routes(rows, ship_capacity_scu=10, auto_load_only=True) == []


def test_auto_load_only_requires_both_the_origin_and_destination():
    """Per user direction, auto-load-only requires BOTH ends of a route, not just the
    origin - is_auto_load is a property of the terminal itself, not documented as
    specific to buying or selling."""
    rows = [
        _row(1, 1, "A", "Origin", price_buy=10, scu_buy=2, is_auto_load=1),
        _row(1, 2, "A", "Destination", price_sell=20, scu_sell=2, is_auto_load=0),
        _row(2, 1, "B", "Origin", price_buy=10, scu_buy=2, is_auto_load=1),
        _row(2, 2, "B", "Destination", price_sell=20, scu_sell=2, is_auto_load=0),
    ]
    assert build_mixed_routes(rows, ship_capacity_scu=10)
    assert build_mixed_routes(rows, ship_capacity_scu=10, auto_load_only=True) == []


def test_auto_load_only_keeps_a_route_confirmed_at_both_ends():
    rows = [
        _row(1, 1, "A", "Origin", price_buy=10, scu_buy=2, is_auto_load=1),
        _row(1, 2, "A", "Destination", price_sell=20, scu_sell=2, is_auto_load=1),
        _row(2, 1, "B", "Origin", price_buy=10, scu_buy=2, is_auto_load=1),
        _row(2, 2, "B", "Destination", price_sell=20, scu_sell=2, is_auto_load=1),
    ]
    assert build_mixed_routes(rows, ship_capacity_scu=10, auto_load_only=True)


def test_system_filter_requires_both_ends_in_the_named_system():
    """Unlike auto-load-only, a system filter must reject a route where only one end is
    in the requested system - crossing systems defeats the point of asking to 'stay in
    Pyro'. Filtering the shared row pool before pairing (not a post-pairing check) should
    already guarantee this - a route can never mix an in-system origin with an
    out-of-system destination if the destination was never in the eligible pool at all."""
    rows = [
        _row(1, 1, "A", "Pyro Origin", price_buy=10, scu_buy=2, star_system_name="Pyro"),
        _row(1, 2, "A", "Stanton Destination", price_sell=20, scu_sell=2, star_system_name="Stanton"),
        _row(2, 1, "B", "Pyro Origin", price_buy=10, scu_buy=2, star_system_name="Pyro"),
        _row(2, 2, "B", "Stanton Destination", price_sell=20, scu_sell=2, star_system_name="Stanton"),
    ]
    assert build_mixed_routes(rows, ship_capacity_scu=10)
    assert build_mixed_routes(rows, ship_capacity_scu=10, system="Pyro") == []
    assert build_mixed_routes(rows, ship_capacity_scu=10, system="Stanton") == []


def test_system_filter_keeps_a_route_confirmed_in_system_at_both_ends():
    rows = [
        _row(1, 1, "A", "Pyro Origin", price_buy=10, scu_buy=2, star_system_name="Pyro"),
        _row(1, 2, "A", "Pyro Destination", price_sell=20, scu_sell=2, star_system_name="Pyro"),
        _row(2, 1, "B", "Pyro Origin", price_buy=10, scu_buy=2, star_system_name="Pyro"),
        _row(2, 2, "B", "Pyro Destination", price_sell=20, scu_sell=2, star_system_name="Pyro"),
    ]
    assert build_mixed_routes(rows, ship_capacity_scu=10, system="Pyro")
