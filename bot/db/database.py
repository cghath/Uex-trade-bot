"""SQLite persistence: price alerts and cached trade-log entries.

Uses aiosqlite so DB calls don't block the Discord event loop. Schema is created/
migrated idempotently on startup via CREATE TABLE IF NOT EXISTS.
"""
from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, AsyncIterator

import aiosqlite
from cryptography.fernet import Fernet, InvalidToken

SCHEMA = """
CREATE TABLE IF NOT EXISTS price_alerts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id INTEGER,
    channel_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    commodity_name TEXT NOT NULL,
    direction TEXT NOT NULL CHECK (direction IN ('sell_at_least', 'buy_at_most')),
    target_price REAL NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    triggered_at TEXT,
    active INTEGER NOT NULL DEFAULT 1
);

CREATE INDEX IF NOT EXISTS idx_price_alerts_active ON price_alerts (active);

CREATE TABLE IF NOT EXISTS trade_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    commodity_name TEXT NOT NULL,
    terminal_name TEXT,
    quantity_scu REAL,
    unit_price REAL,
    operation TEXT NOT NULL CHECK (operation IN ('buy', 'sell')),
    logged_at TEXT NOT NULL DEFAULT (datetime('now')),
    note TEXT
);

CREATE INDEX IF NOT EXISTS idx_trade_log_user ON trade_log (user_id);

CREATE TABLE IF NOT EXISTS user_credentials (
    user_id INTEGER PRIMARY KEY,
    encrypted_secret_key TEXT NOT NULL,
    linked_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS user_ship_preference (
    user_id INTEGER PRIMARY KEY,
    ship_name TEXT NOT NULL,
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS marketplace_alerts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    keyword TEXT NOT NULL,
    operation TEXT NOT NULL CHECK (operation IN ('buy', 'sell')),
    target_price REAL,
    min_quality REAL,
    max_quality REAL,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    active INTEGER NOT NULL DEFAULT 1
);

CREATE INDEX IF NOT EXISTS idx_marketplace_alerts_active ON marketplace_alerts (active);

-- Per-alert dedup so a persistent watch (unlike one-shot price alerts) doesn't re-DM the
-- same listing every poll - only genuinely new matching listings notify.
CREATE TABLE IF NOT EXISTS marketplace_alert_seen_listings (
    alert_id INTEGER NOT NULL,
    listing_id INTEGER NOT NULL,
    seen_at TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (alert_id, listing_id)
);

CREATE TABLE IF NOT EXISTS guild_digest_config (
    guild_id INTEGER PRIMARY KEY,
    channel_id INTEGER NOT NULL,
    hour_utc INTEGER NOT NULL,
    enabled INTEGER NOT NULL DEFAULT 1,
    last_posted_date TEXT,
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Accumulating index of Marketplace items the bot has actually observed being traded, built
-- by periodically snapshotting /marketplace_trends (which only ever exposes ~100 items live
-- at once) and merging in whatever's new. Grows past that ~100 ceiling over time as UEX's own
-- top-100 window rotates through different items across polls. Powers autocomplete on the
-- Marketplace commands so suggestions are scoped to things people actually trade, not the
-- full multi-thousand-item catalog.
CREATE TABLE IF NOT EXISTS marketplace_item_activity (
    id_item INTEGER PRIMARY KEY,
    item_name TEXT NOT NULL,
    negotiations_count INTEGER NOT NULL DEFAULT 0,
    listings_count INTEGER NOT NULL DEFAULT 0,
    first_seen TEXT NOT NULL DEFAULT (datetime('now')),
    last_seen TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Commodity restock watches: unlike price_alerts (one-shot), these are persistent - a
-- background poller checks /commodities_prices across every terminal and notifies whenever
-- one flips from no-stock to has-stock. ship_query is optional (falls back to the watcher's
-- /set-default-ship at notify time if unset) and is only used to describe how much of a
-- restock would fill that ship's hold, not to filter/gate the alert itself. `scope` picks the
-- delivery: 'global' posts in the channel the alert was created in and pings the creator
-- (visible to everyone else there too); 'personal' DMs only the creator, nobody else sees it.
-- Two people watching the same commodity in the same channel stay fully independent alerts,
-- even both on 'global' - no merging/deduping of the underlying watch.
CREATE TABLE IF NOT EXISTS stock_alerts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id INTEGER,
    channel_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    commodity_name TEXT NOT NULL,
    ship_query TEXT,
    scope TEXT NOT NULL DEFAULT 'global' CHECK (scope IN ('personal', 'global')),
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    active INTEGER NOT NULL DEFAULT 1
);

CREATE INDEX IF NOT EXISTS idx_stock_alerts_active ON stock_alerts (active);

-- Per-alert, per-terminal last-known availability, so the poller can tell a genuine
-- empty->available transition (worth notifying) from "still in stock since last poll"
-- (not worth re-notifying every 30 minutes while it just sits there).
CREATE TABLE IF NOT EXISTS stock_alert_terminal_state (
    alert_id INTEGER NOT NULL,
    id_terminal INTEGER NOT NULL,
    was_available INTEGER NOT NULL DEFAULT 0,
    last_seen_scu REAL,
    updated_at TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (alert_id, id_terminal)
);
"""


