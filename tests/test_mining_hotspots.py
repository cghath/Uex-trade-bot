"""Tests for the static, community-sourced mining-hotspot reference table
(bot/uex/mining_hotspots.py) - concentration data, independent of the difficulty table."""
from __future__ import annotations

from bot.uex.mining_hotspots import MiningHotspot, get_mining_hotspots


def test_get_mining_hotspots_matches_the_raw_trade_name():
    hotspots = get_mining_hotspots("Iron (Ore)")
    assert hotspots == [MiningHotspot("Pyro V-c (Adir)", 44)]


def test_get_mining_hotspots_returns_multiple_tied_top_spots():
    hotspots = get_mining_hotspots("Copper (Ore)")
    assert hotspots[0] == MiningHotspot("Hurston", 44)
    assert len(hotspots) > 1


def test_get_mining_hotspots_is_empty_for_a_material_with_no_known_location():
    """Diamond has no entry, matching UEX's own lack of location data for it - both
    independent sources agree, not a lookup bug."""
    assert get_mining_hotspots("Diamond (Raw)") == []


def test_get_mining_hotspots_is_empty_for_an_unlisted_material():
    assert get_mining_hotspots("Nonexistent Ore") == []


def test_get_mining_hotspots_finds_jaclium_at_hathor_caves():
    """Confirms the user's own correction: Jaclium's only real source is the Hathor
    gameplay loop, not a standard mining deposit - this is the one ore in the whole table
    whose top (and only) spot is a cave, not an asteroid belt or moon."""
    assert get_mining_hotspots("Jaclium (Ore)") == [MiningHotspot("Hathor Caves", 19)]
