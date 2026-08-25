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

from bot.uex.marketplace import compute_liquidity_score

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

CREATE TABLE IF NOT EXISTS liquidity_scores (
    item_name TEXT PRIMARY KEY,
    id_item INTEGER,
    score REAL NOT NULL,
    negotiations_success INTEGER NOT NULL DEFAULT 0,
    negotiations_open INTEGER NOT NULL DEFAULT 0,
    listings_count INTEGER NOT NULL DEFAULT 0,
    listings_count_sell INTEGER NOT NULL DEFAULT 0,
    listings_count_buy INTEGER NOT NULL DEFAULT 0,
    last_updated TEXT NOT NULL DEFAULT (datetime('now'))
);

-- One current score is useful for /liquidity-rank; hourly snapshots make the change over
-- time visible to /liquidity-trends. The hour bucket also prevents bot restarts from
-- creating duplicate points for the same item in one refresh window.
CREATE TABLE IF NOT EXISTS liquidity_score_snapshots (
    id_item INTEGER NOT NULL,
    item_name TEXT NOT NULL,
    score REAL NOT NULL,
    negotiations_success INTEGER NOT NULL,
    negotiations_open INTEGER NOT NULL,
    listings_count INTEGER NOT NULL,
    listings_count_sell INTEGER NOT NULL,
    listings_count_buy INTEGER NOT NULL,
    recorded_hour TEXT NOT NULL,
    PRIMARY KEY (id_item, recorded_hour)
);
CREATE INDEX IF NOT EXISTS idx_liquidity_snapshots_item_time
    ON liquidity_score_snapshots (id_item, recorded_hour);

