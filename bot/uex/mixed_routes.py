"""Mixed-commodity route allocation from the locally collected market snapshot."""
from __future__ import annotations

import dataclasses
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
    # Which constraint(s) this item's quantity is tied to: "stock" (origin scu_buy),
    # "demand" (destination scu_sell), "cargo space" (the ship's shared SCU pool), or
    # "budget" (the shared aUEC pool) - see _classify_item_limit. Defaults to () only for
    # callers/tests that predate this field; every item allocate_pair_cargo actually
    # returns has at least one factor.
    limiting_factors: tuple[str, ...] = ()

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
    is_exact: bool

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


def _classify_item_limit(
    quantity: float,
    *,
    stock_cap: float,
    demand_cap: float,
    cargo_space_binding: bool,
    budget_binding: bool,
    search_capped: bool = False,
) -> tuple[str, ...]:
    """Why couldn't this item's quantity be higher? Checked in this order: the item's own
    stock/demand cap first (a fact about this specific commodity, true regardless of why
    the solver picked this exact quantity), falling back to the shared cargo-space/budget
    pool only when neither market cap was reached - a market cap and a shared-pool limit
    are never both genuinely binding for the same item (if stock/demand already explains
    the quantity, more cargo space or budget wouldn't let this item grow anyway). If
    NONE of those four real constraints explain it, `search_capped` means the true
    reason is _exact_allocate's own internal search boundary above
    EXACT_SEARCH_MAX_CAPACITY, not a real market/ship/budget limit - reported as its own
    "search cap" factor rather than silently folded into "cargo space", which would tell
    a player a bigger ship would help when it already has room to spare.
    """
    factors: list[str] = []
    if quantity >= stock_cap:
        factors.append("stock")
    if quantity >= demand_cap:
        factors.append("demand")
    if not factors:
        if cargo_space_binding:
            factors.append("cargo space")
        if budget_binding:
            factors.append("budget")
        if not factors and search_capped:
            factors.append("search cap")
    # Every item allocate_pair_cargo actually builds is provably capped by at least one
    # of the five - see allocate_pair_cargo's docstring - but fail closed rather than
    # silently empty if that invariant is ever violated by a future change.
    return tuple(factors) if factors else ("allocation limit",)


def format_limiting_factors(factors: tuple[str, ...]) -> str:
    """Short parenthetical for display: '(limited by stock)', '(limited by cargo space
    & budget)'. Empty input (a MixedCargoItem predating this field) reads as unknown
    rather than an empty string."""
    if not factors:
        return "limit unknown"
    return "limited by " + " & ".join(factors)


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
        stock_cap = math.floor(float(source["scu_buy"]))
        demand_cap = math.floor(float(destination["scu_sell"]))
        available = min(stock_cap, demand_cap)
        affordable = remaining_scu if math.isinf(remaining_budget) else math.floor(remaining_budget / buy_price)
        quantity = min(available, remaining_scu, affordable)
        if quantity < 1:
            continue
        investment = quantity * buy_price
        profit = quantity * (sell_price - buy_price)
        # Local (at-the-time-of-this-pick) remaining_scu/affordable, not the final totals
        # after the whole greedy pass - this loop never revisits an earlier item once
        # later items consume more of the shared pools, so checking against the FINAL
        # aggregate remaining would misattribute an item that was actually stock/budget
        # bound as "cargo space"-bound just because something later used up what was left.
        cargo_space_binding = quantity == remaining_scu
        budget_binding = (not math.isinf(remaining_budget)) and quantity == affordable
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
                limiting_factors=_classify_item_limit(
                    quantity,
                    stock_cap=stock_cap,
                    demand_cap=demand_cap,
                    cargo_space_binding=cargo_space_binding,
                    budget_binding=budget_binding,
                ),
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
    real_capacity: float,
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

    ``capacity`` bounds the search itself (capped at EXACT_SEARCH_MAX_CAPACITY by
    allocate_pair_cargo for ships above it, purely to keep the brute force fast).
    ``real_capacity`` is the ship's actual, uncapped capacity - used only by the
    limiting-factor annotation below, never the search, so a bigger ship's real spare
    room is never reported as "cargo space limited" just because the search itself
    stopped looking at EXACT_SEARCH_MAX_CAPACITY.
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
                # market_available is the real stock/demand limit, reported to the user
                # as-is; search_bound additionally caps it at this call's (possibly
                # search-capped, see allocate_pair_cargo) capacity so the combinatorial
                # search below never enumerates quantities no ship could carry anyway.
                # Conflating the two previously reported this solver's own capacity cap
                # as if it were the market's real stock/demand limit.
                market_available = math.floor(min(float(source["scu_buy"]), float(destination["scu_sell"])))
                search_bound = min(market_available, capacity)
                items.append((buy_price, sell_price - buy_price, market_available, search_bound, source, destination))
            *prefix, last = items
            ranges = [range(0, item[3] + 1) for item in prefix]
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
                last_quantity = max(0, min(last[3], remaining_capacity, last_affordable))
                quantities = (*prefix_quantities, last_quantity)
                if sum(1 for q in quantities if q > 0) < min_commodities:
                    continue
                total_profit = sum(q * item[1] for q, item in zip(quantities, items))
                if total_profit > best_profit:
                    best_profit = total_profit
                    best_cargo = [
                        MixedCargoItem(
                            id_commodity=int(item[4]["id_commodity"]),
                            commodity_name=str(item[4].get("commodity_name") or "Unknown"),
                            quantity_scu=float(quantity),
                            buy_price=item[0],
                            sell_price=item[0] + item[1],
                            available_scu=float(item[2]),
                            investment=quantity * item[0],
                            profit=quantity * item[1],
                            source=item[4],
                            destination=item[5],
                        )
                        for item, quantity in zip(items, quantities)
                        if quantity > 0
                    ]
    return _annotate_exact_allocation_limits(
        best_cargo, search_capacity=capacity, real_capacity=real_capacity, budget=budget
    )


