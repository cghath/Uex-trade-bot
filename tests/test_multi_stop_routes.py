"""Tests for multi-stop (2-3 leg) trade chain building."""
import bot.uex.multi_stop_routes as multi_stop_routes
from bot.uex.multi_stop_routes import (
    MultiStopLeg,
    MultiStopRoute,
    build_multi_stop_routes,
    find_diminishing_returns_budget,
    sweep_budget_curve,
)


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


def test_start_terminal_id_restricts_chains_to_that_one_origin():
    """/route-from-multi's anchor: two independent chain families exist (starting at
    terminal 1 and terminal 10), 1's is more profitable overall - without the anchor it
    would rank first. With start_terminal_id=10, only chains starting at 10 come back,
    even though they're the LESS profitable family."""
    rows = [
        _row(1, 1, "Stileron", "A", price_buy=100, scu_buy=10),
        _row(1, 2, "Stileron", "B", price_sell=150, scu_sell=10),
        _row(2, 2, "Cobalt", "B", price_buy=50, scu_buy=10),
        _row(2, 3, "Cobalt", "C", price_sell=90, scu_sell=10),
        _row(3, 10, "Diamond", "X", price_buy=20, scu_buy=10),
        _row(3, 11, "Diamond", "Y", price_sell=30, scu_sell=10),
        _row(4, 11, "Quartz", "Y", price_buy=10, scu_buy=10),
        _row(4, 12, "Quartz", "Z", price_sell=15, scu_sell=10),
    ]
    unrestricted = build_multi_stop_routes(rows, ship_capacity_scu=10)
    assert unrestricted[0].stops == (1, 2, 3), "sanity check: the more profitable family ranks first normally"

    anchored = build_multi_stop_routes(rows, ship_capacity_scu=10, start_terminal_id=10)
    assert anchored
    assert all(route.stops[0] == 10 for route in anchored)
    assert all(1 not in route.stops for route in anchored)


def test_start_terminal_id_finds_a_chain_even_when_not_globally_top_ranked():
    """The anchor terminal's own first-hop opportunities must be searchable even if they
    never rank in the top MAX_CANDIDATE_EDGES globally - otherwise a location-anchored
    search could come back empty purely because other terminals looked more profitable
    overall, defeating the entire point of 'best routes starting from HERE.'"""
    # 25 decoy 1-leg opportunities (irrelevant on their own, excluded as single hops, but
    # their endpoints crowd the top-ranked candidate-terminal window) all far more
    # profitable than the anchor's own modest, genuinely valid 2-leg chain.
    decoys = []
    for i in range(25):
        origin, destination = 1000 + i * 2, 1001 + i * 2
        decoys += [
            _row(100 + i, origin, f"Decoy{i}", f"D{origin}", price_buy=10, scu_buy=10),
            _row(100 + i, destination, f"Decoy{i}", f"D{destination}", price_sell=10_000, scu_sell=10),
        ]
    anchor_chain = [
        _row(1, 500, "Stileron", "Anchor", price_buy=100, scu_buy=10),
        _row(1, 501, "Stileron", "Mid", price_sell=110, scu_sell=10),
        _row(2, 501, "Cobalt", "Mid", price_buy=50, scu_buy=10),
        _row(2, 502, "Cobalt", "Final", price_sell=60, scu_sell=10),
    ]
    rows = decoys + anchor_chain

    routes = build_multi_stop_routes(rows, ship_capacity_scu=10, start_terminal_id=500)
    assert routes, "the anchor's own valid chain must still be found"
    assert routes[0].stops == (500, 501, 502)


def test_start_terminal_id_with_no_real_opportunities_returns_empty():
    rows = [
        _row(1, 1, "Stileron", "Origin", price_buy=100, scu_buy=10),
        _row(1, 2, "Stileron", "Destination", price_sell=150, scu_sell=10),
    ]
    assert build_multi_stop_routes(rows, ship_capacity_scu=10, start_terminal_id=999) == []


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


