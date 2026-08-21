"""Tests for the want-to-sell list: slot pairing rules (bot/sell_list.py) and the
user_sell_list persistence layer (bot/db/database.py).

The DB tests run against a real throwaway SQLite file via asyncio.run, so they exercise
the actual schema (NOCASE uniqueness, upsert COALESCE) rather than a mock.
"""
from __future__ import annotations

import asyncio

from cryptography.fernet import Fernet

from bot.db.database import Database
from bot.sell_list import pair_sell_list_inputs


def _slots(*pairs):
    """Pad explicit (item, price) pairs out to the full 10 empty slots."""
    padded = list(pairs) + [(None, None)] * (10 - len(pairs))
    return padded


# -- pairing/validation ------------------------------------------------------


def test_single_item_and_price():
    entries, errors = pair_sell_list_inputs(_slots(("Laranite Raw", 1200.0)))
    assert errors == []
    assert len(entries) == 1
    assert entries[0].item_name == "Laranite Raw"
    assert entries[0].asking_price == 1200.0


def test_multiple_items_preserve_slot_order():
    entries, errors = pair_sell_list_inputs(
        _slots(("Gold", 500.0), ("Quantanium", 9000.0), ("Beryl Raw", 300.0))
    )
    assert errors == []
    assert [e.item_name for e in entries] == ["Gold", "Quantanium", "Beryl Raw"]


def test_item_without_price_is_an_error():
    entries, errors = pair_sell_list_inputs(_slots(("Gold", 500.0), ("Quantanium", None)))
    assert entries == [] or errors  # nothing saved when any error exists (caller checks errors)
    assert len(errors) == 1
    assert "item2" in errors[0] and "price2" in errors[0]


def test_price_without_item_is_an_error():
    _, errors = pair_sell_list_inputs(_slots(("Gold", 500.0), (None, 750.0)))
    assert len(errors) == 1
    assert "price2" in errors[0] and "item2" in errors[0]


def test_whitespace_item_counts_as_missing():
    _, errors = pair_sell_list_inputs(_slots(("   ", 750.0)))
    assert len(errors) == 1
    assert "price1" in errors[0]


def test_gap_slots_are_fine():
    # Users can fill item1 and item5, skipping the middle - slot numbers, not density, matter.
    entries, errors = pair_sell_list_inputs(
        _slots(("Gold", 500.0), (None, None), (None, None), (None, None), ("Beryl Raw", 300.0))
    )
    assert errors == []
    assert [e.item_name for e in entries] == ["Gold", "Beryl Raw"]


def test_duplicate_item_case_insensitive_is_an_error():
    _, errors = pair_sell_list_inputs(_slots(("Gold", 500.0), ("gold", 600.0)))
    assert len(errors) == 1
    assert "item1" in errors[0] and "item2" in errors[0]


def test_zero_or_negative_price_is_an_error():
    _, errors = pair_sell_list_inputs(_slots(("Gold", 0.0), ("Beryl Raw", -5.0)))
    assert len(errors) == 2


def test_item_name_is_stripped():
    entries, errors = pair_sell_list_inputs(_slots(("  Gold  ", 500.0)))
    assert errors == []
    assert entries[0].item_name == "Gold"


def test_completely_empty_submission_yields_an_error():
    entries, errors = pair_sell_list_inputs(_slots())
    assert entries == []
    assert len(errors) == 1


# -- persistence -------------------------------------------------------------


def _make_db(tmp_path) -> Database:
    return Database(tmp_path / "test.sqlite3", Fernet(Fernet.generate_key()))


def test_upsert_classifies_added_vs_updated(tmp_path):
    async def run():
        db = _make_db(tmp_path)
        await db.init()
        added, updated = await db.upsert_sell_list_items(
            1, [{"item_name": "Gold", "asking_price": 500.0, "id_item": 7}]
        )
        assert added == ["Gold"] and updated == []

        added, updated = await db.upsert_sell_list_items(
            1,
            [
                {"item_name": "Gold", "asking_price": 650.0, "id_item": 7},
                {"item_name": "Beryl Raw", "asking_price": 300.0, "id_item": None},
            ],
        )
        assert added == ["Beryl Raw"] and updated == ["Gold"]

        rows = await db.list_user_sell_list(1)
        by_name = {row["item_name"]: row for row in rows}
        assert by_name["Gold"]["asking_price"] == 650.0  # re-add updated the price
        assert len(rows) == 2

    asyncio.run(run())


def test_readding_with_different_case_updates_same_row(tmp_path):
    async def run():
        db = _make_db(tmp_path)
        await db.init()
        await db.upsert_sell_list_items(1, [{"item_name": "Gold", "asking_price": 500.0, "id_item": None}])
        added, updated = await db.upsert_sell_list_items(
            1, [{"item_name": "GOLD", "asking_price": 900.0, "id_item": None}]
        )
        assert added == [] and updated == ["GOLD"]
        rows = await db.list_user_sell_list(1)
        assert len(rows) == 1  # NOCASE uniqueness: still one row, not two
        assert rows[0]["asking_price"] == 900.0

    asyncio.run(run())


def test_readd_without_id_keeps_previously_resolved_id(tmp_path):
    async def run():
        db = _make_db(tmp_path)
        await db.init()
        await db.upsert_sell_list_items(1, [{"item_name": "Gold", "asking_price": 500.0, "id_item": 42}])
        await db.upsert_sell_list_items(1, [{"item_name": "Gold", "asking_price": 600.0, "id_item": None}])
        rows = await db.list_user_sell_list(1)
        assert rows[0]["id_item"] == 42  # COALESCE kept the good id

    asyncio.run(run())


def test_lists_are_per_user_and_removal_is_scoped(tmp_path):
    async def run():
        db = _make_db(tmp_path)
        await db.init()
        await db.upsert_sell_list_items(1, [{"item_name": "Gold", "asking_price": 500.0, "id_item": None}])
        await db.upsert_sell_list_items(2, [{"item_name": "Gold", "asking_price": 999.0, "id_item": None}])

        user1_rows = await db.list_user_sell_list(1)
        assert len(user1_rows) == 1 and user1_rows[0]["asking_price"] == 500.0

        # user 2 can't delete user 1's entry
        assert not await db.remove_sell_list_item(user1_rows[0]["id"], user_id=2)
        assert await db.remove_sell_list_item(user1_rows[0]["id"], user_id=1)
        assert await db.list_user_sell_list(1) == []
        assert len(await db.list_user_sell_list(2)) == 1

    asyncio.run(run())


def test_list_is_sorted_by_name_case_insensitively(tmp_path):
    async def run():
        db = _make_db(tmp_path)
        await db.init()
        await db.upsert_sell_list_items(
            1,
            [
                {"item_name": "quantanium", "asking_price": 1.0, "id_item": None},
                {"item_name": "Beryl Raw", "asking_price": 1.0, "id_item": None},
                {"item_name": "gold", "asking_price": 1.0, "id_item": None},
            ],
        )
        rows = await db.list_user_sell_list(1)
        assert [row["item_name"] for row in rows] == ["Beryl Raw", "gold", "quantanium"]

    asyncio.run(run())
