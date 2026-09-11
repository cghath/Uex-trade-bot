"""Multi-stop trade chains: origin -> stop -> stop -> ... -> destination, one-way.

Each leg is an independent mixed-commodity hop (see bot/uex/mixed_routes.py) - cargo is
fully sold before the next leg's purchase, so ship capacity resets every leg, but
available trading capital compounds: a profitable leg hands its revenue forward as the
next leg's budget. Routes rank by total profit; real per-leg distance
(bot/uex/client.py: get_terminal_distance) is attached later by the cog for context only,
not for ranking - this module stays free of I/O, matching every other bot/uex/*.py helper.
"""
from __future__ import annotations

from dataclasses import dataclass
import math
from typing import Any

from bot.uex.mixed_routes import (
    MixedCargoItem,
    allocate_pair_cargo,
    allocation_is_exact,
    build_pair_opportunities,
)

MAX_LEGS = 3
MAX_CANDIDATE_EDGES = 20
# Empirically checked against the real collected market snapshot (2593 rows): the
# resulting candidate graph (~30 terminals, ~150 edges among them) needs on the order of
# 20,000 edge-considerations to exhaust itself and find its true best chain, with
# measured search time staying flat (~0.3s) even at 100x that - the graph's own size
# bounds the real work regardless of how high this ceiling is set. 50,000 leaves
# comfortable headroom above what real data needed while still being a genuine, finite
# safety valve against a pathological input.
MAX_CHAINS_EXPLORED = 50000


@dataclass(frozen=True)
class MultiStopLeg:
    origin_id: int
    origin_name: str
    destination_id: int
    destination_name: str
    cargo: tuple[MixedCargoItem, ...]
    investment: float
    revenue: float
    profit: float
    is_exact: bool


@dataclass(frozen=True)
class MultiStopRoute:
    legs: tuple[MultiStopLeg, ...]
    investment: float
    revenue: float
    profit: float

    @property
    def stops(self) -> tuple[int, ...]:
        return (self.legs[0].origin_id, *(leg.destination_id for leg in self.legs))

    @property
    def roi_pct(self) -> float:
        return 0.0 if self.investment <= 0 else self.profit / self.investment * 100

    @property
    def is_exact(self) -> bool:
        """False if any leg's cargo allocation is only the two-ordering/capped-exact
        approximation (see allocate_pair_cargo), not a proven optimum for its real
        capacity - a chain is only as exact as its least-exact leg."""
        return all(leg.is_exact for leg in self.legs)


