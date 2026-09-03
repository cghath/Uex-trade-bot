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

from bot.uex.mixed_routes import MixedCargoItem, allocate_pair_cargo, build_pair_opportunities

MAX_LEGS = 3
MAX_CANDIDATE_EDGES = 20
MAX_CHAINS_EXPLORED = 2000


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
) -> list[MultiStopRoute]:
    """Return the best-profit chains of 2-3 profitable legs (MAX_LEGS).

    A 1-leg result is excluded - that is what /mixed-routes already returns, and this
    command exists specifically for chains longer than a single hop. Candidate terminals
    are bounded to the endpoints of the ~MAX_CANDIDATE_EDGES most profitable single legs,
    so the chain search stays a small in-memory graph walk over the same snapshot
    /mixed-routes already reads, with no extra API calls and no scan of every terminal.
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

    # Rank candidate edges TWICE and take the union - neither ranking alone is safe:
    # - At unlimited capital, so an edge that's unaffordable at the start but would
    #   become affordable once an earlier leg's profit compounds the running budget is
    #   still *reachable* by the DFS below (it's excluded later, correctly, only if the
    #   real path-dependent budget never actually gets there).
    # - At the caller's real starting budget, so edges that are immediately affordable
    #   right now can't be crowded out of the bounded top-MAX_CANDIDATE_EDGES window by
    #   edges that only look enormous assuming infinite capital nobody actually has -
    #   reproduced with 20+ such "expensive-at-infinite-budget" decoys pushing a
    #   genuinely affordable chain out of the candidate set entirely.
    # The DFS itself is unaffected either way - it always uses the real, path-dependent
    # remaining_budget for every allocation; this only changes which terminals are
    # *eligible* to be searched.
    def rank_edges(budget: float) -> list[tuple[float, tuple[int, int]]]:
        ranked: list[tuple[float, tuple[int, int]]] = []
        for key, pairs in opportunities.items():
            cargo = allocate_pair_cargo(pairs, capacity=capacity, budget=budget, max_commodities=max_commodities)
            if cargo:
                ranked.append((sum(item.profit for item in cargo), key))
        ranked.sort(key=lambda entry: entry[0], reverse=True)
        return ranked

    # No budget given means capital is already math.inf - the second ranking would be
    # an identical, wasted recomputation, so only do it when there's a real budget to
    # rank against.
    rankings = (rank_edges(math.inf),) if math.isinf(capital) else (rank_edges(math.inf), rank_edges(capital))
    candidate_terminals: set[int] = set()
    for ranked_edges in rankings:
        for _, (origin_id, destination_id) in ranked_edges[:MAX_CANDIDATE_EDGES]:
            candidate_terminals.add(origin_id)
            candidate_terminals.add(destination_id)

    graph: dict[int, list[int]] = {}
    for origin_id, destination_id in opportunities:
        if origin_id in candidate_terminals and destination_id in candidate_terminals:
            graph.setdefault(origin_id, []).append(destination_id)

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
            )
            next_budget = (
                remaining_budget if math.isinf(remaining_budget)
                else remaining_budget - leg_investment + leg_revenue
            )
            extend(next_stop, visited | {next_stop}, (*legs, leg), next_budget)

    for start in candidate_terminals:
        extend(start, frozenset({start}), (), capital)

    routes.sort(key=lambda route: (route.profit, route.roi_pct), reverse=True)
    return routes[:limit]
