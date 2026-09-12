"""Pure logic for /where-to-mine: resolving a raw/mineable commodity by name and describing
where it's actually found (star system/planet/moon/named mining POI), from UEX's own
per-commodity ids_star_systems/ids_planets/ids_moons/ids_poi fields. No Discord, no I/O -
callers fetch the UEX reference rows and pass them in.

The UEX-sourced location list itself is never ranked: UEX has no per-site richness/
abundance data at all, and the only real POI operational flags (is_landable,
has_quantum_marker, is_decommissioned) don't meaningfully differentiate the real mining-
related POI set either (7 total, none decommissioned, as of this module's writing) - a
plain list of everywhere it's known to be found is the honest answer there, not a
fabricated "best spot." A genuinely ranked answer (the richest known concentration
location(s)) comes from a second, separate static table instead - see
bot/uex/mining_hotspots.py - kept apart because it's sourced from a different community
site than the difficulty rating below, with its own staleness risk.

Also attaches a per-ore mining difficulty rating and raw stats from bot/uex/
mining_difficulty.py - the one place in this bot that uses static, externally-sourced game
constants instead of live UEX data (UEX has no mining-mechanic data at all), so it's kept in
its own module with its own staleness disclosure rather than blended in here.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from bot.uex.mining_difficulty import OreMiningProfile, get_mining_difficulty, get_mining_profile
from bot.uex.mining_hotspots import MiningHotspot, get_mining_hotspots


def resolve_mineable_commodity(commodities: list[dict[str, Any]], query: str) -> dict[str, Any] | None:
    """Same tiered exact-then-unique-substring match as resolve_raw_commodity
    (bot/uex/refinery.py), scoped to is_raw only - NOT is_refinable, since a hand-mined
    material with no refinery pathway (e.g. Jaclium) is exactly as relevant to "where do I
    find this" as anything that gets refined."""
    query_lower = query.strip().lower()
    if not query_lower:
        return None
    candidates = [c for c in commodities if c.get("is_raw")]
    for commodity in candidates:
        if query_lower == (commodity.get("name") or "").strip().lower():
            return commodity
    substring_matches = [c for c in candidates if query_lower in (c.get("name") or "").lower()]
    if len(substring_matches) == 1:
        return substring_matches[0]
    return None


def _parse_ids(ids_str: str | None) -> list[int]:
    if not ids_str:
        return []
    result = []
    for part in ids_str.split(","):
        part = part.strip()
        if not part:
            continue
        try:
            result.append(int(part))
        except ValueError:
            continue
    return result


def _names_for_ids(ids_str: str | None, by_id: dict[int, str]) -> list[str]:
    names = [by_id[commodity_id] for commodity_id in _parse_ids(ids_str) if commodity_id in by_id]
    return sorted(set(names))


@dataclass
class MiningLocationInfo:
    commodity_name: str
    star_systems: list[str]
    planets: list[str]
    moons: list[str]
    mining_pois: list[str]  # "POI name (context)" strings, deduped and sorted
    # 'low' / 'medium' / 'high' / None (not in the static reference table - see
    # bot/uex/mining_difficulty.py's own module docstring for why this is community-sourced,
    # not from UEX).
    difficulty: str | None = None
    mining_profile: OreMiningProfile | None = None
    # Richest known concentration spot(s), highest first - empty if this ore isn't in the
    # static reference table (see bot/uex/mining_hotspots.py). A different, independent
    # source from difficulty above, so one can be known without the other.
    hotspots: list[MiningHotspot] = field(default_factory=list)


def describe_mining_locations(
    commodity: dict[str, Any],
    *,
    star_systems_by_id: dict[int, str],
    planets_by_id: dict[int, str],
    moons_by_id: dict[int, str],
    poi_rows_by_id: dict[int, dict[str, Any]],
) -> MiningLocationInfo:
    """Builds a location summary for one raw commodity from its own ids_star_systems/
    ids_planets/ids_moons/ids_poi fields, resolving each id set to real names via reference
    tables the caller fetched from UEX's own /star_systems, /planets, /moons, /poi - only
    mining-related POIs (is_mining_related) are included, since /poi also covers unrelated
    points like trade terminals or outposts that happen to share the same list."""
    poi_lines = []
    for poi_id in _parse_ids(commodity.get("ids_poi")):
        poi = poi_rows_by_id.get(poi_id)
        if poi is None or not poi.get("is_mining_related"):
            continue
        name = poi.get("name")
        if not name:
            continue
        context = poi.get("moon_name") or poi.get("planet_name") or poi.get("star_system_name")
        poi_lines.append(f"{name} ({context})" if context else name)
    return MiningLocationInfo(
        commodity_name=commodity.get("name") or "Unknown",
        star_systems=_names_for_ids(commodity.get("ids_star_systems"), star_systems_by_id),
        planets=_names_for_ids(commodity.get("ids_planets"), planets_by_id),
        moons=_names_for_ids(commodity.get("ids_moons"), moons_by_id),
        mining_pois=sorted(set(poi_lines)),
        difficulty=get_mining_difficulty(commodity.get("name") or ""),
        mining_profile=get_mining_profile(commodity.get("name") or ""),
        hotspots=get_mining_hotspots(commodity.get("name") or ""),
    )
