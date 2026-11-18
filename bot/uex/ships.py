"""Pure helpers for ship cargo capacity and route cargo math. Dependency-free for testing."""
from __future__ import annotations

import difflib

from dataclasses import dataclass


# A ship-name typo's score against the ship it was meant to be (difflib's ratio, against the ship's name or its full
# name with the maker, whichever is closer), and how far ahead of the next ship it must be. Asked for 2026-11-18 after
# "Tiberon" (the Aegis Tiburon) found nothing. Calibrated against UEX's 281 ship names: every name with one letter
# dropped, two adjacent letters swapped or one vowel changed - 9,243 typos - resolved 8,098 to the right ship and
# declined the rest ("constelation" is as close to every Constellation); none resolved to a different ship. The margin
# is the commodity lookups' (bot/uex/trading.py MIN_MATCH_MARGIN).
MIN_SHIP_MATCH_SCORE = 0.75
MIN_SHIP_MATCH_MARGIN = 0.1
# How close a spelling must be to be offered in the ship autocomplete when no name contains what was typed - looser
# than a match, since the player still picks from the list.
MIN_SHIP_SUGGESTION_SCORE = 0.6


def _ship_scores(vehicles: list[dict], query_lower: str, *, as_start: bool = False) -> list[tuple[float, dict]]:
    """Each distinct ship name's score against `query_lower`, best first: the closer of its name and its full name, so
    the two never compete, with the first vehicle carrying that name (UEX lists a few names twice). `as_start` also
    scores the query against each name's first letters - for the autocomplete, where what's typed is usually the
    start of a name ("tiber" is closer to "Stinger" than to all of "Tiburon", but not to its "tibur")."""
    best: dict[str, tuple[float, dict]] = {}
    for vehicle in vehicles:
        name = (vehicle.get("name") or "").strip()
        if not name:
            continue
        spellings = [s.strip().lower() for s in (name, vehicle.get("name_full") or "") if s.strip()]
        if as_start:
            spellings += [spelling[:len(query_lower)] for spelling in spellings]
        score = max(difflib.SequenceMatcher(None, query_lower, spelling).ratio() for spelling in spellings)
        key = name.lower()
        if key not in best:
            best[key] = (score, vehicle)
        elif score > best[key][0]:
            best[key] = (score, best[key][1])
    return sorted(best.values(), key=lambda pair: pair[0], reverse=True)


def resolve_ship(vehicles: list[dict], query: str) -> dict | None:
    """Resolve a typed/saved ship name to its vehicle record.

    Prefers an exact (case-insensitive) match against `name` or `name_full`; falls back to
    a substring match only if it's unique, so e.g. "cutlass" doesn't silently pick one of
    several Cutlass variants. When no name contains the query at all, a typo of one ship's
    name resolves to it ("tiberon" is the Tiburon) - only when it clearly beats every other
    ship (MIN_SHIP_MATCH_SCORE, MIN_SHIP_MATCH_MARGIN), never a guess.
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
    if substring_matches:
        return None
    scored = _ship_scores(vehicles, query_lower)
    if not scored or scored[0][0] < MIN_SHIP_MATCH_SCORE:
        return None
    second = scored[1][0] if len(scored) > 1 else 0.0
    return scored[0][1] if scored[0][0] - second >= MIN_SHIP_MATCH_MARGIN else None


def ship_name_suggestions(vehicles: list[dict], typed: str, limit: int = 25) -> list[str]:
    """The ship autocomplete's choices for what's been typed so far: every name containing it, and when none does, the
    closest spellings ("tiber" offers the Tiburon) - so a misspelled ship still shows up to pick."""
    typed_lower = typed.strip().lower()
    names = list(dict.fromkeys((v.get("name") or "").strip() for v in vehicles if (v.get("name") or "").strip()))
    containing = [name for name in names if typed_lower in name.lower()]
    if containing or not typed_lower:
        return containing[:limit]
    close = [pair for pair in _ship_scores(vehicles, typed_lower, as_start=True) if pair[0] >= MIN_SHIP_SUGGESTION_SCORE]
    # A tie goes to the name starting with the same letter - a typo rarely gets the first one wrong ("tiber" scores
    # "Liberator" as high as "Tiburon").
    close.sort(key=lambda pair: (-pair[0], not pair[1]["name"].strip().lower().startswith(typed_lower[0])))
    return [vehicle["name"].strip() for _score, vehicle in close][:limit]


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