def build_multi_stop_routes(
    market_rows: list[dict[str, Any]],
    *,
    ship_capacity_scu: float,
    budget: float | None = None,
    limit: int = 5,
    max_commodities: int = 3,
    space_only: bool = False,
    capital_access_only: bool = False,
    auto_load_only: bool = False,
    system: str | None = None,
    start_terminal_id: int | None = None,
) -> list[MultiStopRoute]:
    """Return the best-profit chains of 2-3 profitable legs (MAX_LEGS).

    A 1-leg result is excluded - that is what /mixed-routes already returns, and this
    command exists specifically for chains longer than a single hop. Candidate terminals
    are bounded to the endpoints of the ~MAX_CANDIDATE_EDGES most profitable single legs,
    so the chain search stays a small in-memory graph walk over the same snapshot
    /mixed-routes already reads, with no extra API calls and no scan of every terminal.

    start_terminal_id restricts every returned chain to start at that one terminal
    (/route-from-multi's "from wherever I am" anchor) instead of searching from every
    profit-ranked candidate origin. The requested terminal, and every terminal reachable
    from it within MAX_LEGS real hops, is force-added to the candidate set regardless of
    global profit ranking - the profit-ranked candidate window is built assuming the
    search can start ANYWHERE, so a real chain anchored at a lower-ranked terminal would
    otherwise be invisible (or silently truncated partway through) to a location-anchored
    search, even though it's the only origin this call actually cares about.
    """
    capacity = math.floor(float(ship_capacity_scu or 0))
    if capacity <= 0 or limit <= 0:
        return []
    capital = math.inf if budget is None else max(0.0, float(budget))

    opportunities = build_pair_opportunities(
        market_rows,
        space_only=space_only,
        capital_access_only=capital_access_only,
        auto_load_only=auto_load_only,
        system=system,
    )
    if not opportunities:
        return []

    # Terminal names come straight off the raw rows, not allocated cargo, so every
    # opportunity has an entry here regardless of whether it can afford cargo at the
    # *original* budget - a later, budget-compounded leg over the same edge still needs
    # a name to display even if this edge looked unaffordable at the starting budget.
    edge_terminals: dict[tuple[int, int], dict[str, str]] = {
        key: {
            "origin_name": str(pairs[0][0].get("terminal_name") or "Unknown"),
            "destination_name": str(pairs[0][1].get("terminal_name") or "Unknown"),
        }
        for key, pairs in opportunities.items()
    }

    # Rank candidate edges at a SPREAD of budget checkpoints and take the union, not just
    # "unlimited" + "exactly what the caller asked for" - two checkpoints alone still
    # leaves a real blind spot: once a checkpoint budget is large enough that every edge's
    # own allocation is already capped by real stock/demand/cargo space rather than by
    # that budget, its ranking becomes IDENTICAL to the unlimited one (confirmed on real
    # collected data: the requested-budget ranking at 10M had 20/20 overlap with the
    # unlimited ranking, vs only 4/20 at 5M) - permanently excluding any edge that only
    # ranks well at a MODERATE budget, even when it leads to a strictly better route.
    # Reproduced concretely: for one real ship/market snapshot, budget=5,000,000 found a
    # route with BOTH higher profit and higher ROI (3.03M profit, 79.8% ROI) than either
    # budget=10,000,000 or no budget at all (2.86M profit, 32.6% ROI) - not because 5M was
    # "diminishing returns done right" and the others weren't, but because the 10M/
    # unlimited candidate window had already collapsed and never considered the terminals
    # the better 5M-anchored route needed.
    #
    # The DFS itself is unaffected either way - it always uses the real, path-dependent
    # remaining_budget for every allocation; this only changes which terminals are
    # *eligible* to be searched.
    def rank_edges(budget: float) -> list[tuple[float, float, tuple[int, int]]]:
        """Returns (profit, investment, key) tuples, profit-descending."""
        ranked: list[tuple[float, float, tuple[int, int]]] = []
        for key, pairs in opportunities.items():
            cargo = allocate_pair_cargo(pairs, capacity=capacity, budget=budget, max_commodities=max_commodities)
            if cargo:
                ranked.append((
                    sum(item.profit for item in cargo),
                    sum(item.investment for item in cargo),
                    key,
                ))
        ranked.sort(key=lambda entry: entry[0], reverse=True)
        return ranked

    unlimited_ranking = rank_edges(math.inf)

    # With a real budget, anchor checkpoints to fractions of it - matches the caller's
    # actual situation. With no budget at all (capital is math.inf), there's no real
    # number to take fractions of, so use the most capital-hungry top edge's OWN
    # saturation investment (from the unlimited ranking, already computed above) as a
    # data-derived stand-in ceiling instead of skipping straight to "only ever check
    # unlimited" - which is exactly the failure mode this fixes. Adapts to whatever the
    # current game economy actually supports rather than a hardcoded aUEC constant that
    # could go stale as prices change.
    if math.isinf(capital):
        ranking_ceiling = max(
            (investment for _, investment, _ in unlimited_ranking[:MAX_CANDIDATE_EDGES]),
            default=0.0,
        )
    else:
        ranking_ceiling = capital

    rankings = [unlimited_ranking]
    if ranking_ceiling > 0:
        for fraction in (0.1, 0.25, 0.5, 1.0):
            rankings.append(rank_edges(ranking_ceiling * fraction))

    # A ceiling derived from the dominant top edges' own saturation investment (the
    # math.isinf(capital) branch above) is self-referential: fractions of "however much
    # the CURRENTLY-DOMINANT edges can absorb" still favor those same edges, just in
    # smaller quantities - it can never fully exclude them the way a genuinely
    # independent budget figure can, so a tiny-but-highly-efficient edge can stay
    # invisible at every one of those fractions too (confirmed by a regression test).
    # Profit-per-aUEC-invested sidesteps this: it's a scale-invariant efficiency signal,
    # not tied to any absolute dollar checkpoint, so a small-scale-but-highly-efficient
    # edge ranks well regardless of how much capital the dominant edges can absorb. Reuses
    # unlimited_ranking's own (profit, investment) pairs - no extra allocate_pair_cargo
    # calls needed.
    efficiency_ranking = sorted(
        ((profit / investment, key) for profit, investment, key in unlimited_ranking if investment > 0),
        key=lambda entry: entry[0],
        reverse=True,
    )

    candidate_terminals: set[int] = set()
    for ranked_edges in rankings:
        for _, _, (origin_id, destination_id) in ranked_edges[:MAX_CANDIDATE_EDGES]:
            candidate_terminals.add(origin_id)
            candidate_terminals.add(destination_id)
    for _, (origin_id, destination_id) in efficiency_ranking[:MAX_CANDIDATE_EDGES]:
        candidate_terminals.add(origin_id)
        candidate_terminals.add(destination_id)

    if start_terminal_id is not None:
        # Force in every terminal genuinely reachable from the anchor within MAX_LEGS
        # real hops - not just the ones that happened to rank in the top
        # MAX_CANDIDATE_EDGES globally. A full 3-leg chain needs its 2nd and 3rd stop to
        # be candidates too, not just the 1st - a single-hop-only version of this fix
        # still let an unrelated cluster of decoy edges elsewhere in the data crowd the
        # anchor's own genuine 2nd-leg terminal out of the candidate set entirely,
        # silently truncating an anchored search down to at most a 1-leg reach (which
        # then produces nothing at all, since a 1-leg chain is excluded from results).
        # Bounded to the anchor's own local neighborhood (BFS over real opportunities,
        # not the full terminal set), so this stays small regardless of how large the
        # overall market snapshot is.
        reachable = {start_terminal_id}
        frontier = {start_terminal_id}
        for _ in range(MAX_LEGS):
            next_frontier = {
                destination_id
                for origin_id, destination_id in opportunities
                if origin_id in frontier and destination_id not in reachable
            }
            if not next_frontier:
                break
            reachable |= next_frontier
            frontier = next_frontier
        candidate_terminals |= reachable

    # Used only to *order* exploration below, not to filter it - an edge's real
    # per-leg profit is still recomputed against the real, path-dependent budget inside
    # extend() every time.
    edge_profit_potential = {key: profit for profit, _, key in unlimited_ranking}

    graph: dict[int, list[int]] = {}
    for origin_id, destination_id in opportunities:
        if origin_id in candidate_terminals and destination_id in candidate_terminals:
            graph.setdefault(origin_id, []).append(destination_id)
    # A bounded exploration budget (MAX_CHAINS_EXPLORED) shouldn't be spent in whatever
    # arbitrary order the opportunities dict happened to iterate in - order each node's
    # outgoing edges, and which terminal to start from, by profit potential descending,
    # so the most promising branches are explored first and a truncated search still
    # finds a near-best result rather than an arbitrary one.
    for origin_id, destinations in graph.items():
        destinations.sort(key=lambda d: edge_profit_potential.get((origin_id, d), 0.0), reverse=True)
    ordered_starts = sorted(
        candidate_terminals,
        key=lambda t: max((edge_profit_potential.get((t, d), 0.0) for d in graph.get(t, [])), default=0.0),
        reverse=True,
    )
    if start_terminal_id is not None:
        # Only explore from the requested anchor - every OTHER candidate terminal stays
        # searchable as a downstream (2nd/3rd leg) stop via the graph above, it just never
        # starts a chain of its own.
        ordered_starts = [start_terminal_id] if start_terminal_id in candidate_terminals else []

    routes: list[MultiStopRoute] = []
    explored = 0

    def extend(
        current: int,
        visited: frozenset[int],
        legs: tuple[MultiStopLeg, ...],
        remaining_budget: float,
    ) -> None:
        nonlocal explored
        if len(legs) >= 2:
            # Legs are self-funding in sequence (leg N's purchase is capped by the cash
            # actually on hand after leg N-1 sells - see remaining_budget below), so
            # summing each leg's own investment/revenue double-counts money recycled
            # through the chain. The real starting capital required is the deepest cash
            # deficit reached before enough revenue has come back in to cover it.
            running_balance = 0.0
            min_balance = 0.0
            for leg in legs:
                running_balance -= leg.investment
                min_balance = min(min_balance, running_balance)
                running_balance += leg.revenue
            starting_capital = -min_balance
            profit = sum(leg.profit for leg in legs)
            routes.append(
                MultiStopRoute(
                    legs=legs,
                    investment=starting_capital,
                    revenue=starting_capital + profit,
                    profit=profit,
                )
            )
        if len(legs) >= MAX_LEGS:
            return
        for next_stop in graph.get(current, []):
            if next_stop in visited or explored >= MAX_CHAINS_EXPLORED:
                continue
            explored += 1
            pairs = opportunities.get((current, next_stop))
            if not pairs:
                continue
            cargo = allocate_pair_cargo(
                pairs, capacity=capacity, budget=remaining_budget, max_commodities=max_commodities
            )
            if not cargo:
                continue
            names = edge_terminals[(current, next_stop)]
            leg_investment = sum(item.investment for item in cargo)
            leg_profit = sum(item.profit for item in cargo)
            leg_revenue = leg_investment + leg_profit
            leg = MultiStopLeg(
                origin_id=current,
                origin_name=names["origin_name"],
                destination_id=next_stop,
                destination_name=names["destination_name"],
                cargo=tuple(cargo),
                investment=leg_investment,
                revenue=leg_revenue,
                profit=leg_profit,
                is_exact=allocation_is_exact(num_pairs=len(pairs), capacity=capacity),
            )
            next_budget = (
                remaining_budget if math.isinf(remaining_budget)
                else remaining_budget - leg_investment + leg_revenue
            )
            extend(next_stop, visited | {next_stop}, (*legs, leg), next_budget)

    for start in ordered_starts:
        extend(start, frozenset({start}), (), capital)

    routes.sort(key=lambda route: (route.profit, route.roi_pct), reverse=True)
    return routes[:limit]