def test_an_intermediate_budget_checkpoint_keeps_a_chain_from_being_crowded_out_everywhere():
    """Regression: ranking candidate edges at only "unlimited" + "exactly the requested
    budget" still leaves a real blind spot. Confirmed on a live route recommendation: for
    one real ship/market snapshot, budget=5,000,000 found a chain with BOTH higher profit
    and higher ROI than budget=10,000,000 or no budget at all - not because 5M hit a real
    economic sweet spot, but because by 10M/unlimited, every edge's own allocation was
    already capped by real stock/demand rather than by budget, so the "requested budget"
    ranking had collapsed to be IDENTICAL to the unlimited one (confirmed 20/20 overlap),
    permanently excluding the terminals the better 5M-anchored chain needed.

    Reproduced synthetically here: 21 decoy edges each need 50,000 aUEC/unit - completely
    unaffordable (0 units) at a tenth of the requested budget, but hugely profitable at
    BOTH the full requested budget and unlimited budget. A real, valid 2-leg chain scores
    a tiny profit by comparison, but is the ONLY thing rankable at that smaller
    checkpoint (decoys score zero there) - it must not be excluded from the candidate
    window just because it's dominated at every checkpoint the old two-ranking scheme
    actually checked.
    """
    rows = []
    next_id = 100
    for i in range(21):
        origin_id, destination_id = next_id, next_id + 1
        next_id += 2
        rows.append(_row(1000 + i, origin_id, f"Decoy{i}", f"T{origin_id}", price_buy=50000, scu_buy=1000))
        rows.append(_row(1000 + i, destination_id, f"Decoy{i}", f"T{destination_id}", price_sell=100000, scu_sell=1000))
    rows += [
        _row(1, 1, "A", "Origin", price_buy=10, scu_buy=10),
        _row(1, 2, "A", "Midpoint", price_sell=30, scu_sell=10),
        _row(2, 2, "B", "Midpoint", price_buy=5, scu_buy=10),
        _row(2, 3, "B", "Final", price_sell=8, scu_sell=10),
    ]
    routes = build_multi_stop_routes(rows, ship_capacity_scu=10, budget=100_000)
    assert any(route.stops == (1, 2, 3) for route in routes)


def test_no_budget_specified_still_finds_a_chain_only_visible_via_efficiency_ranking():
    """Same failure mode as above, but for the 'no budget given at all' path - which used
    to skip straight to ranking purely at unlimited budget, the single worst case for
    this blind spot (confirmed live: the no-budget recommendation for a real ship was
    strictly worse, on both profit and ROI, than the same search run with an explicit
    moderate budget). This case specifically defeats the budget-checkpoint fix above: with
    no real budget to anchor fractions to, the derived ceiling comes from the DOMINANT
    (decoy) edges' own saturation investment, which is self-referential - a fraction of a
    decoy's own investment still buys a fractional unit of that same decoy (its economics
    scale linearly), so decoys are never excluded by any fraction of their own ceiling.
    Both real edges here have a better profit-per-aUEC-invested ratio (2.0) than the
    decoys (1.0), so the separate efficiency ranking (not tied to any absolute dollar
    checkpoint) is what actually has to surface them.
    """
    rows = []
    next_id = 100
    for i in range(21):
        origin_id, destination_id = next_id, next_id + 1
        next_id += 2
        rows.append(_row(1000 + i, origin_id, f"Decoy{i}", f"T{origin_id}", price_buy=50000, scu_buy=1000))
        rows.append(_row(1000 + i, destination_id, f"Decoy{i}", f"T{destination_id}", price_sell=100000, scu_sell=1000))
    rows += [
        _row(1, 1, "A", "Origin", price_buy=10, scu_buy=10),
        _row(1, 2, "A", "Midpoint", price_sell=30, scu_sell=10),
        _row(2, 2, "B", "Midpoint", price_buy=5, scu_buy=10),
        _row(2, 3, "B", "Final", price_sell=15, scu_sell=10),
    ]
    routes = build_multi_stop_routes(rows, ship_capacity_scu=10)
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


# -- budget-curve sweep: where does more capital stop helping? -------------------------


def _stock_limited_chain_rows(stock=20):
    """A 2-leg chain whose stock caps out fast, so a budget sweep should plateau quickly
    once the swept budget exceeds what real stock/demand can absorb."""
    return [
        _row(1, 1, "Stileron", "A", price_buy=10, scu_buy=stock),
        _row(1, 2, "Stileron", "B", price_sell=50, scu_sell=stock),
        _row(2, 2, "Cobalt", "B", price_buy=20, scu_buy=stock),
        _row(2, 3, "Cobalt", "C", price_sell=90, scu_sell=stock),
    ]


