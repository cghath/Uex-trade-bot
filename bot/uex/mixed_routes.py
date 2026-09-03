"""Mixed-commodity route allocation from the locally collected market snapshot."""
from __future__ import annotations

from dataclasses import dataclass
import itertools
import math
from typing import Any

from bot.uex.practical_routes import terminal_in_system, terminal_supports_auto_load
from bot.uex.supply_demand import has_sell_side_demand


@dataclass(frozen=True)
class MixedCargoItem:
    id_commodity: int
    commodity_name: str
    quantity_scu: float
    buy_price: float
    sell_price: float
    available_scu: float
    investment: float
    profit: float
    source: dict[str, Any]
    destination: dict[str, Any]

    @property
    def profit_per_scu(self) -> float:
        return self.sell_price - self.buy_price


@dataclass(frozen=True)
class MixedRoute:
    origin_id: int
    origin_name: str
    destination_id: int
    destination_name: str
    cargo: tuple[MixedCargoItem, ...]
    cargo_scu: float
    investment: float
    revenue: float
    profit: float

    @property
    def roi_pct(self) -> float:
        return 0.0 if self.investment <= 0 else self.profit / self.investment * 100


def build_pair_opportunities(
    market_rows: list[dict[str, Any]],
    *,
    space_only: bool = False,
    capital_access_only: bool = False,
    auto_load_only: bool = False,
    system: str | None = None,
) -> dict[tuple[int, int], list[tuple[dict[str, Any], dict[str, Any]]]]:
    """Group market rows into every profitable (origin_terminal, destination_terminal)
    pairing, per commodity, after applying the shared route safety filters.

    Filters are applied to the whole row pool before origins/destinations are split out
    of it, so a result can never pair a passing origin with a failing destination.
    Shared by a single hop (build_mixed_routes) and every leg of a multi-stop chain
    (bot/uex/multi_stop_routes.py).
    """
    eligible_rows = [
        r for r in market_rows
        if (not space_only or is_space_terminal(r))
        and (not capital_access_only or supports_capital_cargo_access(r))
        and (system is None or terminal_in_system(r, system))
        and (not auto_load_only or terminal_supports_auto_load(r))
    ]
    origins = [
        r for r in eligible_rows
        if _positive(r.get("price_buy")) and _positive(r.get("scu_buy"))
    ]
    destinations = [
        r for r in eligible_rows
        if _positive(r.get("price_sell"))
        and has_sell_side_demand(r.get("scu_sell"), r.get("status_sell"))
    ]
    destinations_by_commodity: dict[int, list[dict[str, Any]]] = {}
    for row in destinations:
        commodity_id = _integer(row.get("id_commodity"))
        if commodity_id is not None:
            destinations_by_commodity.setdefault(commodity_id, []).append(row)

    opportunities: dict[tuple[int, int], list[tuple[dict[str, Any], dict[str, Any]]]] = {}
    for source in origins:
        commodity_id = _integer(source.get("id_commodity"))
        origin_id = _integer(source.get("id_terminal"))
        if commodity_id is None or origin_id is None:
            continue
        for destination in destinations_by_commodity.get(commodity_id, []):
            destination_id = _integer(destination.get("id_terminal"))
            if destination_id is None or destination_id == origin_id:
                continue
            if float(destination["price_sell"]) <= float(source["price_buy"]):
                continue
            opportunities.setdefault((origin_id, destination_id), []).append((source, destination))
    return opportunities


