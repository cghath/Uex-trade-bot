"""Tests for the change-only UEX data-intelligence persistence layer."""
from __future__ import annotations

import asyncio
import sqlite3

from cryptography.fernet import Fernet

from bot.db.database import Database
from bot.uex.data_health import classify_terminal_health, format_health_note
from bot.uex.supply_demand import analyze_terminal_market_history, classify_supply_evidence
from bot.uex.practical_routes import (
    route_in_system,
    route_practical_notes,
    terminal_in_system,
    terminal_supports_auto_load,
)
from bot.cogs.intelligence_brief import _format_market_shifts
from bot.uex.commodity_risk import (
    commodity_risk_labels,
    format_commodity_risk,
    has_commodity_risk_metadata,
)
from bot.uex.route_presentation import travel_warning


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


def test_get_terminal_market_observations_by_ids_groups_and_filters_by_requested_pairs(tmp_path):
    """Bulk counterpart to get_terminal_market_history's single-pair lookup - used by the
    Evidence-Level Labels inferred-trend fallback across many routes at once. Confirms
    grouping by (id_commodity, id_terminal), that only the requested pairs come back (not
    every row in the table), and that invalid/zero ids are silently skipped rather than
    raising, matching get_route_market_signals_by_ids' own established shape."""
    async def run():
        db = _make_db(tmp_path)
        await db.init()
        row_a = {
            "id_commodity": 1, "id_terminal": 10, "commodity_name": "Gold", "terminal_name": "A",
            "price_buy": 100, "scu_buy": 50, "status_buy": 1,
        }
        row_b = {
            "id_commodity": 1, "id_terminal": 20, "commodity_name": "Gold", "terminal_name": "B",
            "price_sell": 150, "scu_sell": 30, "status_sell": 1,
        }
        row_c = {
            "id_commodity": 2, "id_terminal": 10, "commodity_name": "Cobalt", "terminal_name": "A",
            "price_buy": 20, "scu_buy": 5, "status_buy": 1,
        }
        await db.record_terminal_market_snapshot([row_a, row_b, row_c])
        row_a["scu_buy"] = 60  # change-only: a second, different observation for (1, 10)
        await db.record_terminal_market_snapshot([row_a, row_b, row_c])

        result = await db.get_terminal_market_observations_by_ids([(1, 10), (1, 20), (0, 999), (1, None)])

        assert set(result.keys()) == {(1, 10), (1, 20)}
        assert len(result[(1, 10)]) == 2, "expected both observations for the changed pair"
        assert len(result[(1, 20)]) == 1
        assert (2, 10) not in result, "a real pair not passed in the query must not leak into the result"

    asyncio.run(run())


def test_get_terminal_market_observations_by_ids_returns_empty_for_no_valid_ids(tmp_path):
    async def run():
        db = _make_db(tmp_path)
        await db.init()
        assert await db.get_terminal_market_observations_by_ids([]) == {}
        assert await db.get_terminal_market_observations_by_ids([(0, 0), (None, None)]) == {}

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
        stored_health = await db.get_terminal_data_health_by_ids([9])
        assert stored_health[9]["last_update_days_limit"] == 2
        assert stored_health[9]["last_update_days_percentage"] == 50
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
        rows = await db.get_terminal_data_health_by_ids([9, 10])

        stale = classify_terminal_health(rows[9])
        limited = classify_terminal_health(rows[10])
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


def test_terminal_health_falls_back_to_age_ratio_at_the_exact_50_percent_boundary():
    """Every other classify_terminal_health test supplies last_update_days_percentage
    directly, so the age/age_limit fallback branch (used whenever UEX omits that field)
    has never actually been exercised - including at its own 50% boundary, which must
    agree with the ttl-percentage branch's `<= 50` (i.e. `>=` on the age side, not `>`)."""
    at_the_boundary = classify_terminal_health(
        {
            "terminal_name": "Lorville CBD", "prices_updated_percentage": 90,
            "last_update_days_limit": 2, "last_update_days": 1,
            "has_recent_reports": False,
        }
    )
    just_under_the_boundary = classify_terminal_health(
        {
            "terminal_name": "Lorville CBD", "prices_updated_percentage": 90,
            "last_update_days_limit": 2, "last_update_days": 0.9,
            "has_recent_reports": False,
        }
    )
    assert at_the_boundary.status == "recent"
    assert just_under_the_boundary.status == "fresh"


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


