"""Tests for the change-only UEX data-intelligence persistence layer."""
from __future__ import annotations

import asyncio

from cryptography.fernet import Fernet

from bot.db.database import Database
from bot.uex.data_health import classify_terminal_health, format_health_note
from bot.uex.supply_demand import analyze_terminal_market_history
from bot.uex.practical_routes import route_practical_notes
from bot.uex.commodity_risk import commodity_risk_labels, format_commodity_risk


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


def test_terminal_health_lookup_and_classification_keep_freshness_separate_from_coverage(tmp_path):
    async def run():
        db = _make_db(tmp_path)
        await db.init()
        await db.record_terminal_data_health_snapshot(
            [
                {
                    "id_terminal": 9, "type": "commodity", "terminal_name": "Port Tressler",
                    "prices_total": 10, "prices_updated": 10, "prices_updated_percentage": 100,
                    "last_update_days": 14, "has_recent_reports": False,
                },
                {
                    "id_terminal": 10, "type": "commodity", "terminal_name": "Area18 TDD",
                    "prices_total": 10, "prices_updated": 3, "prices_updated_percentage": 30,
                    "last_update_days": 0, "has_recent_reports": True,
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
            "last_update_days": 1, "has_recent_reports": 1,
        }
    )
    assert health.status == "fresh"
    assert format_health_note(health) is None


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
    assert format_commodity_risk({}) is None


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
