"""Tests for multi-stop (2-3 leg) trade chain building."""
import bot.uex.multi_stop_routes as multi_stop_routes
from bot.uex.multi_stop_routes import build_multi_stop_routes


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


def test_builds_a_multi_leg_chain_and_sums_profit_across_legs():
    rows = [
        _row(1, 1, "Stileron", "Origin", price_buy=100, scu_buy=10),
        _row(1, 2, "Stileron", "Midpoint", price_sell=150, scu_sell=10),
        _row(2, 2, "Cobalt", "Midpoint", price_buy=50, scu_buy=10),
        _row(2, 3, "Cobalt", "Final", price_sell=90, scu_sell=10),
    ]
    (route,) = build_multi_stop_routes(rows, ship_capacity_scu=10)
    assert route.stops == (1, 2, 3)
    assert [leg.profit for leg in route.legs] == [500, 400]
    # investment is the real starting capital needed (leg 1's own 1000), not the naive
    # sum of both legs' investment (1500) - leg 2's 500 is funded from leg 1's revenue,
    # not fresh capital, so summing double-counts money recycled through the chain.
    assert route.investment == 1000
    assert route.profit == 900
    assert route.revenue == 1900


def test_budget_compounds_forward_after_a_profitable_leg():
    """A budget that only covers leg 1 alone must still fund leg 2, since leg 1's
    revenue (investment + profit) becomes the capital available for the next leg."""
    rows = [
        _row(1, 1, "Stileron", "Origin", price_buy=100, scu_buy=10),
        _row(1, 2, "Stileron", "Midpoint", price_sell=150, scu_sell=10),
        _row(2, 2, "Cobalt", "Midpoint", price_buy=50, scu_buy=10),
        _row(2, 3, "Cobalt", "Final", price_sell=90, scu_sell=10),
    ]
    (route,) = build_multi_stop_routes(rows, ship_capacity_scu=10, budget=1000)
    assert len(route.legs) == 2
    assert route.legs[1].investment == 500
    # The route-level summary must reflect real starting capital (1000, what leg 1
    # actually needed), not the naive sum of both legs' investment (1000 + 500 = 1500) -
    # leg 2 was funded from leg 1's own revenue, not fresh capital.
    assert route.investment == 1000
    assert route.revenue == 1900
    assert route.roi_pct == 90.0


def test_a_leg_unaffordable_at_the_starting_budget_is_still_reachable_once_earlier_profit_compounds():
    """Candidate terminals are selected assuming unlimited capital (fix for a real bug):
    ranking edges at the *original* budget would find B->C completely unaffordable (0
    units at budget 50, since it costs 200/unit) and exclude B and C from the candidate
    graph entirely - no DFS depth could ever reach C, regardless of leg order. Ranking at
    unlimited budget still finds this edge worth including as a candidate; the real
    budget (compounded from leg 1's profit) is what actually gates whether leg 2 can
    afford it, inside the search itself."""
    rows = [
        _row(1, 1, "Stileron", "A", price_buy=10, scu_buy=50),
        _row(1, 2, "Stileron", "B", price_sell=50, scu_sell=50),
        _row(2, 2, "Cobalt", "B", price_buy=200, scu_buy=10),
        _row(2, 3, "Cobalt", "C", price_sell=300, scu_sell=10),
    ]
    (route,) = build_multi_stop_routes(rows, ship_capacity_scu=50, budget=50)
    assert route.stops == (1, 2, 3)
    assert route.legs[0].investment == 50
    assert route.legs[1].investment == 200


def test_never_revisits_a_terminal_within_one_chain():
    """1 <-> 2 is profitable in both directions; without a visited-set, 1->2->1 would
    look like a valid 2-leg chain. It must never be produced."""
    rows = [
        _row(1, 1, "Stileron", "A", price_buy=100, scu_buy=10),
        _row(1, 2, "Stileron", "B", price_sell=150, scu_sell=10),
        _row(2, 2, "Cobalt", "B", price_buy=50, scu_buy=10),
        _row(2, 1, "Cobalt", "A", price_sell=90, scu_sell=10),
    ]
    assert build_multi_stop_routes(rows, ship_capacity_scu=10) == []


def test_a_single_profitable_hop_alone_is_excluded():
    """A plain 2-terminal hop is /mixed-routes' job, not this command's."""
    rows = [
        _row(1, 1, "Stileron", "Origin", price_buy=100, scu_buy=10),
        _row(1, 2, "Stileron", "Destination", price_sell=150, scu_sell=10),
    ]
    assert build_multi_stop_routes(rows, ship_capacity_scu=10) == []


def test_system_filter_excludes_a_chain_when_a_middle_terminal_is_out_of_system():
    rows = [
        _row(1, 1, "Stileron", "Origin", price_buy=100, scu_buy=10, star_system_name="Pyro"),
        _row(1, 2, "Stileron", "Midpoint", price_sell=150, scu_sell=10, star_system_name="Stanton"),
        _row(2, 2, "Cobalt", "Midpoint", price_buy=50, scu_buy=10, star_system_name="Stanton"),
        _row(2, 3, "Cobalt", "Final", price_sell=90, scu_sell=10, star_system_name="Pyro"),
    ]
    assert build_multi_stop_routes(rows, ship_capacity_scu=10)
    assert build_multi_stop_routes(rows, ship_capacity_scu=10, system="Pyro") == []


