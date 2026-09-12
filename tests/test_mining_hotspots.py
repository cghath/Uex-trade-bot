"""Tests for the static, community-sourced mining-hotspot reference table
(bot/uex/mining_hotspots.py) - concentration data, independent of the difficulty table."""
from __future__ import annotations

from bot.uex.mining_hotspots import MiningHotspot, get_mining_hotspots


def test_get_mining_hotspots_matches_the_raw_trade_name():
    hotspots = get_mining_hotspots("Iron (Ore)")
    assert hotspots == [MiningHotspot("Pyro V-c (Adir)", 44)]


def test_get_mining_hotspots_returns_multiple_tied_top_spots():
    """Ouratite ties 4 ways at 10% - unlike Copper, whose Clio/Euterpe entries (40%) turned
    out not to be tied with Hurston's real max (44%) at all and were removed."""
    hotspots = get_mining_hotspots("Ouratite (Raw)")
    assert hotspots[0] == MiningHotspot("Aberdeen", 10)
    assert len(hotspots) > 1


def test_get_mining_hotspots_returns_only_the_true_max_not_a_near_miss():
    """Copper's real max is Hurston at 44% - Clio/Euterpe (40%) are close but not tied,
    so the strict "only the real max" rule excludes them entirely rather than keeping
    them as runner-up examples."""
    assert get_mining_hotspots("Copper (Ore)") == [MiningHotspot("Hurston", 44)]


def test_get_mining_hotspots_uncaps_a_large_tied_group():
    """Bexalite/Borase/Gold each tie 9 ways at 18% - the same under-capturing bug found
    for Quantainium (a fixed top-3 cap) was found here too during the full-table audit,
    just with a different-sized real max group."""
    hotspots = get_mining_hotspots("Bexalite (Ore)")
    assert len(hotspots) == 9
    assert all(spot.concentration_pct == 18 for spot in hotspots)


def test_get_mining_hotspots_stileron_is_a_complete_tie_like_quantainium():
    """Stileron's real max (2%) is shared by every one of its 6 known locations - a
    complete tie, not a top group within a longer list, previously shown as a single
    Akiro Cluster entry under the old top-3-style cap."""
    hotspots = get_mining_hotspots("Stileron (Ore)")
    assert len(hotspots) == 6
    assert all(spot.concentration_pct == 2 for spot in hotspots)


def test_get_mining_hotspots_is_empty_for_a_material_with_no_known_location():
    """Diamond has no entry, matching UEX's own lack of location data for it - both
    independent sources agree, not a lookup bug."""
    assert get_mining_hotspots("Diamond (Raw)") == []


def test_get_mining_hotspots_is_empty_for_an_unlisted_material():
    assert get_mining_hotspots("Nonexistent Ore") == []


def test_get_mining_hotspots_quantainium_shows_the_full_tied_list_not_a_top_three():
    """Every one of Quantainium's 14 real locations ties at the exact same 2% - unlike
    every other ore in this table, there's no real "top group" to condense to 3, so the
    complete list is shown rather than an arbitrary subset. UEX's own location data for
    Quantainium is unusually thin (only star system, no planet/moon/POI), making this the
    only real location detail /where-to-mine has for it at all."""
    hotspots = get_mining_hotspots("Quantainium (Raw)")
    assert {spot.location for spot in hotspots} == {
        "Aaron Halo", "Aberdeen", "Arial", "Calliope", "Cellin", "Clio", "Euterpe",
        "Hurston", "Ita", "Lyria", "Magda", "microTech", "Wala", "Yela",
    }
    assert all(spot.concentration_pct == 2 for spot in hotspots)


def test_get_mining_hotspots_finds_jaclium_at_hathor_caves():
    """Confirms the user's own correction: Jaclium's only real source is the Hathor
    gameplay loop, not a standard mining deposit - this is the one ore in the whole table
    whose top (and only) spot is a cave, not an asteroid belt or moon."""
    assert get_mining_hotspots("Jaclium (Ore)") == [MiningHotspot("Hathor Caves", 19)]
