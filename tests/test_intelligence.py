"""Tests for the change-only UEX data-intelligence persistence layer."""
from __future__ import annotations

import asyncio
import sqlite3

from cryptography.fernet import Fernet

from bot.db.database import Database
from bot.uex.data_health import classify_terminal_health, format_health_note
from bot.uex.supply_demand import analyze_terminal_market_history
from bot.uex.practical_routes import route_practical_notes
from bot.cogs.intelligence_brief import _format_cross_system_note, _format_market_shifts
from bot.uex.commodity_risk import (
    commodity_risk_labels,
    format_commodity_risk,
    has_commodity_risk_metadata,
)


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
            "last_update_days_limit": 2, "last_update_days": 1,
            "last_update_days_percentage": 50, "has_recent_reports": True,
        }
        fuel = {
            "id_commodity": 7, "id_terminal": 9, "commodity_name": "Quantum Fuel",
            "terminal_name": "Port Tressler", "price_buy": "2.5", "price_sell": None,
        }
        assert await db.record_terminal_data_health_snapshot([health]) == (1, 1)
        assert await db.record_terminal_data_health_snapshot([health]) == (0, 1)
        stored_health = await db.get_terminal_data_health(["Port Tressler"])
        assert stored_health["port tressler"]["last_update_days_limit"] == 2
        assert stored_health["port tressler"]["last_update_days_percentage"] == 50
        assert await db.record_fuel_price_snapshot([fuel]) == (1, 1)
        assert await db.record_fuel_price_snapshot([fuel]) == (0, 1)

    asyncio.run(run())


def test_existing_data_health_tables_gain_ttl_columns(tmp_path):
    database_path = tmp_path / "legacy.sqlite3"
    with sqlite3.connect(database_path) as sqlite:
        sqlite.executescript(
            """
            CREATE TABLE terminal_data_health_state (
                id_terminal INTEGER NOT NULL, data_type TEXT NOT NULL,
                terminal_name TEXT NOT NULL, prices_total INTEGER, prices_updated INTEGER,
                prices_updated_percentage INTEGER, last_update_days REAL,
                has_recent_reports INTEGER, last_seen TEXT,
                PRIMARY KEY (id_terminal, data_type)
            );
            CREATE TABLE terminal_data_health_observations (
                id_terminal INTEGER NOT NULL, data_type TEXT NOT NULL, observed_at TEXT,
                terminal_name TEXT NOT NULL, prices_total INTEGER, prices_updated INTEGER,
                prices_updated_percentage INTEGER, last_update_days REAL,
                has_recent_reports INTEGER
            );
            """
        )

    async def run():
        db = Database(database_path, Fernet(Fernet.generate_key()))
        await db.init()
        async with db.connect() as sqlite:
            state_columns = {
                row["name"] for row in await (await sqlite.execute(
                    "PRAGMA table_info(terminal_data_health_state)"
                )).fetchall()
            }
            observation_columns = {
                row["name"] for row in await (await sqlite.execute(
                    "PRAGMA table_info(terminal_data_health_observations)"
                )).fetchall()
            }
        assert {"last_update_days_limit", "last_update_days_percentage"} <= state_columns
        assert {"last_update_days_limit", "last_update_days_percentage"} <= observation_columns

    asyncio.run(run())


def test_zero_price_report_count_does_not_fall_through_to_scu_count(tmp_path):
    async def run():
        db = _make_db(tmp_path)
        await db.init()
        row = {
            "id_commodity": 1, "id_terminal": 2, "commodity_name": "Gold",
            "terminal_name": "Area18 TDD", "price_buy": 100, "price_sell": 120,
            "scu_buy": 50, "scu_sell": 25, "status_buy": 3, "status_sell": 1,
            "price_buy_users_rows": 0, "scu_buy_users_rows": 9,
            "price_sell_users_rows": 0, "scu_sell_users_rows": 8,
        }
        await db.record_terminal_market_snapshot([row])
        async with db.connect() as sqlite:
            cursor = await sqlite.execute(
                "SELECT buy_report_count, sell_report_count FROM terminal_market_state"
            )
            stored = await cursor.fetchone()
            assert (stored["buy_report_count"], stored["sell_report_count"]) == (0, 0)

    asyncio.run(run())


def test_route_intelligence_lookups_use_terminal_ids_not_names(tmp_path):
    async def run():
        db = _make_db(tmp_path)
        await db.init()
        await db.record_terminal_market_snapshot(
            [{
                "id_commodity": 1, "id_terminal": 9, "commodity_name": "Gold",
                "terminal_name": "TDD", "price_buy": 100, "price_sell": 120,
                "scu_buy": 50, "scu_sell": 25, "status_buy": 3, "status_sell": 1,
                "price_buy_users_rows": 4, "price_sell_users_rows": 5,
            }]
        )
        await db.record_terminal_data_health_snapshot(
            [{
                "id_terminal": 9, "type": "commodity", "terminal_name": "Admin - TDD",
                "prices_total": 10, "prices_updated": 10, "prices_updated_percentage": 100,
                "last_update_days_limit": 1, "last_update_days": 0,
                "last_update_days_percentage": 100, "has_recent_reports": False,
            }]
        )
        await db.upsert_terminal_reference(
            [{"id": 9, "name": "Trade and Development Division", "is_refuel": 1}]
        )

        health = await db.get_terminal_data_health_by_ids([9])
        signals = await db.get_route_market_signals_by_ids([(1, 9)])
        references = await db.get_terminal_references_by_ids([9])
        assert health[9]["terminal_name"] == "Admin - TDD"
        assert signals[(1, 9)]["terminal_name"] == "TDD"
        assert references[9]["terminal_name"] == "Trade and Development Division"

    asyncio.run(run())


