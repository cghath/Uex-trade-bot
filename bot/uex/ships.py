"""Pure helpers for ship cargo capacity and route cargo math. Dependency-free for testing."""
from __future__ import annotations

from dataclasses import dataclass


def resolve_ship(vehicles: list[dict], query: str) -> dict | None:
    """Resolve a typed/saved ship name to its vehicle record.

    Prefers an exact (case-insensitive) match against `name` or `name_full`; falls back to
    a substring match only if it's unique, so e.g. "cutlass" doesn't silently pick one of
    several Cutlass variants.
    """
    query_lower = query.strip().lower()
    if not query_lower:
        return None

    for vehicle in vehicles:
        name = (vehicle.get("name") or "").strip().lower()
        name_full = (vehicle.get("name_full") or "").strip().lower()
        if query_lower in (name, name_full):
            return vehicle

    substring_matches = [
        v for v in vehicles
        if query_lower in (v.get("name") or "").lower() or query_lower in (v.get("name_full") or "").lower()
    ]
    if len(substring_matches) == 1:
        return substring_matches[0]
    return None


@dataclass
class CargoEstimate:
    max_scu: float
    limited_by: str  # "ship" | "stock" | "budget" | "unknown"
    run_profit: float | None
    investment: float | None


def estimate_route_cargo(
    *,
    per_unit_profit: float,
    origin_scu_available: float | None,
    destination_scu_wanted: float | None,
    ship_cargo_scu: float | None,
    price_origin: float | None = None,
    budget: float | None = None,
) -> CargoEstimate | None:
    """How much of this commodity a run can actually haul, and the resulting total profit.

    The real limit on a haul is the smallest of: how much is in stock to buy at the origin,
    how much the destination will actually take, how much cargo space the ship has, and (if
    given) how much the starting budget can afford at price_origin. Missing/zero values are
    treated as "no data" and excluded from the comparison rather than treated as a hard
    zero, since UEX doesn't report stock for every terminal. budget is ignored (like the
    other optional bounds) when price_origin isn't known - there's nothing to divide it by.
    """
    stock_candidates = [v for v in (origin_scu_available, destination_scu_wanted) if v and v > 0]
    stock_limit = min(stock_candidates) if stock_candidates else None

    candidates: list[tuple[float, str]] = []
    if stock_limit is not None:
        candidates.append((stock_limit, "stock"))
    if ship_cargo_scu is not None and ship_cargo_scu > 0:
        candidates.append((ship_cargo_scu, "ship"))
    if budget is not None and budget > 0 and price_origin is not None and price_origin > 0:
        candidates.append((budget / price_origin, "budget"))

    if not candidates:
        return None

    min_value = min(c[0] for c in candidates)
    tied = [c for c in candidates if c[0] == min_value]
    if len(tied) == 1:
        max_scu, limited_by = tied[0]
    else:
        # Multiple constraints tie for the binding one - credit whichever is most
        # actionable for the player (bring more capital, or a bigger ship) over one that
        # isn't (real-world stock/demand, which no in-game decision changes).
        priority = {"ship": 0, "budget": 1, "stock": 2}
        max_scu, limited_by = min(tied, key=lambda c: priority[c[1]])

    run_profit = round(per_unit_profit * max_scu, 2)
    investment = round(price_origin * max_scu, 2) if price_origin is not None else None
    return CargoEstimate(max_scu=max_scu, limited_by=limited_by, run_profit=run_profit, investment=investment)
