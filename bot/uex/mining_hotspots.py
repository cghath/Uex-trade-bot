"""Static reference table of the richest known concentration location(s) per ore, sourced
from a community-maintained, data-mined ore-by-location table (SCMINER,
https://scminer.rocks/data/ore-by-location as of 2026-09) - NOT from UEX, which has no
per-site concentration/richness data at all (confirmed while building /where-to-mine's
location list). This is the second of two places in this bot using static, externally-
sourced game constants instead of live UEX data (see bot/uex/mining_difficulty.py for the
first) - it will silently go stale the next time a patch changes deposit distributions, and
nothing here will catch that.

Each ore's real source table lists a concentration percentage at every location it can
occur, often with many locations tied at the same top value (e.g. Bexalite ties at 18% across
nine different Pyro locations and asteroid belts). Rather than dumping every tied location,
this keeps at most the top three named spots by concentration, preferring named asteroid
belts/POIs over bare moon names when both tie, since "Glaciem Ring" is a more actionable
answer in a Discord embed than a fourth moon name at the identical percentage. This is a
deliberate condensation of the real numbers, not a claim that untied locations are worse.

Two ores (Savrilium, Torite) have one massive standout location - Breaker Stations' Large
Geode at 100%, dwarfing every other spot for either ore (2-29%) - so those are single-entry.
Diamond and Cobalt have no entry here, matching UEX's own commodity data, which also has no
location information for either - both independent sources agree there's a real gap, not a
lookup bug on either side.
"""
from __future__ import annotations

from dataclasses import dataclass

from bot.uex.mining_difficulty import strip_ore_suffix


@dataclass(frozen=True)
class MiningHotspot:
    location: str
    concentration_pct: float


_HOTSPOTS: dict[str, list[MiningHotspot]] = {
    "Agricium": [MiningHotspot("Yela Asteroid Belt", 29), MiningHotspot("Cellin", 29), MiningHotspot("Daymar", 29)],
    "Aluminum": [MiningHotspot("Hurston", 44)],
    "Aslarite": [MiningHotspot("Yela Asteroid Belt", 29), MiningHotspot("Cellin", 29), MiningHotspot("Daymar", 29)],
    "Beryl": [MiningHotspot("Aaron Halo", 18), MiningHotspot("Wala", 18)],
    "Bexalite": [MiningHotspot("Glaciem Ring", 18), MiningHotspot("Keeger Belt", 18)],
    "Borase": [MiningHotspot("Glaciem Ring", 18), MiningHotspot("Keeger Belt", 18)],
    "Copper": [MiningHotspot("Hurston", 44), MiningHotspot("Clio", 40), MiningHotspot("Euterpe", 40)],
    "Corundum": [MiningHotspot("Hurston", 44)],
    "Gold": [MiningHotspot("Glaciem Ring", 18), MiningHotspot("Keeger Belt", 18)],
    "Hephaestanite": [MiningHotspot("Pyro V-a (Ignis)", 36), MiningHotspot("Pyro V-b (Vatra)", 36)],
    "Iron": [MiningHotspot("Pyro V-c (Adir)", 44)],
    "Jaclium": [MiningHotspot("Hathor Caves", 19)],
    "Laranite": [MiningHotspot("Pyro IV", 29), MiningHotspot("Pyro V-d (Fairo)", 29), MiningHotspot("Wala", 29)],
    "Lindinium": [MiningHotspot("Glaciem Ring", 10), MiningHotspot("Keeger Belt", 10)],
    "Ouratite": [MiningHotspot("Aberdeen", 10), MiningHotspot("Arial", 10), MiningHotspot("Hurston", 10)],
    # Quantainium ties at 2% almost everywhere it occurs at all - Aaron Halo is called out
    # by name in community guides as THE Quantainium hunting ground despite the flat
    # per-location figure, so it's kept as the one entry rather than an arbitrary pick
    # among many equal ties.
    "Quantainium": [MiningHotspot("Aaron Halo", 2)],
    "Quartz": [MiningHotspot("Daymar", 35), MiningHotspot("Pyro III (Bloom)", 35)],
    "Riccite": [MiningHotspot("Akiro Cluster", 10), MiningHotspot("Pyro III (Bloom)", 10)],
    "Savrilium": [MiningHotspot("Breaker Stations (Large Geode)", 100)],
    "Silicon": [MiningHotspot("Pyro V-a (Ignis)", 36), MiningHotspot("Pyro V-b (Vatra)", 36)],
    "Stileron": [MiningHotspot("Akiro Cluster", 2)],
    "Taranite": [MiningHotspot("Cellin", 18), MiningHotspot("Clio", 18), MiningHotspot("Euterpe", 18)],
    "Tin": [MiningHotspot("Hurston", 44)],
    "Titanium": [MiningHotspot("Yela Asteroid Belt", 29), MiningHotspot("Cellin", 29), MiningHotspot("Daymar", 29)],
    "Torite": [MiningHotspot("Breaker Stations (Large Geode)", 100)],
    "Tungsten": [MiningHotspot("Pyro IV", 29), MiningHotspot("Pyro V-d (Fairo)", 29), MiningHotspot("Wala", 29)],
}


def get_mining_hotspots(commodity_name: str) -> list[MiningHotspot]:
    """The richest known concentration spot(s) for one ore, highest first, or an empty
    list if this ore isn't in the reference table (a real gap in the source, matching UEX's
    own lack of location data for the same ore - e.g. Diamond, Cobalt)."""
    return _HOTSPOTS.get(strip_ore_suffix(commodity_name), [])
