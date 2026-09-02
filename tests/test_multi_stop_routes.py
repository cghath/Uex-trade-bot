"""Tests for multi-stop (2-3 leg) trade chain building."""
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