def _greedy_fill(
    pairs: list[tuple[dict[str, Any], dict[str, Any]]],
    *,
    capacity: float,
    budget: float,
    max_commodities: int,
    key: Any,
) -> list[MixedCargoItem]:
    """Greedily load commodities in `key`-descending order, under stock, demand, ship
    capacity, and budget limits. Shared implementation behind allocate_pair_cargo's two
    greedy passes.
    """
    ordered_pairs = sorted(pairs, key=key, reverse=True)
    remaining_scu = capacity
    remaining_budget = budget
    cargo: list[MixedCargoItem] = []
    for source, destination in ordered_pairs:
        if len(cargo) >= max_commodities:
            break
        if remaining_scu < 1:
            break
        buy_price = float(source["price_buy"])
        sell_price = float(destination["price_sell"])
        available = math.floor(min(float(source["scu_buy"]), float(destination["scu_sell"])))
        affordable = remaining_scu if math.isinf(remaining_budget) else math.floor(remaining_budget / buy_price)
        quantity = min(available, remaining_scu, affordable)
        if quantity < 1:
            continue
        investment = quantity * buy_price
        profit = quantity * (sell_price - buy_price)
        cargo.append(
            MixedCargoItem(
                id_commodity=int(source["id_commodity"]),
                commodity_name=str(source.get("commodity_name") or "Unknown"),
                quantity_scu=float(quantity),
                buy_price=buy_price,
                sell_price=sell_price,
                available_scu=float(available),
                investment=investment,
                profit=profit,
                source=source,
                destination=destination,
            )
        )
        remaining_scu -= quantity
        remaining_budget -= investment
    return cargo


def _profit_per_unit(pair: tuple[dict[str, Any], dict[str, Any]]) -> float:
    return float(pair[1]["price_sell"]) - float(pair[0]["price_buy"])


def _profit_per_auec(pair: tuple[dict[str, Any], dict[str, Any]]) -> float:
    buy_price = float(pair[0]["price_buy"])
    return _profit_per_unit(pair) / buy_price if buy_price > 0 else 0.0


# Bounds for the exact search below. itertools.combinations over more candidates, or
# brute-forcing a bigger capacity, grows fast enough to matter: ~65ms at 8 candidates/
# capacity 30, ~85ms at 10/20, ~0.5s at 12/60 (measured). Tight capacity/few candidates
# is exactly where the cheap greedy heuristic below fails worst, and is also the
# cheapest case to solve exactly - larger cases keep the heuristic since
# build_multi_stop_routes' search can call this thousands of times per command.
EXACT_SEARCH_MAX_CANDIDATES = 8
EXACT_SEARCH_MAX_CAPACITY = 25


def _exact_allocate(
    pairs: list[tuple[dict[str, Any], dict[str, Any]]],
    *,
    capacity: int,
    budget: float,
    max_commodities: int,
    min_commodities: int,
) -> list[MixedCargoItem]:
    """Exact best allocation for a small candidate set: try every subset of size
    min_commodities..max_commodities, and for each subset, brute-force every quantity
    combination of all-but-one item (bounded by capacity, since a unit of any commodity
    always costs exactly 1 SCU) with the last item's quantity chosen greedily from
    whatever capacity/budget remains - provably optimal for a fixed subset, since with
    only one item left to decide, using as much of it as still fits is always at least
    as good as using less (profit per unit is always positive here). Exhausting every
    subset this way finds the true global optimum, not an approximation.
    """
    n = len(pairs)
    best_profit = 0.0
    best_cargo: list[MixedCargoItem] = []
    for size in range(max(1, min_commodities), min(max_commodities, n) + 1):
        for combo in itertools.combinations(range(n), size):
            items = []
            for idx in combo:
                source, destination = pairs[idx]
                buy_price = float(source["price_buy"])
                sell_price = float(destination["price_sell"])
                available = math.floor(min(float(source["scu_buy"]), float(destination["scu_sell"]), capacity))
                items.append((buy_price, sell_price - buy_price, available, source, destination))
            *prefix, last = items
            ranges = [range(0, item[2] + 1) for item in prefix]
            for prefix_quantities in itertools.product(*ranges):
                used_capacity = sum(prefix_quantities)
                if used_capacity > capacity:
                    continue
                used_cost = sum(q * item[0] for q, item in zip(prefix_quantities, prefix))
                if not math.isinf(budget) and used_cost > budget:
                    continue
                remaining_capacity = capacity - used_capacity
                remaining_budget = budget - used_cost
                last_buy_price = last[0]
                last_affordable = (
                    remaining_capacity if math.isinf(remaining_budget) or last_buy_price <= 0
                    else math.floor(remaining_budget / last_buy_price)
                )
                last_quantity = max(0, min(last[2], remaining_capacity, last_affordable))
                quantities = (*prefix_quantities, last_quantity)
                if sum(1 for q in quantities if q > 0) < min_commodities:
                    continue
                total_profit = sum(q * item[1] for q, item in zip(quantities, items))
                if total_profit > best_profit:
                    best_profit = total_profit
                    best_cargo = [
                        MixedCargoItem(
                            id_commodity=int(item[3]["id_commodity"]),
                            commodity_name=str(item[3].get("commodity_name") or "Unknown"),
                            quantity_scu=float(quantity),
                            buy_price=item[0],
                            sell_price=item[0] + item[1],
                            available_scu=float(item[2]),
                            investment=quantity * item[0],
                            profit=quantity * item[1],
                            source=item[3],
                            destination=item[4],
                        )
                        for item, quantity in zip(items, quantities)
                        if quantity > 0
                    ]
    return best_cargo


