"""Tests for the change-only UEX data-intelligence persistence layer."""
from __future__ import annotations

import asyncio

from cryptography.fernet import Fernet

from bot.db.database import Database


def _make_db(tmp_path) -> Database:
    return Database(tmp_path / "intelligence.sqlite3", Fernet(Fernet.generate_key()))


def test_terminal_market_history_only_records_initial_and_changed_states(tmp_path):
    async def run():
        db = _make_db(tmp_path)
        await db.init()
        row = {
            "id_commodity": "1", "id_terminal": "2", "commodity_name": "Gold",
            "terminal_name": "Area18 TDD", "price_buy": "100", "price_sell": "120",
            "scu_buy": "50", "scu_sell": "25", "status_buy": "3", "status_sell": "1",
            "quality": "900", "volatility_price_buy": "0.2", "volatility_price_sell": "0.4",
            "price_buy_users_rows": "7", "price_sell_users_rows": "5",
        }
        assert await db.record_terminal_market_snapshot([row]) == (1, 1)
        assert await db.record_terminal_market_snapshot([row]) == (0, 1)
        row["status_sell"] = "2"
        assert await db.record_terminal_market_snapshot([row]) == (1, 1)
        async with db.connect() as sqlite:
            cursor = await sqlite.execute("SELECT COUNT(*) AS count FROM terminal_market_observations")
            assert (await cursor.fetchone())["count"] == 2

    asyncio.run(run())


def test_reference_flags_preserve_uex_zero_and_one_strings(tmp_path):
    async def run():
        db = _make_db(tmp_path)
        await db.init()
        assert await db.upsert_commodity_reference(
            [{"id": "1", "name": "Safe Cargo", "is_illegal": "0", "is_volatile_qt": "1"}]
        ) == 1
        async with db.connect() as sqlite:
            cursor = await sqlite.execute(
                "SELECT is_illegal, is_volatile_qt FROM commodity_reference WHERE id_commodity = 1"
            )
            row = await cursor.fetchone()
            assert (row["is_illegal"], row["is_volatile_qt"]) == (0, 1)

    asyncio.run(run())


def test_data_health_and_fuel_snapshots_are_change_only(tmp_path):
    async def run():
        db = _make_db(tmp_path)
        await db.init()
        health = {
            "id_terminal": 9, "type": "commodity", "terminal_name": "Port Tressler",
            "prices_total": 10, "prices_updated": 8, "prices_updated_percentage": 80,
            "last_update_days": 1, "has_recent_reports": True,
        }
        fuel = {
            "id_commodity": 7, "id_terminal": 9, "commodity_name": "Quantum Fuel",
            "terminal_name": "Port Tressler", "price_buy": "2.5", "price_sell": None,
        }
        assert await db.record_terminal_data_health_snapshot([health]) == (1, 1)
        assert await db.record_terminal_data_health_snapshot([health]) == (0, 1)
        assert await db.record_fuel_price_snapshot([fuel]) == (1, 1)
        assert await db.record_fuel_price_snapshot([fuel]) == (0, 1)

    asyncio.run(run())