class Database:
    def __init__(self, path: Path, fernet: Fernet) -> None:
        self._path = path
        self._fernet = fernet
        self._path.parent.mkdir(parents=True, exist_ok=True)

    async def init(self) -> None:
        async with aiosqlite.connect(self._path) as db:
            await db.executescript(SCHEMA)
            await self._run_migrations(db)
            await db.commit()

    async def _run_migrations(self, db: aiosqlite.Connection) -> None:
        """Additive-only migrations for columns added to a table after it may have already
        been created (via CREATE TABLE IF NOT EXISTS above, which only creates - it never
        alters an existing table). SQLite has no "ADD COLUMN IF NOT EXISTS", so each ALTER is
        just attempted and a "duplicate column" failure (already-migrated or freshly-created
        with the column already in SCHEMA) is treated as success, not an error.
        """
        migrations = [
            "ALTER TABLE marketplace_alerts ADD COLUMN min_quality REAL",
            "ALTER TABLE marketplace_alerts ADD COLUMN max_quality REAL",
            # A DB created before the personal/global scope option existed has stock_alerts
            # rows with no `scope` column at all - this backfills them as 'global', matching
            # their actual existing behavior (channel post, ping the creator) exactly, so
            # nothing changes for anyone's already-running watches.
            "ALTER TABLE stock_alerts ADD COLUMN scope TEXT NOT NULL DEFAULT 'global'",
        ]
        for statement in migrations:
            try:
                await db.execute(statement)
            except aiosqlite.OperationalError as exc:
                if "duplicate column name" not in str(exc).lower():
                    raise

    @asynccontextmanager
    async def connect(self) -> AsyncIterator[aiosqlite.Connection]:
        db = await aiosqlite.connect(self._path)
        db.row_factory = aiosqlite.Row
        try:
            yield db
        finally:
            await db.close()

    # -- price alerts ---------------------------------------------------

    async def add_price_alert(
        self,
        *,
        guild_id: int | None,
        channel_id: int,
        user_id: int,
        commodity_name: str,
        direction: str,
        target_price: float,
    ) -> int:
        async with self.connect() as db:
            cursor = await db.execute(
                """INSERT INTO price_alerts
                   (guild_id, channel_id, user_id, commodity_name, direction, target_price)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (guild_id, channel_id, user_id, commodity_name, direction, target_price),
            )
            await db.commit()
            return cursor.lastrowid

    async def list_active_alerts(self) -> list[dict[str, Any]]:
        async with self.connect() as db:
            cursor = await db.execute("SELECT * FROM price_alerts WHERE active = 1")
            rows = await cursor.fetchall()
            return [dict(row) for row in rows]

    async def list_user_alerts(self, user_id: int) -> list[dict[str, Any]]:
        async with self.connect() as db:
            cursor = await db.execute(
                "SELECT * FROM price_alerts WHERE user_id = ? AND active = 1", (user_id,)
            )
            rows = await cursor.fetchall()
            return [dict(row) for row in rows]

    async def deactivate_alert(self, alert_id: int) -> None:
        async with self.connect() as db:
            await db.execute(
                "UPDATE price_alerts SET active = 0, triggered_at = datetime('now') WHERE id = ?",
                (alert_id,),
            )
            await db.commit()

    async def remove_alert(self, alert_id: int, user_id: int) -> bool:
        async with self.connect() as db:
            cursor = await db.execute(
                "DELETE FROM price_alerts WHERE id = ? AND user_id = ?", (alert_id, user_id)
            )
            await db.commit()
            return cursor.rowcount > 0

    # -- trade log --------------------------------------------------------

    async def log_trade(
        self,
        *,
        user_id: int,
        commodity_name: str,
        operation: str,
        terminal_name: str | None = None,
        quantity_scu: float | None = None,
        unit_price: float | None = None,
        note: str | None = None,
    ) -> int:
        async with self.connect() as db:
            cursor = await db.execute(
                """INSERT INTO trade_log
                   (user_id, commodity_name, terminal_name, quantity_scu, unit_price, operation, note)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (user_id, commodity_name, terminal_name, quantity_scu, unit_price, operation, note),
            )
            await db.commit()
            return cursor.lastrowid

    async def get_trade_log(self, user_id: int, limit: int = 20) -> list[dict[str, Any]]:
        async with self.connect() as db:
            cursor = await db.execute(
                "SELECT * FROM trade_log WHERE user_id = ? ORDER BY logged_at DESC LIMIT ?",
                (user_id, limit),
            )
            rows = await cursor.fetchall()
            return [dict(row) for row in rows]

    # -- per-user UEX credentials ------------------------------------------
    # Each Discord user can link their own UEX secret_key so /uex-trades (and any
    # future user-scoped endpoint) reads *their* account, not a single shared one.
    # Keys are encrypted at rest with the Fernet key in data/credentials.key.

    async def set_user_secret_key(self, user_id: int, secret_key: str) -> None:
        encrypted = self._fernet.encrypt(secret_key.encode()).decode()
        async with self.connect() as db:
            await db.execute(
                """INSERT INTO user_credentials (user_id, encrypted_secret_key, linked_at)
                   VALUES (?, ?, datetime('now'))
                   ON CONFLICT(user_id) DO UPDATE SET
                       encrypted_secret_key = excluded.encrypted_secret_key,
                       linked_at = datetime('now')""",
                (user_id, encrypted),
            )
            await db.commit()

    async def get_user_secret_key(self, user_id: int) -> str | None:
        async with self.connect() as db:
            cursor = await db.execute(
                "SELECT encrypted_secret_key FROM user_credentials WHERE user_id = ?",
                (user_id,),
            )
            row = await cursor.fetchone()
            if not row:
                return None
            try:
                return self._fernet.decrypt(row["encrypted_secret_key"].encode()).decode()
            except InvalidToken:
                # Key file changed/lost since this was stored; treat as unlinked.
                return None

    async def has_linked_uex_account(self, user_id: int) -> bool:
        return await self.get_user_secret_key(user_id) is not None

    async def remove_user_secret_key(self, user_id: int) -> bool:
        async with self.connect() as db:
            cursor = await db.execute(
                "DELETE FROM user_credentials WHERE user_id = ?", (user_id,)
            )
            await db.commit()
            return cursor.rowcount > 0

    # -- per-user default ship (for cargo/SCU math on /best-route) ------------
    # Just the ship name is stored - it's re-resolved against UEX's /vehicles list
    # (cached client-side) at use time, so a ship rename on UEX's end doesn't leave
    # a stale id pointing at the wrong thing.

    async def set_default_ship(self, user_id: int, ship_name: str) -> None:
        async with self.connect() as db:
            await db.execute(
                """INSERT INTO user_ship_preference (user_id, ship_name, updated_at)
                   VALUES (?, ?, datetime('now'))
                   ON CONFLICT(user_id) DO UPDATE SET
                       ship_name = excluded.ship_name,
                       updated_at = datetime('now')""",
                (user_id, ship_name),
            )
            await db.commit()

    async def get_default_ship(self, user_id: int) -> str | None:
        async with self.connect() as db:
            cursor = await db.execute(
                "SELECT ship_name FROM user_ship_preference WHERE user_id = ?", (user_id,)
            )
            row = await cursor.fetchone()
            return row["ship_name"] if row else None

    async def clear_default_ship(self, user_id: int) -> bool:
        async with self.connect() as db:
            cursor = await db.execute(
                "DELETE FROM user_ship_preference WHERE user_id = ?", (user_id,)
            )
            await db.commit()
            return cursor.rowcount > 0

    async def list_linked_user_ids(self) -> list[int]:
        """All Discord user ids with a linked UEX account - used by /leaderboard to know
        who to pull verified /user_trades for."""
        async with self.connect() as db:
            cursor = await db.execute("SELECT user_id FROM user_credentials")
            rows = await cursor.fetchall()
            return [row["user_id"] for row in rows]

    # -- marketplace listing alerts -----------------------------------------
    # Unlike price_alerts (one-shot: fire once, deactivate), these are persistent watches -
    # new matching listings keep appearing over time, so each alert stays active and instead
    # dedups per listing id (marketplace_alert_seen_listings) so the same listing never
    # notifies twice.

    async def add_marketplace_alert(
        self,
        *,
        user_id: int,
        keyword: str,
        operation: str,
        target_price: float | None = None,
        min_quality: float | None = None,
        max_quality: float | None = None,
    ) -> int:
        async with self.connect() as db:
            cursor = await db.execute(
                """INSERT INTO marketplace_alerts (user_id, keyword, operation, target_price, min_quality, max_quality)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (user_id, keyword, operation, target_price, min_quality, max_quality),
            )
            await db.commit()
            return cursor.lastrowid

    async def list_active_marketplace_alerts(self) -> list[dict[str, Any]]:
        async with self.connect() as db:
            cursor = await db.execute("SELECT * FROM marketplace_alerts WHERE active = 1")
            rows = await cursor.fetchall()
            return [dict(row) for row in rows]

    async def list_user_marketplace_alerts(self, user_id: int) -> list[dict[str, Any]]:
        async with self.connect() as db:
            cursor = await db.execute(
                "SELECT * FROM marketplace_alerts WHERE user_id = ? AND active = 1", (user_id,)
            )
            rows = await cursor.fetchall()
            return [dict(row) for row in rows]

    async def remove_marketplace_alert(self, alert_id: int, user_id: int) -> bool:
        async with self.connect() as db:
            cursor = await db.execute(
                "DELETE FROM marketplace_alerts WHERE id = ? AND user_id = ?", (alert_id, user_id)
            )
            await db.execute(
                "DELETE FROM marketplace_alert_seen_listings WHERE alert_id = ?", (alert_id,)
            )
            await db.commit()
            return cursor.rowcount > 0

    async def get_seen_marketplace_listing_ids(self, alert_id: int) -> set[int]:
        async with self.connect() as db:
            cursor = await db.execute(
                "SELECT listing_id FROM marketplace_alert_seen_listings WHERE alert_id = ?", (alert_id,)
            )
            rows = await cursor.fetchall()
            return {row["listing_id"] for row in rows}

    async def mark_marketplace_listing_seen(self, alert_id: int, listing_id: int) -> None:
        async with self.connect() as db:
            await db.execute(
                """INSERT OR IGNORE INTO marketplace_alert_seen_listings (alert_id, listing_id)
                   VALUES (?, ?)""",
                (alert_id, listing_id),
            )
            await db.commit()

    # -- per-guild daily digest config ---------------------------------------

    async def set_guild_digest_config(self, *, guild_id: int, channel_id: int, hour_utc: int) -> None:
        async with self.connect() as db:
            await db.execute(
                """INSERT INTO guild_digest_config (guild_id, channel_id, hour_utc, enabled, updated_at)
                   VALUES (?, ?, ?, 1, datetime('now'))
                   ON CONFLICT(guild_id) DO UPDATE SET
                       channel_id = excluded.channel_id,
                       hour_utc = excluded.hour_utc,
                       enabled = 1,
                       updated_at = datetime('now')""",
                (guild_id, channel_id, hour_utc),
            )
            await db.commit()

    async def disable_guild_digest(self, guild_id: int) -> bool:
        async with self.connect() as db:
            cursor = await db.execute(
                "UPDATE guild_digest_config SET enabled = 0, updated_at = datetime('now') WHERE guild_id = ? AND enabled = 1",
                (guild_id,),
            )
            await db.commit()
            return cursor.rowcount > 0

    async def get_guild_digest_config(self, guild_id: int) -> dict[str, Any] | None:
        async with self.connect() as db:
            cursor = await db.execute(
                "SELECT * FROM guild_digest_config WHERE guild_id = ?", (guild_id,)
            )
            row = await cursor.fetchone()
            return dict(row) if row else None

    async def list_enabled_guild_digest_configs(self) -> list[dict[str, Any]]:
        async with self.connect() as db:
            cursor = await db.execute("SELECT * FROM guild_digest_config WHERE enabled = 1")
            rows = await cursor.fetchall()
            return [dict(row) for row in rows]

    async def mark_guild_digest_posted(self, guild_id: int, date_str: str) -> None:
        async with self.connect() as db:
            await db.execute(
                "UPDATE guild_digest_config SET last_posted_date = ? WHERE guild_id = ?",
                (date_str, guild_id),
            )
            await db.commit()

    # -- accumulating Marketplace traded-items index --------------------------

    async def upsert_marketplace_item_activity(self, rows: list[dict[str, Any]]) -> None:
        """Merge a fresh /marketplace_trends snapshot into the accumulating index. Each row's
        negotiations_count/listings_count is UEX's own point-in-time stat, not a per-poll
        delta, so a re-observed item is overwritten with the latest values, never summed
        across polls - only first_seen is preserved across updates, tracking how long the
        bot has known about the item."""
        params = [
            (r["id_item"], r["item_name"], r.get("negotiations_count") or 0, r.get("listings_count") or 0)
            for r in rows
            if r.get("id_item") is not None and r.get("item_name")
        ]
        if not params:
            return
        async with self.connect() as db:
            await db.executemany(
                """INSERT INTO marketplace_item_activity (id_item, item_name, negotiations_count, listings_count, last_seen)
                   VALUES (?, ?, ?, ?, datetime('now'))
                   ON CONFLICT(id_item) DO UPDATE SET
                       item_name = excluded.item_name,
                       negotiations_count = excluded.negotiations_count,
                       listings_count = excluded.listings_count,
                       last_seen = datetime('now')""",
                params,
            )
            await db.commit()

    async def list_marketplace_item_activity(self) -> list[dict[str, Any]]:
        async with self.connect() as db:
            cursor = await db.execute("SELECT * FROM marketplace_item_activity")
            rows = await cursor.fetchall()
            return [dict(row) for row in rows]

    async def count_marketplace_item_activity(self) -> int:
        async with self.connect() as db:
            cursor = await db.execute("SELECT COUNT(*) AS c FROM marketplace_item_activity")
            row = await cursor.fetchone()
            return row["c"] if row else 0

    # -- commodity restock (stock) alerts ------------------------------------
    # Persistent watches, like marketplace alerts - see stock_alert_terminal_state above for
    # how repeat-notification is avoided while a terminal just stays in stock.

    async def add_stock_alert(
        self,
        *,
        guild_id: int | None,
        channel_id: int,
        user_id: int,
        commodity_name: str,
        ship_query: str | None = None,
        scope: str = "global",
    ) -> int:
        async with self.connect() as db:
            cursor = await db.execute(
                """INSERT INTO stock_alerts (guild_id, channel_id, user_id, commodity_name, ship_query, scope)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (guild_id, channel_id, user_id, commodity_name, ship_query, scope),
            )
            await db.commit()
            return cursor.lastrowid

    async def list_active_stock_alerts(self) -> list[dict[str, Any]]:
        async with self.connect() as db:
            cursor = await db.execute("SELECT * FROM stock_alerts WHERE active = 1")
            rows = await cursor.fetchall()
            return [dict(row) for row in rows]

    async def list_user_stock_alerts(self, user_id: int) -> list[dict[str, Any]]:
        async with self.connect() as db:
            cursor = await db.execute(
                "SELECT * FROM stock_alerts WHERE user_id = ? AND active = 1", (user_id,)
            )
            rows = await cursor.fetchall()
            return [dict(row) for row in rows]

    async def remove_stock_alert(self, alert_id: int, user_id: int) -> bool:
        async with self.connect() as db:
            cursor = await db.execute(
                "DELETE FROM stock_alerts WHERE id = ? AND user_id = ?", (alert_id, user_id)
            )
            await db.execute(
                "DELETE FROM stock_alert_terminal_state WHERE alert_id = ?", (alert_id,)
            )
            await db.commit()
            return cursor.rowcount > 0

    async def get_stock_alert_terminal_state(self, alert_id: int) -> dict[int, dict[str, Any]]:
        """Returns {id_terminal: {"was_available": bool, "last_seen_scu": float|None}}."""
        async with self.connect() as db:
            cursor = await db.execute(
                "SELECT * FROM stock_alert_terminal_state WHERE alert_id = ?", (alert_id,)
            )
            rows = await cursor.fetchall()
            return {
                row["id_terminal"]: {"was_available": bool(row["was_available"]), "last_seen_scu": row["last_seen_scu"]}
                for row in rows
            }

    async def upsert_stock_alert_terminal_state(
        self, alert_id: int, id_terminal: int, was_available: bool, last_seen_scu: float | None
    ) -> None:
        async with self.connect() as db:
            await db.execute(
                """INSERT INTO stock_alert_terminal_state (alert_id, id_terminal, was_available, last_seen_scu, updated_at)
                   VALUES (?, ?, ?, ?, datetime('now'))
                   ON CONFLICT(alert_id, id_terminal) DO UPDATE SET
                       was_available = excluded.was_available,
                       last_seen_scu = excluded.last_seen_scu,
                       updated_at = datetime('now')""",
                (alert_id, id_terminal, int(was_available), last_seen_scu),
            )
            await db.commit()
