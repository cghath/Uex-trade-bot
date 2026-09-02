"""Mixed-commodity route allocation from the locally collected market snapshot."""
from __future__ import annotations

from dataclasses import dataclass
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


def allocate_pair_cargo(
    pairs: list[tuple[dict[str, Any], dict[str, Any]]],
    *,
    capacity: float,
    budget: float,
    max_commodities: int,
    min_commodities: int = 1,
) -> list[MixedCargoItem]:
    """Load commodities for one origin/destination pair, under stock, demand, ship
    capacity, and budget limits, keeping whichever of two greedy orderings earns more.

    Highest profit-*per-unit*-SCU first is the obvious greedy choice, but under a binding
    budget it can pick badly: an expensive, high-margin commodity that only a token
    quantity is affordable can crowd out a cheaper, lower-margin one that would have used
    the same budget far more completely (concrete case: buy 90/sell 140 vs buy 10/sell 19,
    budget 100, capacity 10 - per-unit-first nets 59 profit; buying only the cheaper
    commodity nets 90 with the same inputs). Also trying profit-*per-aUEC-invested* order
    catches this without a full knapsack search over commodity subsets.

    ``min_commodities`` matters because the two orderings don't just reach different
    profit totals, they can reach a different *number* of commodities loaded: the
    efficiency ordering above floods all 10 capacity/100 budget into the cheap commodity
    alone (1 item, profit 90), while the margin ordering spreads across both (2 items,
    profit 59) - picking purely by profit would silently drop a caller's "at least N
    commodities" requirement (build_mixed_routes needs 2; a single commodity that fills
    the ship is /best-route's job, not a mixed load). An ordering that doesn't reach
    min_commodities is never preferred over one that does, regardless of its profit.
    Ties keep today's per-unit-first result.
    """
    by_margin = _greedy_fill(pairs, capacity=capacity, budget=budget, max_commodities=max_commodities, key=_profit_per_unit)
    by_efficiency = _greedy_fill(pairs, capacity=capacity, budget=budget, max_commodities=max_commodities, key=_profit_per_auec)
    qualifying = [cargo for cargo in (by_margin, by_efficiency) if len(cargo) >= min_commodities]
    if qualifying:
        return max(qualifying, key=lambda cargo: sum(item.profit for item in cargo))
    return by_margin


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