-- Accumulating index of Marketplace items the bot has actually observed being traded, built
-- by periodically snapshotting /marketplace_trends (which only ever exposes ~100 items live
-- at once) and merging in whatever's new. Grows past that ~100 ceiling over time as UEX's own
-- top-100 window rotates through different items across polls. Powers autocomplete on the
-- Marketplace commands so suggestions are scoped to things people actually trade, not the
-- full multi-thousand-item catalog. (Whether an item is quality-bearing, and at which
-- tiers, lives in marketplace_item_tier_stats below - a tier >= 1 row there is the signal.)
CREATE TABLE IF NOT EXISTS marketplace_item_activity (
    id_item INTEGER PRIMARY KEY,
    item_name TEXT NOT NULL,
    negotiations_count INTEGER NOT NULL DEFAULT 0,
    listings_count INTEGER NOT NULL DEFAULT 0,
    first_seen TEXT NOT NULL DEFAULT (datetime('now')),
    last_seen TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Per-tier "sub-item" stats for Marketplace items, accumulated from the hourly
-- /marketplace_prices_averages_all snapshot (bot/cogs/marketplace.py). UEX documents
-- that dump as one row per unique id_item x quality_tier x operation x currency x unit
-- combination, and that combination is this table's primary key - so each quality tier of
-- an item is effectively its own row ("Laranite Raw Q6 sell in UEC per scu"), with its own
-- listing count and price averages. Tier 0 (Q0 / no quality set) is a real tier and is
-- stored too - quality-less items live entirely at tier 0. Like marketplace_item_activity,
-- rows accumulate: the dump only covers combos with activity in UEX's rolling window, so a
-- combo absent from the current snapshot keeps its last-known values instead of being
-- deleted; first_seen/last_seen bracket when it was actually observed.
CREATE TABLE IF NOT EXISTS marketplace_item_tier_stats (
    id_item INTEGER NOT NULL,
    item_name TEXT NOT NULL,
    quality_tier INTEGER NOT NULL,
    operation TEXT NOT NULL,
    currency TEXT NOT NULL,
    unit TEXT NOT NULL,
    listings_count INTEGER NOT NULL DEFAULT 0,
    price_avg REAL,
    price_avg_week REAL,
    price_avg_month REAL,
    first_seen TEXT NOT NULL DEFAULT (datetime('now')),
    last_seen TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (id_item, quality_tier, operation, currency, unit)
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

-- Raw Materials Deal Scanner: one implicit watch per user (not multiple named alerts like
-- marketplace_alerts) - the single channel where that user wants proactive "steal"
-- notifications posted. Setting a new channel just replaces the old one.
CREATE TABLE IF NOT EXISTS user_scanner_channel (
    user_id INTEGER PRIMARY KEY,
    channel_id INTEGER NOT NULL,
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Per-user dedup for scanner notifications, so a listing that's still a qualifying deal on the
-- next poll doesn't notify the same user again. Keyed on user_id directly (not an
-- alert_id like marketplace_alert_seen_listings) since there's only one watch per user.
CREATE TABLE IF NOT EXISTS scanner_seen_listings (
    user_id INTEGER NOT NULL,
    listing_id INTEGER NOT NULL,
    seen_at TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (user_id, listing_id)
);

-- UEX data-intelligence foundation. These tables deliberately keep one current state plus an
-- append-only record only when a value changes. That gives future restock/route-confidence
-- features real history without writing a complete copy of every terminal every hour.
CREATE TABLE IF NOT EXISTS terminal_market_state (
    id_commodity INTEGER NOT NULL,
    id_terminal INTEGER NOT NULL,
    commodity_name TEXT NOT NULL,
    terminal_name TEXT NOT NULL,
    price_buy REAL,
    price_sell REAL,
    scu_buy REAL,
    scu_sell REAL,
    status_buy INTEGER,
    status_sell INTEGER,
    quality INTEGER,
    volatility_buy REAL,
    volatility_sell REAL,
    buy_report_count INTEGER,
    sell_report_count INTEGER,
    last_seen TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (id_commodity, id_terminal)
);

CREATE TABLE IF NOT EXISTS terminal_market_observations (
    id_commodity INTEGER NOT NULL,
    id_terminal INTEGER NOT NULL,
    observed_at TEXT NOT NULL DEFAULT (datetime('now')),
    commodity_name TEXT NOT NULL,
    terminal_name TEXT NOT NULL,
    price_buy REAL,
    price_sell REAL,
    scu_buy REAL,
    scu_sell REAL,
    status_buy INTEGER,
    status_sell INTEGER,
    quality INTEGER,
    volatility_buy REAL,
    volatility_sell REAL,
    buy_report_count INTEGER,
    sell_report_count INTEGER
);
CREATE INDEX IF NOT EXISTS idx_terminal_market_observations_lookup
    ON terminal_market_observations (id_commodity, id_terminal, observed_at);

CREATE TABLE IF NOT EXISTS terminal_data_health_state (
    id_terminal INTEGER NOT NULL,
    data_type TEXT NOT NULL,
    terminal_name TEXT NOT NULL,
    prices_total INTEGER,
    prices_updated INTEGER,
    prices_updated_percentage INTEGER,
    last_update_days REAL,
    has_recent_reports INTEGER,
    last_seen TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (id_terminal, data_type)
);

CREATE TABLE IF NOT EXISTS terminal_data_health_observations (
    id_terminal INTEGER NOT NULL,
    data_type TEXT NOT NULL,
    observed_at TEXT NOT NULL DEFAULT (datetime('now')),
    terminal_name TEXT NOT NULL,
    prices_total INTEGER,
    prices_updated INTEGER,
    prices_updated_percentage INTEGER,
    last_update_days REAL,
    has_recent_reports INTEGER
);
CREATE INDEX IF NOT EXISTS idx_terminal_data_health_observations_lookup
    ON terminal_data_health_observations (id_terminal, data_type, observed_at);

CREATE TABLE IF NOT EXISTS fuel_price_state (
    id_commodity INTEGER NOT NULL,
    id_terminal INTEGER NOT NULL,
    commodity_name TEXT NOT NULL,
    terminal_name TEXT NOT NULL,
    price_buy REAL,
    price_sell REAL,
    last_seen TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (id_commodity, id_terminal)
);

CREATE TABLE IF NOT EXISTS fuel_price_observations (
    id_commodity INTEGER NOT NULL,
    id_terminal INTEGER NOT NULL,
    observed_at TEXT NOT NULL DEFAULT (datetime('now')),
    commodity_name TEXT NOT NULL,
    terminal_name TEXT NOT NULL,
    price_buy REAL,
    price_sell REAL
);
CREATE INDEX IF NOT EXISTS idx_fuel_price_observations_lookup
    ON fuel_price_observations (id_terminal, id_commodity, observed_at);

CREATE TABLE IF NOT EXISTS terminal_reference (
    id_terminal INTEGER PRIMARY KEY,
    terminal_name TEXT NOT NULL,
    terminal_type TEXT,
    id_space_station INTEGER,
    space_station_name TEXT,
    id_outpost INTEGER,
    outpost_name TEXT,
    id_city INTEGER,
    star_system_name TEXT,
    planet_name TEXT,
    moon_name TEXT,
    city_name TEXT,
    max_container_size INTEGER,
    has_loading_dock INTEGER,
    has_freight_elevator INTEGER,
    is_cargo_center INTEGER,
    is_refuel INTEGER,
    is_repair INTEGER,
    is_player_owned INTEGER,
    last_seen TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS commodity_reference (
    id_commodity INTEGER PRIMARY KEY,
    commodity_name TEXT NOT NULL,
    is_illegal INTEGER,
    is_volatile_qt INTEGER,
    is_volatile_time INTEGER,
    is_explosive INTEGER,
    is_buggy INTEGER,
    is_raw INTEGER,
    is_refined INTEGER,
    is_mineral INTEGER,
    is_harvestable INTEGER,
    last_seen TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS refinery_yield_observations (
    id_commodity INTEGER NOT NULL,
    id_terminal INTEGER NOT NULL,
    recorded_day TEXT NOT NULL,
    commodity_name TEXT NOT NULL,
    terminal_name TEXT NOT NULL,
    yield_bonus INTEGER,
    yield_bonus_week INTEGER,
    yield_bonus_month INTEGER,
    PRIMARY KEY (id_commodity, id_terminal, recorded_day)
);

CREATE TABLE IF NOT EXISTS marketplace_tier_observations (
    id_item INTEGER NOT NULL,
    quality_tier INTEGER NOT NULL,
    operation TEXT NOT NULL,
    currency TEXT NOT NULL,
    unit TEXT NOT NULL,
    observed_at TEXT NOT NULL DEFAULT (datetime('now')),
    item_name TEXT NOT NULL,
    listings_count INTEGER NOT NULL,
    price_avg REAL,
    price_avg_week REAL,
    price_avg_month REAL
);
CREATE INDEX IF NOT EXISTS idx_marketplace_tier_observations_lookup
    ON marketplace_tier_observations (id_item, quality_tier, operation, currency, unit, observed_at);
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
            "ALTER TABLE liquidity_scores ADD COLUMN id_item INTEGER",
            "ALTER TABLE liquidity_scores ADD COLUMN negotiations_success INTEGER NOT NULL DEFAULT 0",
            "ALTER TABLE liquidity_scores ADD COLUMN negotiations_open INTEGER NOT NULL DEFAULT 0",
            "ALTER TABLE liquidity_scores ADD COLUMN listings_count INTEGER NOT NULL DEFAULT 0",
            "ALTER TABLE liquidity_scores ADD COLUMN listings_count_sell INTEGER NOT NULL DEFAULT 0",
            "ALTER TABLE liquidity_scores ADD COLUMN listings_count_buy INTEGER NOT NULL DEFAULT 0",
            "ALTER TABLE liquidity_score_snapshots ADD COLUMN listings_count_sell INTEGER NOT NULL DEFAULT 0",
            "ALTER TABLE liquidity_score_snapshots ADD COLUMN listings_count_buy INTEGER NOT NULL DEFAULT 0",
            "ALTER TABLE terminal_reference ADD COLUMN id_space_station INTEGER",
            "ALTER TABLE terminal_reference ADD COLUMN space_station_name TEXT",
            "ALTER TABLE terminal_reference ADD COLUMN id_outpost INTEGER",
            "ALTER TABLE terminal_reference ADD COLUMN outpost_name TEXT",
            "ALTER TABLE terminal_reference ADD COLUMN id_city INTEGER",
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

    # -- UEX data-intelligence snapshots -------------------------------------

    @staticmethod
    def _number(value: Any) -> float | None:
        if value is None or value == "":
            return None
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

    @classmethod
    def _integer(cls, value: Any) -> int | None:
        number = cls._number(value)
        return int(number) if number is not None else None

    @classmethod
    def _flag(cls, value: Any) -> int:
        """Normalize UEX's mixed 0/1, strings, and booleans without treating '0' as true."""
        if isinstance(value, str) and value.strip().lower() in {"true", "yes"}:
            return 1
        return 1 if (cls._integer(value) or 0) != 0 else 0

    async def record_terminal_market_snapshot(self, rows: list[dict[str, Any]]) -> tuple[int, int]:
        """Store all currently known terminal commodity states, appending history only for
        changed values. Returns ``(changed_rows, valid_rows)`` for concise collector logs."""
        normalized: list[tuple[Any, ...]] = []
        for row in rows:
            id_commodity = self._integer(row.get("id_commodity"))
            id_terminal = self._integer(row.get("id_terminal"))
            commodity_name = row.get("commodity_name")
            terminal_name = row.get("terminal_name")
            if id_commodity is None or id_terminal is None or not commodity_name or not terminal_name:
                continue
            normalized.append(
                (
                    id_commodity, id_terminal, str(commodity_name), str(terminal_name),
                    self._number(row.get("price_buy")), self._number(row.get("price_sell")),
                    self._number(row.get("scu_buy")), self._number(row.get("scu_sell")),
                    self._integer(row.get("status_buy")), self._integer(row.get("status_sell")),
                    self._integer(row.get("quality")),
                    self._number(row.get("volatility_price_buy")), self._number(row.get("volatility_price_sell")),
                    self._integer(row.get("price_buy_users_rows") or row.get("scu_buy_users_rows")),
                    self._integer(row.get("price_sell_users_rows") or row.get("scu_sell_users_rows")),
                )
            )
        if not normalized:
            return (0, 0)

        async with self.connect() as db:
            cursor = await db.execute(
                """SELECT id_commodity, id_terminal, commodity_name, terminal_name, price_buy,
                          price_sell, scu_buy, scu_sell, status_buy, status_sell, quality,
                          volatility_buy, volatility_sell, buy_report_count, sell_report_count
                   FROM terminal_market_state"""
            )
            existing = {
                (row["id_commodity"], row["id_terminal"]): tuple(row)[2:]
                for row in await cursor.fetchall()
            }
            changed = [row for row in normalized if existing.get((row[0], row[1])) != row[2:]]
            await db.executemany(
                """INSERT INTO terminal_market_state
                   (id_commodity, id_terminal, commodity_name, terminal_name, price_buy, price_sell,
                    scu_buy, scu_sell, status_buy, status_sell, quality, volatility_buy,
                    volatility_sell, buy_report_count, sell_report_count, last_seen)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now'))
                   ON CONFLICT(id_commodity, id_terminal) DO UPDATE SET
                       commodity_name=excluded.commodity_name, terminal_name=excluded.terminal_name,
                       price_buy=excluded.price_buy, price_sell=excluded.price_sell, scu_buy=excluded.scu_buy,
                       scu_sell=excluded.scu_sell, status_buy=excluded.status_buy, status_sell=excluded.status_sell,
                       quality=excluded.quality, volatility_buy=excluded.volatility_buy,
                       volatility_sell=excluded.volatility_sell,
                       buy_report_count=excluded.buy_report_count, sell_report_count=excluded.sell_report_count,
                       last_seen=datetime('now')""",
                normalized,
            )
            if changed:
                await db.executemany(
                    """INSERT INTO terminal_market_observations
                       (id_commodity, id_terminal, commodity_name, terminal_name, price_buy, price_sell,
                        scu_buy, scu_sell, status_buy, status_sell, quality, volatility_buy,
                        volatility_sell, buy_report_count, sell_report_count)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    changed,
                )
            await db.commit()
        return (len(changed), len(normalized))

    async def record_terminal_data_health_snapshot(self, rows: list[dict[str, Any]]) -> tuple[int, int]:
        normalized = []
        for row in rows:
            id_terminal = self._integer(row.get("id_terminal"))
            data_type = row.get("type")
            terminal_name = row.get("terminal_name")
            if id_terminal is None or not data_type or not terminal_name:
                continue
            normalized.append(
                (
                    id_terminal, str(data_type), str(terminal_name), self._integer(row.get("prices_total")),
                    self._integer(row.get("prices_updated")), self._integer(row.get("prices_updated_percentage")),
                    self._number(row.get("last_update_days")), self._flag(row.get("has_recent_reports")),
                )
            )
        if not normalized:
            return (0, 0)
        async with self.connect() as db:
            cursor = await db.execute(
                """SELECT id_terminal, data_type, terminal_name, prices_total, prices_updated,
                          prices_updated_percentage, last_update_days, has_recent_reports
                   FROM terminal_data_health_state"""
            )
            existing = {(row["id_terminal"], row["data_type"]): tuple(row)[2:] for row in await cursor.fetchall()}
            changed = [row for row in normalized if existing.get((row[0], row[1])) != row[2:]]
            await db.executemany(
                """INSERT INTO terminal_data_health_state
                   (id_terminal, data_type, terminal_name, prices_total, prices_updated,
                    prices_updated_percentage, last_update_days, has_recent_reports, last_seen)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, datetime('now'))
                   ON CONFLICT(id_terminal, data_type) DO UPDATE SET
                       terminal_name=excluded.terminal_name, prices_total=excluded.prices_total,
                       prices_updated=excluded.prices_updated,
                       prices_updated_percentage=excluded.prices_updated_percentage,
                       last_update_days=excluded.last_update_days,
                       has_recent_reports=excluded.has_recent_reports, last_seen=datetime('now')""",
                normalized,
            )
            if changed:
                await db.executemany(
                    """INSERT INTO terminal_data_health_observations
                       (id_terminal, data_type, terminal_name, prices_total, prices_updated,
                        prices_updated_percentage, last_update_days, has_recent_reports)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                    changed,
                )
            await db.commit()
        return (len(changed), len(normalized))

    async def get_terminal_data_health(
        self, terminal_names: list[str], data_type: str = "commodity"
    ) -> dict[str, dict[str, Any]]:
        """Return current data-monitor rows keyed by case-folded terminal name.

        Route responses expose terminal names but not consistently terminal ids, so the
        user-facing health layer deliberately joins on UEX's canonical terminal name.
        """
        names = sorted({name.strip().casefold() for name in terminal_names if name and name.strip()})
        if not names:
            return {}
        placeholders = ",".join("?" for _ in names)
        async with self.connect() as db:
            cursor = await db.execute(
                f"""SELECT * FROM terminal_data_health_state
                    WHERE data_type = ? AND lower(terminal_name) IN ({placeholders})""",
                [data_type, *names],
            )
            rows = await cursor.fetchall()
            return {row["terminal_name"].casefold(): dict(row) for row in rows}

    async def get_route_market_signals(
        self, commodity_ids: list[int], terminal_names: list[str]
    ) -> dict[tuple[int, str], dict[str, Any]]:
        """Current report-depth and volatility signals for route-confidence scoring."""
        ids = sorted(set(commodity_ids))
        names = sorted({name.casefold() for name in terminal_names if name})
        if not ids or not names:
            return {}
        id_marks = ",".join("?" for _ in ids)
        name_marks = ",".join("?" for _ in names)
        async with self.connect() as db:
            cursor = await db.execute(
                f"""SELECT * FROM terminal_market_state
                    WHERE id_commodity IN ({id_marks})
                      AND lower(terminal_name) IN ({name_marks})""",
                [*ids, *names],
            )
            rows = await cursor.fetchall()
            return {(row["id_commodity"], row["terminal_name"].casefold()): dict(row) for row in rows}

    async def get_mixed_route_market_rows(self) -> list[dict[str, Any]]:
        """Current market snapshot enriched with terminal and commodity warning metadata."""
        async with self.connect() as db:
            cursor = await db.execute(
                """SELECT m.*,
                          t.terminal_type, t.id_space_station, t.space_station_name,
                          t.id_outpost, t.outpost_name, t.id_city,
                          t.star_system_name, t.planet_name, t.moon_name,
                          t.city_name, t.max_container_size, t.has_loading_dock,
                          t.has_freight_elevator, t.is_cargo_center, t.is_refuel,
                          t.is_repair, t.is_player_owned,
                          c.is_illegal, c.is_volatile_qt, c.is_volatile_time,
                          c.is_explosive, c.is_buggy
                   FROM terminal_market_state AS m
                   LEFT JOIN terminal_reference AS t ON t.id_terminal = m.id_terminal
                   LEFT JOIN commodity_reference AS c ON c.id_commodity = m.id_commodity"""
            )
            return [dict(row) for row in await cursor.fetchall()]

    async def get_route_terminal_references(
        self, commodity_ids: list[int], terminal_names: list[str]
    ) -> dict[tuple[int, str], dict[str, Any]]:
        """Resolve route terminal metadata through market-state terminal ids.

        Reference names can include role prefixes that route names omit, so joining by the
        shared UEX terminal id is materially safer than matching terminal_reference names.
        """
        ids = sorted(set(commodity_ids))
        names = sorted({name.casefold() for name in terminal_names if name})
        if not ids or not names:
            return {}
        id_marks = ",".join("?" for _ in ids)
        name_marks = ",".join("?" for _ in names)
        async with self.connect() as db:
            cursor = await db.execute(
                f"""SELECT m.id_commodity, m.terminal_name AS market_terminal_name, r.*
                    FROM terminal_market_state AS m
                    JOIN terminal_reference AS r ON r.id_terminal = m.id_terminal
                    WHERE m.id_commodity IN ({id_marks})
                      AND lower(m.terminal_name) IN ({name_marks})""",
                [*ids, *names],
            )
            rows = await cursor.fetchall()
            return {
                (row["id_commodity"], row["market_terminal_name"].casefold()): dict(row)
                for row in rows
            }

    async def get_commodity_references(self, commodity_ids: list[int]) -> dict[int, dict[str, Any]]:
        """Return collected operational flags for the requested commodities."""
        ids = sorted(set(commodity_ids))
        if not ids:
            return {}
        placeholders = ",".join("?" for _ in ids)
        async with self.connect() as db:
            cursor = await db.execute(
                f"SELECT * FROM commodity_reference WHERE id_commodity IN ({placeholders})",
                ids,
            )
            rows = await cursor.fetchall()
            return {row["id_commodity"]: dict(row) for row in rows}

    async def get_terminal_market_history(
        self, commodity_name: str, terminal_name: str
    ) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
        """Return current state plus change-only history for one commodity/terminal pair."""
        async with self.connect() as db:
            cursor = await db.execute(
                """SELECT * FROM terminal_market_state
                   WHERE lower(commodity_name) = lower(?) AND lower(terminal_name) = lower(?)""",
                (commodity_name.strip(), terminal_name.strip()),
            )
            state_row = await cursor.fetchone()
            if not state_row:
                return None, []
            cursor = await db.execute(
                """SELECT * FROM terminal_market_observations
                   WHERE id_commodity = ? AND id_terminal = ? ORDER BY observed_at""",
                (state_row["id_commodity"], state_row["id_terminal"]),
            )
            observations = await cursor.fetchall()
            return dict(state_row), [dict(row) for row in observations]

    async def find_terminal_market_names(self, commodity_name: str, query: str, limit: int = 10) -> list[str]:
        """Suggest known terminal names when an exact /terminal-history lookup misses."""
        async with self.connect() as db:
            cursor = await db.execute(
                """SELECT terminal_name FROM terminal_market_state
                   WHERE lower(commodity_name) = lower(?) AND lower(terminal_name) LIKE lower(?)
                   ORDER BY terminal_name LIMIT ?""",
                (commodity_name.strip(), f"%{query.strip()}%", limit),
            )
            return [row["terminal_name"] for row in await cursor.fetchall()]

    async def record_fuel_price_snapshot(self, rows: list[dict[str, Any]]) -> tuple[int, int]:
        normalized = []
        for row in rows:
            id_commodity = self._integer(row.get("id_commodity"))
            id_terminal = self._integer(row.get("id_terminal"))
            commodity_name = row.get("commodity_name")
            terminal_name = row.get("terminal_name")
            if id_commodity is None or id_terminal is None or not commodity_name or not terminal_name:
                continue
            normalized.append(
                (id_commodity, id_terminal, str(commodity_name), str(terminal_name),
                 self._number(row.get("price_buy")), self._number(row.get("price_sell")))
            )
        if not normalized:
            return (0, 0)
        async with self.connect() as db:
            cursor = await db.execute(
                "SELECT id_commodity, id_terminal, commodity_name, terminal_name, price_buy, price_sell FROM fuel_price_state"
            )
            existing = {(row["id_commodity"], row["id_terminal"]): tuple(row)[2:] for row in await cursor.fetchall()}
            changed = [row for row in normalized if existing.get((row[0], row[1])) != row[2:]]
            await db.executemany(
                """INSERT INTO fuel_price_state
                   (id_commodity, id_terminal, commodity_name, terminal_name, price_buy, price_sell, last_seen)
                   VALUES (?, ?, ?, ?, ?, ?, datetime('now'))
                   ON CONFLICT(id_commodity, id_terminal) DO UPDATE SET
                       commodity_name=excluded.commodity_name, terminal_name=excluded.terminal_name,
                       price_buy=excluded.price_buy, price_sell=excluded.price_sell, last_seen=datetime('now')""",
                normalized,
            )
            if changed:
                await db.executemany(
                    """INSERT INTO fuel_price_observations
                       (id_commodity, id_terminal, commodity_name, terminal_name, price_buy, price_sell)
                       VALUES (?, ?, ?, ?, ?, ?)""",
                    changed,
                )
            await db.commit()
        return (len(changed), len(normalized))

    async def upsert_terminal_reference(self, rows: list[dict[str, Any]]) -> int:
        params = []
        for row in rows:
            id_terminal = self._integer(row.get("id"))
            name = row.get("name") or row.get("terminal_name")
            if id_terminal is None or not name:
                continue
            params.append(
                (id_terminal, str(name), row.get("type"), self._integer(row.get("id_space_station")),
                 row.get("space_station_name"), self._integer(row.get("id_outpost")), row.get("outpost_name"),
                 self._integer(row.get("id_city")), row.get("star_system_name"), row.get("planet_name"),
                 row.get("moon_name"), row.get("city_name"), self._integer(row.get("max_container_size")),
                 self._flag(row.get("has_loading_dock")), self._flag(row.get("has_freight_elevator")),
                 self._flag(row.get("is_cargo_center")), self._flag(row.get("is_refuel")), self._flag(row.get("is_repair")),
                 self._flag(row.get("is_player_owned")))
            )
        if not params:
            return 0
        async with self.connect() as db:
            await db.executemany(
                """INSERT INTO terminal_reference
                   (id_terminal, terminal_name, terminal_type, id_space_station, space_station_name,
                    id_outpost, outpost_name, id_city, star_system_name, planet_name, moon_name, city_name,
                    max_container_size, has_loading_dock, has_freight_elevator, is_cargo_center, is_refuel,
                    is_repair, is_player_owned, last_seen)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now'))
                   ON CONFLICT(id_terminal) DO UPDATE SET
                       terminal_name=excluded.terminal_name, terminal_type=excluded.terminal_type,
                       id_space_station=excluded.id_space_station, space_station_name=excluded.space_station_name,
                       id_outpost=excluded.id_outpost, outpost_name=excluded.outpost_name, id_city=excluded.id_city,
                       star_system_name=excluded.star_system_name, planet_name=excluded.planet_name,
                       moon_name=excluded.moon_name, city_name=excluded.city_name,
                       max_container_size=excluded.max_container_size,
                       has_loading_dock=excluded.has_loading_dock,
                       has_freight_elevator=excluded.has_freight_elevator,
                       is_cargo_center=excluded.is_cargo_center, is_refuel=excluded.is_refuel,
                       is_repair=excluded.is_repair, is_player_owned=excluded.is_player_owned,
                       last_seen=datetime('now')""",
                params,
            )
            await db.commit()
        return len(params)

    async def upsert_commodity_reference(self, rows: list[dict[str, Any]]) -> int:
        params = []
        for row in rows:
            id_commodity = self._integer(row.get("id"))
            name = row.get("name")
            if id_commodity is None or not name:
                continue
            params.append(
                (id_commodity, str(name), self._flag(row.get("is_illegal")), self._flag(row.get("is_volatile_qt")),
                 self._flag(row.get("is_volatile_time")), self._flag(row.get("is_explosive")),
                 self._flag(row.get("is_buggy")), self._flag(row.get("is_raw")), self._flag(row.get("is_refined")),
                 self._flag(row.get("is_mineral")), self._flag(row.get("is_harvestable"))))
        if not params:
            return 0
        async with self.connect() as db:
            await db.executemany(
                """INSERT INTO commodity_reference
                   (id_commodity, commodity_name, is_illegal, is_volatile_qt, is_volatile_time, is_explosive,
                    is_buggy, is_raw, is_refined, is_mineral, is_harvestable, last_seen)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now'))
                   ON CONFLICT(id_commodity) DO UPDATE SET
                       commodity_name=excluded.commodity_name, is_illegal=excluded.is_illegal,
                       is_volatile_qt=excluded.is_volatile_qt, is_volatile_time=excluded.is_volatile_time,
                       is_explosive=excluded.is_explosive, is_buggy=excluded.is_buggy,
                       is_raw=excluded.is_raw, is_refined=excluded.is_refined,
                       is_mineral=excluded.is_mineral, is_harvestable=excluded.is_harvestable,
                       last_seen=datetime('now')""",
                params,
            )
            await db.commit()
        return len(params)

    async def record_refinery_yield_snapshot(self, rows: list[dict[str, Any]]) -> int:
        params = []
        for row in rows:
            id_commodity = self._integer(row.get("id_commodity"))
            id_terminal = self._integer(row.get("id_terminal"))
            name, terminal = row.get("commodity_name"), row.get("terminal_name")
            if id_commodity is None or id_terminal is None or not name or not terminal:
                continue
            params.append(
                (id_commodity, id_terminal, str(name), str(terminal), self._integer(row.get("value")),
                 self._integer(row.get("value_week")), self._integer(row.get("value_month")))
            )
        if not params:
            return 0
        async with self.connect() as db:
            await db.executemany(
                """INSERT INTO refinery_yield_observations
                   (id_commodity, id_terminal, recorded_day, commodity_name, terminal_name,
                    yield_bonus, yield_bonus_week, yield_bonus_month)
                   VALUES (?, ?, date('now'), ?, ?, ?, ?, ?)
                   ON CONFLICT(id_commodity, id_terminal, recorded_day) DO UPDATE SET
                       commodity_name=excluded.commodity_name, terminal_name=excluded.terminal_name,
                       yield_bonus=excluded.yield_bonus, yield_bonus_week=excluded.yield_bonus_week,
                       yield_bonus_month=excluded.yield_bonus_month""",
                params,
            )
            await db.commit()
        return len(params)

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

    async def get_digest_data_freshness(self) -> dict[str, str | None]:
        """Latest successful timestamps for the collectors summarized by the daily digest."""
        async with self.connect() as db:
            cursor = await db.execute(
                """SELECT
                       (SELECT MAX(last_seen) FROM terminal_market_state) AS terminal_market,
                       (SELECT MAX(recorded_hour) FROM liquidity_score_snapshots) AS liquidity,
                       (SELECT MAX(last_seen) FROM marketplace_item_activity) AS marketplace"""
            )
            row = await cursor.fetchone()
            return dict(row) if row else {
                "terminal_market": None, "liquidity": None, "marketplace": None
            }

    async def get_terminal_market_shifts(self, hours: int = 24) -> list[dict[str, Any]]:
        """Supply and demand changes between each market's oldest/newest observations."""
        async with self.connect() as db:
            cursor = await db.execute(
                """WITH windowed AS (
                       SELECT * FROM terminal_market_observations
                       WHERE observed_at >= datetime('now', ?)
                   ), bounds AS (
                       SELECT id_commodity, id_terminal, MIN(observed_at) first_at, MAX(observed_at) last_at
                       FROM windowed GROUP BY id_commodity, id_terminal HAVING COUNT(*) >= 2
                   )
                   SELECT latest.commodity_name, latest.terminal_name,
                          earliest.scu_buy AS previous_supply, latest.scu_buy AS current_supply,
                          earliest.scu_sell AS previous_demand, latest.scu_sell AS current_demand,
                          COALESCE(latest.scu_buy, 0) - COALESCE(earliest.scu_buy, 0) AS supply_change,
                          COALESCE(latest.scu_sell, 0) - COALESCE(earliest.scu_sell, 0) AS demand_change
                   FROM bounds
                   JOIN windowed earliest ON earliest.id_commodity=bounds.id_commodity
                     AND earliest.id_terminal=bounds.id_terminal AND earliest.observed_at=bounds.first_at
                   JOIN windowed latest ON latest.id_commodity=bounds.id_commodity
                     AND latest.id_terminal=bounds.id_terminal AND latest.observed_at=bounds.last_at""",
                (f"-{hours} hours",),
            )
            return [dict(row) for row in await cursor.fetchall()]

    # -- liquidity scores -------------------------------------------------------

    async def update_liquidity_scores(self, activity_rows: list[dict[str, Any]]) -> int:
        """Store liquidity scores from one already-fetched Marketplace trends snapshot.

        Fetching belongs to the Marketplace cog so the activity index and leaderboard use
        the same hourly UEX response instead of making a second API call.
        """
        if not activity_rows:
            return 0

        count = 0
        async with self.connect() as db:
            for row in activity_rows:
                item_name = row.get("item_name")
                id_item = row.get("id_item")
                if not item_name or id_item is None:
                    continue

                score = compute_liquidity_score(row)
                successful = int(float(row.get("negotiations_success") or 0))
                open_negotiations = int(float(row.get("negotiations_open") or 0))
                listings_count = int(float(row.get("listings_count") or 0))
                sell_listings = int(float(row.get("listings_count_sell") or 0))
                buy_listings = int(float(row.get("listings_count_buy") or 0))
                
                await db.execute(
                    """INSERT INTO liquidity_scores
                       (item_name, id_item, score, negotiations_success, negotiations_open, listings_count, listings_count_sell, listings_count_buy, last_updated)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, datetime('now'))
                       ON CONFLICT(item_name) DO UPDATE SET
                           id_item = excluded.id_item,
                           score = excluded.score,
                           negotiations_success = excluded.negotiations_success,
                           negotiations_open = excluded.negotiations_open,
                           listings_count = excluded.listings_count,
                           listings_count_sell = excluded.listings_count_sell,
                           listings_count_buy = excluded.listings_count_buy,
                           last_updated = datetime('now')""",
                    (item_name, id_item, score, successful, open_negotiations, listings_count, sell_listings, buy_listings),
                )
                await db.execute(
                    """INSERT INTO liquidity_score_snapshots
                       (id_item, item_name, score, negotiations_success, negotiations_open, listings_count, listings_count_sell, listings_count_buy, recorded_hour)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, strftime('%Y-%m-%d %H:00:00', 'now'))
                       ON CONFLICT(id_item, recorded_hour) DO UPDATE SET
                           item_name = excluded.item_name,
                           score = excluded.score,
                           negotiations_success = excluded.negotiations_success,
                           negotiations_open = excluded.negotiations_open,
                           listings_count = excluded.listings_count,
                           listings_count_sell = excluded.listings_count_sell,
                           listings_count_buy = excluded.listings_count_buy""",
                    (id_item, item_name, score, successful, open_negotiations, listings_count, sell_listings, buy_listings),
                )
                count += 1
            await db.commit()
        return count

    async def get_top_liquidity_items(self, limit: int = 10) -> list[dict[str, Any]]:
        """Returns the top N items with the highest liquidity scores."""
        async with self.connect() as db:
            cursor = await db.execute(
                "SELECT * FROM liquidity_scores ORDER BY score DESC LIMIT ?",
                (limit,)
            )
            rows = await cursor.fetchall()
            return [dict(row) for row in rows]

    async def get_liquidity_history(self, item_name: str, hours: int = 24 * 7) -> list[dict[str, Any]]:
        """Return one tracked item's hourly liquidity snapshots, oldest first."""
        async with self.connect() as db:
            cursor = await db.execute(
                """SELECT * FROM liquidity_score_snapshots
                   WHERE item_name = ? COLLATE NOCASE AND recorded_hour >= datetime('now', ?)
                   ORDER BY recorded_hour ASC""",
                (item_name, f"-{hours} hours"),
            )
            rows = await cursor.fetchall()
            return [dict(row) for row in rows]

    async def find_liquidity_items(self, query: str, limit: int = 25) -> list[dict[str, Any]]:
        """Autocomplete-ready current liquidity items, ordered by score."""
        async with self.connect() as db:
            cursor = await db.execute(
                """SELECT item_name, id_item FROM liquidity_scores
                   WHERE item_name LIKE ? COLLATE NOCASE
                   ORDER BY score DESC LIMIT ?""",
                (f"%{query}%", limit),
            )
            rows = await cursor.fetchall()
            return [dict(row) for row in rows]

    async def get_liquidity_movers(
        self, hours: int = 24, limit: int = 10, direction: str | None = None
    ) -> list[dict[str, Any]]:
        """Largest score changes over the requested window, optionally split by direction."""
        if direction not in (None, "up", "down"):
            raise ValueError("direction must be 'up', 'down', or None")
        direction_sql = {
            "up": "WHERE score_change > 0",
            "down": "WHERE score_change < 0",
            None: "",
        }[direction]
        async with self.connect() as db:
            cursor = await db.execute(
                f"""WITH windowed AS (
                       SELECT * FROM liquidity_score_snapshots
                       WHERE recorded_hour >= datetime('now', ?)
                   ), bounds AS (
                       SELECT id_item, MIN(recorded_hour) AS first_hour, MAX(recorded_hour) AS last_hour
                       FROM windowed GROUP BY id_item HAVING COUNT(*) >= 2
                   ), movements AS (
                       SELECT latest.item_name, latest.id_item, earliest.score AS previous_score,
                              latest.score AS current_score, latest.score - earliest.score AS score_change,
                              earliest.recorded_hour AS first_hour, latest.recorded_hour AS last_hour
                       FROM bounds
                       JOIN windowed AS earliest
                         ON earliest.id_item = bounds.id_item AND earliest.recorded_hour = bounds.first_hour
                       JOIN windowed AS latest
                         ON latest.id_item = bounds.id_item AND latest.recorded_hour = bounds.last_hour
                   )
                   SELECT * FROM movements
                   {direction_sql}
                   ORDER BY ABS(score_change) DESC LIMIT ?""",
                (f"-{hours} hours", limit),
            )
            rows = await cursor.fetchall()
            return [dict(row) for row in rows]

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

    # -- per-tier Marketplace item stats ("sub-items") ------------------------

    async def upsert_marketplace_tier_stats(self, rows: list[dict[str, Any]]) -> None:
        """Merge a fresh /marketplace_prices_averages_all snapshot into the per-tier stats
        table. Same merge semantics as upsert_marketplace_item_activity: each row's stats
        are UEX's own point-in-time averages, so a re-observed (item, tier, operation,
        currency, unit) combo is overwritten with the latest values - only first_seen
        survives updates. Rows are pre-coerced by extract_tier_stats (bot/uex/marketplace.py),
        which guarantees id_item/quality_tier are ints and prices are floats or None."""
        if not rows:
            return
        async with self.connect() as db:
            cursor = await db.execute(
                """SELECT id_item, quality_tier, operation, currency, unit, item_name,
                          listings_count, price_avg, price_avg_week, price_avg_month
                   FROM marketplace_item_tier_stats"""
            )
            existing = {
                (row["id_item"], row["quality_tier"], row["operation"], row["currency"], row["unit"]): tuple(row)[5:]
                for row in await cursor.fetchall()
            }
            history_cursor = await db.execute(
                """SELECT DISTINCT id_item, quality_tier, operation, currency, unit
                   FROM marketplace_tier_observations"""
            )
            history_keys = {
                (row["id_item"], row["quality_tier"], row["operation"], row["currency"], row["unit"])
                for row in await history_cursor.fetchall()
            }
            changed = [
                r for r in rows
                if (
                    (key := (r["id_item"], r["quality_tier"], r["operation"], r["currency"], r["unit"]))
                    not in history_keys
                    or existing.get(key)
                    != (r["item_name"], r["listings_count"], r.get("price_avg"), r.get("price_avg_week"), r.get("price_avg_month"))
                )
            ]
            await db.executemany(
                """INSERT INTO marketplace_item_tier_stats
                   (id_item, item_name, quality_tier, operation, currency, unit,
                    listings_count, price_avg, price_avg_week, price_avg_month, last_seen)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now'))
                   ON CONFLICT(id_item, quality_tier, operation, currency, unit) DO UPDATE SET
                       item_name = excluded.item_name,
                       listings_count = excluded.listings_count,
                       price_avg = excluded.price_avg,
                       price_avg_week = excluded.price_avg_week,
                       price_avg_month = excluded.price_avg_month,
                       last_seen = datetime('now')""",
                [
                    (
                        r["id_item"], r["item_name"], r["quality_tier"], r["operation"],
                        r["currency"], r["unit"], r["listings_count"],
                        r.get("price_avg"), r.get("price_avg_week"), r.get("price_avg_month"),
                    )
                    for r in rows
                ],
            )
            if changed:
                await db.executemany(
                    """INSERT INTO marketplace_tier_observations
                       (id_item, quality_tier, operation, currency, unit, item_name, listings_count,
                        price_avg, price_avg_week, price_avg_month)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    [
                        (
                            r["id_item"], r["quality_tier"], r["operation"], r["currency"], r["unit"],
                            r["item_name"], r["listings_count"], r.get("price_avg"),
                            r.get("price_avg_week"), r.get("price_avg_month"),
                        )
                        for r in changed
                    ],
                )
            await db.commit()

    async def get_item_tier_stats(self, id_item: int) -> list[dict[str, Any]]:
        """Every accumulated per-tier row for one item - each row is one of the item's
        "sub-items" (a tier x operation x currency x unit combo), ordered sell before buy
        then tier ascending to match how the averages command displays live data."""
        async with self.connect() as db:
            cursor = await db.execute(
                """SELECT * FROM marketplace_item_tier_stats WHERE id_item = ?
                   ORDER BY CASE operation WHEN 'sell' THEN 0 WHEN 'buy' THEN 1 ELSE 2 END,
                            quality_tier, currency, unit""",
                (id_item,),
            )
            rows = await cursor.fetchall()
            return [dict(row) for row in rows]

    async def get_known_quality_tiers(self, id_items: list[int]) -> dict[int, set[int]]:
        """{id_item: {tiers}} of the *real* quality tiers (>= 1, so excluding Q0/no-quality)
        each of these items has ever been observed trading at - i.e. which of the 8 possible
        tier sub-items actually exist in the wild for the item. An id with no tier >= 1 rows
        is simply absent from the result - "no real tiers" and "unknown item" read the same,
        both meaning the item isn't known to be quality-bearing."""
        ids = [id_item for id_item in id_items if id_item is not None]
        if not ids:
            return {}
        placeholders = ",".join("?" for _ in ids)
        async with self.connect() as db:
            cursor = await db.execute(
                f"""SELECT DISTINCT id_item, quality_tier FROM marketplace_item_tier_stats
                    WHERE quality_tier >= 1 AND id_item IN ({placeholders})""",
                ids,
            )
            rows = await cursor.fetchall()
            result: dict[int, set[int]] = {}
            for row in rows:
                result.setdefault(row["id_item"], set()).add(row["quality_tier"])
            return result

    async def count_marketplace_tier_stats(self) -> tuple[int, int]:
        """(total accumulated tier-combo rows, distinct items with at least one real tier
        >= 1) - the second number is how many items the index knows as quality-bearing via
        actual per-tier data, for the /marketplace-index-status readout."""
        async with self.connect() as db:
            cursor = await db.execute(
                """SELECT COUNT(*) AS combos,
                          COUNT(DISTINCT CASE WHEN quality_tier >= 1 THEN id_item END) AS quality_items
                   FROM marketplace_item_tier_stats"""
            )
            row = await cursor.fetchone()
            return (row["combos"], row["quality_items"]) if row else (0, 0)

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

    # -- undervalued scanner ---------------------------------------------------

    async def set_scanner_channel(self, user_id: int, channel_id: int) -> None:
        async with self.connect() as db:
            await db.execute(
                """INSERT INTO user_scanner_channel (user_id, channel_id, updated_at)
                   VALUES (?, ?, datetime('now'))
                   ON CONFLICT(user_id) DO UPDATE SET
                       channel_id = excluded.channel_id,
                       updated_at = datetime('now')""",
                (user_id, channel_id),
            )
            await db.commit()

    async def get_scanner_channel(self, user_id: int) -> int | None:
        async with self.connect() as db:
            cursor = await db.execute(
                "SELECT channel_id FROM user_scanner_channel WHERE user_id = ?", (user_id,)
            )
            row = await cursor.fetchone()
            return row["channel_id"] if row else None

    async def list_scanner_watchers(self) -> list[dict[str, Any]]:
        """Every user with a scanner channel configured - polled by the background loop."""
        async with self.connect() as db:
            cursor = await db.execute("SELECT user_id, channel_id FROM user_scanner_channel")
            rows = await cursor.fetchall()
            return [dict(row) for row in rows]

    async def get_seen_scanner_listing_ids(self, user_id: int) -> set[int]:
        async with self.connect() as db:
            cursor = await db.execute(
                "SELECT listing_id FROM scanner_seen_listings WHERE user_id = ?", (user_id,)
            )
            rows = await cursor.fetchall()
            return {row["listing_id"] for row in rows}

    async def mark_scanner_listing_seen(self, user_id: int, listing_id: int) -> None:
        async with self.connect() as db:
            await db.execute(
                "INSERT OR IGNORE INTO scanner_seen_listings (user_id, listing_id) VALUES (?, ?)",
                (user_id, listing_id),
            )
            await db.commit()