def test_sweep_budget_curve_plateaus_once_stock_is_saturated():
    rows = _stock_limited_chain_rows(stock=20)
    points = sweep_budget_curve(rows, ship_capacity_scu=20, starting_budget=100, growth_factor=3.0)
    # The chain needs at most 20*10 = 200 aUEC for leg 1 - well before the sweep reaches
    # anything enormous, profit/investment must stop changing.
    assert points[-2].profit == points[-1].profit
    assert points[-2].investment == points[-1].investment
    # Must not have swept all the way to max_points (12) - the whole point of early exit.
    assert len(points) < 12


def test_sweep_budget_curve_profit_never_decreases_as_budget_grows():
    rows = _stock_limited_chain_rows(stock=50)
    points = sweep_budget_curve(rows, ship_capacity_scu=50, starting_budget=50, growth_factor=2.0)
    for earlier, later in zip(points, points[1:]):
        assert later.profit >= earlier.profit - 1e-9, (earlier, later)


def test_sweep_budget_curve_reports_best_so_far_when_a_larger_budget_finds_less(monkeypatch):
    """build_multi_stop_routes' candidate ranking is a bounded heuristic - each sweep
    point ranks candidates independently for its own specific budget, and confirmed on
    real collected data, a LARGER budget's own fraction checkpoints can occasionally miss
    a combination that a SMALLER budget's checkpoints already found (one real ship's
    sweep found less profit at 10,935,000 than it had already found at 3,645,000). Since
    a bigger budget can never truly make the best ACHIEVABLE profit go down (you can
    always choose not to spend the extra capital), the sweep must report the
    best-so-far result instead of a visibly-impossible dip."""
    def fake_route(profit, investment):
        leg = MultiStopLeg(1, "A", 2, "B", (), investment, investment + profit, profit, True)
        return MultiStopRoute(legs=(leg,), investment=investment, revenue=investment + profit, profit=profit)

    fake_routes_by_budget = {
        100: [fake_route(500, 100)],
        300: [fake_route(1000, 300)],
        900: [fake_route(700, 900)],  # a real dip: less profit at a bigger budget
    }

    def fake_build(rows, *, budget, **kwargs):
        return fake_routes_by_budget.get(int(budget), [])

    monkeypatch.setattr(multi_stop_routes, "build_multi_stop_routes", fake_build)
    points = sweep_budget_curve(
        [], ship_capacity_scu=10, starting_budget=100, growth_factor=3.0, max_points=3
    )
    assert [p.budget for p in points] == [100, 300, 900]
    assert [p.profit for p in points] == [500, 1000, 1000]
    # The dipped point reports the SMALLER budget's own better result, not a fabricated
    # investment/ROI pair invented for the larger budget.
    assert points[2].investment == 300
    assert points[2].roi_pct == points[1].roi_pct


def test_sweep_budget_curve_does_not_declare_saturation_at_one_unit_affordability():
    """Real defect: the stopping heuristic used to treat a repeated signature as proof of
    real saturation as soon as the swept budget could afford ONE unit of the priciest
    known buy opportunity - but a pricier chain can keep improving for several more
    geometric steps once it can afford MULTIPLE units of it, still well within real
    stock/demand and ship capacity. Concrete counterexample: a cheap 2-hop chain (1
    SCU each leg) nets a fixed profit; a pricier chain (100 aUEC/unit, 100 SCU stock)
    first becomes affordable for exactly one unit at budget=135, tying the cheap
    chain's profit and (under the old floor, exactly the priciest known price) wrongly
    satisfying the stop condition right there - even though budget=405 already shows
    real further improvement from buying more than one unit."""
    rows = [
        _row(1, 1, "Cheap", "A", price_buy=1, scu_buy=1),
        _row(1, 2, "Cheap", "B", price_sell=3, scu_sell=1),
        _row(2, 2, "Cheap2", "B", price_buy=1, scu_buy=1),
        _row(2, 3, "Cheap2", "C", price_sell=3, scu_sell=1),
        _row(3, 4, "Expensive", "D", price_buy=100, scu_buy=100),
        _row(3, 5, "Expensive", "E", price_sell=102, scu_sell=100),
        _row(4, 5, "Expensive2", "E", price_buy=100, scu_buy=100),
        _row(4, 6, "Expensive2", "F", price_sell=102, scu_sell=100),
    ]
    points = sweep_budget_curve(
        rows, ship_capacity_scu=100, starting_budget=5, growth_factor=3.0, max_points=6
    )
    # Must not stop the instant the expensive chain affords exactly one unit (budget=135,
    # profit tied with the cheap chain at 4.0) - real improvement from buying more of it
    # is still available just one geometric step further (budget=405, profit=16.0).
    assert any(p.profit > 4.0 for p in points), points


