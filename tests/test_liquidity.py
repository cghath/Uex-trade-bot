"""Tests for persistence of the Marketplace liquidity leaderboard."""
from __future__ import annotations

import asyncio

from cryptography.fernet import Fernet

from bot.db.database import Database


def _make_db(tmp_path) -> Database:
    return Database(tmp_path / "test.sqlite3", Fernet(Fernet.generate_key()))


def test_liquidity_scores_use_the_snapshot_and_replace_previous_values(tmp_path):
    async def run():
        db = _make_db(tmp_path)
        await db.init()

        count = await db.update_liquidity_scores(
            [
                {"item_name": "Gold", "negotiations_count": 4},
                {"item_name": "Silver", "negotiations_count": 9},
                {"item_name": "Ignored", "negotiations_count": 2},
                {"negotiations_count": 99},
            ]
        )
        assert count == 3
        rows = await db.get_top_liquidity_items()
        assert [(row["item_name"], row["score"]) for row in rows] == [
            ("Silver", 90.0),
            ("Gold", 40.0),
            ("Ignored", 20.0),
        ]

        count = await db.update_liquidity_scores([{"item_name": "Gold", "negotiations_count": 7}])
        assert count == 1
        rows = await db.get_top_liquidity_items()
        assert [(row["item_name"], row["score"]) for row in rows] == [
            ("Silver", 90.0),
            ("Gold", 70.0),
            ("Ignored", 20.0),
        ]

    asyncio.run(run())
