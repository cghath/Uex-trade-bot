"""Tests for persistence of the Marketplace liquidity leaderboard."""
from __future__ import annotations

import asyncio
import sqlite3

from cryptography.fernet import Fernet

from bot.db.database import Database
from bot.cogs.liquidity import _format_rating_change
from bot.cogs.help import _add_command_fields
from bot.cogs.digest import _format_sellability_digest
from bot.uex.marketplace import compute_liquidity_score


def _make_db(tmp_path) -> Database:
    return Database(tmp_path / "test.sqlite3", Fernet(Fernet.generate_key()))


def test_rating_change_display_uses_plain_language_and_a_bounded_scale():
    assert _format_rating_change(45, 72) == "📈 **Up 27 points** · 45 → 72 / 100"
    assert _format_rating_change(38, 36) == "📉 **Down 2 points** · 38 → 36 / 100"
    assert _format_rating_change(14, 15) == "📈 **Up 1 point** · 14 → 15 / 100"


def test_intro_splits_oversized_command_categories_without_truncating_lines():
    class FakeEmbed:
        def __init__(self):
            self.fields = []

        def add_field(self, **kwargs):
            self.fields.append(kwargs)

    embed = FakeEmbed()
    lines = ["a" * 600, "b" * 600, "c" * 20]
    _add_command_fields(embed, "Marketplace", lines)
    assert [field["name"] for field in embed.fields] == ["Marketplace", "Marketplace (continued)"]
    assert [field["value"] for field in embed.fields] == ["a" * 600, ("b" * 600) + "\n" + ("c" * 20)]
    assert all(len(field["value"]) <= 1024 for field in embed.fields)


def test_digest_sellability_section_includes_rankings_and_rating_shifts():
    value = _format_sellability_digest(
        [{"item_name": "Gold", "id_item": 1, "score": 74}],
        [{"item_name": "Gold", "id_item": 1, "previous_score": 70, "current_score": 74}],
    )
    assert "**Best to list now**" in value
    assert "**74/100**" in value
    assert "📈" in value
    assert "up 4 pts (70 → 74)" in value


def test_liquidity_score_weights_completed_and_open_negotiations_against_sell_supply():
    score = compute_liquidity_score(
        {"negotiations_success": 20, "negotiations_open": 20, "listings_count": 100, "listings_count_sell": 10}
    )
    assert score == 43.48


def test_liquidity_score_is_bounded_and_buy_postings_raise_sellability():
    without_buy_orders = compute_liquidity_score(
        {"negotiations_success": 10, "listings_count_sell": 5, "listings_count_buy": 0}
    )
    with_buy_orders = compute_liquidity_score(
        {"negotiations_success": 10, "listings_count_sell": 5, "listings_count_buy": 5}
    )
    assert 0 < without_buy_orders < with_buy_orders < 100


def test_liquidity_score_is_zero_when_there_are_no_active_listings():
    assert compute_liquidity_score({"negotiations_success": 50, "listings_count": 0}) == 0.0


def test_liquidity_score_falls_back_to_total_negotiations_when_detail_is_missing():
    assert compute_liquidity_score({"negotiations_count": 10, "listings_count": 5}) == 29.41


def test_liquidity_scores_use_the_snapshot_and_replace_previous_values(tmp_path):
    async def run():
        db = _make_db(tmp_path)
        await db.init()

        count = await db.update_liquidity_scores(
            [
                {"id_item": 1, "item_name": "Gold", "negotiations_success": 4, "listings_count": 10},
                {"id_item": 2, "item_name": "Silver", "negotiations_success": 9, "listings_count": 10},
                {"id_item": 3, "item_name": "Ignored", "negotiations_success": 2, "listings_count": 10},
                {"negotiations_success": 99, "listings_count": 1},
            ]
        )
        assert count == 3
        rows = await db.get_top_liquidity_items()
        assert [(row["item_name"], row["score"]) for row in rows] == [
                ("Silver", 18.75),
                ("Gold", 9.3),
                ("Ignored", 4.88),
        ]

        count = await db.update_liquidity_scores(
            [{"id_item": 1, "item_name": "Gold", "negotiations_success": 7, "listings_count": 10}]
        )
        assert count == 1
        rows = await db.get_top_liquidity_items()
        assert [(row["item_name"], row["score"]) for row in rows] == [
                ("Silver", 18.75),
                ("Gold", 15.22),
                ("Ignored", 4.88),
        ]
        assert rows[1]["id_item"] == 1
        assert rows[1]["listings_count"] == 10
        assert len(await db.get_liquidity_history("Gold")) == 1
        assert [row["item_name"] for row in await db.find_liquidity_items("il")] == ["Silver"]

    asyncio.run(run())


def test_liquidity_movers_compare_oldest_and_newest_snapshot_in_window(tmp_path):
    async def run():
        db = _make_db(tmp_path)
        await db.init()
        async with db.connect() as sqlite:
            await sqlite.executescript(
                """INSERT INTO liquidity_score_snapshots
                   (id_item, item_name, score, negotiations_success, negotiations_open, listings_count, listings_count_sell, listings_count_buy, recorded_hour)
                   VALUES (1, 'Rising', 100, 1, 0, 1, 1, 0, datetime('now', '-2 hours'));
                   INSERT INTO liquidity_score_snapshots
                   (id_item, item_name, score, negotiations_success, negotiations_open, listings_count, listings_count_sell, listings_count_buy, recorded_hour)
                   VALUES (1, 'Rising', 240, 2, 0, 1, 1, 0, datetime('now', '-1 hours'));
                   INSERT INTO liquidity_score_snapshots
                   (id_item, item_name, score, negotiations_success, negotiations_open, listings_count, listings_count_sell, listings_count_buy, recorded_hour)
                   VALUES (2, 'Falling', 300, 3, 0, 1, 1, 0, datetime('now', '-2 hours'));
                   INSERT INTO liquidity_score_snapshots
                   (id_item, item_name, score, negotiations_success, negotiations_open, listings_count, listings_count_sell, listings_count_buy, recorded_hour)
                   VALUES (2, 'Falling', 220, 2, 0, 1, 1, 0, datetime('now', '-1 hours'));"""
            )
            await sqlite.commit()
        movers = await db.get_liquidity_movers()
        assert [(row["item_name"], row["score_change"]) for row in movers] == [
            ("Rising", 140.0),
            ("Falling", -80.0),
        ]

    asyncio.run(run())


def test_existing_liquidity_table_is_migrated_before_new_snapshots_are_written(tmp_path):
    path = tmp_path / "test.sqlite3"
    with sqlite3.connect(path) as sqlite:
        sqlite.execute(
            """CREATE TABLE liquidity_scores (
                   item_name TEXT PRIMARY KEY, score REAL NOT NULL,
                   last_updated TEXT NOT NULL DEFAULT (datetime('now'))
               )"""
        )
        sqlite.execute("INSERT INTO liquidity_scores (item_name, score) VALUES ('Old row', 10)")

    async def run():
        db = Database(path, Fernet(Fernet.generate_key()))
        await db.init()
        await db.update_liquidity_scores(
            [{"id_item": 1, "item_name": "New row", "negotiations_success": 3, "listings_count": 1}]
        )
        rows = await db.get_top_liquidity_items()
        assert rows[0]["item_name"] == "New row"
        assert rows[0]["id_item"] == 1

    asyncio.run(run())