def test_multiple_chains_rank_by_total_profit_descending():
    rows = [
        _row(1, 1, "Stileron", "Origin", price_buy=100, scu_buy=10),
        _row(1, 2, "Stileron", "Midpoint", price_sell=150, scu_sell=10),
        _row(2, 2, "Cobalt", "Midpoint", price_buy=50, scu_buy=10),
        _row(2, 3, "Cobalt", "Final", price_sell=90, scu_sell=10),
        _row(3, 1, "Diamond", "Origin", price_buy=20, scu_buy=10),
        _row(3, 4, "Diamond", "OtherMid", price_sell=30, scu_sell=10),
        _row(4, 4, "Quartz", "OtherMid", price_buy=10, scu_buy=10),
        _row(4, 5, "Quartz", "OtherFinal", price_sell=15, scu_sell=10),
    ]
    routes = build_multi_stop_routes(rows, ship_capacity_scu=10)
    assert [route.stops for route in routes] == [(1, 2, 3), (1, 4, 5)]


def test_a_real_budget_ranking_keeps_an_affordable_chain_from_being_crowded_out():
    """Regression: candidate terminals used to be ranked by profit at *unlimited* budget
    only (to keep budget-compounded-but-later-affordable edges reachable - see the other
    test above). But ranking purely on unlimited-budget profit lets edges that would need
    far more capital than any realistic chain could ever compound to dominate the bounded
    candidate window, crowding out a chain that's genuinely affordable right now. 21
    decoy edges, each needing 100,000 aUEC/unit (utterly unaffordable at budget 100) but
    scoring enormous at unlimited budget, must not be able to exclude a real, valid,
    budget=100-affordable 2-leg chain from the search entirely."""
    rows = []
    next_id = 100
    for i in range(21):
        origin_id, destination_id = next_id, next_id + 1
        next_id += 2
        rows.append(_row(1000 + i, origin_id, f"Decoy{i}", f"T{origin_id}", price_buy=100000, scu_buy=1000))
        rows.append(_row(1000 + i, destination_id, f"Decoy{i}", f"T{destination_id}", price_sell=200000, scu_sell=1000))
    rows += [
        _row(1, 1, "A", "Origin", price_buy=10, scu_buy=10),
        _row(1, 2, "A", "Midpoint", price_sell=30, scu_sell=10),
        _row(2, 2, "B", "Midpoint", price_buy=5, scu_buy=10),
        _row(2, 3, "B", "Final", price_sell=8, scu_sell=10),
    ]
    routes = build_multi_stop_routes(rows, ship_capacity_scu=10, budget=100)
    assert any(route.stops == (1, 2, 3) for route in routes)


def test_route_is_exact_reflects_the_least_exact_leg():
    """Regression: the exactness disclosure previously lived only in the cog, and only
    checked ship capacity against EXACT_SEARCH_MAX_CAPACITY - MultiStopRoute now carries
    its own is_exact (all(leg.is_exact for leg in legs), see allocate_pair_cargo's
    docstring), so a chain is only as exact as its least-exact leg."""
    rows = [
        _row(1, 1, "Stileron", "Origin", price_buy=100, scu_buy=1000),
        _row(1, 2, "Stileron", "Midpoint", price_sell=150, scu_sell=1000),
        _row(2, 2, "Cobalt", "Midpoint", price_buy=50, scu_buy=1000),
        _row(2, 3, "Cobalt", "Final", price_sell=90, scu_sell=1000),
    ]
    (exact_route,) = build_multi_stop_routes(rows, ship_capacity_scu=10)
    assert exact_route.is_exact is True
    (approximate_route,) = build_multi_stop_routes(rows, ship_capacity_scu=30)
    assert approximate_route.is_exact is False


def test_exploration_visits_the_most_promising_edges_first_not_insertion_order(monkeypatch):
    """Regression: on the real collected market snapshot, a 24-SCU ship with a
    100,000-aUEC budget found only a 323,124-profit chain while a valid 426,056-profit
    chain existed - the shared MAX_CHAINS_EXPLORED budget was being consumed in whatever
    order the opportunities dict happened to iterate in, not by how promising each edge
    actually was. 20 decoy first-legs from the same starting terminal (each a dead end,
    inserted before the real chain so insertion order is stacked against it) must not be
    able to exhaust a small exploration budget before the one edge that actually leads
    to a valid, far more profitable chain ever gets tried."""
    monkeypatch.setattr(multi_stop_routes, "MAX_CHAINS_EXPLORED", 5)
    rows = []
    for i in range(20):
        rows.append(_row(100 + i, 1, f"Decoy{i}", "Start", price_buy=10, scu_buy=10))
        rows.append(_row(100 + i, 3 + i, f"Decoy{i}", f"DeadEnd{i}", price_sell=11, scu_sell=10))
    rows += [
        _row(1, 1, "Good", "Start", price_buy=10, scu_buy=10),
        _row(1, 2, "Good", "Midpoint", price_sell=1010, scu_sell=10),
        _row(2, 2, "Good2", "Midpoint", price_buy=10, scu_buy=10),
        _row(2, 999, "Good2", "Final", price_sell=1010, scu_sell=10),
    ]
    routes = build_multi_stop_routes(rows, ship_capacity_scu=10, limit=5)
    assert any(route.stops == (1, 2, 999) for route in routes)
