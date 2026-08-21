"""Tests for the quality system: the 0-1000 -> quality_tier bucket mapping, deriving
which items are quality-bearing from averages rows, and the DB flag/store paths."""
from __future__ import annotations

import asyncio

from cryptography.fernet import Fernet

from bot.db.database import Database
from bot.sell_list import QUALITY_STEPS, quality_to_tier
from bot.uex.marketplace import extract_quality_item_ids


# -- quality_to_tier: UEX's exact (uneven) buckets ----------------------------


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


def test_picker_steps_cover_every_tier():
    # The select menu's fixed steps must be able to express all 8 tiers - notably 950,
    # since Q950-1000 starts mid-hundred and plain 100-steps would skip that boundary.
    assert {quality_to_tier(step) for step in QUALITY_STEPS} == set(range(8))


# -- extract_quality_item_ids -------------------------------------------------


def test_items_with_real_tiers_are_flagged_and_tier_zero_is_not():
    rows = [
        {"id_item": 1, "quality_tier": 0},  # Q0-only item: NOT quality-bearing
        {"id_item": 2, "quality_tier": 6},
        {"id_item": 2, "quality_tier": 7},  # duplicate id collapses into the set
        {"id_item": "3", "quality_tier": "1"},  # string-typed numbers still count
        {"id_item": None, "quality_tier": 5},  # unusable rows skipped
        {"id_item": 4},  # no tier at all
    ]
    assert extract_quality_item_ids(rows) == {2, 3}


# -- persistence --------------------------------------------------------------


def _make_db(tmp_path) -> Database:
    return Database(tmp_path / "test.sqlite3", Fernet(Fernet.generate_key()))


def test_quality_flags_are_sticky_and_queryable(tmp_path):
    async def run():
        db = _make_db(tmp_path)
        await db.init()
        await db.upsert_marketplace_item_activity(
            [
                {"id_item": 10, "item_name": "Laranite Raw", "negotiations_count": 5, "listings_count": 2},
                {"id_item": 11, "item_name": "WiDoW", "negotiations_count": 9, "listings_count": 4},
            ]
        )
        await db.mark_items_have_quality([10])
        assert await db.get_quality_flagged_item_ids([10, 11, None]) == {10}

        # A later snapshot that doesn't mention item 10 leaves the flag set (sticky), and a
        # re-observed activity row doesn't reset it either.
        await db.upsert_marketplace_item_activity(
            [{"id_item": 10, "item_name": "Laranite Raw", "negotiations_count": 6, "listings_count": 1}]
        )
        await db.mark_items_have_quality([])
        assert await db.get_quality_flagged_item_ids([10, 11]) == {10}

        # Flagging an id with no index row is a quiet no-op, not an error.
        await db.mark_items_have_quality([999])
        assert await db.get_quality_flagged_item_ids([999]) == set()

    asyncio.run(run())


def test_set_sell_list_quality_updates_matching_entry(tmp_path):
    async def run():
        db = _make_db(tmp_path)
        await db.init()
        await db.upsert_sell_list_items(
            1, [{"item_name": "Laranite Raw", "asking_price": 1200.0, "id_item": 10}]
        )

        # Case-insensitive name match, per the column's NOCASE collation.
        assert await db.set_sell_list_quality(1, "laranite raw", quality=950, quality_tier=7)
        rows = await db.list_user_sell_list(1)
        assert rows[0]["quality"] == 950 and rows[0]["quality_tier"] == 7

        # Wrong user or unknown item: no update, reported as False.
        assert not await db.set_sell_list_quality(2, "Laranite Raw", quality=500, quality_tier=2)
        assert not await db.set_sell_list_quality(1, "Gold", quality=500, quality_tier=2)
        rows = await db.list_user_sell_list(1)
        assert rows[0]["quality"] == 950  # untouched by the failed attempts

        # Re-adding the item (price update) keeps the stored quality.
        await db.upsert_sell_list_items(
            1, [{"item_name": "Laranite Raw", "asking_price": 1500.0, "id_item": None}]
        )
        rows = await db.list_user_sell_list(1)
        assert rows[0]["asking_price"] == 1500.0
        assert rows[0]["quality"] == 950 and rows[0]["quality_tier"] == 7

    asyncio.run(run())
