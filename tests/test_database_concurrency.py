"""Regression tests for Database's SQLite connection configuration.

Real production incident: two independent background collectors in
bot/cogs/intelligence.py (the 1h data-health loop and the 2h terminal-market loop, which
coincide every 2h since both start counting from the same bot-startup moment) each open
their own connection and can write around the same moment. Without an explicit
busy_timeout, aiosqlite/sqlite3's own default (5s) was in effect - confirmed via a
standalone repro (one connection holding a write transaction open indefinitely, a second
connection's write failing after exactly ~5.5s with 'database is locked'). Live logs
showed the terminal-market snapshot failing with exactly that error at a 1h/2h
coincidence point, silently dropping a collection cycle (caught and logged, not fatal,
but a real data-freshness gap - the digest then reports terminal-market data as
'overdue').
"""
from __future__ import annotations

import asyncio

from cryptography.fernet import Fernet

from bot.db.database import Database


def _make_db(tmp_path) -> Database:
    return Database(tmp_path / "concurrency.sqlite3", Fernet(Fernet.generate_key()))


def test_connections_use_wal_mode_and_a_generous_busy_timeout(tmp_path):
    async def run():
        db = _make_db(tmp_path)
        await db.init()
        async with db.connect() as conn:
            cursor = await conn.execute("PRAGMA journal_mode")
            (journal_mode,) = await cursor.fetchone()
            cursor = await conn.execute("PRAGMA busy_timeout")
            (busy_timeout,) = await cursor.fetchone()
        assert journal_mode.lower() == "wal"
        # Comfortably above aiosqlite/sqlite3's own 5s default, which measured
        # production contention (two collector loops coinciding every 2h) could still
        # occasionally exceed.
        assert busy_timeout >= 30000

    asyncio.run(run())


def test_a_writer_waits_out_a_brief_concurrent_lock_instead_of_failing_immediately(tmp_path):
    """A second connection's write while another briefly holds the write lock must wait
    for it (succeeding once the lock clears), not raise 'database is locked' outright -
    the actual shape of the two collector loops racing to write at the same tick. The
    6s hold is deliberately past aiosqlite/sqlite3's own 5s default timeout (confirmed:
    a connection with no explicit busy_timeout fails at ~5.5s against the same
    contention) - it's here to prove the 30s configured timeout actually covers
    contention the old implicit default wouldn't have, not just typical contention
    either default would tolerate."""
    async def run():
        db = _make_db(tmp_path)
        await db.init()

        async with db.connect() as blocker:
            await blocker.execute("BEGIN IMMEDIATE")
            await blocker.execute(
                "INSERT INTO price_alerts (channel_id, user_id, commodity_name, direction, target_price) "
                "VALUES (1, 1, 'Gold', 'sell_at_least', 100)"
            )

            async def release_after_delay():
                await asyncio.sleep(6)
                await blocker.commit()

            release_task = asyncio.create_task(release_after_delay())

            async with db.connect() as conn:
                await conn.execute(
                    "INSERT INTO price_alerts (channel_id, user_id, commodity_name, direction, target_price) "
                    "VALUES (2, 2, 'Iron', 'buy_at_most', 50)"
                )
                await conn.commit()

            await release_task

        async with db.connect() as conn:
            cursor = await conn.execute("SELECT COUNT(*) FROM price_alerts")
            (count,) = await cursor.fetchone()
        assert count == 2

    asyncio.run(run())