@dataclass(frozen=True)
class BudgetCurvePoint:
    budget: float
    profit: float
    investment: float
    roi_pct: float
    stops: tuple[int, ...]


def sweep_budget_curve(
    market_rows: list[dict[str, Any]],
    *,
    ship_capacity_scu: float,
    space_only: bool = False,
    capital_access_only: bool = False,
    auto_load_only: bool = False,
    system: str | None = None,
    starting_budget: float = 5_000,
    growth_factor: float = 3.0,
    max_points: int = 12,
) -> list[BudgetCurvePoint]:
    """Sweep starting budget from small to large, tracking the best multi-stop chain's
    profit/investment/ROI at each step - answers "where does more capital stop helping?"
    for a specific ship against the current market snapshot.

    Stops early once a swept budget produces the byte-for-byte identical best chain
    (same profit and investment, rounded) as the previous one AND that budget already
    covers buying a FULL SHIP-CAPACITY quantity of the most expensive single buy
    opportunity anywhere in the data. Adjacent equality alone is not proof of
    saturation: two consecutive budgets can both be too poor to reach a pricier chain
    that only unlocks further out, and would otherwise look identical purely because
    neither could afford it yet (confirmed: a sweep that stopped at two matching-but-
    still-poor points missed a chain worth 100x more, reachable only a couple of
    geometric steps further). A one-unit affordability floor isn't enough either - a
    tie can appear the instant a pricier opportunity becomes affordable for exactly
    ONE unit, while buying MORE of it (still well within real stock/demand and ship
    capacity) keeps improving for several more geometric steps (confirmed: a
    synthetic pricier chain ties a cheap chain's profit the moment its own one-unit
    price is first affordable, then goes on to beat it substantially once more budget
    lets it buy multiple units). Only once the budget could afford filling the ship's
    ENTIRE cargo hold with the priciest known opportunity is a repeated signature a
    real plateau - no larger budget could ever let any single buy go further than a
    full hold of it, so real stock/demand/cargo capacity is what's binding from there
    on, not budget. Geometric growth (3x by default) covers a wide range of ship sizes
    in a bounded number of build_multi_stop_routes calls, each of which can itself
    take real wall-clock time (candidate-ranking runs multiple passes - see that
    function's own docstring) - this is deliberately capped at max_points rather than
    run unbounded (a market with an expensive enough opportunity can still exhaust
    every point without ever reaching this floor - the curve just keeps climbing
    instead of falsely reporting a plateau), and is meant to be called from a worker
    thread, not the event loop.
    """
    known_buy_prices = [float(row["price_buy"]) for row in market_rows if (row.get("price_buy") or 0) > 0]
    affordability_floor = max(known_buy_prices, default=0.0) * ship_capacity_scu

    points: list[BudgetCurvePoint] = []
    budget = starting_budget
    previous_signature: tuple[float, float] | None = None
    for _ in range(max_points):
        routes = build_multi_stop_routes(
            market_rows,
            ship_capacity_scu=ship_capacity_scu,
            budget=budget,
            limit=1,
            space_only=space_only,
            capital_access_only=capital_access_only,
            auto_load_only=auto_load_only,
            system=system,
        )
        best = routes[0] if routes else None
        if best is None:
            points.append(BudgetCurvePoint(budget=budget, profit=0.0, investment=0.0, roi_pct=0.0, stops=()))
            previous_signature = None
        else:
            signature = (round(best.profit, 2), round(best.investment, 2))
            points.append(
                BudgetCurvePoint(
                    budget=budget,
                    profit=best.profit,
                    investment=best.investment,
                    roi_pct=best.roi_pct,
                    stops=best.stops,
                )
            )
            if signature == previous_signature and budget >= affordability_floor:
                break
            previous_signature = signature
        budget *= growth_factor
    return _enforce_monotonic_profit(points)


