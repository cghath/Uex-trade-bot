"""Static reference table of per-ore mining difficulty, sourced from community-maintained,
data-mined game constants (SC DataHub, https://sc-datahub.com/tools/mining/ores as of
2026-09) - NOT from UEX, which has no mining-mechanic data at all (no mass/resistance/
instability/charge field anywhere in its API; confirmed by inspecting the full endpoint
reference before building this). This is the one piece of static, externally-sourced
game-balance data in this bot; everywhere else pulls live from UEX or from this bot's own
collected observations. It will silently go stale the next time Star Citizen rebalances
mining, and nothing here will catch that - refresh manually against the source above if a
rating starts looking wrong.

Difficulty combines two of the source's four stats:
- instability: charge-level volatility (0-1000) - the source's own description of what
  makes a rock dangerous/hard to keep in its optimal charge window. Unambiguous: higher is
  always more volatile.
- resistance: how much laser power it takes to penetrate the rock - also unambiguous once
  read as a modifier around a zero baseline (the source's own description: "high resistance
  requiring high-power lasers"), so a negative value means below-baseline/easier, not an
  undefined quantity.
Optimal charge window and explosion multiplier are exposed as a side note only (see
OreMiningProfile.optimal_window), not folded into the rating - both also carry negative
values in the source, but unlike resistance, no plain-language description of what negative
means for either was found, so treating them as ranking inputs would be guessing rather than
reading real data.
"""
from __future__ import annotations

from dataclasses import dataclass

_TIER_ORDER = {"low": 0, "medium": 1, "high": 2}


@dataclass(frozen=True)
class OreMiningProfile:
    instability: float
    resistance: float
    optimal_window: float  # side note only - see module docstring


# Ore name (matching UEX's commodity name with " (Raw)"/" (Ore)" stripped) -> real game
# constants from the source above. Construction Materials are salvage-derived, not mined
# from a rock at all, so they're deliberately absent, not a gap. Cobalt is also absent -
# not listed in the source table at the time this was written; leave it out rather than
# guess a value.
_PROFILES: dict[str, OreMiningProfile] = {
    "Agricium": OreMiningProfile(instability=350, resistance=0.50, optimal_window=2.00),
    "Aluminum": OreMiningProfile(instability=0, resistance=-0.40, optimal_window=-0.50),
    "Aslarite": OreMiningProfile(instability=700, resistance=0.50, optimal_window=0.60),
    "Beryl": OreMiningProfile(instability=350, resistance=0.65, optimal_window=1.50),
    "Bexalite": OreMiningProfile(instability=600, resistance=0.60, optimal_window=0.40),
    "Borase": OreMiningProfile(instability=40, resistance=0.30, optimal_window=0.50),
    "Copper": OreMiningProfile(instability=50, resistance=-0.70, optimal_window=-0.90),
    "Corundum": OreMiningProfile(instability=50, resistance=0.10, optimal_window=0.50),
    "Diamond": OreMiningProfile(instability=0, resistance=-0.07, optimal_window=0.25),
    "Gold": OreMiningProfile(instability=550, resistance=0.50, optimal_window=2.10),
    "Hephaestanite": OreMiningProfile(instability=400, resistance=-0.30, optimal_window=0.50),
    "Iron": OreMiningProfile(instability=50, resistance=-0.40, optimal_window=-0.90),
    "Jaclium": OreMiningProfile(instability=100, resistance=0.50, optimal_window=3.00),
    "Laranite": OreMiningProfile(instability=400, resistance=0.50, optimal_window=0.50),
    "Lindinium": OreMiningProfile(instability=1000, resistance=0.95, optimal_window=0.23),
    "Ouratite": OreMiningProfile(instability=600, resistance=0.60, optimal_window=0.60),
    "Quantainium": OreMiningProfile(instability=1000, resistance=0.95, optimal_window=2.30),
    "Quartz": OreMiningProfile(instability=50, resistance=-0.70, optimal_window=0.50),
    "Riccite": OreMiningProfile(instability=850, resistance=0.95, optimal_window=2.30),
    "Savrilium": OreMiningProfile(instability=1000, resistance=0.95, optimal_window=2.30),
    "Silicon": OreMiningProfile(instability=50, resistance=-0.20, optimal_window=0.50),
    "Stileron": OreMiningProfile(instability=870, resistance=0.60, optimal_window=0.60),
    "Taranite": OreMiningProfile(instability=700, resistance=0.50, optimal_window=0.60),
    "Tin": OreMiningProfile(instability=0, resistance=-0.20, optimal_window=0.50),
    "Titanium": OreMiningProfile(instability=0, resistance=0.10, optimal_window=-0.70),
    "Torite": OreMiningProfile(instability=550, resistance=0.25, optimal_window=2.10),
    "Tungsten": OreMiningProfile(instability=0, resistance=-0.40, optimal_window=-0.70),
}

# Tier cutoffs split at the real gaps in the actual values (nothing sits between 100-200 or
# 400-550 instability; nothing between 0.30-0.50 or 0.65-0.95 resistance), not an arbitrary
# evenly-spaced threshold.
_INSTABILITY_LOW_MAX = 100
_INSTABILITY_MEDIUM_MAX = 400
_RESISTANCE_LOW_MAX = 0.30
_RESISTANCE_MEDIUM_MAX = 0.65


def strip_ore_suffix(name: str) -> str:
    """'Quantainium (Raw)' -> 'Quantainium', 'Iron (Ore)' -> 'Iron' - UEX suffixes a raw
    commodity's own trade name, but the difficulty table (like the refined counterpart's
    name) uses the bare ore name."""
    for suffix in (" (Raw)", " (Ore)"):
        if name.endswith(suffix):
            return name[: -len(suffix)]
    return name


def get_mining_profile(commodity_name: str) -> OreMiningProfile | None:
    """The raw stats for one ore, or None if it isn't in the reference table (a
    salvage-derived material that isn't mined from a rock at all, or a real gap in the
    source - both look the same from here)."""
    return _PROFILES.get(strip_ore_suffix(commodity_name))


def _instability_tier(instability: float) -> str:
    if instability <= _INSTABILITY_LOW_MAX:
        return "low"
    if instability <= _INSTABILITY_MEDIUM_MAX:
        return "medium"
    return "high"


def _resistance_tier(resistance: float) -> str:
    if resistance <= _RESISTANCE_LOW_MAX:
        return "low"
    if resistance <= _RESISTANCE_MEDIUM_MAX:
        return "medium"
    return "high"


def get_mining_difficulty(commodity_name: str) -> str | None:
    """'low' / 'medium' / 'high' overall, or None if this ore isn't in the reference table.
    Takes the WORSE of the instability tier and the resistance tier, not an average -
    either dimension alone can make a rock genuinely hard (a volatile-but-soft rock is not
    the same problem as a stable-but-dense one, but both are real problems), so averaging
    them would understate whichever one is actually the binding difficulty."""
    profile = get_mining_profile(commodity_name)
    if profile is None:
        return None
    instability_tier = _instability_tier(profile.instability)
    resistance_tier = _resistance_tier(profile.resistance)
    return max(instability_tier, resistance_tier, key=lambda tier: _TIER_ORDER[tier])