def _annotate_exact_allocation_limits(
    cargo: list[MixedCargoItem], *, search_capacity: float, real_capacity: float, budget: float
) -> list[MixedCargoItem]:
    """Label each item in an exact-solver winning combo with why its quantity couldn't be
    higher. Unlike the greedy path (see _greedy_fill's own local per-item check), using
    the FINAL aggregate remaining capacity/budget here is exact, not an approximation:
    _exact_allocate searches every valid combination jointly, not sequentially, so if any
    item's quantity were below its own stock/demand cap AND increasing it by one unit
    still fit both REAL capacity and budget, that strictly-more-profitable combo (every
    included item has positive profit per unit) would have been found and returned
    instead - see allocate_pair_cargo's docstring.

    Cargo-space bindingness is checked against real_capacity (the ship's actual size),
    not search_capacity (the solver's own internal search boundary, capped at
    EXACT_SEARCH_MAX_CAPACITY above that threshold) - a prior version conflated the two,
    reporting "cargo space" whenever the search-capped total was reached even when the
    real ship still had room to spare. When neither a real constraint (stock, demand,
    real cargo space, budget) explains an item's quantity, but the search itself stopped
    at search_capacity, that's search_capped - a limitation of this solver's own bounded
    search, not anything about the ship, market, or budget.
    """
    if not cargo:
        return cargo
    total_scu = sum(item.quantity_scu for item in cargo)
    total_investment = sum(item.investment for item in cargo)
    remaining_real_capacity = real_capacity - total_scu
    remaining_budget = budget - total_investment
    search_capped = search_capacity < real_capacity and total_scu >= search_capacity
    annotated = []
    for item in cargo:
        stock_cap = math.floor(float(item.source.get("scu_buy") or 0))
        demand_cap = math.floor(float(item.destination.get("scu_sell") or 0))
        factors = _classify_item_limit(
            item.quantity_scu,
            stock_cap=stock_cap,
            demand_cap=demand_cap,
            cargo_space_binding=remaining_real_capacity < 1,
            budget_binding=(not math.isinf(remaining_budget)) and remaining_budget < item.buy_price,
            search_capped=search_capped,
        )
        annotated.append(dataclasses.replace(item, limiting_factors=factors))
    return annotated


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
                real_capacity=capacity,
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
                is_exact=allocation_is_exact(num_pairs=len(pairs), capacity=capacity),
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
    """Require a confirmed external freight elevator/cargo dock or XL-capable station hangar.

    Missing station metadata fails closed. Terminal-level loading-dock/freight-elevator
    data still permits surface locations with explicitly reported external cargo
    infrastructure. Deliberately does NOT check has_docking_port - UEX's docking-collar
    mechanic is a known-unreliable way to service a capital ship, so it doesn't count as
    confirmed access the way a physical dock/elevator/XL pad does.
    """
    if (
        _truthy(terminal.get("has_loading_dock"))
        or _truthy(terminal.get("has_freight_elevator"))
        or _truthy(terminal.get("station_has_loading_dock"))
    ):
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
