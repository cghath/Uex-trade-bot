"""Tests for the static, community-sourced mining-difficulty reference table
(bot/uex/mining_difficulty.py) - the one place in this bot that isn't backed by live UEX
data or a collected observation."""
from __future__ import annotations

from bot.uex.mining_difficulty import (
    get_mining_difficulty,
    get_mining_profile,
    strip_ore_suffix,
)


def test_strip_ore_suffix_removes_raw_and_ore_suffixes():
    assert strip_ore_suffix("Quantainium (Raw)") == "Quantainium"
    assert strip_ore_suffix("Iron (Ore)") == "Iron"


def test_strip_ore_suffix_leaves_a_bare_name_untouched():
    assert strip_ore_suffix("Quantainium") == "Quantainium"


def test_get_mining_profile_matches_the_raw_trade_name():
    profile = get_mining_profile("Quantainium (Raw)")
    assert profile is not None
    assert profile.instability == 1000
    assert profile.resistance == 0.95


def test_get_mining_profile_is_none_for_an_unlisted_material():
    """Construction Materials are salvage-derived, not mined from a rock - correctly
    absent, not a lookup bug."""
    assert get_mining_profile("Construction Material Pebbles") is None
    assert get_mining_profile("Nonexistent Ore") is None


def test_get_mining_difficulty_high_instability_and_high_resistance_is_high():
    assert get_mining_difficulty("Quantainium (Raw)") == "high"


def test_get_mining_difficulty_low_instability_and_low_resistance_is_low():
    assert get_mining_difficulty("Iron (Ore)") == "low"


def test_get_mining_difficulty_takes_the_worse_of_the_two_dimensions():
    """Jaclium has LOW instability (100) but MEDIUM resistance (0.50) - the combined
    rating must reflect the worse dimension (medium), not the better one, and not average
    them into something softer than either real risk."""
    assert get_mining_difficulty("Jaclium (Ore)") == "medium"


def test_get_mining_difficulty_high_instability_alone_is_enough_for_high():
    """Gold has HIGH instability (550) but only MEDIUM resistance (0.50) - still high
    overall, since either dimension alone can make a rock genuinely dangerous."""
    assert get_mining_difficulty("Gold (Ore)") == "high"


def test_get_mining_difficulty_is_none_for_an_unlisted_material():
    assert get_mining_difficulty("Nonexistent Ore") is None