def test_terminal_health_lookup_and_classification_keep_freshness_separate_from_coverage(tmp_path):
    async def run():
        db = _make_db(tmp_path)
        await db.init()
        await db.record_terminal_data_health_snapshot(
            [
                {
                    "id_terminal": 9, "type": "commodity", "terminal_name": "Port Tressler",
                    "prices_total": 10, "prices_updated": 10, "prices_updated_percentage": 100,
                    "last_update_days_limit": 1, "last_update_days": 14,
                    "last_update_days_percentage": 0, "has_recent_reports": False,
                },
                {
                    "id_terminal": 10, "type": "commodity", "terminal_name": "Area18 TDD",
                    "prices_total": 10, "prices_updated": 3, "prices_updated_percentage": 30,
                    "last_update_days_limit": 1, "last_update_days": 0,
                    "last_update_days_percentage": 100, "has_recent_reports": True,
                },
            ]
        )
        rows = await db.get_terminal_data_health(["port tressler", "AREA18 TDD"])

        stale = classify_terminal_health(rows["port tressler"])
        limited = classify_terminal_health(rows["area18 tdd"])
        assert stale.status == "stale"
        assert "14d old" in format_health_note(stale)
        assert limited.status == "limited"
        assert "30% coverage" in format_health_note(limited)

    asyncio.run(run())


def test_recent_terminal_health_without_a_warning_formats_as_none():
    health = classify_terminal_health(
        {
            "terminal_name": "Lorville CBD", "prices_updated_percentage": 90,
            "last_update_days_limit": 2, "last_update_days": 1,
            "last_update_days_percentage": 50, "has_recent_reports": 1,
        }
    )
    assert health.status == "recent"
    assert format_health_note(health) is None


def test_pending_report_queue_is_not_used_as_terminal_freshness():
    just_updated = classify_terminal_health(
        {
            "terminal_name": "Port Tressler", "prices_updated_percentage": 100,
            "last_update_days": 0, "last_update_days_limit": 2,
            "last_update_days_percentage": 100, "has_recent_reports": False,
        }
    )
    expired_with_pending_report = classify_terminal_health(
        {
            "terminal_name": "Area18 TDD", "prices_updated_percentage": 100,
            "last_update_days": 2, "last_update_days_limit": 2,
            "last_update_days_percentage": 0, "has_recent_reports": True,
        }
    )
    assert just_updated.status == "fresh"
    assert expired_with_pending_report.status == "stale"


def test_cross_system_note_never_prints_none_as_a_system_name():
    incomplete = _format_cross_system_note(None, "Stanton")
    assert incomplete is not None
    assert "None" not in incomplete
    assert "incomplete" in incomplete
    assert _format_cross_system_note("Stanton", "Stanton") is None
    assert "Pyro → Stanton" in (_format_cross_system_note("Pyro", "Stanton") or "")


def test_supply_demand_history_is_time_weighted_for_change_only_rows():
    history = analyze_terminal_market_history(
        [
            {"observed_at": "2026-08-01 00:00:00", "price_buy": 10, "scu_buy": 50,
             "price_sell": 12, "scu_sell": 100, "status_sell": 1},
            {"observed_at": "2026-08-01 06:00:00", "price_buy": 10, "scu_buy": 0,
             "price_sell": 12, "scu_sell": 100, "status_sell": 7},
            {"observed_at": "2026-08-01 18:00:00", "price_buy": 10, "scu_buy": 50,
             "price_sell": 12, "scu_sell": 100, "status_sell": 1},
        ],
        observed_until="2026-08-02 00:00:00",
    )
    assert history is not None
    assert history.observed_hours == 24
    assert history.supply_available_pct == 50
    assert history.demand_available_pct == 50
    assert history.state_changes == 2
    assert history.enough_history


def test_supply_demand_history_marks_short_windows_preliminary():
    history = analyze_terminal_market_history(
        [{"observed_at": "2026-08-01 00:00:00", "price_buy": 1, "scu_buy": 1}],
        observed_until="2026-08-01 12:00:00",
    )
    assert history is not None
    assert not history.enough_history


