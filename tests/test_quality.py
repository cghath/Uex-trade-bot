"""Tests for the 0-1000 -> quality_tier bucket mapping the scanner relies on to compare
a listing only against its own tier's average (bot/uex/marketplace.py: quality_to_tier).
Which items/tiers are quality-bearing is covered by tests/test_tier_stats.py."""
from __future__ import annotations

from bot.uex.marketplace import quality_to_tier


def test_tier_boundaries_match_uex_buckets():
    # 0 = Q0, 1 = Q1-499, 2 = Q500-599, 3 = Q600-699, 4 = Q700-799,
    # 5 = Q800-899, 6 = Q900-949, 7 = Q950-1000
    expectations = [
        (0, 0),
        (1, 1), (250, 1), (499, 1),
        (500, 2), (599, 2),
        (600, 3), (699, 3),
        (700, 4), (799, 4),
        (800, 5), (899, 5),
        (900, 6), (949, 6),
        (950, 7), (1000, 7),
    ]
    for quality, expected_tier in expectations:
        assert quality_to_tier(quality) == expected_tier, (quality, expected_tier)


def test_out_of_range_is_clamped_not_raised():
    assert quality_to_tier(-50) == 0
    assert quality_to_tier(1300) == 7