def allocate_pair_cargo(
    pairs: list[tuple[dict[str, Any], dict[str, Any]]],
    *,
    capacity: float,
    budget: float,
    max_commodities: int,
    min_commodities: int = 1,
) -> list[MixedCargoItem]:
    """Load commodities for one origin/destination pair, under stock, demand, ship
    capacity, and budget limits - exactly, for a small enough candidate set and
    capacity (see _exact_allocate), otherwise via whichever of two greedy orderings
    earns more.

    Highest profit-*per-unit*-SCU first is the obvious greedy choice, but under a binding
    budget it can pick badly: an expensive, high-margin commodity that only a token
    quantity is affordable can crowd out a cheaper, lower-margin one that would have used
    the same budget far more completely (concrete case: buy 90/sell 140 vs buy 10/sell 19,
    budget 100, capacity 10 - per-unit-first nets 59 profit; buying only the cheaper
    commodity nets 90 with the same inputs). Trying profit-*per-aUEC-invested* order too
    catches that specific case, but the two-ordering approach is still a real
    approximation, not a solver - a random search over small scenarios found cases over
    2x off the true optimum. _exact_allocate closes that gap outright when the search
    space is small enough to brute-force in bounded time; larger cases keep this
    two-ordering approximation, documented as a deliberate speed trade-off, not a claim
    of universal optimality.

    Capacity above EXACT_SEARCH_MAX_CAPACITY still gets a capped exact solve (as if the
    ship only had EXACT_SEARCH_MAX_CAPACITY SCU) compared against the heuristic's
    full-capacity result, keeping whichever earns more - the capped solution is always a
    valid allocation for the larger ship too (it just doesn't try to use the extra
    capacity), so this can only help. Without it, crossing the threshold could make a
    *bigger* ship score worse than a smaller one would for identical data (confirmed: 26
    SCU scoring worse than 25 SCU, purely from losing access to the exact solve a 25-SCU
    ship still gets) - callers whose recommendation could be at or near this boundary
    should disclose that larger loads are approximate above it.

    ``min_commodities`` matters because different strategies can reach a different
    *number* of commodities loaded, not just different profit totals: an ordering that
    doesn't reach min_commodities is never preferred over one that does, regardless of
    its profit (build_mixed_routes needs 2; a single commodity that fills the ship is
    /best-route's job, not a mixed load). Ties keep today's per-unit-first result.
    """
    candidates: list[list[MixedCargoItem]] = []
    if len(pairs) <= EXACT_SEARCH_MAX_CANDIDATES:
        capped_capacity = min(int(capacity), EXACT_SEARCH_MAX_CAPACITY)
        candidates.append(
            _exact_allocate(
                pairs,
                capacity=capped_capacity,
                budget=budget,
                max_commodities=max_commodities,
                min_commodities=min_commodities,
            )
        )
    if capacity > EXACT_SEARCH_MAX_CAPACITY or len(pairs) > EXACT_SEARCH_MAX_CANDIDATES:
        by_margin = _greedy_fill(pairs, capacity=capacity, budget=budget, max_commodities=max_commodities, key=_profit_per_unit)
        by_efficiency = _greedy_fill(pairs, capacity=capacity, budget=budget, max_commodities=max_commodities, key=_profit_per_auec)
        candidates.extend([by_margin, by_efficiency])
    qualifying = [cargo for cargo in candidates if len(cargo) >= min_commodities]
    if qualifying:
        return max(qualifying, key=lambda cargo: sum(item.profit for item in cargo))
    return candidates[0] if candidates else []


