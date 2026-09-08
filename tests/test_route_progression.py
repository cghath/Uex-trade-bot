"""Tests for Recommendation Outcome Tracking (Phase 1): the pure outcome-to-terminal-state
mapping in bot/uex/route_progression.py, and the Database CRUD methods it's built on."""
from __future__ import annotations

import asyncio

import aiosqlite
from cryptography.fernet import Fernet
import pytest

from bot.db.database import Database
from bot.uex.route_progression import terminal_state_update_for_outcome


def _make_db(tmp_path) -> Database:
    return Database(tmp_path / "route_progression.sqlite3", Fernet(Fernet.generate_key()))


# -- terminal_state_update_for_outcome (pure logic) --------------------------------------

def _leg(**overrides):
    base = dict(
        id_commodity=1, id_terminal=10, commodity_name="Gold", terminal_name="Area18 TDD",
        side="buy", quoted_price=100.0, quoted_scu=50.0, quoted_status=3,
    )
    base.update(overrides)
    return base


def test_matched_outcome_reconfirms_the_quoted_figures():
    row = terminal_state_update_for_outcome(outcome="matched", **_leg())
    assert row["price_buy"] == 100.0
    assert row["scu_buy"] == 50.0
    assert row["status_buy"] == 3


def test_matched_outcome_with_no_quoted_figures_writes_nothing():
    row = terminal_state_update_for_outcome(
        outcome="matched", **_leg(quoted_price=None, quoted_scu=None, quoted_status=None)
    )
    assert row is None


def test_missing_outcome_confirms_a_hard_zero_on_the_buy_side():
    row = terminal_state_update_for_outcome(outcome="missing", **_leg(side="buy"))
    assert row["scu_buy"] == 0.0
    assert row["status_buy"] == 1, "buy-side empty is status code 1, not the sell-side no-demand code"


def test_missing_outcome_uses_the_sell_side_no_demand_code_not_buy_side_empty():
    """UEX's 1-7 status bands mean opposite things on buy vs sell (see CLAUDE.local.md) -
    a sell-side 'nothing wanted' must never be written as status 1, which means the
    opposite (out of stock) on the buy side."""
    row = terminal_state_update_for_outcome(
        outcome="missing", **_leg(side="sell", terminal_name="Terra Gateway")
    )
    assert row["scu_sell"] == 0.0
    assert row["status_sell"] == 7


def test_less_outcome_with_no_actual_scu_writes_nothing():
    """actual_scu is required for 'less' - writing scu=None would silently null out
    whatever this pair already had, not just skip the update."""
    row = terminal_state_update_for_outcome(outcome="less", **_leg())
    assert row is None


def test_less_outcome_writes_the_confirmed_partial_amount():
    row = terminal_state_update_for_outcome(outcome="less", actual_scu=20.0, **_leg())
    assert row["scu_buy"] == 20.0
    assert row["price_buy"] == 100.0, "falls back to the quoted price when no actual price is given"
    assert row["status_buy"] is None, "a nonzero partial amount doesn't map to any single confident band"


def test_less_outcome_with_zero_actual_scu_is_treated_as_confirmed_empty():
    row = terminal_state_update_for_outcome(outcome="less", actual_scu=0.0, **_leg())
    assert row["scu_buy"] == 0.0
    assert row["status_buy"] == 1


def test_more_outcome_requires_a_precision():
    with pytest.raises(ValueError):
        terminal_state_update_for_outcome(outcome="more", actual_scu=80.0, **_leg())


def test_more_outcome_with_floor_precision_writes_nothing():
    """The one case this module exists to guard: a lower bound ('at least this much was
    there') must never be written into terminal_market_state as if it were exact."""
    row = terminal_state_update_for_outcome(
        outcome="more", actual_scu=80.0, precision="floor", **_leg()
    )
    assert row is None


def test_more_outcome_with_exact_precision_confirms_the_terminal_is_now_drained():
    """'Drained' means the CONFIRMED POST-LEG state is empty, not 'equal to how much was
    taken' - actual_scu describes the transaction, not what's left afterward."""
    row = terminal_state_update_for_outcome(
        outcome="more", actual_scu=80.0, actual_price=95.0, precision="exact", **_leg()
    )
    assert row["scu_buy"] == 0.0
    assert row["status_buy"] == 1
    assert row["price_buy"] == 95.0


def test_more_outcome_on_the_sell_side_uses_the_no_demand_code_when_drained():
    row = terminal_state_update_for_outcome(
        outcome="more", actual_scu=30.0, precision="exact",
        **_leg(side="sell", terminal_name="Terra Gateway"),
    )
    assert row["scu_sell"] == 0.0
    assert row["status_sell"] == 7


def test_invalid_side_raises():
    with pytest.raises(ValueError):
        terminal_state_update_for_outcome(outcome="matched", **_leg(side="both"))


def test_invalid_outcome_raises():
    with pytest.raises(ValueError):
        terminal_state_update_for_outcome(outcome="not_a_real_outcome", **_leg())


# -- Database CRUD ------------------------------------------------------------------------