def test_sweep_budget_curve_respects_max_points_when_never_plateauing():
    # A single commodity with effectively unlimited stock and no cargo-space cap growing
    # the ship never saturates within a reasonable sweep - must still stop at max_points,
    # not run forever.
    rows = [
        _row(1, 1, "Endless", "A", price_buy=10, scu_buy=10**9),
        _row(1, 2, "Endless", "B", price_sell=20, scu_sell=10**9),
        _row(2, 2, "Endless2", "B", price_buy=10, scu_buy=10**9),
        _row(2, 3, "Endless2", "C", price_sell=20, scu_sell=10**9),
    ]
    points = sweep_budget_curve(
        rows, ship_capacity_scu=10**9, starting_budget=100, growth_factor=2.0, max_points=5
    )
    assert len(points) == 5


def test_find_diminishing_returns_budget_identifies_the_plateau_start():
    rows = _stock_limited_chain_rows(stock=20)
    points = sweep_budget_curve(rows, ship_capacity_scu=20, starting_budget=50, growth_factor=2.0)
    plateau_budget = find_diminishing_returns_budget(points)
    assert plateau_budget is not None
    # Every point from the plateau budget onward must share the final profit/investment;
    # every point before it must not (otherwise an earlier, smaller budget would be the
    # true plateau start instead).
    final = (round(points[-1].profit, 2), round(points[-1].investment, 2))
    for point in points:
        signature = (round(point.profit, 2), round(point.investment, 2))
        if point.budget >= plateau_budget:
            assert signature == final
        else:
            assert signature != final


def test_find_diminishing_returns_budget_is_none_for_a_single_point():
    assert find_diminishing_returns_budget([]) is None
    rows = _stock_limited_chain_rows()
    points = sweep_budget_curve(rows, ship_capacity_scu=20, starting_budget=50, max_points=1)
    assert find_diminishing_returns_budget(points) is None


def test_budget_sweep_does_not_stop_before_a_pricier_chain_unlocks():
    """Real defect: two adjacent budgets that both happen to be too poor to afford a
    pricier chain look byte-for-byte identical, which the old early-stop treated as proof
    of saturation - stopping the sweep before a later, much more profitable chain ever had
    a chance to unlock. A repeated signature only proves saturation once the swept budget
    can afford at least one unit of every known buy opportunity in the data."""
    def pair(commodity, origin, destination, buy, sell, stock=1):
        common = dict(id_commodity=commodity, commodity_name=f"Item{commodity}", status_sell=1)
        return [
            _row(commodity, origin, common["commodity_name"], f"T{origin}", price_buy=buy, scu_buy=stock),
            _row(commodity, destination, common["commodity_name"], f"T{destination}", price_sell=sell, scu_sell=stock),
        ]

    rows = (
        pair(1, 1, 2, 1, 2) + pair(2, 2, 3, 1, 2)
        + pair(3, 4, 5, 100, 200) + pair(4, 5, 6, 100, 200)
    )
    points = sweep_budget_curve(rows, ship_capacity_scu=1, starting_budget=5, growth_factor=3, max_points=4)
    reachable = build_multi_stop_routes(rows, ship_capacity_scu=1, budget=135, limit=1)[0]
    assert points[-1].profit == reachable.profit, [(p.budget, p.profit) for p in points]


def test_find_diminishing_returns_budget_is_none_for_a_still_rising_curve():
    """Real defect: the final point in a sweep always trivially matches itself, so a
    strictly-still-improving curve (no plateau anywhere) reported its last swept budget as
    a false 'diminishing returns' point. A genuine plateau needs the final signature to
    repeat at least once BEFORE the last point, not just match itself."""
    from bot.uex.multi_stop_routes import BudgetCurvePoint

    points = [BudgetCurvePoint(budget=b, profit=b, investment=b, roi_pct=100, stops=(1, 2, 3)) for b in (5, 15, 45)]
    assert find_diminishing_returns_budget(points) is None