def allocation_is_exact(*, num_pairs: int, capacity: float) -> bool:
    """True when allocate_pair_cargo's result for this pair count/capacity is a proven
    global optimum, not an approximation - callers can use this to disclose when a
    recommendation might not be the true best (see allocate_pair_cargo's docstring)."""
    return num_pairs <= EXACT_SEARCH_MAX_CANDIDATES and capacity <= EXACT_SEARCH_MAX_CAPACITY


def build_mixed_routes(
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
) -> list[MixedRoute]:
    """Return profitable same-origin/same-destination mixed loads.

    Cargo is allocated by profit per SCU. Stock, destination demand, ship capacity, and
    optional investment capital are all hard limits. Whole SCU quantities are used because
    those are actionable at commodity kiosks. A result must contain at least two commodities;
    if one commodity can fill the ship by itself, it is a normal /best-route candidate instead.

    ``auto_load_only`` and ``system`` (e.g. 'Stanton', 'Pyro', 'Nyx') both require BOTH
    ends of a route to satisfy them - each is applied to the whole shared row pool
    *before* origins/destinations are split out of it, so every candidate on both sides
    is already confirmed, and a route built from this filtered pool can never pair a
    passing origin with a failing destination.
    """
    capacity = math.floor(float(ship_capacity_scu or 0))
    if capacity <= 0 or limit <= 0 or max_commodities < 2:
        return []
    capital = math.inf if budget is None else max(0.0, float(budget))

    opportunities = build_pair_opportunities(
        market_rows,
        space_only=space_only,
        capital_access_only=capital_access_only,
        auto_load_only=auto_load_only,
        system=system,
    )

    routes: list[MixedRoute] = []
    for (origin_id, destination_id), pairs in opportunities.items():
        cargo = allocate_pair_cargo(
            pairs, capacity=capacity, budget=capital, max_commodities=max_commodities, min_commodities=2
        )
        if len(cargo) < 2:
            continue
        investment = sum(item.investment for item in cargo)
        profit = sum(item.profit for item in cargo)
        routes.append(
            MixedRoute(
                origin_id=origin_id,
                origin_name=str(cargo[0].source.get("terminal_name") or "Unknown"),
                destination_id=destination_id,
                destination_name=str(cargo[0].destination.get("terminal_name") or "Unknown"),
                cargo=tuple(cargo),
                cargo_scu=sum(item.quantity_scu for item in cargo),
                investment=investment,
                revenue=investment + profit,
                profit=profit,
            )
        )

    routes.sort(key=lambda route: (route.profit, route.roi_pct), reverse=True)
    return routes[:limit]


def is_space_terminal(terminal: dict[str, Any]) -> bool:
    """Return true only for a terminal explicitly tied to a UEX space station.

    Planet names are not sufficient because orbital stations inherit the planet they orbit.
    Unknown legacy rows fail closed when the safety filter is enabled.
    """
    station_id = _integer(terminal.get("id_space_station"))
    return bool(station_id is not None and station_id > 0) or bool(
        str(terminal.get("space_station_name") or "").strip()
    )


def requires_capital_cargo_access(vehicle: dict[str, Any]) -> bool:
    """UEX marks Polaris-scale ships as XL and dock-operated cargo ships explicitly."""
    return str(vehicle.get("pad_type") or "").strip().upper() == "XL" or _truthy(
        vehicle.get("is_loading_dock")
    )


def supports_capital_cargo_access(terminal: dict[str, Any]) -> bool:
    """Require a confirmed external cargo dock or XL-capable station hangar.

    Missing station metadata fails closed. Terminal-level loading-dock data still permits
    surface locations with an explicitly reported external cargo elevator.
    """
    if _truthy(terminal.get("has_loading_dock")) or _truthy(terminal.get("station_has_loading_dock")):
        return True
    pad_types = {
        part.strip().upper()
        for part in str(terminal.get("station_pad_types") or "").replace(",", "|").split("|")
        if part.strip()
    }
    return "XL" in pad_types


def _positive(value: Any) -> bool:
    try:
        return value is not None and float(value) > 0
    except (TypeError, ValueError):
        return False


def _integer(value: Any) -> int | None:
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _truthy(value: Any) -> bool:
    return value is True or str(value).strip().lower() in {"1", "true", "yes"}