def _enforce_monotonic_profit(points: list[BudgetCurvePoint]) -> list[BudgetCurvePoint]:
    """A larger starting budget can never make the true best achievable profit go down -
    you can always choose not to spend the extra capital, so whatever a smaller budget
    already achieved remains achievable at any larger one too. build_multi_stop_routes'
    candidate-terminal ranking is a bounded heuristic, though (see its own docstring on
    the budget-checkpoint/efficiency rankings) - each sweep point ranks candidates
    independently for its own specific budget, and occasionally the fractions checked at
    one budget miss a combination that a smaller budget's own fractions happened to find
    (confirmed on real collected data: one ship's sweep found LOWER profit at 10,935,000
    than it had already found at 3,645,000). When that happens, this reports the
    best-so-far result instead of a visibly-impossible dip - not hiding a bug, but
    reflecting the economic fact that the smaller budget's own already-found result is
    still valid at the larger budget too.
    """
    best_so_far: BudgetCurvePoint | None = None
    adjusted: list[BudgetCurvePoint] = []
    for point in points:
        if best_so_far is None or point.profit > best_so_far.profit:
            best_so_far = point
            adjusted.append(point)
        else:
            adjusted.append(
                BudgetCurvePoint(
                    budget=point.budget,
                    profit=best_so_far.profit,
                    investment=best_so_far.investment,
                    roi_pct=best_so_far.roi_pct,
                    stops=best_so_far.stops,
                )
            )
    return adjusted


def find_diminishing_returns_budget(points: list[BudgetCurvePoint]) -> float | None:
    """The smallest swept budget whose result already matches the LARGEST swept budget's
    result - i.e. the point beyond which more capital stopped changing the recommendation,
    within the range that was actually swept. The final point always matches itself, which
    is not evidence of a plateau by itself - a strictly-still-rising curve would trivially
    "match" only its own last point and report that budget as the supposed plateau start,
    a false saturation claim for a curve that was still improving right up to the edge of
    what was swept. Requires the final signature to repeat at least once BEFORE the last
    point too; otherwise this is "still improving, not established in this range," which
    is None here, same as a curve that never plateaus at all."""
    if len(points) < 2:
        return None
    final_signature = (round(points[-1].profit, 2), round(points[-1].investment, 2))
    matches = [point for point in points if (round(point.profit, 2), round(point.investment, 2)) == final_signature]
    if len(matches) < 2:
        return None
    return matches[0].budget