def test_old_collected_health_is_not_still_fresh():
    """A10: classify_terminal_health used only UEX's own age/TTL fields, captured at
    whatever moment the row was last collected - if the collector stops running (a crash,
    a bug, a long outage), the last successfully stored row keeps looking "fresh" forever,
    purely because it looked fresh the one time it actually ran."""
    from datetime import datetime, timezone

    health = classify_terminal_health(
        {
            "terminal_name": "Example", "prices_updated_percentage": 100,
            "last_update_days": 0, "last_update_days_limit": 3,
            "last_update_days_percentage": 100, "last_seen": "2020-01-01 00:00:00",
        },
        now=datetime(2026, 9, 5, tzinfo=timezone.utc),
    )
    assert health.status == "unknown"


def test_locally_stale_health_note_does_not_claim_ttl_metadata_is_missing():
    """Follow-up review finding: classify_terminal_health's A10 fix added a SECOND,
    distinct cause of status=='unknown' (the bot's own collection has gone stale, even
    though UEX's TTL fields say fresh) - but format_health_note was never updated to
    match, and kept hardcoding the message for the ORIGINAL cause ("TTL metadata
    missing"). For the local-staleness path, TTL metadata is NOT missing - it's fully
    present and says the data looked fine; the real problem is the bot hasn't re-checked
    it recently. The old message was self-contradictory (claims metadata is missing while
    showing an age figure that came from that same "missing" metadata)."""
    from datetime import datetime, timezone

    locally_stale = classify_terminal_health(
        {
            "terminal_name": "Example", "prices_updated_percentage": 100,
            "last_update_days": 0, "last_update_days_limit": 3,
            "last_update_days_percentage": 100, "last_seen": "2020-01-01 00:00:00",
        },
        now=datetime(2026, 9, 5, tzinfo=timezone.utc),
    )
    assert locally_stale.status == "unknown"
    assert locally_stale.locally_stale is True
    note = format_health_note(locally_stale)
    assert "metadata missing" not in note.lower(), note
    assert "stalled" in note.lower() or "hasn't re-checked" in note.lower(), note


def test_genuinely_missing_ttl_metadata_still_gets_its_own_message():
    """Regression guard: the locally_stale distinction must not swallow the ORIGINAL
    "unknown" cause - a row with no TTL fields at all (and no last_seen) still gets the
    "TTL metadata missing" message, unchanged."""
    missing_ttl = classify_terminal_health({"terminal_name": "Example"})
    assert missing_ttl.status == "unknown"
    assert missing_ttl.locally_stale is False
    note = format_health_note(missing_ttl)
    assert "metadata missing" in note.lower(), note


def test_recently_collected_health_is_unaffected_by_the_staleness_check():
    from datetime import datetime, timezone

    health = classify_terminal_health(
        {
            "terminal_name": "Example", "prices_updated_percentage": 100,
            "last_update_days": 0, "last_update_days_limit": 3,
            "last_update_days_percentage": 100, "last_seen": "2026-09-05 11:30:00",
        },
        now=datetime(2026, 9, 5, 12, 0, 0, tzinfo=timezone.utc),
    )
    assert health.status == "fresh"


def test_missing_last_seen_does_not_trigger_the_staleness_check():
    """Not every caller/row is guaranteed to carry last_seen - its absence must not be
    treated as "infinitely stale"."""
    health = classify_terminal_health(
        {
            "terminal_name": "Example", "prices_updated_percentage": 100,
            "last_update_days": 0, "last_update_days_limit": 3,
            "last_update_days_percentage": 100,
        }
    )
    assert health.status == "fresh"


def test_local_staleness_does_not_override_an_already_stale_or_limited_status():
    """The local-collection check only ever downgrades "fresh"/"recent" to "unknown" - it
    must never relabel a status UEX's own TTL already marked stale/limited, which carries
    more specific information than a generic "unknown"."""
    from datetime import datetime, timezone

    stale = classify_terminal_health(
        {
            "terminal_name": "Example", "prices_updated_percentage": 100,
            "last_update_days": 5, "last_update_days_limit": 3,
            "last_update_days_percentage": 0, "last_seen": "2020-01-01 00:00:00",
        },
        now=datetime(2026, 9, 5, tzinfo=timezone.utc),
    )
    assert stale.status == "stale"


