"""Static reference table of the richest known concentration location(s) per ore, sourced
from a community-maintained, data-mined ore-by-location table (SCMINER,
https://scminer.rocks/data/ore-by-location as of 2026-09) - NOT from UEX, which has no
per-site concentration/richness data at all (confirmed while building /where-to-mine's
location list). This is the second of two places in this bot using static, externally-
sourced game constants instead of live UEX data (see bot/uex/mining_difficulty.py for the
first) - it will silently go stale the next time a patch changes deposit distributions, and
nothing here will catch that.

Each ore's real source table lists a concentration percentage at every location it can
occur, often with many locations tied at the same top value (e.g. nine different Pyro
locations and asteroid belts all tie at 18% for Bexalite). The rule here is simple and
consistent across every ore: show every location tied at that ore's own real maximum
concentration, whatever that count happens to be - never an arbitrary fixed cap. An earlier
version of this table capped every ore at 3 example locations regardless of how many
actually tied at the max, which silently dropped real tied locations for eleven different
ores (as few as 4 shown when 7, 9, or even a complete 14-way tie for Quantainium were real) -
found only because a user checked specific locations against the source by hand. A fixed cap
looks tidier than a variable-length list, but "tidy" was hiding real, verifiable data; this
table now shows exactly what the source shows.

Two ores (Savrilium, Torite) have one massive standout location - Breaker Stations' Large
Geode at 100%, dwarfing every other spot for either ore (2-29%) - so those are correctly
single-entry, not a case of this table hiding anything. Diamond and Cobalt have no entry
here, matching UEX's own commodity data, which also has no location information for
either - both independent sources agree there's a real gap, not a lookup bug on either side.
"""
from __future__ import annotations

from dataclasses import dataclass

from bot.uex.mining_difficulty import strip_ore_suffix


@dataclass(frozen=True)
class MiningHotspot:
    location: str
    concentration_pct: float


