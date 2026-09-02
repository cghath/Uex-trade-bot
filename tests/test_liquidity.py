"""Tests for persistence of the Marketplace liquidity leaderboard."""
from __future__ import annotations

import asyncio
import sqlite3
from datetime import datetime, timezone

from cryptography.fernet import Fernet

from bot.db.database import Database
from bot.cogs.liquidity import _format_rating_change
from bot.cogs.help import _add_command_fields
from bot.cogs.digest import _format_data_freshness, _format_sellability_digest_fields
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
    rankings, shifts_up, shifts_down = _format_sellability_digest_fields(
        [{"item_name": "Gold", "id_item": 1, "score": 74}],
        [{"item_name": "Gold", "id_item": 1, "previous_score": 70, "current_score": 74}],
        [{"item_name": "Silver", "id_item": 2, "previous_score": 50, "current_score": 43}],
    )
    assert "**74/100**" in rankings
    assert "📈" in shifts_up
    assert "up 4 pts (70 → 74)" in shifts_up
    assert "📉" in shifts_down
    assert "down 7 pts (50 → 43)" in shifts_down
    assert all(len(field) <= 1024 for field in (rankings, shifts_up, shifts_down))


def test_digest_data_freshness_is_compact_and_flags_overdue_collectors():
    value = _format_data_freshness(
        {
            "terminal_market": "2026-08-25 11:30:00",
            "liquidity": "2026-08-25 14:30:00",
            "marketplace": None,
        },
        now=datetime(2026, 8, 25, 15, 0, tzinfo=timezone.utc),
    )
    assert "✅ **Liquidity ratings:** 30m ago" in value
    assert "⚠️ **Terminal markets:** 3h 30m ago · overdue" in value
    assert "⚠️ **Marketplace index:** not collected yet" in value
    assert len(value) <= 1024


def test_marking_digest_posted_is_persisted(tmp_path):
    async def run():
        db = _make_db(tmp_path)
        await db.init()
        await db.set_guild_digest_config(guild_id=1, channel_id=2, hour_utc=12)
        await db.mark_guild_digest_posted(1, "2026-08-25")
        config = await db.get_guild_digest_config(1)
        assert config["last_posted_date"] == "2026-08-25"

    asyncio.run(run())


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


def test_liquidity_score_ranks_demand_signals_completed_over_open_over_buy_postings():
    """Regression test: the weights must stay ordered completed > open > buy posting.

    An earlier version weighted buy postings at 2.0 - twice a completed sale - so an item
    with zero actual sales scored higher than one with five completed sales. For a
    "will this sell?" rating that is backwards, and it contradicted the module's own
    description of buy postings as the weaker, shorter-lived signal. The existing tests
    only asserted that buy postings raise the score at all, so nothing caught it.
    """
    supply = {"listings_count_sell": 5}
    five_completed = compute_liquidity_score({**supply, "negotiations_success": 5})
    five_open = compute_liquidity_score({**supply, "negotiations_open": 5})
    five_buy_postings = compute_liquidity_score({**supply, "listings_count_buy": 5})

    assert five_completed > five_open > five_buy_postings > 0
    # The specific failure that motivated this: real sales must outrank pure want-ads.
    assert five_completed > five_buy_postings


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


def test_liquidity_movers_can_be_limited_independently_by_direction(tmp_path):
    async def run():
        db = _make_db(tmp_path)
        await db.init()
        async with db.connect() as sqlite:
            for item_id in range(1, 7):
                await sqlite.execute(
                    """INSERT INTO liquidity_score_snapshots
                       (id_item, item_name, score, negotiations_success, negotiations_open,
                        listings_count, listings_count_sell, listings_count_buy, recorded_hour)
                       VALUES (?, ?, 10, 1, 0, 1, 1, 0, datetime('now', '-2 hours'))""",
                    (item_id, f"Rising {item_id}"),
                )
                await sqlite.execute(
                    """INSERT INTO liquidity_score_snapshots
                       (id_item, item_name, score, negotiations_success, negotiations_open,
                        listings_count, listings_count_sell, listings_count_buy, recorded_hour)
                       VALUES (?, ?, ?, 1, 0, 1, 1, 0, datetime('now', '-1 hour'))""",
                    (item_id, f"Rising {item_id}", 10 + item_id),
                )
            for item_id in range(7, 13):
                await sqlite.execute(
                    """INSERT INTO liquidity_score_snapshots
                       (id_item, item_name, score, negotiations_success, negotiations_open,
                        listings_count, listings_count_sell, listings_count_buy, recorded_hour)
                       VALUES (?, ?, 20, 1, 0, 1, 1, 0, datetime('now', '-2 hours'))""",
                    (item_id, f"Falling {item_id}"),
                )
                await sqlite.execute(
                    """INSERT INTO liquidity_score_snapshots
                       (id_item, item_name, score, negotiations_success, negotiations_open,
                        listings_count, listings_count_sell, listings_count_buy, recorded_hour)
                       VALUES (?, ?, ?, 1, 0, 1, 1, 0, datetime('now', '-1 hour'))""",
                    (item_id, f"Falling {item_id}", 20 - (item_id - 6)),
                )
            await sqlite.commit()
        gainers = await db.get_liquidity_movers(limit=4, direction="up")
        losers = await db.get_liquidity_movers(limit=4, direction="down")
        assert len(gainers) == len(losers) == 4
        assert all(row["score_change"] > 0 for row in gainers)
        assert all(row["score_change"] < 0 for row in losers)

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