def test_travel_warning_never_prints_none_as_a_system_name():
    """/intelligence-brief used to have its own _format_cross_system_note with this same
    guard - now folded into the shared bot.uex.route_presentation.travel_warning (used by
    /best-route, /top-routes, /mixed-routes, /multi-stop-route, and /intelligence-brief
    alike), so the regression is pinned there instead."""
    for has_real_distance in (True, False):
        incomplete = travel_warning(None, "Stanton", has_real_distance=has_real_distance)
        assert incomplete is None or "None" not in incomplete


def test_travel_warning_same_system_depends_on_whether_distance_is_already_shown():
    assert travel_warning("Stanton", "Stanton", has_real_distance=True) is None
    assert travel_warning("Stanton", "Stanton", has_real_distance=False) is not None


def test_travel_warning_cross_system_always_speaks_up():
    assert "Pyro → Stanton" in (travel_warning("Pyro", "Stanton", has_real_distance=False) or "")
    assert "Pyro → Stanton" in (travel_warning("Pyro", "Stanton", has_real_distance=True) or "")


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


def _fresh_health():
    return classify_terminal_health(dict(last_update_days_percentage=80, prices_updated_percentage=100))


def _stale_health():
    return classify_terminal_health(dict(last_update_days_percentage=0, prices_updated_percentage=100))


def _long_history():
    return analyze_terminal_market_history(
        [
            {"observed_at": "2026-08-01 00:00:00", "price_buy": 10, "scu_buy": 50,
             "price_sell": 12, "scu_sell": 0, "status_sell": 7},
            {"observed_at": "2026-08-01 06:00:00", "price_buy": 10, "scu_buy": 0,
             "price_sell": 12, "scu_sell": 100, "status_sell": 1},
        ],
        observed_until="2026-08-02 00:00:00",
    )


def test_evidence_level_is_current_when_scu_is_live_and_health_is_fresh():
    level = classify_supply_evidence(scu=500, health=_fresh_health(), history=None, side="supply")
    assert level.tier == "current"
    assert level.quantity_scu == 500


def test_evidence_level_is_aging_when_scu_is_live_but_health_is_degraded():
    level = classify_supply_evidence(scu=500, health=_stale_health(), history=None, side="supply")
    assert level.tier == "aging"
    assert level.quantity_scu == 500


def test_evidence_level_confirmed_zero_stays_current_not_unknown():
    """The whole point of Evidence-Level Labels: a REAL reported zero must never look the
    same as having no information at all."""
    level = classify_supply_evidence(scu=0, health=_fresh_health(), history=None, side="supply")
    assert level.tier == "current"
    assert level.quantity_scu == 0


def test_evidence_level_falls_back_to_inferred_when_no_live_scu_but_enough_history():
    level = classify_supply_evidence(scu=None, health=None, history=_long_history(), side="supply")
    assert level.tier == "inferred"
    assert level.historical_availability_pct is not None
    assert level.observed_hours == 24


def test_evidence_level_demand_side_reads_the_demand_percentage_not_supply():
    history = _long_history()
    supply = classify_supply_evidence(scu=None, health=None, history=history, side="supply")
    demand = classify_supply_evidence(scu=None, health=None, history=history, side="demand")
    assert supply.historical_availability_pct == history.supply_available_pct
    assert demand.historical_availability_pct == history.demand_available_pct
    assert supply.historical_availability_pct != demand.historical_availability_pct


def test_evidence_level_is_unknown_when_no_live_scu_and_no_history():
    level = classify_supply_evidence(scu=None, health=None, history=None, side="supply")
    assert level.tier == "unknown"
    assert level.quantity_scu is None


def test_evidence_level_is_unknown_when_history_is_too_short_to_infer_from():
    short_history = analyze_terminal_market_history(
        [{"observed_at": "2026-08-01 00:00:00", "price_buy": 1, "scu_buy": 1}],
        observed_until="2026-08-01 12:00:00",
    )
    assert not short_history.enough_history
    level = classify_supply_evidence(scu=None, health=None, history=short_history, side="supply")
    assert level.tier == "unknown"


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


