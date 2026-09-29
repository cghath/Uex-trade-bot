"""Two things that grew on the Pi without limit (audit REL-9, REL-10).

REL-9: UexClient's response cache only ever replaced an entry when the same key was asked
for again, so high-variety keys (distance pairs, per-listing lookups) piled up for as long
as the bot ran. REL-10: liquidity_score_snapshots gained ~12k rows a day with no retention
(445k rows after 37 days on the Pi), both of its queries scanned the whole table, and one
of its indexes exactly duplicated the primary key.
"""
import asyncio

import aiosqlite
from cryptography.fernet import Fernet

from bot.db import database as database_module
from bot.db.database import Database
from bot.uex import client as client_module
from bot.uex.client import UexClient


# -- REL-9: the UEX response cache is bounded ------------------------------------------------

def _client() -> UexClient:
    return UexClient(app_token="test", base_url="https://uex.test")


def test_expired_cache_entries_are_swept():
    client = _client()
    for i in range(50):
        client._store_cached(("GET", "old", i), -1, "stale")  # already expired
    assert len(client._cache) == 50, "not swept yet"
    for i in range(client_module._CACHE_SWEEP_EVERY):
        client._store_cached(("GET", "fresh", i), 3600, "live")
    assert not any(key[1] == "old" for key in client._cache), "expired entries swept"
    assert sum(key[1] == "fresh" for key in client._cache) == client_module._CACHE_SWEEP_EVERY
    asyncio.run(client.aclose())


def test_cache_never_grows_past_its_cap_and_drops_the_oldest_writes(monkeypatch):
    monkeypatch.setattr(client_module, "_CACHE_MAX_ENTRIES", 10)
    client = _client()
    for i in range(25):
        client._store_cached(("GET", "k", i), 3600, i)
    assert len(client._cache) == 10
    assert sorted(key[2] for key in client._cache) == list(range(15, 25)), "the newest 10 survive"
    client._store_cached(("GET", "k", 15), 3600, "rewritten")  # a rewrite counts as newest
    client._store_cached(("GET", "k", 99), 3600, "new")
    assert ("GET", "k", 15) in client._cache and ("GET", "k", 16) not in client._cache
    asyncio.run(client.aclose())


# -- REL-10: liquidity snapshots are kept 14 days and indexed --------------------------------

def _db(tmp_path) -> Database:
    return Database(tmp_path / "liquidity.sqlite3", Fernet(Fernet.generate_key()))


async def _add_snapshot(db: Database, id_item: int, hours_ago: int) -> None:
    async with db.connect() as conn:
        await conn.execute(
            """INSERT INTO liquidity_score_snapshots
               (id_item, item_name, score, negotiations_success, negotiations_open, listings_count,
                listings_count_sell, listings_count_buy, recorded_hour)
               VALUES (?, ?, 10, 1, 0, 1, 1, 0, strftime('%Y-%m-%d %H:00:00', 'now', ?))""",
            (id_item, f"Item {id_item}", f"-{hours_ago} hours"),
        )
        await conn.commit()


def test_snapshots_older_than_the_retention_window_are_dropped(tmp_path):
    async def run():
        db = _db(tmp_path)
        await db.init()
        retention_hours = database_module.LIQUIDITY_SNAPSHOT_RETENTION_DAYS * 24
        await _add_snapshot(db, 1, retention_hours + 5)   # too old
        await _add_snapshot(db, 2, retention_hours - 5)   # still inside the window
        await _add_snapshot(db, 3, 24 * 7)                # /liquidity-trends' own 7-day view
        await db.update_liquidity_scores([{"id_item": 9, "item_name": "Gold", "negotiations_success": 1,
                                           "listings_count": 1}])
        async with db.connect() as conn:
            cursor = await conn.execute("SELECT id_item FROM liquidity_score_snapshots ORDER BY id_item")
            return [row[0] for row in await cursor.fetchall()]

    assert asyncio.run(run()) == [2, 3, 9]
    assert database_module.LIQUIDITY_SNAPSHOT_RETENTION_DAYS * 24 >= 2 * 24 * 7, \
        "keeps at least twice the longest window any command reads"


def test_snapshot_queries_use_an_index_and_the_duplicate_is_gone(tmp_path):
    async def run():
        db = _db(tmp_path)
        await db.init()
        await db.init()  # the schema re-runs on every startup; must stay idempotent
        async with aiosqlite.connect(tmp_path / "liquidity.sqlite3") as conn:
            indexes = [row[1] async for row in await conn.execute("PRAGMA index_list(liquidity_score_snapshots)")]
            plans = []
            for query in (
                "SELECT * FROM liquidity_score_snapshots WHERE recorded_hour >= datetime('now', '-24 hours')",
                "SELECT * FROM liquidity_score_snapshots WHERE item_name = 'gold' COLLATE NOCASE "
                "AND recorded_hour >= datetime('now', '-168 hours')",
            ):
                steps = [row[-1] async for row in await conn.execute("EXPLAIN QUERY PLAN " + query)]
                plans.append(" ".join(steps))
        return indexes, plans

    indexes, plans = asyncio.run(run())
    assert "idx_liquidity_snapshots_item_time" not in indexes
    assert {"idx_liquidity_snapshots_hour", "idx_liquidity_snapshots_name_hour"} <= set(indexes)
    assert "idx_liquidity_snapshots_hour" in plans[0], plans[0]
    assert "idx_liquidity_snapshots_name_hour" in plans[1], plans[1]


def test_pruning_runs_in_small_batches_capped_per_call(tmp_path, monkeypatch):
    """The Pi's backlog was ~278k rows: as one DELETE it held the write lock for seconds.
    Batches are separate transactions, capped per call, so a backlog drains over several
    hourly runs instead of one long stall."""
    monkeypatch.setattr(database_module, "LIQUIDITY_PRUNE_BATCH_ROWS", 2)
    monkeypatch.setattr(database_module, "LIQUIDITY_PRUNE_MAX_BATCHES", 2)

    async def run():
        db = _db(tmp_path)
        await db.init()
        too_old = database_module.LIQUIDITY_SNAPSHOT_RETENTION_DAYS * 24 + 10
        for id_item in range(5):
            await _add_snapshot(db, id_item, too_old)
        return [await db.prune_liquidity_snapshots() for _ in range(3)]

    assert asyncio.run(run()) == [4, 1, 0]