def test_terminal_market_name_search_is_scoped_to_commodity(tmp_path):
    async def run():
        db = _make_db(tmp_path)
        await db.init()
        rows = [
            {"id_commodity": 1, "id_terminal": 10, "commodity_name": "Gold",
             "terminal_name": "Area18 TDD", "price_buy": 10, "scu_buy": 5},
            {"id_commodity": 1, "id_terminal": 11, "commodity_name": "Gold",
             "terminal_name": "Orison TDD", "price_buy": 11, "scu_buy": 5},
            {"id_commodity": 2, "id_terminal": 12, "commodity_name": "Copper",
             "terminal_name": "Area18 TDD", "price_buy": 3, "scu_buy": 5},
        ]
        await db.record_terminal_market_snapshot(rows)
        assert await db.find_terminal_market_names("Gold", "tdd", limit=25) == [
            "Area18 TDD", "Orison TDD"
        ]
        assert await db.find_terminal_market_names("Copper", "ori", limit=25) == []

    asyncio.run(run())


def test_practical_route_notes_report_confirmed_limits_and_services():
    notes = route_practical_notes(
        {"max_container_size": 8, "has_freight_elevator": 0, "has_loading_dock": 0,
         "is_player_owned": 1, "is_refuel": 1, "is_repair": 0, "is_cargo_center": 0},
        {"max_container_size": 32, "has_freight_elevator": 1, "has_loading_dock": 0,
         "is_player_owned": 0, "is_refuel": 0, "is_repair": 1, "is_cargo_center": 1},
    )
    assert "⚠️ Origin: maximum container size 8 SCU" in notes
    assert "⚠️ Origin: no freight elevator or loading dock reported" in notes
    assert any("player-owned" in note for note in notes)
    assert "Origin services: refuel" in notes
    assert "Destination services: repair, cargo center" in notes


def test_commodity_risk_labels_are_specific_and_do_not_overstate_illegality():
    commodity = {
        "is_illegal": 1, "is_explosive": 1, "is_volatile_qt": 1,
        "is_volatile_time": 1, "is_buggy": 1,
    }
    labels = commodity_risk_labels(commodity)
    assert "restricted in some jurisdictions" in labels
    assert "explosion risk" in labels
    assert "volatile during quantum travel" in labels
    assert "becomes unstable over time" in labels
    assert "recent gameplay bugs reported" in labels
    assert "no legal buyer" not in format_commodity_risk(commodity)


def test_safe_commodity_has_no_risk_line():
    safe = {key: 0 for key in (
        "is_illegal", "is_explosive", "is_volatile_qt", "is_volatile_time", "is_buggy"
    )}
    assert has_commodity_risk_metadata(safe)
    assert format_commodity_risk(safe) is None


def test_missing_commodity_risk_metadata_is_an_explicit_warning():
    assert not has_commodity_risk_metadata(None)
    assert "metadata unavailable" in (format_commodity_risk(None) or "")
    assert "metadata unavailable" in (format_commodity_risk({"is_illegal": 0}) or "")


def test_intelligence_brief_formats_supply_and_demand_direction():
    rows = [
        {"commodity_name": "Gold", "terminal_name": "Station", "supply_change": 50},
        {"commodity_name": "Iron", "terminal_name": "Outpost", "supply_change": -20},
    ]
    value = _format_market_shifts(rows, "supply_change")
    assert "📈 **Gold** at Station: +50 SCU" in value
    assert "📉 **Iron** at Outpost: -20 SCU" in value


def test_terminal_market_shifts_compare_oldest_and_newest_observation(tmp_path):
    async def run():
        db = _make_db(tmp_path)
        await db.init()
        async with db.connect() as sqlite:
            await sqlite.executescript(
                """INSERT INTO terminal_market_observations
                   (id_commodity,id_terminal,observed_at,commodity_name,terminal_name,scu_buy,scu_sell)
                   VALUES (1,10,datetime('now','-2 hours'),'Gold','Station',10,100);
                   INSERT INTO terminal_market_observations
                   (id_commodity,id_terminal,observed_at,commodity_name,terminal_name,scu_buy,scu_sell)
                   VALUES (1,10,datetime('now','-1 hour'),'Gold','Station',40,70);"""
            )
            await sqlite.commit()
        (shift,) = await db.get_terminal_market_shifts()
        assert shift["supply_change"] == 30
        assert shift["demand_change"] == -30

    asyncio.run(run())


def test_marketplace_tier_history_seeds_an_existing_current_state(tmp_path):
    async def run():
        db = _make_db(tmp_path)
        await db.init()
        tier = {
            "id_item": 5, "item_name": "Gold", "quality_tier": 0, "operation": "sell",
            "currency": "UEC", "unit": "unit", "listings_count": 3,
            "price_avg": 100.0, "price_avg_week": 95.0, "price_avg_month": 90.0,
        }
        await db.upsert_marketplace_tier_stats([tier])
        async with db.connect() as sqlite:
            await sqlite.execute("DELETE FROM marketplace_tier_observations")
            await sqlite.commit()
        await db.upsert_marketplace_tier_stats([tier])
        async with db.connect() as sqlite:
            cursor = await sqlite.execute("SELECT COUNT(*) AS count FROM marketplace_tier_observations")
            assert (await cursor.fetchone())["count"] == 1

    asyncio.run(run())