def test_terminal_supports_auto_load_is_distinct_from_loading_dock_and_fails_closed():
    # UEX exposes is_auto_load separately from has_loading_dock/has_freight_elevator -
    # a terminal with physical loading-dock infrastructure doesn't necessarily also
    # offer the purchase-time auto-load-onto-stored-ship feature, and vice versa.
    assert terminal_supports_auto_load({"is_auto_load": 1, "has_loading_dock": 0}) is True
    assert terminal_supports_auto_load({"is_auto_load": 0, "has_loading_dock": 1}) is False
    assert terminal_supports_auto_load({"has_loading_dock": 1}) is False
    assert terminal_supports_auto_load({}) is False
    assert terminal_supports_auto_load(None) is False


def test_terminal_and_route_in_system_fail_closed_and_require_both_ends():
    assert terminal_in_system({"star_system_name": "Pyro"}, "Pyro") is True
    assert terminal_in_system({"star_system_name": "Stanton"}, "Pyro") is False
    assert terminal_in_system({}, "Pyro") is False
    assert terminal_in_system(None, "Pyro") is False

    pyro = {"star_system_name": "Pyro"}
    stanton = {"star_system_name": "Stanton"}
    assert route_in_system(pyro, pyro, "Pyro") is True
    assert route_in_system(pyro, stanton, "Pyro") is False  # crosses systems
    assert route_in_system(pyro, None, "Pyro") is False  # unknown destination fails closed
    assert route_in_system(pyro, stanton, None) is True  # no filter requested


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


def test_terminal_market_shifts_reports_a_single_recent_change_against_an_old_baseline(tmp_path):
    """A11: a long-stable market (nothing recorded for two days) followed by exactly one
    recent change only ever has ONE observation inside a 24h window - the original query
    required 2+ in-window rows before it would report anything, silently dropping this
    real, large shift. The fix compares the latest observation against the closest prior
    baseline even when that baseline sits outside the window entirely."""
    async def run():
        db = _make_db(tmp_path)
        await db.init()
        async with db.connect() as sqlite:
            await sqlite.executescript(
                """INSERT INTO terminal_market_observations
                   (id_commodity,id_terminal,observed_at,commodity_name,terminal_name,scu_buy,scu_sell)
                   VALUES (1,1,datetime('now','-2 days'),'Ore','Terminal',100,200);
                   INSERT INTO terminal_market_observations
                   (id_commodity,id_terminal,observed_at,commodity_name,terminal_name,scu_buy,scu_sell)
                   VALUES (1,1,datetime('now','-1 hour'),'Ore','Terminal',600,200);"""
            )
            await sqlite.commit()
        (shift,) = await db.get_terminal_market_shifts()
        assert shift["supply_change"] == 500
        assert shift["demand_change"] == 0

    asyncio.run(run())


def test_terminal_market_shifts_excludes_a_pair_with_only_one_ever_observation(tmp_path):
    """A single ever-recorded data point has no earlier state to compare against - must not
    show up as a "change" of 0 (which would be indistinguishable from a genuinely unchanged
    market), it should be excluded entirely."""
    async def run():
        db = _make_db(tmp_path)
        await db.init()
        async with db.connect() as sqlite:
            await sqlite.execute(
                """INSERT INTO terminal_market_observations
                   (id_commodity,id_terminal,observed_at,commodity_name,terminal_name,scu_buy,scu_sell)
                   VALUES (1,1,datetime('now','-1 hour'),'Ore','Terminal',100,200)"""
            )
            await sqlite.commit()
        assert await db.get_terminal_market_shifts() == []

    asyncio.run(run())