_HOTSPOTS: dict[str, list[MiningHotspot]] = {
    "Agricium": [
        MiningHotspot("Aberdeen", 29), MiningHotspot("Cellin", 29), MiningHotspot("Daymar", 29),
        MiningHotspot("Ita", 29), MiningHotspot("Pyro V-e (Fuego)", 29), MiningHotspot("Yela", 29),
        MiningHotspot("Yela Asteroid Belt", 29),
    ],
    "Aluminum": [MiningHotspot("Hurston", 44)],
    "Aslarite": [
        MiningHotspot("Aberdeen", 29), MiningHotspot("Cellin", 29), MiningHotspot("Daymar", 29),
        MiningHotspot("Ita", 29), MiningHotspot("Pyro V-e (Fuego)", 29), MiningHotspot("Yela", 29),
        MiningHotspot("Yela Asteroid Belt", 29),
    ],
    "Beryl": [MiningHotspot("Aaron Halo", 18), MiningHotspot("Wala", 18)],
    "Bexalite": [
        MiningHotspot("Glaciem Ring", 18), MiningHotspot("Keeger Belt", 18), MiningHotspot("Pyro III (Bloom)", 18),
        MiningHotspot("Pyro IV", 18), MiningHotspot("Pyro V-a (Ignis)", 18), MiningHotspot("Pyro V-b (Vatra)", 18),
        MiningHotspot("Pyro V-c (Adir)", 18), MiningHotspot("Pyro V-f (Vuur)", 18), MiningHotspot("Pyro VI (Terminus)", 18),
    ],
    "Borase": [
        MiningHotspot("Glaciem Ring", 18), MiningHotspot("Keeger Belt", 18), MiningHotspot("Pyro III (Bloom)", 18),
        MiningHotspot("Pyro IV", 18), MiningHotspot("Pyro V-a (Ignis)", 18), MiningHotspot("Pyro V-b (Vatra)", 18),
        MiningHotspot("Pyro V-c (Adir)", 18), MiningHotspot("Pyro V-f (Vuur)", 18), MiningHotspot("Pyro VI (Terminus)", 18),
    ],
    "Copper": [MiningHotspot("Hurston", 44)],
    "Corundum": [MiningHotspot("Hurston", 44)],
    "Gold": [
        MiningHotspot("Glaciem Ring", 18), MiningHotspot("Keeger Belt", 18), MiningHotspot("Pyro III (Bloom)", 18),
        MiningHotspot("Pyro IV", 18), MiningHotspot("Pyro V-a (Ignis)", 18), MiningHotspot("Pyro V-b (Vatra)", 18),
        MiningHotspot("Pyro V-c (Adir)", 18), MiningHotspot("Pyro V-f (Vuur)", 18), MiningHotspot("Pyro VI (Terminus)", 18),
    ],
    "Hephaestanite": [MiningHotspot("Pyro V-a (Ignis)", 36), MiningHotspot("Pyro V-b (Vatra)", 36)],
    "Iron": [MiningHotspot("Pyro V-c (Adir)", 44)],
    "Jaclium": [MiningHotspot("Hathor Caves", 19)],
    "Laranite": [MiningHotspot("Pyro IV", 29), MiningHotspot("Pyro V-d (Fairo)", 29), MiningHotspot("Wala", 29)],
    "Lindinium": [MiningHotspot("Glaciem Ring", 10), MiningHotspot("Keeger Belt", 10)],
    "Ouratite": [
        MiningHotspot("Aberdeen", 10), MiningHotspot("Arial", 10), MiningHotspot("Hurston", 10),
        MiningHotspot("Yela Asteroid Belt", 10),
    ],
    # UEX's OWN location data for Quantainium is unusually thin (only ids_star_systems is
    # populated - no planet/moon/POI linkage at all), making this the only real location
    # detail /where-to-mine has for it at all. Exclusively a Stanton ore per the source -
    # entirely absent from Pyro and Hathor Caves.
    "Quantainium": [
        MiningHotspot("Aaron Halo", 2), MiningHotspot("Aberdeen", 2), MiningHotspot("Arial", 2),
        MiningHotspot("Calliope", 2), MiningHotspot("Cellin", 2), MiningHotspot("Clio", 2),
        MiningHotspot("Euterpe", 2), MiningHotspot("Hurston", 2), MiningHotspot("Ita", 2),
        MiningHotspot("Lyria", 2), MiningHotspot("Magda", 2), MiningHotspot("microTech", 2),
        MiningHotspot("Wala", 2), MiningHotspot("Yela", 2),
    ],
    "Quartz": [MiningHotspot("Daymar", 35), MiningHotspot("Pyro III (Bloom)", 35)],
    "Riccite": [
        MiningHotspot("Akiro Cluster", 10), MiningHotspot("Pyro III (Bloom)", 10), MiningHotspot("Pyro V-a (Ignis)", 10),
        MiningHotspot("Pyro V-c (Adir)", 10), MiningHotspot("Pyro VI (Terminus)", 10),
    ],
    "Savrilium": [MiningHotspot("Breaker Stations (Large Geode)", 100)],
    "Silicon": [MiningHotspot("Pyro V-a (Ignis)", 36), MiningHotspot("Pyro V-b (Vatra)", 36)],
    "Stileron": [
        MiningHotspot("Akiro Cluster", 2), MiningHotspot("Pyro I", 2), MiningHotspot("Pyro II (Monox)", 2),
        MiningHotspot("Pyro III (Bloom)", 2), MiningHotspot("Pyro IV", 2), MiningHotspot("Pyro VI (Terminus)", 2),
    ],
    "Taranite": [
        MiningHotspot("Cellin", 18), MiningHotspot("Clio", 18), MiningHotspot("Euterpe", 18), MiningHotspot("Yela", 18),
    ],
    "Tin": [MiningHotspot("Hurston", 44)],
    "Titanium": [
        MiningHotspot("Aberdeen", 29), MiningHotspot("Cellin", 29), MiningHotspot("Daymar", 29),
        MiningHotspot("Ita", 29), MiningHotspot("Pyro V-e (Fuego)", 29), MiningHotspot("Yela", 29),
        MiningHotspot("Yela Asteroid Belt", 29),
    ],
    "Torite": [MiningHotspot("Breaker Stations (Large Geode)", 100)],
    "Tungsten": [MiningHotspot("Pyro IV", 29), MiningHotspot("Pyro V-d (Fairo)", 29), MiningHotspot("Wala", 29)],
}


def get_mining_hotspots(commodity_name: str) -> list[MiningHotspot]:
    """The richest known concentration spot(s) for one ore, highest first, or an empty
    list if this ore isn't in the reference table (a real gap in the source, matching UEX's
    own lack of location data for the same ore - e.g. Diamond, Cobalt)."""
    return _HOTSPOTS.get(strip_ore_suffix(commodity_name), [])
