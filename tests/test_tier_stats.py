"""Tests for per-tier Marketplace "sub-item" stats: the extract_tier_stats parser
(bot/uex/marketplace.py) and the marketplace_item_tier_stats persistence layer
(bot/db/database.py).

The DB tests run against a real throwaway SQLite file via asyncio.run, so they exercise
the actual schema (the 5-column composite primary key and upsert semantics), not a mock.
"""
from __future__ import annotations

import asyncio

from cryptography.fernet import Fernet

from bot.db.database import Database
from bot.uex.marketplace import extract_tier_stats


def _row(**overrides) -> dict:
    """A realistic /marketplace_prices_averages_all row; numbers as JSON strings on
    purpose, since that's how Marketplace endpoints have been observed to send them."""
    row = {
        "id": 1,
        "id_item": "42",
        "item_name": "Laranite Raw",
        "quality_tier": "6",
        "operation": "sell",
        "currency": "UEC",
        "unit": "scu",
        "listings_count": "5",
        "price_avg": "1180",
        "price_avg_week": "1200.5",
        "price_avg_month": "1234",
        "game_version": "4.3",
    }
    row.update(overrides)
    return row


# -- extract_tier_stats ------------------------------------------------------


def test_string_numbers_are_coerced():
    stats = extract_tier_stats([_row()])
    assert stats == [
        {
            "id_item": 42,
            "item_name": "Laranite Raw",
            "quality_tier": 6,
            "operation": "sell",
            "currency": "UEC",
            "unit": "scu",
            "listings_count": 5,
            "price_avg": 1180.0,
            "price_avg_week": 1200.5,
            "price_avg_month": 1234.0,
        }
    ]


def test_tier_zero_is_kept_as_a_real_tier():
    stats = extract_tier_stats([_row(quality_tier=0)])
    assert len(stats) == 1
    assert stats[0]["quality_tier"] == 0


def test_rows_missing_identity_are_dropped():
    rows = [
        _row(id_item=None),
        _row(quality_tier=None),
        _row(quality_tier="garbage"),
        _row(item_name=None),
        _row(item_name=""),
        _row(operation=None),
        _row(),
    ]
    assert len(extract_tier_stats(rows)) == 1


def test_priceless_row_is_kept_its_existence_is_the_signal():
    # Unlike the display parser, a row with no parsable prices still proves the tier
    # sub-item trades - it must survive so get_known_quality_tiers can see it.
    stats = extract_tier_stats([_row(price_avg=None, price_avg_week=None, price_avg_month=None)])
    assert len(stats) == 1
    assert stats[0]["price_avg"] is None


def test_operation_is_normalized_and_defaults_applied():
    stats = extract_tier_stats([_row(operation=" SELL ", currency=None, unit=None, listings_count=None)])
    assert stats[0]["operation"] == "sell"
    assert stats[0]["currency"] == "UEC"
    assert stats[0]["unit"] == "unit"
    assert stats[0]["listings_count"] == 0


# -- marketplace_item_tier_stats persistence ---------------------------------


def _make_db(tmp_path) -> Database:
    return Database(tmp_path / "test.sqlite3", Fernet(Fernet.generate_key()))


def test_each_tier_combo_is_its_own_row(tmp_path):
    async def run():
        db = _make_db(tmp_path)
        await db.init()
        await db.upsert_marketplace_tier_stats(
            extract_tier_stats(
                [
                    _row(quality_tier=1),
                    _row(quality_tier=6),
                    _row(quality_tier=6, operation="buy"),
                    _row(quality_tier=6, currency="WIF"),
                ]
            )
        )
        rows = await db.get_item_tier_stats(42)
        assert len(rows) == 4
        combos, quality_items = await db.count_marketplace_tier_stats()
        assert combos == 4
        assert quality_items == 1

    asyncio.run(run())


def test_reobserved_combo_updates_in_place_not_duplicates(tmp_path):
    async def run():
        db = _make_db(tmp_path)
        await db.init()
        await db.upsert_marketplace_tier_stats(extract_tier_stats([_row()]))
        first = (await db.get_item_tier_stats(42))[0]
        await db.upsert_marketplace_tier_stats(
            extract_tier_stats([_row(listings_count="9", price_avg="1500")])
        )
        rows = await db.get_item_tier_stats(42)
        assert len(rows) == 1
        assert rows[0]["listings_count"] == 9
        assert rows[0]["price_avg"] == 1500.0
        assert rows[0]["first_seen"] == first["first_seen"]

    asyncio.run(run())


def test_absent_combos_survive_later_snapshots(tmp_path):
    # The dump only covers UEX's rolling activity window - a tier that traded once must
    # stay known even when later snapshots no longer include it.
    async def run():
        db = _make_db(tmp_path)
        await db.init()
        await db.upsert_marketplace_tier_stats(extract_tier_stats([_row(quality_tier=7)]))
        await db.upsert_marketplace_tier_stats(extract_tier_stats([_row(quality_tier=1)]))
        tiers = await db.get_known_quality_tiers([42])
        assert tiers == {42: {1, 7}}

    asyncio.run(run())


def test_known_quality_tiers_excludes_tier_zero_and_unknown_ids(tmp_path):
    async def run():
        db = _make_db(tmp_path)
        await db.init()
        await db.upsert_marketplace_tier_stats(
            extract_tier_stats(
                [
                    _row(id_item=42, quality_tier=0),
                    _row(id_item=42, quality_tier=3),
                    _row(id_item=99, item_name="Pistol", quality_tier=0),
                ]
            )
        )
        tiers = await db.get_known_quality_tiers([42, 99, 1234, None])
        assert tiers == {42: {3}}
        assert await db.get_known_quality_tiers([]) == {}

    asyncio.run(run())


def test_tier_stats_ordered_sell_first_then_tier_ascending(tmp_path):
    async def run():
        db = _make_db(tmp_path)
        await db.init()
        await db.upsert_marketplace_tier_stats(
            extract_tier_stats(
                [
                    _row(operation="buy", quality_tier=1),
                    _row(operation="sell", quality_tier=7),
                    _row(operation="sell", quality_tier=2),
                ]
            )
        )
        rows = await db.get_item_tier_stats(42)
        assert [(r["operation"], r["quality_tier"]) for r in rows] == [
            ("sell", 2),
            ("sell", 7),
            ("buy", 1),
        ]

    asyncio.run(run())


def test_empty_upsert_is_a_noop(tmp_path):
    async def run():
        db = _make_db(tmp_path)
        await db.init()
        await db.upsert_marketplace_tier_stats([])
        assert await db.count_marketplace_tier_stats() == (0, 0)

    asyncio.run(run())