def test_create_thread_and_fetch_legs_round_trips(tmp_path):
    async def run():
        db = _make_db(tmp_path)
        await db.init()
        await db.create_route_progression_thread(
            thread_id=1, user_id=100, guild_id=200, route_kind="best_route",
            route_snapshot={"title": "Gold: A -> B"},
            legs=[
                {"side": "buy", "id_terminal": 10, "id_commodity": 1, "quoted_price": 100, "quoted_scu": 50, "quoted_status": 3},
                {"side": "sell", "id_terminal": 20, "id_commodity": 1, "quoted_price": 150, "quoted_scu": 40, "quoted_status": 2},
            ],
        )
        thread = await db.get_route_progression_thread(1)
        assert thread["status"] == "in_progress"
        assert thread["total_legs"] == 2
        assert thread["route_kind"] == "best_route"

        legs = await db.get_route_progression_legs(1)
        assert [leg["side"] for leg in legs] == ["buy", "sell"]
        assert legs[0]["outcome"] is None
        assert legs[1]["quoted_price"] == 150

    asyncio.run(run())


def test_record_leg_outcome_updates_only_the_targeted_leg(tmp_path):
    async def run():
        db = _make_db(tmp_path)
        await db.init()
        await db.create_route_progression_thread(
            thread_id=1, user_id=100, guild_id=200, route_kind="best_route",
            route_snapshot={},
            legs=[
                {"side": "buy", "id_terminal": 10, "id_commodity": 1},
                {"side": "sell", "id_terminal": 20, "id_commodity": 1},
            ],
        )
        await db.record_route_progression_leg_outcome(
            thread_id=1, leg_index=0, outcome="matched",
        )
        legs = await db.get_route_progression_legs(1)
        assert legs[0]["outcome"] == "matched"
        assert legs[0]["reported_at"] is not None
        assert legs[1]["outcome"] is None, "the second leg must be untouched"

    asyncio.run(run())


def test_set_thread_status_completed_sets_completed_at(tmp_path):
    async def run():
        db = _make_db(tmp_path)
        await db.init()
        await db.create_route_progression_thread(
            thread_id=1, user_id=100, guild_id=200, route_kind="best_route",
            route_snapshot={}, legs=[{"side": "buy", "id_terminal": 10, "id_commodity": 1}],
        )
        await db.set_route_progression_thread_status(1, "completed")
        thread = await db.get_route_progression_thread(1)
        assert thread["status"] == "completed"
        assert thread["completed_at"] is not None

    asyncio.run(run())


def test_stale_thread_detection_uses_the_latest_leg_report_not_just_creation(tmp_path):
    async def run():
        db = _make_db(tmp_path)
        await db.init()
        async with db.connect() as conn:
            # thread 1: created long ago, no legs ever reported -> stale by created_at.
            await conn.execute(
                """INSERT INTO route_progression_threads
                   (thread_id, user_id, guild_id, route_kind, route_snapshot, total_legs, created_at)
                   VALUES (1, 100, 200, 'best_route', '{}', 1, datetime('now', '-72 hours'))"""
            )
            # thread 2: created long ago, but a leg was reported recently -> NOT stale.
            await conn.execute(
                """INSERT INTO route_progression_threads
                   (thread_id, user_id, guild_id, route_kind, route_snapshot, total_legs, created_at)
                   VALUES (2, 100, 200, 'best_route', '{}', 1, datetime('now', '-72 hours'))"""
            )
            await conn.execute(
                """INSERT INTO route_progression_legs
                   (thread_id, leg_index, side, id_terminal, id_commodity, reported_at)
                   VALUES (2, 0, 'buy', 10, 1, datetime('now', '-1 hours'))"""
            )
            # thread 3: created recently -> not stale.
            await conn.execute(
                """INSERT INTO route_progression_threads
                   (thread_id, user_id, guild_id, route_kind, route_snapshot, total_legs)
                   VALUES (3, 100, 200, 'best_route', '{}', 1)"""
            )
            await conn.commit()

        stale = await db.get_stale_route_progression_threads(older_than_hours=48)
        assert {row["thread_id"] for row in stale} == {1}

    asyncio.run(run())


def test_route_progression_thread_row_source_defaults_and_overwrites_on_conflict(tmp_path):
    """record_terminal_market_snapshot's new source tagging: a player report correctly
    overwrites a prior UEX row's source, and a later UEX poll correctly reverts it - the
    local correction is temporary, not permanent, matching the documented design."""
    async def run():
        db = _make_db(tmp_path)
        await db.init()
        row = {
            "id_commodity": 1, "id_terminal": 10, "commodity_name": "Gold",
            "terminal_name": "Area18 TDD", "price_buy": 100, "scu_buy": 50, "status_buy": 3,
        }
        assert await db.record_terminal_market_snapshot([row]) == (1, 1)
        async with db.connect() as conn:
            cursor = await conn.execute(
                "SELECT source FROM terminal_market_state WHERE id_commodity = 1 AND id_terminal = 10"
            )
            assert (await cursor.fetchone())["source"] == "uex"

        player_row = {**row, "scu_buy": 0, "status_buy": 1}
        await db.record_terminal_market_snapshot([player_row], source="player_report")
        async with db.connect() as conn:
            cursor = await conn.execute(
                "SELECT source, scu_buy FROM terminal_market_state WHERE id_commodity = 1 AND id_terminal = 10"
            )
            fetched = await cursor.fetchone()
            assert fetched["source"] == "player_report"
            assert fetched["scu_buy"] == 0

        # A later real UEX poll reverts the source tag, even if the figures happen to match.
        await db.record_terminal_market_snapshot([player_row])
        async with db.connect() as conn:
            cursor = await conn.execute(
                "SELECT source FROM terminal_market_state WHERE id_commodity = 1 AND id_terminal = 10"
            )
            assert (await cursor.fetchone())["source"] == "uex"

    asyncio.run(run())
