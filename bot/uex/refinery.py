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


def display_terminal_name(terminal_name: str, star_system_name: str | None) -> str:
    """UEX's own /refineries_yields terminal_name only embeds a system suffix for some
    terminals (gateway terminals disambiguating same-named gateways in different systems,
    e.g. two different "Nyx Gateway" terminals, one in Pyro and one in Stanton) and never
    for others - showing raw terminal_name as-is is inconsistent about which recommendation
    tells you what system to fly to. Appends " (System)" using the separately-reported,
    structured star_system_name field so every terminal shows its system the same way;
    skipped only when that exact system name is already present in the terminal's own name
    (the gateway case), to avoid "Nyx Gateway (Stanton) (Stanton)"."""
    if not star_system_name:
        return terminal_name
    if star_system_name.lower() in terminal_name.lower():
        return terminal_name
    return f"{terminal_name} ({star_system_name})"


@dataclass
class TerminalYield:
    id_terminal: int
    terminal_name: str
    combined_score: float
    star_system_name: str | None = None
    # commodity_name -> yield_bonus at this terminal; a commodity absent here means this
    # terminal has no recorded yield-bonus data for it, not a confirmed 0% bonus.
    per_commodity: dict[str, int] = field(default_factory=dict)
    # Whether this terminal's own star system is one of the requested ore(s)' real mining
    # system(s) - None when the caller didn't supply mining_star_systems to
    # rank_refinery_terminals (unknown/not evaluated), True/False once it was. User-reported
    # real case: Quantainium's own highest-yield refinery (Levski, Nyx, +5%) outranks every
    # Stanton refinery by raw yield bonus alone, even though Quantainium can only be mined in
    # Stanton - ranking purely on yield can recommend a multi-system flight for cargo the
    # player could only have picked up somewhere else entirely. Never used to EXCLUDE a
    # terminal, only to sort in-system options first and let the caller disclose the rest - a
    # refinery in another system is still a real, usable option once the ore is actually in
    # your cargo hold, so hiding it would trade one bad recommendation for a missing one.
    in_mining_system: bool | None = None


@dataclass(frozen=True)
class HaulSystems:
    """Which star systems a haul's refineries are judged against, and what (if anything) the player should be
    told about how that was decided."""
    systems: frozenset[str]  # empty = nothing to judge by, so ranking is by yield alone
    note: str | None = None


def combine_mining_systems(systems_by_ore: dict[str, set[str]]) -> HaulSystems:
    """The systems a MULTI-ORE haul's refineries should be judged against.

    Judging against the UNION of every ore's mining systems (the original behaviour) flagged a refinery as
    fine if it sat near ANY of the ores, so for a haul of Quantainium (Stanton only) and an ore mined in Pyro
    a Pyro refinery went unflagged even though the Quantainium could never have been picked up there. What a
    player can actually plan around is a system where every ore of the haul is mined, so that intersection is
    used whenever it isn't empty. When the ores share no system there is no single mining trip for the whole
    haul: the union is used (a refinery is still usable once the cargo is in the hold) and the disparity is
    stated instead of silently blurred. An ore with no mining-location data can't narrow anything - unknown
    is not "mined nowhere" - so it is left out and named in the note.

    One ore (or none with data) behaves exactly as before, with no note."""
    known = {ore: set(systems) for ore, systems in systems_by_ore.items() if systems}
    unknown = [ore for ore, systems in systems_by_ore.items() if not systems]
    if not known:
        return HaulSystems(frozenset())
    union = frozenset().union(*known.values())
    common = frozenset.intersection(*(frozenset(s) for s in known.values()))
    notes: list[str] = []
    if len(known) == 1:
        systems = union
    elif common:
        systems = common
        if common != union:
            notes.append(
                f"For this combined haul ⚠️ is judged against {', '.join(sorted(common))}, the only "
                f"system{'s' if len(common) != 1 else ''} where every one of these ores is mined."
            )
    else:
        systems = union
        listing = "; ".join(f"{ore}: {', '.join(sorted(s))}" for ore, s in sorted(known.items()))
        notes.append(
            f"These ores aren't mined in a common system ({listing}), so no single mining trip covers the "
            "whole haul - ⚠️ only marks refineries outside all of those systems."
        )
    if unknown:
        notes.append(
            f"No mining-location data for {', '.join(sorted(unknown))}, so ⚠️ only reflects where "
            f"{', '.join(sorted(known))} {'is' if len(known) == 1 else 'are'} mined."
        )
    return HaulSystems(systems, " ".join(notes) or None)


