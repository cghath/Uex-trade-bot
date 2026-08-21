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
    limited_by: str  # "ship" | "stock" | "unknown"
    run_profit: float | None


def estimate_route_cargo(
    *,
    per_unit_profit: float,
    origin_scu_available: float | None,
    destination_scu_wanted: float | None,
    ship_cargo_scu: float | None,
) -> CargoEstimate | None:
    """How much of this commodity a run can actually haul, and the resulting total profit.

    The real limit on a haul is the smallest of: how much is in stock to buy at the origin,
    how much the destination will actually take, and how much cargo space the ship has.
    Missing/zero values are treated as "no data" and excluded from the comparison rather
    than treated as a hard zero, since UEX doesn't report stock for every terminal.
    """
    stock_candidates = [v for v in (origin_scu_available, destination_scu_wanted) if v and v > 0]
    stock_limit = min(stock_candidates) if stock_candidates else None

    candidates: list[tuple[float, str]] = []
    if stock_limit is not None:
        candidates.append((stock_limit, "stock"))
    if ship_cargo_scu is not None and ship_cargo_scu > 0:
        candidates.append((ship_cargo_scu, "ship"))

    if not candidates:
        return None

    max_scu, limited_by = min(candidates, key=lambda c: c[0])
    # If both bounds exist and are equal, credit "ship" as the binding constraint - more
    # actionable for the player than an arbitrary stock-vs-ship tie-break.
    if len(candidates) == 2 and candidates[0][0] == candidates[1][0]:
        limited_by = "ship"

    run_profit = round(per_unit_profit * max_scu, 2)
    return CargoEstimate(max_scu=max_scu, limited_by=limited_by, run_profit=run_profit)
