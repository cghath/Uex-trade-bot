"""Pure, dependency-free logic for the Refinery Advisor (/refinery-advisor): matching a
raw/refinable commodity by name, ranking refinery terminals by yield bonus - across up to
three commodities at once, since a mined rock/asteroid usually yields more than one - and
filtering the 9 refining methods down to the high-yield ones. No Discord, no I/O; callers
fetch the UEX/DB rows and pass them in.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

HIGH_YIELD_RATING = 3

COST_LABELS = {1: "low", 2: "medium", 3: "high"}
SPEED_LABELS = {1: "slow", 2: "medium", 3: "fast"}


def resolve_raw_commodity(commodities: list[dict[str, Any]], query: str) -> dict[str, Any] | None:
    """Same tiered exact-then-unique-substring match as resolve_ship (bot/uex/ships.py),
    scoped to commodities flagged both is_raw and is_refinable - the same set
    /refinery-advisor's own autocomplete suggests, so a name outside it is "not found"
    rather than silently matching some other, non-refinable commodity of a similar name."""
    query_lower = query.strip().lower()
    if not query_lower:
        return None
    candidates = [c for c in commodities if c.get("is_raw") and c.get("is_refinable")]
    for commodity in candidates:
        if query_lower == (commodity.get("name") or "").strip().lower():
            return commodity
    substring_matches = [c for c in candidates if query_lower in (c.get("name") or "").lower()]
    if len(substring_matches) == 1:
        return substring_matches[0]
    return None


def high_yield_refining_methods(methods: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Only the methods rated high-yield (rating_yield == 3) - the whole point of this
    advisor is a short, actionable list, not all 9 methods with ratings to weigh yourself.
    Cheapest first, then fastest, among the high-yield ones. Refining methods aren't tied
    to a specific terminal or commodity, so callers show this list once per command."""
    high_yield = [m for m in methods if m.get("rating_yield") == HIGH_YIELD_RATING]
    return sorted(high_yield, key=lambda m: (m.get("rating_cost", 0), -m.get("rating_speed", 0)))


@dataclass
class TerminalYield:
    id_terminal: int
    terminal_name: str
    combined_score: float
    # commodity_name -> yield_bonus at this terminal; a commodity absent here means this
    # terminal has no recorded yield-bonus data for it, not a confirmed 0% bonus.
    per_commodity: dict[str, int] = field(default_factory=dict)


def rank_refinery_terminals(
    yield_rows_by_commodity: dict[str, list[dict[str, Any]]], *, limit: int = 5
) -> list[TerminalYield]:
    """Combines per-commodity refinery-yield-bonus rows (one list per requested commodity,
    each row shaped like refinery_yield_observations: id_terminal/terminal_name/yield_bonus)
    into one ranked list of terminals, for a haul of up to three commodities mined together.
    A terminal's combined_score is the SUM of whatever of the requested commodities it has
    yield data for - a terminal missing data for one of several requested commodities is
    still ranked on its smaller sum, not excluded outright, since dropping it entirely would
    hide the single best stop for a 2-of-3 match. This is a simple additive approximation
    (not weighted by how much of each ore was actually mined, which this advisor doesn't
    ask for), good enough to compare "best overall stop" candidates. Ties broken by
    terminal name for determinism."""
    by_terminal: dict[int, TerminalYield] = {}
    for commodity_name, rows in yield_rows_by_commodity.items():
        for row in rows:
            id_terminal = row.get("id_terminal")
            bonus = row.get("yield_bonus")
            if id_terminal is None or bonus is None:
                continue
            terminal = by_terminal.setdefault(
                id_terminal,
                TerminalYield(
                    id_terminal=id_terminal, terminal_name=row.get("terminal_name") or "Unknown", combined_score=0.0
                ),
            )
            terminal.per_commodity[commodity_name] = bonus
            terminal.combined_score += bonus
    ranked = sorted(by_terminal.values(), key=lambda t: (-t.combined_score, t.terminal_name))
    return ranked[:limit]