def rank_refinery_terminals(
    yield_rows_by_commodity: dict[str, list[dict[str, Any]]],
    *,
    limit: int | None = 5,
    mining_star_systems: set[str] | None = None,
) -> list[TerminalYield]:
    """Combines per-commodity refinery-yield-bonus rows (one list per requested commodity,
    each row shaped like refinery_yield_observations: id_terminal/terminal_name/yield_bonus)
    into one ranked list of terminals, for a haul of up to three commodities mined together.
    A terminal's combined_score is the SUM of whatever of the requested commodities it has
    yield data for - a terminal missing data for one of several requested commodities is
    still ranked on its smaller sum, not excluded outright, since dropping it entirely would
    hide the single best stop for a 2-of-3 match. This is a simple additive approximation
    (not weighted by how much of each ore was actually mined, which this advisor doesn't
    ask for), good enough to compare "best overall stop" candidates.

    `mining_star_systems` (the systems the haul is judged against - see combine_mining_systems for
    how several ores are combined - from the same ids_star_systems data /where-to-mine reads) is
    optional and additive:
    when given (and non-empty), every terminal is tagged in_mining_system and a terminal
    whose own system ISN'T in that set is ranked after every in-system terminal, regardless
    of yield bonus - ties within each group still broken by yield bonus then terminal name.
    Omitting it (or passing an empty set, e.g. no ids_star_systems data exists for any
    requested ore) preserves the original pure-yield ordering exactly, and leaves every
    terminal's in_mining_system as None (unknown, not "confirmed out of system")."""
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
                    id_terminal=id_terminal,
                    terminal_name=row.get("terminal_name") or "Unknown",
                    combined_score=0.0,
                    star_system_name=row.get("star_system_name"),
                ),
            )
            terminal.per_commodity[commodity_name] = bonus
            terminal.combined_score += bonus

    if mining_star_systems:
        for terminal in by_terminal.values():
            terminal.in_mining_system = terminal.star_system_name in mining_star_systems
        sort_key = lambda t: (0 if t.in_mining_system else 1, -t.combined_score, t.terminal_name)
    else:
        sort_key = lambda t: (-t.combined_score, t.terminal_name)

    ranked = sorted(by_terminal.values(), key=sort_key)
    return ranked if limit is None else ranked[:limit]


def select_terminals_to_show(
    ranked: list[TerminalYield],
    *,
    min_shown: int,
    max_in_system: int,
    min_out_of_system: int = 1,
) -> list[TerminalYield]:
    """Picks which of an already-ranked, UNTRUNCATED terminal list to display. A flat top-N
    cut (the original behavior) silently drops real in-system refineries once an ore has
    more than N of them - confirmed on real data: Quantainium has 6 Stanton refineries but
    the advisor only showed 5, and Corundum has 11. Since the ranking already sorts every
    in-mining-system terminal first, this shows all of them (capped at `max_in_system` so a
    pathological ore can't flood the embed), then fills with the next-best terminals so at
    least `min_shown` appear in total, and always keeps at least `min_out_of_system` of the
    best-yield terminals from another system when any exist - that's the flagged
    "there's a higher yield elsewhere, but it's outside where this ore is mined" tradeoff
    the caller discloses with a warning marker, so the higher-yield option is visible
    rather than silently absent. When mining systems aren't known (in_mining_system is
    None everywhere) nothing counts as in- or out-of-system, and this degrades to the plain
    top `min_shown` of the yield-ordered list.

    Only ever trims from the far end of the ranking, never reorders - the result is always
    a prefix of `ranked` plus, at most, the best out-of-system terminals appended after it.
    """
    in_system = [t for t in ranked if t.in_mining_system is True]
    if not in_system:
        return ranked[:min_shown]

    shown = in_system[:max_in_system]
    out_of_system = [t for t in ranked if t.in_mining_system is not True]
    fill = max(min_out_of_system, min_shown - len(shown), 0)
    return shown + out_of_system[:fill]