def test_terminal_market_shifts_new_market_uses_earliest_not_most_recent_fallback(tmp_path):
    """Follow-up review finding: the fix for the single-recent-change bug ranked both the
    pre-window baseline AND the in-window fallback in one CTE ordered `... DESC` throughout
    - correct for the pre-window tier (want the most recent one, closest to the window
    boundary) but wrong for the in-window fallback tier, which should use the EARLIEST
    in-window observation (the original, pre-fix behavior for this exact case). With no
    pre-window baseline and three in-window observations (100 @ -3h, 600 @ -2h, 200 @ -1h =
    latest), the buggy ordering picked -2h (600) as "baseline" - the second most recent,
    not the earliest - reporting -400 instead of the correct +100 against the true
    earliest reference point."""
    async def run():
        db = _make_db(tmp_path)
        await db.init()
        async with db.connect() as sqlite:
            for hours, stock in [(3, 100), (2, 600), (1, 200)]:
                await sqlite.execute(
                    """INSERT INTO terminal_market_observations
                       (id_commodity,id_terminal,observed_at,commodity_name,terminal_name,scu_buy,scu_sell)
                       VALUES (1,1,datetime('now',?),'Ore','Terminal',?,100)""",
                    (f"-{hours} hours", stock),
                )
            await sqlite.commit()
        (shift,) = await db.get_terminal_market_shifts()
        assert shift["supply_change"] == 100, shift

    asyncio.run(run())


def test_terminal_market_shifts_never_mixes_measurements_from_two_baseline_rows(tmp_path):
    """Follow-up review finding: the fix for the 3-observation ordering bug picked
    previous_supply and previous_demand independently via COALESCE(pwb.scu_buy,
    iwe.scu_buy) / COALESCE(pwb.scu_sell, iwe.scu_sell) - column by column, not row by
    row. Whenever the real pre-window baseline row exists but has just ONE of its two
    measurements NULL, this silently borrowed the OTHER measurement from a completely
    different row (the in-window fallback), presenting one "since baseline" comparison
    built from two different points in time. With a 48h-old baseline (supply unknown,
    demand 500) and in-window rows at -3h (600, 400) and -1h/latest (200, 300), the buggy
    query reported previous_supply=600 (borrowed from -3h) alongside previous_demand=500
    (correctly from the real 48h baseline) - a fabricated -400 supply_change that doesn't
    describe any single real comparison. The fix selects the baseline as one row: a
    genuinely unknown measurement on the chosen row stays unknown (None), and its
    corresponding *_change is None rather than a number computed against a substituted
    value."""
    async def run():
        db = _make_db(tmp_path)
        await db.init()
        async with db.connect() as sqlite:
            for hours, supply, demand in [(48, None, 500), (3, 600, 400), (1, 200, 300)]:
                await sqlite.execute(
                    """INSERT INTO terminal_market_observations
                       (id_commodity,id_terminal,observed_at,commodity_name,terminal_name,scu_buy,scu_sell)
                       VALUES (1,1,datetime('now',?),'Ore','Terminal',?,?)""",
                    (f"-{hours} hours", supply, demand),
                )
            await sqlite.commit()
        (shift,) = await db.get_terminal_market_shifts()
        assert shift["previous_supply"] is None, shift
        assert shift["supply_change"] is None, shift
        assert shift["previous_demand"] == 500, shift
        assert shift["demand_change"] == -200, shift

    asyncio.run(run())


def test_terminal_market_shifts_unknown_current_supply_is_not_reported_as_zero(tmp_path):
    """Follow-up review finding: the previous fix preserved a NULL BASELINE measurement
    (previous_supply/previous_demand) instead of fabricating a change against it - but the
    symmetric case on the CURRENT (latest) side was still wrapped in
    COALESCE(latest.scu_buy, 0), so a known baseline (500) paired with a genuinely unknown
    CURRENT value (UEX simply didn't report scu_buy this cycle) computed 0 - 500 = -500,
    inventing a complete-depletion shift that current_supply itself reports as unknown,
    not zero. Each *_change must be NULL whenever EITHER side is unknown, symmetrically."""
    async def run():
        db = _make_db(tmp_path)
        await db.init()
        async with db.connect() as sqlite:
            await sqlite.execute(
                """INSERT INTO terminal_market_observations
                   (id_commodity,id_terminal,observed_at,commodity_name,terminal_name,scu_buy,scu_sell)
                   VALUES (1,1,datetime('now','-48 hours'),'Ore','Terminal',500,100)"""
            )
            await sqlite.execute(
                """INSERT INTO terminal_market_observations
                   (id_commodity,id_terminal,observed_at,commodity_name,terminal_name,scu_buy,scu_sell)
                   VALUES (1,1,datetime('now','-1 hour'),'Ore','Terminal',NULL,100)"""
            )
            await sqlite.commit()
        (shift,) = await db.get_terminal_market_shifts()
        assert shift["current_supply"] is None, shift
        assert shift["supply_change"] is None, shift

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
