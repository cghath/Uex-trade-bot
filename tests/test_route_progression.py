"""Tests for Recommendation Outcome Tracking (Phase 1): the pure outcome-to-terminal-state
mapping in bot/uex/route_progression.py, the Database CRUD methods it's built on, and the
leg-outcome View/Modal claim logic in bot/cogs/route_progression.py."""
from __future__ import annotations

import asyncio
import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, Mock

import aiosqlite
from cryptography.fernet import Fernet
import discord
import pytest

from bot.cogs import route_progression as route_progression_module
from bot.cogs.route_progression import (
    AbandonConfirmView,
    ActualAmountModal,
    HedgeReportModal,
    HedgeReportView,
    LegOutcomeView,
    MoreOutcomeFollowupView,
    RouteLegInput,
    RouteProgression,
    TrackableRoute,
)
from bot.cogs.route_progression import _embed_with_outcome
from bot.db.database import Database
from bot.uex.route_progression import (
    SUPPRESSION_HOURS,
    describe_leg_outcome,
    is_reportable_amount,
    terminal_state_update_for_outcome,
    update_confirms_depletion,
)


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


def test_matched_outcome_prefers_market_scu_over_the_allocated_quoted_scu():
    """Real defect: /mixed-routes and /multi-stop-route's quoted_scu is the cargo
    ALLOCATED to one ship/budget (capped by capacity), not the terminal's real stock. A
    'matched' report only confirms that planned transaction went through - it must write
    back the real quoted market figure (market_scu), not silently shrink the terminal to
    the size of this one purchase."""
    row = terminal_state_update_for_outcome(
        outcome="matched", market_scu=500.0, **_leg(quoted_scu=6.0)
    )
    assert row["scu_buy"] == 500.0


def test_matched_outcome_falls_back_to_quoted_scu_when_market_scu_is_not_given():
    """/best-route and /top-routes never pass market_scu, because their quoted_scu
    already IS the real market figure directly - unchanged behavior for those callers."""
    row = terminal_state_update_for_outcome(outcome="matched", **_leg(quoted_scu=50.0))
    assert row["scu_buy"] == 50.0


def test_terminal_state_update_rejects_non_finite_actual_scu():
    """Defense-in-depth: even though the Discord modal should already reject inf/nan
    before this is ever called, the row-building boundary refuses it too rather than
    trusting every caller got validation right."""
    with pytest.raises(ValueError):
        terminal_state_update_for_outcome(outcome="less", actual_scu=float("inf"), **_leg())
    with pytest.raises(ValueError):
        terminal_state_update_for_outcome(outcome="less", actual_scu=float("nan"), **_leg())


def test_terminal_state_update_rejects_negative_actual_price_and_scu():
    with pytest.raises(ValueError):
        terminal_state_update_for_outcome(outcome="less", actual_scu=-5.0, **_leg())
    with pytest.raises(ValueError):
        terminal_state_update_for_outcome(
            outcome="less", actual_scu=10.0, actual_price=-1.0, **_leg()
        )


def test_is_reportable_amount_boundary_cases():
    assert is_reportable_amount(None) is True, "not provided is always valid here"
    assert is_reportable_amount(0.0) is True, "zero is a legitimate empty-stock report"
    assert is_reportable_amount(float("inf")) is False
    assert is_reportable_amount(float("nan")) is False
    assert is_reportable_amount(-1.0) is False
    assert is_reportable_amount(-1.0, allow_negative=True) is True


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


# -- update_confirms_depletion (pure logic, suppression window) --------------------------

def test_update_confirms_depletion_true_for_missing_outcome():
    row = terminal_state_update_for_outcome(outcome="missing", **_leg(side="buy"))
    assert update_confirms_depletion(row, side="buy") is True


def test_update_confirms_depletion_true_for_a_drained_more_outcome():
    row = terminal_state_update_for_outcome(
        outcome="more", actual_scu=80.0, precision="exact", **_leg()
    )
    assert update_confirms_depletion(row, side="buy") is True


def test_update_confirms_depletion_false_for_matched():
    row = terminal_state_update_for_outcome(outcome="matched", **_leg())
    assert update_confirms_depletion(row, side="buy") is False


def test_update_confirms_depletion_false_for_a_positive_less_report():
    row = terminal_state_update_for_outcome(outcome="less", actual_scu=20.0, **_leg())
    assert update_confirms_depletion(row, side="buy") is False


def test_update_confirms_depletion_false_for_a_floor_capped_more_outcome():
    """A 'more' outcome with precision='floor' never even reaches terminal_state_update_
    for_outcome (it returns None) - there was more there, the player/cargo capped them,
    not the terminal. Confirms depletion checking treats None the same way."""
    row = terminal_state_update_for_outcome(
        outcome="more", actual_scu=80.0, precision="floor", **_leg()
    )
    assert row is None
    assert update_confirms_depletion(row, side="buy") is False


def test_update_confirms_depletion_false_for_a_less_report_with_no_actual_scu():
    row = terminal_state_update_for_outcome(outcome="less", **_leg())
    assert row is None
    assert update_confirms_depletion(row, side="buy") is False


def test_update_confirms_depletion_checks_the_requested_side_not_whichever_the_row_is_for():
    """A buy-side depletion row must not register as depletion when checked for 'sell' -
    the two sides use different empty status codes (buy=1, sell=7), so a mismatched side
    check must read False rather than silently comparing against the wrong code."""
    row = terminal_state_update_for_outcome(outcome="missing", **_leg(side="buy"))
    assert update_confirms_depletion(row, side="sell") is False


def test_update_confirms_depletion_rejects_an_invalid_side():
    with pytest.raises(ValueError):
        update_confirms_depletion(None, side="both")


# -- describe_leg_outcome / _embed_with_outcome (pure logic) -----------------------------

def test_describe_leg_outcome_matched():
    assert describe_leg_outcome(outcome="matched") == "**Reported:** Matched the quote."


def test_describe_leg_outcome_missing():
    assert describe_leg_outcome(outcome="missing") == "**Reported:** Nothing was there."


def test_describe_leg_outcome_less_includes_the_actual_amount_and_price():
    text = describe_leg_outcome(outcome="less", actual_scu=20.0, actual_price=95.0)
    assert "Less than quoted" in text
    assert "20 SCU" in text
    assert "95.00 aUEC/unit" in text


def test_describe_leg_outcome_less_with_no_price_omits_the_price_clause():
    text = describe_leg_outcome(outcome="less", actual_scu=20.0)
    assert "aUEC/unit" not in text


def test_describe_leg_outcome_more_exact_says_drained():
    text = describe_leg_outcome(outcome="more", actual_scu=80.0, precision="exact")
    assert "More than quoted" in text
    assert "drained" in text


def test_describe_leg_outcome_more_floor_says_capped():
    text = describe_leg_outcome(outcome="more", actual_scu=80.0, precision="floor")
    assert "at least 80 SCU" in text
    assert "capped" in text


def test_describe_leg_outcome_more_without_precision_raises():
    with pytest.raises(ValueError):
        describe_leg_outcome(outcome="more", actual_scu=80.0)


def test_describe_leg_outcome_rejects_an_invalid_outcome():
    with pytest.raises(ValueError):
        describe_leg_outcome(outcome="not_a_real_outcome")


def test_embed_with_outcome_appends_under_the_existing_description():
    embed = discord.Embed(description="Quoted: 100.00 aUEC/unit · 50 SCU")
    new_embed = _embed_with_outcome(embed, "**Reported:** Matched the quote.")
    assert new_embed.description == "Quoted: 100.00 aUEC/unit · 50 SCU\n\n**Reported:** Matched the quote."
    assert embed.description == "Quoted: 100.00 aUEC/unit · 50 SCU", "the original embed must not be mutated"


def test_embed_with_outcome_with_no_existing_description_just_sets_it():
    embed = discord.Embed(description=None)
    new_embed = _embed_with_outcome(embed, "**Reported:** Nothing was there.")
    assert new_embed.description == "**Reported:** Nothing was there."


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


def test_record_player_report_market_update_preserves_the_unreported_side_and_metadata(tmp_path):
    """Real defect: a buy-side player report was erasing the sell side's price/demand/
    status and quality/volatility/report-count metadata, because it went through
    record_terminal_market_snapshot's full-row-replace semantics with those fields simply
    absent (defaulting to NULL) from the partial row. record_player_report_market_update
    must touch ONLY the columns the report actually carries."""
    async def run():
        db = _make_db(tmp_path)
        await db.init()
        await db.record_terminal_market_snapshot([dict(
            id_commodity=1, id_terminal=10, commodity_name="Gold", terminal_name="Area18 TDD",
            price_buy=100, price_sell=90, scu_buy=50, scu_sell=80, status_buy=3, status_sell=2,
            quality=4, volatility_price_buy=2, price_sell_users_rows=7,
        )])
        await db.record_player_report_market_update(
            {
                "id_commodity": 1, "id_terminal": 10, "commodity_name": "Gold",
                "terminal_name": "Area18 TDD", "price_buy": 100, "scu_buy": 50, "status_buy": 3,
            },
            thread_id=1, leg_index=0,
        )
        async with db.connect() as conn:
            cursor = await conn.execute(
                "SELECT * FROM terminal_market_state WHERE id_commodity = 1 AND id_terminal = 10"
            )
            row = dict(await cursor.fetchone())
        assert row["price_sell"] == 90
        assert row["scu_sell"] == 80
        assert row["status_sell"] == 2
        assert row["quality"] == 4
        assert row["volatility_buy"] == 2
        assert row["sell_report_count"] == 7
        assert row["source"] == "player_report"

    asyncio.run(run())


def test_record_player_report_market_update_preserves_the_buy_side_on_a_sell_report(tmp_path):
    """Neighboring case of the same defect: the reverse direction (a sell-side report
    must not erase the buy side) needs the identical guarantee."""
    async def run():
        db = _make_db(tmp_path)
        await db.init()
        await db.record_terminal_market_snapshot([dict(
            id_commodity=1, id_terminal=10, commodity_name="Gold", terminal_name="Area18 TDD",
            price_buy=100, price_sell=90, scu_buy=50, scu_sell=80, status_buy=3, status_sell=2,
        )])
        await db.record_player_report_market_update(
            {
                "id_commodity": 1, "id_terminal": 10, "commodity_name": "Gold",
                "terminal_name": "Area18 TDD", "price_sell": 95, "scu_sell": 20, "status_sell": None,
            },
            thread_id=1, leg_index=0,
        )
        async with db.connect() as conn:
            cursor = await conn.execute(
                "SELECT * FROM terminal_market_state WHERE id_commodity = 1 AND id_terminal = 10"
            )
            row = dict(await cursor.fetchone())
        assert row["price_buy"] == 100
        assert row["scu_buy"] == 50
        assert row["status_buy"] == 3
        assert row["price_sell"] == 95
        assert row["scu_sell"] == 20

    asyncio.run(run())


def test_record_player_report_market_update_a_present_none_writes_a_real_null(tmp_path):
    """A column PRESENT in the row (even as None - e.g. a 'missing' outcome's confirmed-
    unknown price) must actually be written as NULL, not treated the same as an absent
    key. This is what distinguishes 'confirmed unknown now' from 'never reported'."""
    async def run():
        db = _make_db(tmp_path)
        await db.init()
        await db.record_terminal_market_snapshot([dict(
            id_commodity=1, id_terminal=10, commodity_name="Gold", terminal_name="Area18 TDD",
            price_buy=100, scu_buy=50, status_buy=3,
        )])
        await db.record_player_report_market_update(
            {
                "id_commodity": 1, "id_terminal": 10, "commodity_name": "Gold",
                "terminal_name": "Area18 TDD", "price_buy": None, "scu_buy": 0.0, "status_buy": 1,
            },
            thread_id=1, leg_index=0,
        )
        async with db.connect() as conn:
            cursor = await conn.execute(
                "SELECT * FROM terminal_market_state WHERE id_commodity = 1 AND id_terminal = 10"
            )
            row = dict(await cursor.fetchone())
        assert row["price_buy"] is None
        assert row["scu_buy"] == 0.0
        assert row["status_buy"] == 1

    asyncio.run(run())


def test_record_player_report_market_update_creates_a_new_row_when_none_existed(tmp_path):
    """A player report can be the FIRST thing ever recorded for a pair (no prior UEX
    collector data) - the untouched columns simply stay NULL, not an error."""
    async def run():
        db = _make_db(tmp_path)
        await db.init()
        await db.record_player_report_market_update(
            {
                "id_commodity": 1, "id_terminal": 10, "commodity_name": "Gold",
                "terminal_name": "Area18 TDD", "price_buy": 100.0, "scu_buy": 50.0, "status_buy": 3,
            },
            thread_id=1, leg_index=0,
        )
        async with db.connect() as conn:
            cursor = await conn.execute(
                "SELECT * FROM terminal_market_state WHERE id_commodity = 1 AND id_terminal = 10"
            )
            row = dict(await cursor.fetchone())
        assert row["price_buy"] == 100.0
        assert row["price_sell"] is None
        assert row["quality"] is None
        assert row["source"] == "player_report"

    asyncio.run(run())


def test_get_route_progression_track_record_counts_matched_vs_total_per_pair(tmp_path):
    async def run():
        db = _make_db(tmp_path)
        await db.init()
        await db.create_route_progression_thread(
            thread_id=1, user_id=100, guild_id=200, route_kind="best_route", route_snapshot={},
            legs=[
                {"side": "buy", "id_terminal": 10, "id_commodity": 1},
                {"side": "buy", "id_terminal": 10, "id_commodity": 1},
                {"side": "buy", "id_terminal": 10, "id_commodity": 1},
                {"side": "sell", "id_terminal": 20, "id_commodity": 1},
            ],
        )
        await db.record_route_progression_leg_outcome(thread_id=1, leg_index=0, outcome="matched")
        await db.record_route_progression_leg_outcome(thread_id=1, leg_index=1, outcome="matched")
        await db.record_route_progression_leg_outcome(thread_id=1, leg_index=2, outcome="less", actual_scu=10)
        # leg_index 3 (sell) is left unreported - must not count toward either total.

        result = await db.get_route_progression_track_record([(1, 10, "buy"), (1, 20, "sell")])
        assert result[(1, 10, "buy")] == (2, 3)
        assert (1, 20, "sell") not in result

    asyncio.run(run())


def test_get_route_progression_track_record_only_returns_requested_pairs(tmp_path):
    async def run():
        db = _make_db(tmp_path)
        await db.init()
        await db.create_route_progression_thread(
            thread_id=1, user_id=100, guild_id=200, route_kind="best_route", route_snapshot={},
            legs=[
                {"side": "buy", "id_terminal": 10, "id_commodity": 1},
                {"side": "buy", "id_terminal": 10, "id_commodity": 2},
            ],
        )
        await db.record_route_progression_leg_outcome(thread_id=1, leg_index=0, outcome="matched")
        await db.record_route_progression_leg_outcome(thread_id=1, leg_index=1, outcome="missing")

        result = await db.get_route_progression_track_record([(1, 10, "buy")])
        assert result == {(1, 10, "buy"): (1, 1)}, "commodity 2 at the same terminal must not leak in"

    asyncio.run(run())


# -- Suppression window (suppress_terminal_market_side / get_suppressed_sides_by_ids) ----

def _future(hours: float) -> str:
    return (datetime.now(timezone.utc) + timedelta(hours=hours)).strftime("%Y-%m-%d %H:%M:%S")


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


async def _seed_market_row(db: Database, **overrides) -> None:
    row = dict(
        id_commodity=1, id_terminal=10, commodity_name="Gold", terminal_name="Area18 TDD",
        price_buy=100, price_sell=90, scu_buy=50, scu_sell=40, status_buy=3, status_sell=2,
    )
    row.update(overrides)
    await db.record_terminal_market_snapshot([row])


async def _create_thread_for_leg(db: Database, thread_id: int, leg) -> None:
    """Seeds a single-leg route_progression_threads/route_progression_legs row matching
    `leg` - needed by the handle_leg_outcome end-to-end tests below now that
    record_route_progression_leg_outcome's outcome-recording UPDATE only commits against
    a real, pre-existing leg row (see its own docstring in bot/db/database.py)."""
    await db.create_route_progression_thread(
        thread_id=thread_id, user_id=1, guild_id=1, route_kind="best_route", route_snapshot={},
        legs=[{
            "side": leg.side, "id_terminal": leg.id_terminal, "id_commodity": leg.id_commodity,
            "quoted_price": leg.quoted_price, "quoted_scu": leg.quoted_scu, "quoted_status": leg.quoted_status,
        }],
    )


def test_suppress_terminal_market_side_round_trips_through_get_suppressed_sides_by_ids(tmp_path):
    async def run():
        db = _make_db(tmp_path)
        await db.init()
        await _seed_market_row(db)
        until = _future(SUPPRESSION_HOURS)
        await db.suppress_terminal_market_side(
            id_commodity=1, id_terminal=10, side="buy", until=until, thread_id=1, leg_index=0
        )

        result = await db.get_suppressed_sides_by_ids([(1, 10)], now=_now())
        assert result == {(1, 10): {"buy": True, "sell": False}}

    asyncio.run(run())


def test_get_suppressed_sides_by_ids_a_pair_with_no_active_suppression_is_absent(tmp_path):
    async def run():
        db = _make_db(tmp_path)
        await db.init()
        await _seed_market_row(db)
        result = await db.get_suppressed_sides_by_ids([(1, 10)], now=_now())
        assert (1, 10) not in result

    asyncio.run(run())


def test_get_suppressed_sides_by_ids_an_expired_suppression_reads_as_not_suppressed(tmp_path):
    async def run():
        db = _make_db(tmp_path)
        await db.init()
        await _seed_market_row(db)
        expired_until = _future(-1)  # 1 hour in the past
        await db.suppress_terminal_market_side(
            id_commodity=1, id_terminal=10, side="buy", until=expired_until, thread_id=1, leg_index=0
        )

        result = await db.get_suppressed_sides_by_ids([(1, 10)], now=_now())
        assert (1, 10) not in result

    asyncio.run(run())


def test_get_suppressed_sides_by_ids_only_returns_requested_pairs(tmp_path):
    async def run():
        db = _make_db(tmp_path)
        await db.init()
        await _seed_market_row(db)
        await _seed_market_row(db, id_commodity=2, id_terminal=20, commodity_name="Cobalt", terminal_name="Elsewhere")
        until = _future(SUPPRESSION_HOURS)
        await db.suppress_terminal_market_side(
            id_commodity=1, id_terminal=10, side="buy", until=until, thread_id=1, leg_index=0
        )
        await db.suppress_terminal_market_side(
            id_commodity=2, id_terminal=20, side="sell", until=until, thread_id=1, leg_index=0
        )

        result = await db.get_suppressed_sides_by_ids([(1, 10)], now=_now())
        assert result == {(1, 10): {"buy": True, "sell": False}}
        assert (2, 20) not in result, "a pair not in the requested list must not leak in"

    asyncio.run(run())


def test_get_mixed_route_market_rows_masks_a_suppressed_side_to_zero_stock(tmp_path):
    """The allocator (allocate_pair_cargo) this feeds already treats zero stock/demand as
    'skip this side' - masking at read time means no separate suppression-aware code is
    needed anywhere in bot/uex/mixed_routes.py or multi_stop_routes.py."""
    async def run():
        db = _make_db(tmp_path)
        await db.init()
        await _seed_market_row(db)
        until = _future(SUPPRESSION_HOURS)
        await db.suppress_terminal_market_side(
            id_commodity=1, id_terminal=10, side="buy", until=until, thread_id=1, leg_index=0
        )

        rows = await db.get_mixed_route_market_rows()
        row = next(r for r in rows if r["id_commodity"] == 1 and r["id_terminal"] == 10)
        assert row["scu_buy"] == 0
        assert row["scu_sell"] == 40, "the unsuppressed sell side must be untouched"

    asyncio.run(run())


def test_get_mixed_route_market_rows_an_expired_suppression_no_longer_masks(tmp_path):
    async def run():
        db = _make_db(tmp_path)
        await db.init()
        await _seed_market_row(db)
        expired_until = _future(-1)
        await db.suppress_terminal_market_side(
            id_commodity=1, id_terminal=10, side="buy", until=expired_until, thread_id=1, leg_index=0
        )

        rows = await db.get_mixed_route_market_rows()
        row = next(r for r in rows if r["id_commodity"] == 1 and r["id_terminal"] == 10)
        assert row["scu_buy"] == 50, "an expired suppression must not mask the real stock"

    asyncio.run(run())


def test_handle_leg_outcome_missing_suppresses_the_reported_side(tmp_path):
    """End-to-end: the real cog method, not a fake - a 'missing' report must trigger
    suppression for exactly the side that was reported, not the other side."""
    async def run():
        db = _make_db(tmp_path)
        await db.init()
        await _seed_market_row(db)
        cog = RouteProgression.__new__(RouteProgression)
        cog.bot = type("FakeBot", (), {"db": db})()
        cog._active_legs = {}

        leg = _leg_input(id_terminal=10, id_commodity=1, quoted_price=100.0, quoted_scu=50.0, quoted_status=3)
        await _create_thread_for_leg(db, 999, leg)
        await cog.handle_leg_outcome(None, 999, 0, leg, outcome="missing")

        result = await db.get_suppressed_sides_by_ids([(1, 10)], now=_now())
        assert result == {(1, 10): {"buy": True, "sell": False}}

    asyncio.run(run())


def test_handle_leg_outcome_matched_does_not_suppress(tmp_path):
    async def run():
        db = _make_db(tmp_path)
        await db.init()
        await _seed_market_row(db)
        cog = RouteProgression.__new__(RouteProgression)
        cog.bot = type("FakeBot", (), {"db": db})()
        cog._active_legs = {}

        leg = _leg_input(id_terminal=10, id_commodity=1, quoted_price=100.0, quoted_scu=50.0, quoted_status=3)
        await _create_thread_for_leg(db, 999, leg)
        await cog.handle_leg_outcome(None, 999, 0, leg, outcome="matched")

        result = await db.get_suppressed_sides_by_ids([(1, 10)], now=_now())
        assert (1, 10) not in result

    asyncio.run(run())


def test_handle_leg_outcome_a_positive_less_report_does_not_suppress(tmp_path):
    async def run():
        db = _make_db(tmp_path)
        await db.init()
        await _seed_market_row(db)
        cog = RouteProgression.__new__(RouteProgression)
        cog.bot = type("FakeBot", (), {"db": db})()
        cog._active_legs = {}

        leg = _leg_input(id_terminal=10, id_commodity=1, quoted_price=100.0, quoted_scu=50.0, quoted_status=3)
        await _create_thread_for_leg(db, 999, leg)
        await cog.handle_leg_outcome(None, 999, 0, leg, outcome="less", actual_scu=20.0)

        result = await db.get_suppressed_sides_by_ids([(1, 10)], now=_now())
        assert (1, 10) not in result

    asyncio.run(run())


def test_handle_leg_outcome_a_floor_capped_more_report_does_not_suppress(tmp_path):
    """'more' with precision='floor' means there was MORE there, not less - the exact
    opposite of a signal to stop recommending this side."""
    async def run():
        db = _make_db(tmp_path)
        await db.init()
        await _seed_market_row(db)
        cog = RouteProgression.__new__(RouteProgression)
        cog.bot = type("FakeBot", (), {"db": db})()
        cog._active_legs = {}

        leg = _leg_input(id_terminal=10, id_commodity=1, quoted_price=100.0, quoted_scu=50.0, quoted_status=3)
        await _create_thread_for_leg(db, 999, leg)
        await cog.handle_leg_outcome(None, 999, 0, leg, outcome="more", actual_scu=80.0, precision="floor")

        result = await db.get_suppressed_sides_by_ids([(1, 10)], now=_now())
        assert (1, 10) not in result

    asyncio.run(run())


def test_handle_leg_outcome_a_drained_more_report_suppresses(tmp_path):
    async def run():
        db = _make_db(tmp_path)
        await db.init()
        await _seed_market_row(db)
        cog = RouteProgression.__new__(RouteProgression)
        cog.bot = type("FakeBot", (), {"db": db})()
        cog._active_legs = {}

        leg = _leg_input(id_terminal=10, id_commodity=1, quoted_price=100.0, quoted_scu=50.0, quoted_status=3)
        await _create_thread_for_leg(db, 999, leg)
        await cog.handle_leg_outcome(None, 999, 0, leg, outcome="more", actual_scu=80.0, precision="exact")

        result = await db.get_suppressed_sides_by_ids([(1, 10)], now=_now())
        assert result == {(1, 10): {"buy": True, "sell": False}}

    asyncio.run(run())


def test_handle_leg_outcome_sell_side_missing_suppresses_only_sell(tmp_path):
    async def run():
        db = _make_db(tmp_path)
        await db.init()
        await _seed_market_row(db)
        cog = RouteProgression.__new__(RouteProgression)
        cog.bot = type("FakeBot", (), {"db": db})()
        cog._active_legs = {}

        leg = _leg_input(
            side="sell", id_terminal=10, id_commodity=1,
            terminal_name="Area18 TDD", quoted_price=90.0, quoted_scu=40.0, quoted_status=2,
        )
        await _create_thread_for_leg(db, 999, leg)
        await cog.handle_leg_outcome(None, 999, 0, leg, outcome="missing")

        result = await db.get_suppressed_sides_by_ids([(1, 10)], now=_now())
        assert result == {(1, 10): {"buy": False, "sell": True}}

    asyncio.run(run())


def test_handle_leg_outcome_suppression_expires_after_roughly_the_configured_window(tmp_path):
    async def run():
        db = _make_db(tmp_path)
        await db.init()
        await _seed_market_row(db)
        cog = RouteProgression.__new__(RouteProgression)
        cog.bot = type("FakeBot", (), {"db": db})()
        cog._active_legs = {}

        leg = _leg_input(id_terminal=10, id_commodity=1, quoted_price=100.0, quoted_scu=50.0, quoted_status=3)
        await _create_thread_for_leg(db, 999, leg)
        await cog.handle_leg_outcome(None, 999, 0, leg, outcome="missing")

        row = (await db.get_mixed_route_market_rows())[0]
        suppressed_until = datetime.strptime(row["buy_suppressed_until"], "%Y-%m-%d %H:%M:%S").replace(
            tzinfo=timezone.utc
        )
        delta_hours = (suppressed_until - datetime.now(timezone.utc)).total_seconds() / 3600
        assert abs(delta_hours - SUPPRESSION_HOURS) < 0.01

    asyncio.run(run())


# -- LegOutcomeView / ActualAmountModal / MoreOutcomeFollowupView claim logic -------------

class _FakeResponse:
    def __init__(
        self, *, fail_edit: bool = False, fail_send_message: bool = False,
        fail_exception: BaseException | None = None,
    ):
        self.messages = []
        self.modals = []
        self.edited_views = []
        self._fail_edit = fail_edit
        self._fail_send_message = fail_send_message
        # Defaults to discord.HTTPException so every pre-existing caller is unaffected -
        # pass a plain exception (e.g. RuntimeError) to prove a claim-release path
        # broadened to `except Exception:` also fires for a non-Discord failure like a
        # transport timeout, not just Discord's own HTTP error type.
        self._fail_exception = fail_exception or discord.HTTPException(NS(status=500, reason="test"), "test")

    async def send_modal(self, modal):
        self.modals.append(modal)

    async def send_message(self, *args, **kwargs):
        if self._fail_send_message:
            raise self._fail_exception
        self.messages.append((args, kwargs))

    async def edit_message(self, **kwargs):
        if self._fail_edit:
            raise self._fail_exception
        self.edited_views.append(kwargs)


class _FakeInteraction:
    def __init__(
        self, *, fail_edit: bool = False, fail_send_message: bool = False,
        fail_exception: BaseException | None = None,
    ):
        self.response = _FakeResponse(
            fail_edit=fail_edit, fail_send_message=fail_send_message, fail_exception=fail_exception,
        )
        self.channel = None


class _FakeMessage:
    def __init__(self, *, embeds=None):
        self.embeds = list(embeds) if embeds else []
        self.edits = []

    async def edit(self, **kwargs):
        self.edits.append(kwargs)
        if "embed" in kwargs:
            self.embeds = [kwargs["embed"]]


class _FakeCog:
    """Stands in for RouteProgression in the View/Modal claim-logic tests below - these
    exercise the durable-commit-point call sites (_record_leg_outcome_durably/
    _abandon_thread_durably), matching what the real Views/Modals call, but without the
    real retry wrapper's own behavior - see test_route_progression_retry.py-style tests
    further down for that.

    handled defaults to True (the ordinary case: the real method's return value says the
    outcome/abandon was recorded outright or durably queued) - a test proving the carry-
    forward "neither succeeded, restore reportability" defect sets it False to simulate
    the real method's own False return."""
    def __init__(self, *, handled: bool = True):
        self.calls = []
        self.abandon_calls = []
        self.handled = handled

    async def _record_leg_outcome_durably(self, channel, thread_id, leg_index, leg, **kwargs):
        self.calls.append((thread_id, leg_index, leg, kwargs))
        return self.handled

    async def _abandon_thread_durably(self, channel, thread_id, **kwargs):
        self.abandon_calls.append((thread_id, kwargs))
        return self.handled


def _leg_input(**overrides):
    base = dict(
        side="buy", id_terminal=1, id_commodity=1, terminal_name="Area18 TDD",
        commodity_name="Gold", display_label="Buy Gold at Area18 TDD",
        quoted_price=100.0, quoted_scu=50.0, quoted_status=3,
    )
    base.update(overrides)
    return RouteLegInput(**base)


def test_opening_a_less_modal_does_not_claim_the_leg():
    """The exact bug a live user hit: clicking 'Less / not there' then cancelling the
    modal without submitting anything must leave the leg reportable, not permanently
    stuck showing 'This leg was already reported.' forever."""
    async def run():
        cog = _FakeCog()
        view = LegOutcomeView(cog=cog, thread_id=1, leg_index=0, leg=_leg_input())
        interaction = _FakeInteraction()
        await view.less.callback(interaction)
        assert view.resolved is False
        assert len(interaction.response.modals) == 1

    asyncio.run(run())


def test_opening_a_more_modal_does_not_claim_the_leg():
    async def run():
        cog = _FakeCog()
        view = LegOutcomeView(cog=cog, thread_id=1, leg_index=0, leg=_leg_input())
        interaction = _FakeInteraction()
        await view.more.callback(interaction)
        assert view.resolved is False
        assert len(interaction.response.modals) == 1

    asyncio.run(run())


def test_a_cancelled_modal_can_be_retried_with_a_different_button():
    """After clicking 'Less' and abandoning it (never submitting), clicking 'More'
    instead still succeeds - neither button locks the leg just by being clicked."""
    async def run():
        cog = _FakeCog()
        view = LegOutcomeView(cog=cog, thread_id=1, leg_index=0, leg=_leg_input())
        await view.less.callback(_FakeInteraction())
        second = _FakeInteraction()
        await view.more.callback(second)
        assert view.resolved is False
        assert len(second.response.modals) == 1

    asyncio.run(run())


def test_matched_button_claims_the_leg_and_reports_immediately():
    async def run():
        cog = _FakeCog()
        view = LegOutcomeView(cog=cog, thread_id=1, leg_index=0, leg=_leg_input())
        interaction = _FakeInteraction()
        await view.matched.callback(interaction)
        assert view.resolved is True
        assert len(cog.calls) == 1
        assert cog.calls[0][3]["outcome"] == "matched"

        second = _FakeInteraction()
        await view.matched.callback(second)
        assert len(cog.calls) == 1, "a second click must not double-report"
        assert "already reported" in second.response.messages[0][0][0]

    asyncio.run(run())


def test_matched_button_shows_the_reported_outcome_under_the_original_quoted_line():
    """The exact behavior the user asked for: after a leg is reported, the SAME message
    box shows what was reported, under the original 'Quoted: ...' text - not just
    disabled buttons with no visible result."""
    async def run():
        cog = _FakeCog()
        view = LegOutcomeView(cog=cog, thread_id=1, leg_index=0, leg=_leg_input())
        view.message = _FakeMessage(embeds=[discord.Embed(description="Quoted: 100.00 aUEC/unit · 50 SCU")])
        interaction = _FakeInteraction()
        await view.matched.callback(interaction)

        edited_embed = interaction.response.edited_views[0]["embed"]
        assert "Quoted: 100.00 aUEC/unit · 50 SCU" in edited_embed.description
        assert "Matched the quote" in edited_embed.description

    asyncio.run(run())


def test_matched_button_with_no_stored_message_still_reports_without_error():
    """view.message is only set once _post_leg_prompt actually sends it - must not crash
    if it's somehow still None (matches every other test in this file, which never set it)."""
    async def run():
        cog = _FakeCog()
        view = LegOutcomeView(cog=cog, thread_id=1, leg_index=0, leg=_leg_input())
        interaction = _FakeInteraction()
        await view.matched.callback(interaction)
        assert "embed" not in interaction.response.edited_views[0]
        assert len(cog.calls) == 1

    asyncio.run(run())


def test_a_failed_acknowledgement_releases_the_claim_so_a_retry_can_record_the_outcome():
    """Real defect: matched() claimed the leg BEFORE acking the interaction. If that ack
    failed (a real Discord 500, a rate limit, a network blip), nothing was ever persisted
    (handle_leg_outcome never ran) but the leg was already marked resolved forever - a
    retry click was rejected as 'already reported' with no outcome ever recorded."""
    async def run():
        cog = _FakeCog()
        view = LegOutcomeView(cog=cog, thread_id=1, leg_index=0, leg=_leg_input())
        failing = _FakeInteraction(fail_edit=True)
        with pytest.raises(discord.HTTPException):
            await view.matched.callback(failing)
        assert view.resolved is False, "must be released - nothing was persisted"
        assert not cog.calls

        retry = _FakeInteraction()
        await view.matched.callback(retry)
        assert view.resolved is True
        assert len(cog.calls) == 1, "the retry must actually record the outcome"

    asyncio.run(run())


def test_disable_in_background_swallows_any_exception_not_just_http():
    """Carry-forward defect from the 2026-09-13 audit: disable_in_background (the
    PARENT view's own purely cosmetic message edit - separate from the interaction's own
    acknowledgement) used to catch only discord.HTTPException. It holds no claim of its
    own to release, but every real caller invokes it BETWEEN their own successful
    acknowledgement and the real durable write - so a non-HTTPException escaping here
    (a raw transport timeout, not a real Discord error response) skipped that durable
    call entirely, even though the leg was already claimed and the user already saw it
    acknowledged. Fixed by swallowing any exception, not just discord.HTTPException -
    nothing durable rides on this specific edit landing."""
    async def run():
        cog = _FakeCog()
        view = LegOutcomeView(cog=cog, thread_id=1, leg_index=0, leg=_leg_input())
        broken_message = _FakeMessage()
        broken_message.edit = AsyncMock(side_effect=RuntimeError("connection reset"))
        view.message = broken_message

        await view.disable_in_background("some outcome line")  # must not raise

    asyncio.run(run())


def test_disable_in_background_failure_does_not_block_the_durable_abandon_call():
    """Same defect, proven end-to-end through one of its four real call sites -
    AbandonConfirmView.confirm, matching the audit's own literal reproduction. The
    interaction's own acknowledgement succeeds; only the SEPARATE parent-message cosmetic
    edit inside disable_in_background fails. Before the fix, that RuntimeError escaped
    disable_in_background uncaught and _abandon_thread_durably was never called - the
    route was left claimed/locked with nothing actually persisted. The identical shape
    covers the other three call sites (ActualAmountModal's 'less' flow and both
    MoreOutcomeFollowupView buttons), which all share this same disable_in_background
    call, so a per-call-site regression test isn't needed for each of them."""
    async def run():
        cog = _FakeCog()
        view = LegOutcomeView(cog=cog, thread_id=1, leg_index=0, leg=_leg_input())
        broken_message = _FakeMessage(embeds=[discord.Embed(description="Quoted: 100.00 aUEC/unit · 50 SCU")])
        broken_message.edit = AsyncMock(side_effect=RuntimeError("connection reset"))
        view.message = broken_message
        confirm_view = AbandonConfirmView(cog=cog, thread_id=1, parent_view=view)

        await confirm_view.confirm.callback(_FakeInteraction())  # must not raise

        assert len(cog.abandon_calls) == 1, (
            "the durable abandon call must still run despite the cosmetic parent-message edit failing"
        )

    asyncio.run(run())


def test_a_non_discord_failed_acknowledgement_also_releases_the_claim():
    """Audit finding: the release-on-failure above was originally scoped to
    `except discord.HTTPException:` only - a timeout or other transport-level failure
    (not a discord.py HTTP error) would consume the claim without releasing it, since it
    isn't an HTTPException. Broadened to `except Exception:` so ANY failure before
    persistence releases the claim, proven here with a plain RuntimeError."""
    async def run():
        cog = _FakeCog()
        view = LegOutcomeView(cog=cog, thread_id=1, leg_index=0, leg=_leg_input())
        failing = _FakeInteraction(fail_edit=True, fail_exception=RuntimeError("connection reset"))
        with pytest.raises(RuntimeError):
            await view.matched.callback(failing)
        assert view.resolved is False, "must be released even for a non-HTTPException failure"
        assert not cog.calls

        retry = _FakeInteraction()
        await view.matched.callback(retry)
        assert view.resolved is True
        assert len(cog.calls) == 1

    asyncio.run(run())


def test_a_failed_less_modal_acknowledgement_releases_the_claim():
    """Same defect class as the button case above, for the 'less' modal's own commit
    point (ActualAmountModal.on_submit)."""
    async def run():
        cog = _FakeCog()
        view = LegOutcomeView(cog=cog, thread_id=1, leg_index=0, leg=_leg_input())
        modal = ActualAmountModal(
            cog=cog, thread_id=1, leg_index=0, leg=view.leg, flow="less", parent_view=view
        )
        modal.scu_input._value = "20"
        modal.price_input._value = ""
        failing = _FakeInteraction(fail_send_message=True)
        with pytest.raises(discord.HTTPException):
            await modal.on_submit(failing)
        assert view.resolved is False
        assert not cog.calls

        retry_modal = ActualAmountModal(
            cog=cog, thread_id=1, leg_index=0, leg=view.leg, flow="less", parent_view=view
        )
        retry_modal.scu_input._value = "20"
        retry_modal.price_input._value = ""
        await retry_modal.on_submit(_FakeInteraction())
        assert view.resolved is True
        assert len(cog.calls) == 1

    asyncio.run(run())


def test_a_non_discord_failed_less_modal_acknowledgement_also_releases_the_claim():
    """Same broadened-exception-handling proof as above, for ActualAmountModal.on_submit's
    own claim-release path."""
    async def run():
        cog = _FakeCog()
        view = LegOutcomeView(cog=cog, thread_id=1, leg_index=0, leg=_leg_input())
        modal = ActualAmountModal(
            cog=cog, thread_id=1, leg_index=0, leg=view.leg, flow="less", parent_view=view
        )
        modal.scu_input._value = "20"
        modal.price_input._value = ""
        failing = _FakeInteraction(fail_send_message=True, fail_exception=RuntimeError("connection reset"))
        with pytest.raises(RuntimeError):
            await modal.on_submit(failing)
        assert view.resolved is False
        assert not cog.calls

        retry_modal = ActualAmountModal(
            cog=cog, thread_id=1, leg_index=0, leg=view.leg, flow="less", parent_view=view
        )
        retry_modal.scu_input._value = "20"
        retry_modal.price_input._value = ""
        await retry_modal.on_submit(_FakeInteraction())
        assert view.resolved is True
        assert len(cog.calls) == 1

    asyncio.run(run())


def test_a_failed_more_outcome_followup_acknowledgement_releases_the_claim():
    """Same defect class for MoreOutcomeFollowupView's own commit points."""
    async def run():
        cog = _FakeCog()
        view = LegOutcomeView(cog=cog, thread_id=1, leg_index=0, leg=_leg_input())
        followup = MoreOutcomeFollowupView(
            cog=cog, thread_id=1, leg_index=0, leg=view.leg, actual_price=None, actual_scu=80.0,
            parent_view=view,
        )
        failing = _FakeInteraction(fail_edit=True)
        with pytest.raises(discord.HTTPException):
            await followup.drained.callback(failing)
        assert view.resolved is False
        assert not cog.calls
        assert all(not item.disabled for item in followup.children), "buttons must be re-enabled too"

        await followup.drained.callback(_FakeInteraction())
        assert view.resolved is True
        assert len(cog.calls) == 1

    asyncio.run(run())


def test_a_non_discord_failed_more_outcome_followup_acknowledgement_also_releases_the_claim():
    """Same broadened-exception-handling proof as above, for
    MoreOutcomeFollowupView.drained/capacity_limited's own claim-release path."""
    async def run():
        cog = _FakeCog()
        view = LegOutcomeView(cog=cog, thread_id=1, leg_index=0, leg=_leg_input())
        followup = MoreOutcomeFollowupView(
            cog=cog, thread_id=1, leg_index=0, leg=view.leg, actual_price=None, actual_scu=80.0,
            parent_view=view,
        )
        failing = _FakeInteraction(fail_edit=True, fail_exception=RuntimeError("connection reset"))
        with pytest.raises(RuntimeError):
            await followup.drained.callback(failing)
        assert view.resolved is False
        assert not cog.calls
        assert all(not item.disabled for item in followup.children), "buttons must be re-enabled too"

        await followup.drained.callback(_FakeInteraction())
        assert view.resolved is True
        assert len(cog.calls) == 1

    asyncio.run(run())


# -- Restoring real reportability when BOTH the save and its durable recovery fail ------
# Carry-forward defect from the 2026-09-13 audit: _record_leg_outcome_durably/
# _abandon_thread_durably's final notice ("please report this leg again") used to go out
# regardless of whether the button the user would need to click for that was still
# usable - claim() had already disabled it, and disable_in_background had already pushed
# that disabled state to the real Discord message, so "report it again" was a dead end
# even though the message said otherwise. Fixed by having both methods return whether
# they actually handled it (recorded outright or durably queued) and having every caller
# release the claim AND re-push the now-re-enabled view when they return False - proven
# here via _FakeCog(handled=False), which simulates that real False return.

def test_matched_button_restores_reportability_when_recovery_also_fails():
    async def run():
        cog = _FakeCog(handled=False)
        view = LegOutcomeView(cog=cog, thread_id=1, leg_index=0, leg=_leg_input())
        message = _FakeMessage(embeds=[discord.Embed(description="Quoted: 100.00 aUEC/unit · 50 SCU")])
        view.message = message

        await view.matched.callback(_FakeInteraction())

        assert view.resolved is False, "the leg must become reportable again"
        assert all(not item.disabled for item in view.children), "buttons must be re-enabled"
        assert message.edits[-1]["view"] is view, "the real Discord message must reflect it"

    asyncio.run(run())


def test_less_modal_restores_reportability_when_recovery_also_fails():
    async def run():
        cog = _FakeCog(handled=False)
        view = LegOutcomeView(cog=cog, thread_id=1, leg_index=0, leg=_leg_input())
        message = _FakeMessage(embeds=[discord.Embed(description="Quoted: 100.00 aUEC/unit · 50 SCU")])
        view.message = message
        modal = ActualAmountModal(
            cog=cog, thread_id=1, leg_index=0, leg=view.leg, flow="less", parent_view=view
        )
        modal.scu_input._value = "20"
        modal.price_input._value = ""

        await modal.on_submit(_FakeInteraction())

        assert view.resolved is False, "the leg must become reportable again"
        assert all(not item.disabled for item in view.children), "buttons must be re-enabled"
        assert message.edits[-1]["view"] is view, "the real Discord message must reflect it"

    asyncio.run(run())


def test_more_outcome_followup_restores_reportability_when_recovery_also_fails():
    async def run():
        cog = _FakeCog(handled=False)
        view = LegOutcomeView(cog=cog, thread_id=1, leg_index=0, leg=_leg_input())
        message = _FakeMessage(embeds=[discord.Embed(description="Quoted: 100.00 aUEC/unit · 50 SCU")])
        view.message = message
        followup = MoreOutcomeFollowupView(
            cog=cog, thread_id=1, leg_index=0, leg=view.leg, actual_price=None, actual_scu=80.0,
            parent_view=view,
        )

        await followup.drained.callback(_FakeInteraction())

        assert view.resolved is False, "the leg must become reportable again"
        assert all(not item.disabled for item in view.children), "buttons must be re-enabled"
        assert message.edits[-1]["view"] is view, "the real Discord message must reflect it"

    asyncio.run(run())


def test_abandon_confirm_restores_reportability_when_recovery_also_fails():
    async def run():
        cog = _FakeCog(handled=False)
        view = LegOutcomeView(cog=cog, thread_id=1, leg_index=0, leg=_leg_input())
        message = _FakeMessage(embeds=[discord.Embed(description="Quoted: 100.00 aUEC/unit · 50 SCU")])
        view.message = message
        confirm_view = AbandonConfirmView(cog=cog, thread_id=1, parent_view=view)

        await confirm_view.confirm.callback(_FakeInteraction())

        assert view.resolved is False, "the leg must become reportable again"
        assert all(not item.disabled for item in view.children), "buttons must be re-enabled"
        assert message.edits[-1]["view"] is view, "the real Discord message must reflect it"

    asyncio.run(run())


def test_a_failed_abandon_confirmation_acknowledgement_releases_the_claim():
    """Same defect class for AbandonConfirmView's confirm button."""
    async def run():
        cog = _FakeCog()
        view = LegOutcomeView(cog=cog, thread_id=1, leg_index=0, leg=_leg_input())
        confirm_view = AbandonConfirmView(cog=cog, thread_id=1, parent_view=view)
        failing = _FakeInteraction(fail_edit=True)
        with pytest.raises(discord.HTTPException):
            await confirm_view.confirm.callback(failing)
        assert view.resolved is False
        assert not cog.abandon_calls

        await confirm_view.confirm.callback(_FakeInteraction())
        assert view.resolved is True
        assert len(cog.abandon_calls) == 1

    asyncio.run(run())


def test_a_non_discord_failed_abandon_confirmation_acknowledgement_also_releases_the_claim():
    """Same broadened-exception-handling proof as above, for AbandonConfirmView.confirm's
    own claim-release path."""
    async def run():
        cog = _FakeCog()
        view = LegOutcomeView(cog=cog, thread_id=1, leg_index=0, leg=_leg_input())
        confirm_view = AbandonConfirmView(cog=cog, thread_id=1, parent_view=view)
        failing = _FakeInteraction(fail_edit=True, fail_exception=RuntimeError("connection reset"))
        with pytest.raises(RuntimeError):
            await confirm_view.confirm.callback(failing)
        assert view.resolved is False
        assert not cog.abandon_calls

        await confirm_view.confirm.callback(_FakeInteraction())
        assert view.resolved is True
        assert len(cog.abandon_calls) == 1

    asyncio.run(run())


def test_less_modal_submission_claims_the_leg_and_reports_the_actual_amount():
    async def run():
        cog = _FakeCog()
        view = LegOutcomeView(cog=cog, thread_id=1, leg_index=0, leg=_leg_input())
        modal = ActualAmountModal(
            cog=cog, thread_id=1, leg_index=0, leg=view.leg, flow="less", parent_view=view
        )
        modal.scu_input._value = "20"
        modal.price_input._value = ""
        await modal.on_submit(_FakeInteraction())

        assert view.resolved is True
        assert len(cog.calls) == 1
        kwargs = cog.calls[0][3]
        assert kwargs["outcome"] == "less"
        assert kwargs["actual_scu"] == 20.0

    asyncio.run(run())


def test_less_modal_with_zero_scu_reports_missing_instead():
    async def run():
        cog = _FakeCog()
        view = LegOutcomeView(cog=cog, thread_id=1, leg_index=0, leg=_leg_input())
        modal = ActualAmountModal(
            cog=cog, thread_id=1, leg_index=0, leg=view.leg, flow="less", parent_view=view
        )
        modal.scu_input._value = "0"
        modal.price_input._value = ""
        await modal.on_submit(_FakeInteraction())

        assert cog.calls[0][3]["outcome"] == "missing"

    asyncio.run(run())


def test_less_modal_rejects_infinite_scu_before_claiming_or_reporting():
    """Real defect: float() happily parses 'inf'/'nan' (not a ValueError), so a malformed
    SCU report could reach terminal_market_state as positive infinity. Must be rejected
    before the leg is even claimed - not just before the DB write."""
    async def run():
        cog = _FakeCog()
        view = LegOutcomeView(cog=cog, thread_id=1, leg_index=0, leg=_leg_input())
        modal = ActualAmountModal(
            cog=cog, thread_id=1, leg_index=0, leg=view.leg, flow="less", parent_view=view
        )
        modal.scu_input._value = "inf"
        modal.price_input._value = ""
        interaction = _FakeInteraction()
        await modal.on_submit(interaction)

        assert view.resolved is False, "must not claim the leg on a rejected report"
        assert not cog.calls
        assert "non-negative" in interaction.response.messages[0][0][0]

    asyncio.run(run())


def test_less_modal_rejects_negative_scu():
    async def run():
        cog = _FakeCog()
        view = LegOutcomeView(cog=cog, thread_id=1, leg_index=0, leg=_leg_input())
        modal = ActualAmountModal(
            cog=cog, thread_id=1, leg_index=0, leg=view.leg, flow="less", parent_view=view
        )
        modal.scu_input._value = "-5"
        modal.price_input._value = ""
        await modal.on_submit(_FakeInteraction())

        assert view.resolved is False
        assert not cog.calls

    asyncio.run(run())


def test_less_modal_rejects_a_negative_price():
    async def run():
        cog = _FakeCog()
        view = LegOutcomeView(cog=cog, thread_id=1, leg_index=0, leg=_leg_input())
        modal = ActualAmountModal(
            cog=cog, thread_id=1, leg_index=0, leg=view.leg, flow="less", parent_view=view
        )
        modal.scu_input._value = "20"
        modal.price_input._value = "-10"
        await modal.on_submit(_FakeInteraction())

        assert view.resolved is False
        assert not cog.calls

    asyncio.run(run())


def test_less_modal_accepts_zero_scu_as_a_valid_boundary_value():
    """Neighboring boundary case: zero must stay valid (a legitimate 'nothing was there'
    report), not get caught by the same non-negative check that rejects -5."""
    async def run():
        cog = _FakeCog()
        view = LegOutcomeView(cog=cog, thread_id=1, leg_index=0, leg=_leg_input())
        modal = ActualAmountModal(
            cog=cog, thread_id=1, leg_index=0, leg=view.leg, flow="less", parent_view=view
        )
        modal.scu_input._value = "0"
        modal.price_input._value = "0"
        await modal.on_submit(_FakeInteraction())

        assert view.resolved is True
        assert cog.calls[0][3]["outcome"] == "missing"

    asyncio.run(run())


def test_less_modal_submission_after_the_leg_was_already_resolved_is_refused():
    async def run():
        cog = _FakeCog()
        view = LegOutcomeView(cog=cog, thread_id=1, leg_index=0, leg=_leg_input())
        view.resolved = True  # e.g. resolved via a race with another path
        modal = ActualAmountModal(
            cog=cog, thread_id=1, leg_index=0, leg=view.leg, flow="less", parent_view=view
        )
        modal.scu_input._value = "20"
        modal.price_input._value = ""
        interaction = _FakeInteraction()
        await modal.on_submit(interaction)

        assert not cog.calls, "a modal submitted after the leg resolved must not report again"
        assert "already reported" in interaction.response.messages[0][0][0]

    asyncio.run(run())


def test_less_modal_submission_shows_the_reported_amount_on_the_original_message():
    async def run():
        cog = _FakeCog()
        view = LegOutcomeView(cog=cog, thread_id=1, leg_index=0, leg=_leg_input())
        view.message = _FakeMessage(embeds=[discord.Embed(description="Quoted: 100.00 aUEC/unit · 50 SCU")])
        modal = ActualAmountModal(
            cog=cog, thread_id=1, leg_index=0, leg=view.leg, flow="less", parent_view=view
        )
        modal.scu_input._value = "20"
        modal.price_input._value = ""
        await modal.on_submit(_FakeInteraction())

        edited_embed = view.message.edits[-1]["embed"]
        assert "Quoted: 100.00 aUEC/unit · 50 SCU" in edited_embed.description
        assert "Less than quoted" in edited_embed.description
        assert "20 SCU" in edited_embed.description

    asyncio.run(run())


def test_more_outcome_drained_shows_the_reported_amount_on_the_original_message():
    async def run():
        cog = _FakeCog()
        view = LegOutcomeView(cog=cog, thread_id=1, leg_index=0, leg=_leg_input())
        view.message = _FakeMessage(embeds=[discord.Embed(description="Quoted: 100.00 aUEC/unit · 50 SCU")])
        followup = MoreOutcomeFollowupView(
            cog=cog, thread_id=1, leg_index=0, leg=view.leg, actual_price=None, actual_scu=80.0,
            parent_view=view,
        )
        await followup.drained.callback(_FakeInteraction())

        edited_embed = view.message.edits[-1]["embed"]
        assert "More than quoted" in edited_embed.description
        assert "drained" in edited_embed.description

    asyncio.run(run())


def test_more_outcome_capacity_limited_shows_the_floor_report_on_the_original_message():
    async def run():
        cog = _FakeCog()
        view = LegOutcomeView(cog=cog, thread_id=1, leg_index=0, leg=_leg_input())
        view.message = _FakeMessage(embeds=[discord.Embed(description="Quoted: 100.00 aUEC/unit · 50 SCU")])
        followup = MoreOutcomeFollowupView(
            cog=cog, thread_id=1, leg_index=0, leg=view.leg, actual_price=None, actual_scu=80.0,
            parent_view=view,
        )
        await followup.capacity_limited.callback(_FakeInteraction())

        edited_embed = view.message.edits[-1]["embed"]
        assert "at least 80 SCU" in edited_embed.description
        assert "capped" in edited_embed.description

    asyncio.run(run())


def test_abandoning_a_route_shows_an_abandoned_note_on_the_original_message():
    async def run():
        cog = _FakeCog()
        view = LegOutcomeView(cog=cog, thread_id=1, leg_index=0, leg=_leg_input())
        view.message = _FakeMessage(embeds=[discord.Embed(description="Quoted: 100.00 aUEC/unit · 50 SCU")])
        confirm_view = AbandonConfirmView(cog=cog, thread_id=1, parent_view=view)
        await confirm_view.confirm.callback(_FakeInteraction())

        edited_embed = view.message.edits[-1]["embed"]
        assert "Quoted: 100.00 aUEC/unit · 50 SCU" in edited_embed.description
        assert "Route abandoned" in edited_embed.description

    asyncio.run(run())


def test_more_modal_submission_does_not_claim_until_the_followup_view_resolves():
    async def run():
        cog = _FakeCog()
        view = LegOutcomeView(cog=cog, thread_id=1, leg_index=0, leg=_leg_input())
        modal = ActualAmountModal(
            cog=cog, thread_id=1, leg_index=0, leg=view.leg, flow="more", parent_view=view
        )
        modal.scu_input._value = "80"
        modal.price_input._value = ""
        interaction = _FakeInteraction()
        await modal.on_submit(interaction)

        assert view.resolved is False, "the modal only collects the amount, it doesn't commit"
        followup_view = interaction.response.messages[0][1]["view"]
        assert isinstance(followup_view, MoreOutcomeFollowupView)

        await followup_view.drained.callback(_FakeInteraction())
        assert view.resolved is True
        assert cog.calls[0][3]["outcome"] == "more"
        assert cog.calls[0][3]["precision"] == "exact"

    asyncio.run(run())


def test_more_outcome_followup_capacity_limited_sets_floor_precision_and_is_single_shot():
    async def run():
        cog = _FakeCog()
        view = LegOutcomeView(cog=cog, thread_id=1, leg_index=0, leg=_leg_input())
        followup = MoreOutcomeFollowupView(
            cog=cog, thread_id=1, leg_index=0, leg=view.leg, actual_price=None, actual_scu=80.0,
            parent_view=view,
        )
        await followup.capacity_limited.callback(_FakeInteraction())
        assert view.resolved is True
        assert cog.calls[0][3]["precision"] == "floor"

        second = _FakeInteraction()
        await followup.drained.callback(second)
        assert len(cog.calls) == 1, "the parent leg is already resolved - a second button must not commit again"

    asyncio.run(run())


# -- Abandon route -------------------------------------------------------------------------

def test_opening_the_abandon_confirmation_does_not_claim_the_leg():
    async def run():
        cog = _FakeCog()
        view = LegOutcomeView(cog=cog, thread_id=1, leg_index=0, leg=_leg_input())
        interaction = _FakeInteraction()
        await view.abandon.callback(interaction)
        assert view.resolved is False
        assert len(interaction.response.messages) == 1
        confirm_view = interaction.response.messages[0][1]["view"]
        assert isinstance(confirm_view, AbandonConfirmView)

    asyncio.run(run())


def test_confirming_abandonment_claims_the_leg_and_closes_the_thread():
    async def run():
        cog = _FakeCog()
        view = LegOutcomeView(cog=cog, thread_id=1, leg_index=0, leg=_leg_input())
        confirm_view = AbandonConfirmView(cog=cog, thread_id=1, parent_view=view)
        await confirm_view.confirm.callback(_FakeInteraction())

        assert view.resolved is True
        assert len(cog.abandon_calls) == 1
        assert cog.abandon_calls[0][0] == 1
        assert not cog.calls, "abandoning must not also report a leg outcome"

    asyncio.run(run())


def test_cancelling_abandonment_leaves_the_leg_reportable():
    async def run():
        cog = _FakeCog()
        view = LegOutcomeView(cog=cog, thread_id=1, leg_index=0, leg=_leg_input())
        confirm_view = AbandonConfirmView(cog=cog, thread_id=1, parent_view=view)
        await confirm_view.cancel.callback(_FakeInteraction())

        assert view.resolved is False
        assert not cog.abandon_calls
        # The leg is still open - a real outcome button still works.
        interaction = _FakeInteraction()
        await view.matched.callback(interaction)
        assert view.resolved is True
        assert len(cog.calls) == 1

    asyncio.run(run())


def test_a_second_abandon_confirmation_after_the_leg_resolved_is_refused():
    """Race guard: if the leg was reported through another path (or a first Abandon
    confirmation already went through) while this confirmation view was still open,
    confirming again must not also close the thread."""
    async def run():
        cog = _FakeCog()
        view = LegOutcomeView(cog=cog, thread_id=1, leg_index=0, leg=_leg_input())
        view.claim()  # simulate the leg already having resolved via another path
        confirm_view = AbandonConfirmView(cog=cog, thread_id=1, parent_view=view)
        interaction = _FakeInteraction()
        await confirm_view.confirm.callback(interaction)

        assert not cog.abandon_calls
        assert "already reported" in interaction.response.messages[0][0][0]

    asyncio.run(run())


def test_abandon_thread_sets_status_before_touching_the_channel(tmp_path):
    """abandon_thread (the real cog method, not the fake used above) must mark the thread
    abandoned even when the channel can't be reached/messaged - matching the same
    "DB status set before the risky I/O" discipline as the rest of this codebase's
    recovery paths."""
    from bot.cogs.route_progression import RouteProgression

    async def run():
        db = _make_db(tmp_path)
        await db.init()
        await db.create_route_progression_thread(
            thread_id=1, user_id=100, guild_id=200, route_kind="best_route", route_snapshot={},
            legs=[{"side": "buy", "id_terminal": 10, "id_commodity": 1}],
        )
        cog = RouteProgression.__new__(RouteProgression)
        cog.bot = type("FakeBot", (), {"db": db})()
        cog._active_legs = {1: ["placeholder"]}

        await cog.abandon_thread(None, 1, reason="test")

        thread = await db.get_route_progression_thread(1)
        assert thread["status"] == "abandoned"
        assert 1 not in cog._active_legs

    asyncio.run(run())


# -- start_tracking partial-failure gaps (audit-confirmed defect #4) ---------------------
# Real gap: between creating the Discord thread and successfully posting the first leg
# prompt, start_tracking has several await points (add_user, the DB write, two intro
# messages, the first leg prompt) that can each fail independently. Before this fix, a
# failure at any of them except the very first (create_thread itself) either propagated
# uncaught (no error handling at all) or was misreported as "couldn't create a thread"
# when the thread had, in fact, already been created - either way leaving an orphaned
# Discord thread (and sometimes an orphaned in_progress DB row) behind with no cleanup.

class _FakeTextChannel(discord.TextChannel):
    """A real discord.TextChannel subclass so isinstance(channel, discord.TextChannel) -
    which start_tracking's own real code checks - still passes."""
    def __init__(self):
        self.create_thread = AsyncMock()


class _FakeTrackingThread(discord.Thread):
    """A real discord.Thread subclass, matching _FakeThreadChannel's own pattern above,
    with every method start_tracking can call on a freshly created thread stubbed out."""
    def __init__(self, thread_id: int):
        self.id = thread_id  # mention is a read-only property derived from id
        self.add_user = AsyncMock()
        self.send = AsyncMock()
        self.delete = AsyncMock()


class _FakeStartTrackingInteraction:
    def __init__(self, *, channel, user_id: int = 1, guild_id: int = 1):
        self.channel = channel
        self.response = NS(defer=AsyncMock())
        self.followup = NS(send=AsyncMock())
        self.user = NS(id=user_id)
        self.guild_id = guild_id
        self.message = None


def _trackable_route(**overrides) -> TrackableRoute:
    base = dict(route_kind="best_route", title="Test Route", legs=[_leg_input(id_terminal=10, id_commodity=1)])
    base.update(overrides)
    return TrackableRoute(**base)


def test_start_tracking_reports_the_permissions_hint_when_thread_creation_itself_fails(tmp_path):
    """When create_thread itself is what fails, nothing was ever created - no cleanup is
    needed, and the original, more specific hint (this is the single most common real
    failure - a missing Manage Threads permission) is worth keeping over the generic
    partial-failure message used once a thread actually exists."""
    async def run():
        db = _make_db(tmp_path)
        await db.init()
        cog = RouteProgression.__new__(RouteProgression)
        cog.bot = type("FakeBot", (), {"db": db})()
        cog._active_legs = {}
        channel = _FakeTextChannel()
        channel.create_thread = AsyncMock(
            side_effect=discord.HTTPException(NS(status=403, reason="Forbidden"), "no perms")
        )
        interaction = _FakeStartTrackingInteraction(channel=channel)

        await cog.start_tracking(interaction, _trackable_route())

        message = interaction.followup.send.call_args.args[0]
        assert "missing permissions" in message.lower()

    asyncio.run(run())


def test_start_tracking_cleans_up_an_orphaned_thread_when_add_user_fails(tmp_path):
    """Real gap: add_user failing was previously caught by the SAME handler as
    create_thread itself, which blamed 'couldn't create a private thread' even though the
    thread genuinely was created - and never cleaned it up, since nothing at that point
    even tracked that a thread now existed with nobody able to see the flow in it."""
    async def run():
        db = _make_db(tmp_path)
        await db.init()
        cog = RouteProgression.__new__(RouteProgression)
        cog.bot = type("FakeBot", (), {"db": db})()
        cog._active_legs = {}
        thread = _FakeTrackingThread(555)
        thread.add_user = AsyncMock(side_effect=discord.HTTPException(NS(status=500, reason="x"), "x"))
        channel = _FakeTextChannel()
        channel.create_thread = AsyncMock(return_value=thread)
        interaction = _FakeStartTrackingInteraction(channel=channel)

        await cog.start_tracking(interaction, _trackable_route())

        assert thread.delete.await_count == 1, "the orphaned thread must be cleaned up, not left behind"
        assert await db.get_route_progression_thread(555) is None
        message = interaction.followup.send.call_args.args[0]
        assert "try tracking the route again" in message
        assert "missing permissions" not in message.lower(), (
            "must not misreport this as a thread-creation failure - the thread was created fine"
        )

    asyncio.run(run())


def test_start_tracking_cleans_up_when_the_db_write_fails(tmp_path, monkeypatch):
    """Real gap: this step had NO error handling at all - a DB failure here propagated
    straight out of start_tracking (this bot has no global app-command error handler), so
    the interaction was left stuck on 'thinking...' forever while the already-created,
    already-user-added thread sat orphaned with no DB row and no explanation."""
    async def run():
        db = _make_db(tmp_path)
        await db.init()
        cog = RouteProgression.__new__(RouteProgression)
        cog.bot = type("FakeBot", (), {"db": db})()
        cog._active_legs = {}
        monkeypatch.setattr(db, "create_route_progression_thread", AsyncMock(side_effect=RuntimeError("db lock")))
        thread = _FakeTrackingThread(556)
        channel = _FakeTextChannel()
        channel.create_thread = AsyncMock(return_value=thread)
        interaction = _FakeStartTrackingInteraction(channel=channel)

        await cog.start_tracking(interaction, _trackable_route())  # must not raise

        assert thread.delete.await_count == 1
        assert 556 not in cog._active_legs
        interaction.followup.send.assert_awaited_once()

    asyncio.run(run())


def test_start_tracking_cleans_up_when_the_intro_message_fails(tmp_path):
    """Real gap: by this point the DB row DOES exist (in_progress, advanced_to_index=-1)
    but nothing was ever posted - previously self-healed only once the 48h abandonment
    poller happened to sweep it, with zero communication to the user in the meantime."""
    async def run():
        db = _make_db(tmp_path)
        await db.init()
        cog = RouteProgression.__new__(RouteProgression)
        cog.bot = type("FakeBot", (), {"db": db})()
        cog._active_legs = {}
        thread = _FakeTrackingThread(557)
        thread.send = AsyncMock(side_effect=RuntimeError("network hiccup"))
        channel = _FakeTextChannel()
        channel.create_thread = AsyncMock(return_value=thread)
        interaction = _FakeStartTrackingInteraction(channel=channel)

        await cog.start_tracking(interaction, _trackable_route())

        assert thread.delete.await_count == 1
        assert await db.get_route_progression_thread(557) is None, "the DB row must be rolled back too"
        assert 557 not in cog._active_legs

    asyncio.run(run())


def test_start_tracking_cleans_up_when_the_first_leg_prompt_fails(tmp_path):
    """Same rollback, for the last step in the chain - _post_leg_prompt's own claim-
    release (fixed earlier this round to catch any exception, not just
    discord.HTTPException) already protects the DB-level advance claim; this proves
    start_tracking's own outer rollback covers this step too, consistently with every
    other one, rather than the old bespoke 'try tracking the route again, no cleanup'
    handling this one step alone used to get."""
    async def run():
        db = _make_db(tmp_path)
        await db.init()
        cog = RouteProgression.__new__(RouteProgression)
        cog.bot = type("FakeBot", (), {"db": db})()
        cog._active_legs = {}
        thread = _FakeTrackingThread(558)
        channel = _FakeTextChannel()
        channel.create_thread = AsyncMock(return_value=thread)
        interaction = _FakeStartTrackingInteraction(channel=channel)

        async def failing_post_leg_prompt(*args, **kwargs):
            raise discord.HTTPException(NS(status=500, reason="x"), "send failed")

        cog._post_leg_prompt = failing_post_leg_prompt

        await cog.start_tracking(interaction, _trackable_route())

        assert thread.delete.await_count == 1
        assert await db.get_route_progression_thread(558) is None
        assert 558 not in cog._active_legs

    asyncio.run(run())


def test_start_tracking_does_not_claim_nothing_was_left_behind_when_thread_cleanup_fails(tmp_path):
    """Audit finding #2 (2026-09-15 executive audit): the rollback path's own
    thread.delete() can itself fail (the thread is still there), but the old code sent
    the reassuring "nothing was left behind to get stuck" message unconditionally - a
    user acting on that message has no idea a real orphaned thread exists."""
    async def run():
        db = _make_db(tmp_path)
        await db.init()
        cog = RouteProgression.__new__(RouteProgression)
        cog.bot = type("FakeBot", (), {"db": db})()
        cog._active_legs = {}
        thread = _FakeTrackingThread(560)
        thread.add_user = AsyncMock(side_effect=discord.HTTPException(NS(status=500, reason="x"), "x"))
        thread.delete = AsyncMock(side_effect=discord.HTTPException(NS(status=500, reason="x"), "cleanup failed"))
        channel = _FakeTextChannel()
        channel.create_thread = AsyncMock(return_value=thread)
        interaction = _FakeStartTrackingInteraction(channel=channel)

        await cog.start_tracking(interaction, _trackable_route())  # must not raise

        message = interaction.followup.send.call_args.args[0]
        assert "nothing was left behind" not in message, (
            "the cleanup itself failed - the message must not claim it didn't"
        )

    asyncio.run(run())


def test_start_tracking_does_not_claim_nothing_was_left_behind_when_db_cleanup_fails(tmp_path, monkeypatch):
    """Same gap as above, for the DB-row side of cleanup: delete_route_progression_thread
    failing (already logged via logger.exception) still let the unconditional "nothing
    was left behind" message go out even though the in_progress row is still there."""
    async def run():
        db = _make_db(tmp_path)
        await db.init()
        cog = RouteProgression.__new__(RouteProgression)
        cog.bot = type("FakeBot", (), {"db": db})()
        cog._active_legs = {}
        thread = _FakeTrackingThread(561)
        thread.send = AsyncMock(side_effect=RuntimeError("network hiccup"))
        channel = _FakeTextChannel()
        channel.create_thread = AsyncMock(return_value=thread)
        interaction = _FakeStartTrackingInteraction(channel=channel)
        monkeypatch.setattr(
            db, "delete_route_progression_thread", AsyncMock(side_effect=RuntimeError("db lock"))
        )

        await cog.start_tracking(interaction, _trackable_route())  # must not raise

        message = interaction.followup.send.call_args.args[0]
        assert "nothing was left behind" not in message, (
            "the DB cleanup itself failed - the message must not claim it didn't"
        )

    asyncio.run(run())


def test_start_tracking_survives_a_non_http_exception_during_thread_cleanup(tmp_path):
    """Audit finding #2: the old code only caught discord.HTTPException around
    thread.delete() - a non-HTTPException (a raw transport error, a bug in a
    discord.py internal) escaped the rollback's own except block entirely, which this
    bot has no global app-command error handler to catch, leaving the interaction stuck
    on "thinking..." forever."""
    async def run():
        db = _make_db(tmp_path)
        await db.init()
        cog = RouteProgression.__new__(RouteProgression)
        cog.bot = type("FakeBot", (), {"db": db})()
        cog._active_legs = {}
        thread = _FakeTrackingThread(562)
        thread.add_user = AsyncMock(side_effect=discord.HTTPException(NS(status=500, reason="x"), "x"))
        thread.delete = AsyncMock(side_effect=RuntimeError("connection reset"))
        channel = _FakeTextChannel()
        channel.create_thread = AsyncMock(return_value=thread)
        interaction = _FakeStartTrackingInteraction(channel=channel)

        await cog.start_tracking(interaction, _trackable_route())  # must not raise

        interaction.followup.send.assert_awaited_once()

    asyncio.run(run())


def test_start_tracking_happy_path_leaves_nothing_orphaned(tmp_path):
    async def run():
        db = _make_db(tmp_path)
        await db.init()
        cog = RouteProgression.__new__(RouteProgression)
        cog.bot = type("FakeBot", (), {"db": db})()
        cog._active_legs = {}
        thread = _FakeTrackingThread(559)
        channel = _FakeTextChannel()
        channel.create_thread = AsyncMock(return_value=thread)
        interaction = _FakeStartTrackingInteraction(channel=channel)

        await cog.start_tracking(interaction, _trackable_route())

        thread.delete.assert_not_awaited()
        thread_row = await db.get_route_progression_thread(559)
        assert thread_row is not None and thread_row["status"] == "in_progress"
        assert 559 in cog._active_legs
        message = interaction.followup.send.call_args.args[0]
        assert "Started tracking" in message

    asyncio.run(run())


# -- Post-ack durable retry (_record_leg_outcome_durably / _abandon_thread_durably) -------
# Real defect: a button/modal's claim() + Discord acknowledgement already happened by the
# time handle_leg_outcome/abandon_thread runs - the leg looks "reported" and its buttons
# are already disabled, so a failure at that point could not fall back on "let them click
# again." Both wrappers retry the whole (idempotent) call a bounded number of times before
# giving up, and must never let an exception escape a button/modal callback either way.

class _FakeThreadChannel(discord.Thread):
    """A real discord.Thread subclass (not a duck-typed stand-in) so isinstance(channel,
    discord.Thread) - which handle_leg_outcome/abandon_thread's own real code checks -
    still passes, with send() overridden to avoid discord.Thread's own read-only slots."""
    def __init__(self):
        self.send = AsyncMock()


def _fake_thread_channel() -> "_FakeThreadChannel":
    return _FakeThreadChannel()


def test_record_leg_outcome_durably_retries_a_transient_failure_and_succeeds(monkeypatch):
    from bot.cogs.route_progression import RouteProgression

    async def run():
        cog = RouteProgression.__new__(RouteProgression)
        monkeypatch.setattr("bot.cogs.route_progression.POST_ACK_RETRY_DELAY_SECONDS", 0)
        call_count = {"n": 0}

        async def flaky_handle(*args, **kwargs):
            call_count["n"] += 1
            if call_count["n"] < 3:
                raise RuntimeError("transient DB lock")

        cog.handle_leg_outcome = flaky_handle
        channel = _fake_thread_channel()

        await cog._record_leg_outcome_durably(channel, 1, 0, _leg_input(), outcome="matched")

        assert call_count["n"] == 3, "must keep retrying until it succeeds"
        channel.send.assert_not_awaited(), "no failure notice once a retry succeeds"

    asyncio.run(run())


def test_record_leg_outcome_durably_gives_up_after_exhausting_retries_without_raising(monkeypatch):
    """The whole point of this wrapper: a permanent failure must still not escape the
    button/modal callback that called it (this bot has no global app-command error
    handler) - it can only log and tell the thread something needs attention."""
    from bot.cogs.route_progression import POST_ACK_RETRY_ATTEMPTS, RouteProgression

    async def run():
        cog = RouteProgression.__new__(RouteProgression)
        monkeypatch.setattr("bot.cogs.route_progression.POST_ACK_RETRY_DELAY_SECONDS", 0)
        call_count = {"n": 0}

        async def always_fails(*args, **kwargs):
            call_count["n"] += 1
            raise RuntimeError("permanent failure")

        cog.handle_leg_outcome = always_fails
        # handle_leg_outcome never even reaches a DB write in this fully-mocked scenario,
        # so the outcome genuinely was never saved - queue_route_progression_leg_recovery
        # also fails here, and get_route_progression_leg (finding #4's already-saved
        # check) must correctly find nothing.
        db = Mock()
        db.queue_route_progression_leg_recovery = AsyncMock(side_effect=RuntimeError("queue also down"))
        db.get_route_progression_leg = AsyncMock(return_value=None)
        cog.bot = type("FakeBot", (), {"db": db})()
        channel = _fake_thread_channel()

        await cog._record_leg_outcome_durably(channel, 1, 0, _leg_input(), outcome="matched")

        assert call_count["n"] == POST_ACK_RETRY_ATTEMPTS
        channel.send.assert_awaited_once()
        assert "went wrong" in channel.send.call_args.args[0]

    asyncio.run(run())


def test_abandon_thread_durably_retries_a_transient_failure_and_succeeds(monkeypatch):
    from bot.cogs.route_progression import RouteProgression

    async def run():
        cog = RouteProgression.__new__(RouteProgression)
        monkeypatch.setattr("bot.cogs.route_progression.POST_ACK_RETRY_DELAY_SECONDS", 0)
        call_count = {"n": 0}

        async def flaky_abandon(*args, **kwargs):
            call_count["n"] += 1
            if call_count["n"] < 2:
                raise RuntimeError("transient DB lock")

        cog.abandon_thread = flaky_abandon
        channel = _fake_thread_channel()

        await cog._abandon_thread_durably(channel, 1, reason="test")

        assert call_count["n"] == 2
        channel.send.assert_not_awaited()

    asyncio.run(run())


def test_abandon_thread_durably_gives_up_after_exhausting_retries_without_raising(monkeypatch):
    from bot.cogs.route_progression import POST_ACK_RETRY_ATTEMPTS, RouteProgression

    async def run():
        cog = RouteProgression.__new__(RouteProgression)
        monkeypatch.setattr("bot.cogs.route_progression.POST_ACK_RETRY_DELAY_SECONDS", 0)
        call_count = {"n": 0}

        async def always_fails(*args, **kwargs):
            call_count["n"] += 1
            raise RuntimeError("permanent failure")

        cog.abandon_thread = always_fails
        channel = _fake_thread_channel()

        await cog._abandon_thread_durably(channel, 1, reason="test")

        assert call_count["n"] == POST_ACK_RETRY_ATTEMPTS
        channel.send.assert_awaited_once()
        assert "went wrong" in channel.send.call_args.args[0]

    asyncio.run(run())


# -- Durable recovery + conflicting-report guard (two confirmed audit findings) ----------
# 1. Exhausted post-ack retries used to leave no durable recovery state - the thread just
#    stayed locked until the 48h abandonment poller. Fixed with a
#    route_progression_pending_actions queue and a short-interval poller that finishes the
#    action later, fully reconstructed from the DB (never RouteProgression._active_legs).
# 2. record_route_progression_leg_outcome's UPDATE was unconditional - two reports for the
#    same leg (a genuinely duplicate live prompt, or a retried handle_leg_outcome) could
#    silently overwrite each other, and a retried handle_leg_outcome could re-post the
#    next leg's prompt a second time. Fixed with an `outcome IS NULL` guard (first report
#    wins, everything after is a no-op) plus claim_route_progression_advance (only the
#    first caller to reach a given leg_index/completion actually sends anything).

def _leg_dict(leg: RouteLegInput) -> dict:
    return {
        "side": leg.side, "id_terminal": leg.id_terminal, "id_commodity": leg.id_commodity,
        "terminal_name": leg.terminal_name, "commodity_name": leg.commodity_name,
        "display_label": leg.display_label, "quoted_price": leg.quoted_price,
        "quoted_scu": leg.quoted_scu, "quoted_status": leg.quoted_status, "market_scu": leg.market_scu,
    }


async def _create_thread_for_legs(db: Database, thread_id: int, legs: list[RouteLegInput], **filters) -> None:
    await db.create_route_progression_thread(
        thread_id=thread_id, user_id=1, guild_id=1, route_kind="best_route",
        route_snapshot={"title": "test route", "legs": [_leg_dict(leg) for leg in legs], **filters},
        legs=[_leg_dict(leg) for leg in legs],
    )


def test_record_route_progression_leg_outcome_a_second_write_is_a_no_op(tmp_path):
    async def run():
        db = _make_db(tmp_path)
        await db.init()
        await db.create_route_progression_thread(
            thread_id=1, user_id=1, guild_id=1, route_kind="best_route", route_snapshot={},
            legs=[{"side": "buy", "id_terminal": 10, "id_commodity": 1}],
        )

        first = await db.record_route_progression_leg_outcome(thread_id=1, leg_index=0, outcome="matched")
        second = await db.record_route_progression_leg_outcome(
            thread_id=1, leg_index=0, outcome="missing"
        )

        assert first is True
        assert second is False, "a second write for an already-reported leg must be rejected"
        stored = await db.get_route_progression_leg(1, 0)
        assert stored["outcome"] == "matched", "the first (winning) report must not be overwritten"

    asyncio.run(run())


def test_claim_route_progression_advance_only_the_first_caller_per_index_wins(tmp_path):
    async def run():
        db = _make_db(tmp_path)
        await db.init()
        await db.create_route_progression_thread(
            thread_id=1, user_id=1, guild_id=1, route_kind="best_route", route_snapshot={},
            legs=[{"side": "buy", "id_terminal": 10, "id_commodity": 1}],
        )

        assert await db.claim_route_progression_advance(1, to_index=0) is True
        assert await db.claim_route_progression_advance(1, to_index=0) is False, "same index twice must lose"
        assert await db.claim_route_progression_advance(1, to_index=1) is True
        assert await db.claim_route_progression_advance(1, to_index=1) is False
        assert await db.claim_route_progression_advance(1, to_index=0) is False, "must never go backwards"

    asyncio.run(run())


def test_set_route_progression_thread_status_does_not_overwrite_a_non_in_progress_thread(tmp_path):
    """Audit-confirmed defect #3: route outcomes/completion don't atomically require
    status='in_progress'. Concrete race: a user abandons a route right as the final leg's
    completion write is in flight - the unconditional UPDATE let 'completed' silently
    stomp back over 'abandoned' (or vice versa, for two racing writes in either order)."""
    async def run():
        db = _make_db(tmp_path)
        await db.init()
        await db.create_route_progression_thread(
            thread_id=1, user_id=1, guild_id=1, route_kind="best_route", route_snapshot={},
            legs=[{"side": "buy", "id_terminal": 10, "id_commodity": 1}],
        )
        assert await db.set_route_progression_thread_status(1, "abandoned") is True

        result = await db.set_route_progression_thread_status(1, "completed")

        assert result is False, "a thread that already left in_progress must refuse a further status write"
        thread_row = await db.get_route_progression_thread(1)
        assert thread_row["status"] == "abandoned", "the winning status must not be overwritten"

    asyncio.run(run())


def test_claim_route_progression_advance_refuses_once_the_thread_is_no_longer_in_progress(tmp_path):
    """Same defect, for the advance-claim used to guard posting a leg prompt or the
    completion message - without this, a leg-outcome report racing an abandonment could
    still post a next-leg prompt (or a completion message) into an already-abandoned
    thread."""
    async def run():
        db = _make_db(tmp_path)
        await db.init()
        await db.create_route_progression_thread(
            thread_id=1, user_id=1, guild_id=1, route_kind="best_route", route_snapshot={},
            legs=[{"side": "buy", "id_terminal": 10, "id_commodity": 1}],
        )
        assert await db.set_route_progression_thread_status(1, "abandoned") is True

        assert await db.claim_route_progression_advance(1, to_index=0) is False, (
            "an abandoned thread must never let a new leg prompt or completion be claimed"
        )

    asyncio.run(run())


def test_record_route_progression_leg_outcome_refuses_once_the_thread_is_no_longer_in_progress(tmp_path):
    """Same defect, for the outcome write itself - without this, a leg-outcome report
    racing an abandonment could still record an outcome (and drive its market-state side
    effects) for a route that's no longer being tracked."""
    async def run():
        db = _make_db(tmp_path)
        await db.init()
        await db.create_route_progression_thread(
            thread_id=1, user_id=1, guild_id=1, route_kind="best_route", route_snapshot={},
            legs=[{"side": "buy", "id_terminal": 10, "id_commodity": 1}],
        )
        assert await db.set_route_progression_thread_status(1, "abandoned") is True

        recorded = await db.record_route_progression_leg_outcome(thread_id=1, leg_index=0, outcome="matched")

        assert recorded is False, "an abandoned thread's leg outcome must never be recorded"
        stored = await db.get_route_progression_leg(1, 0)
        assert stored["outcome"] is None, "no outcome should have been written"

    asyncio.run(run())


def test_pending_route_progression_actions_queue_round_trips(tmp_path):
    async def run():
        db = _make_db(tmp_path)
        await db.init()
        leg = _leg_input(id_terminal=10, id_commodity=1)
        await db.queue_route_progression_leg_recovery(
            thread_id=1, leg_index=0, side=leg.side, id_terminal=leg.id_terminal,
            id_commodity=leg.id_commodity, terminal_name=leg.terminal_name,
            commodity_name=leg.commodity_name, display_label=leg.display_label,
            quoted_price=leg.quoted_price, quoted_scu=leg.quoted_scu, quoted_status=leg.quoted_status,
            market_scu=leg.market_scu, outcome="missing", actual_price=None, actual_scu=None, precision=None,
        )
        await db.queue_route_progression_abandon_recovery(thread_id=2, reason="test")

        pending = await db.get_pending_route_progression_actions()
        assert len(pending) == 2
        leg_action = next(p for p in pending if p["action_kind"] == "leg_outcome")
        abandon_action = next(p for p in pending if p["action_kind"] == "abandon")
        assert leg_action["thread_id"] == 1 and leg_action["outcome"] == "missing"
        assert abandon_action["thread_id"] == 2 and abandon_action["reason"] == "test"
        assert leg_action["attempts"] == 0

        await db.mark_route_progression_pending_action_attempted(leg_action["id"])
        reloaded = await db.get_pending_route_progression_actions()
        assert next(p for p in reloaded if p["id"] == leg_action["id"])["attempts"] == 1

        await db.delete_route_progression_pending_action(leg_action["id"])
        remaining = await db.get_pending_route_progression_actions()
        assert [p["id"] for p in remaining] == [abandon_action["id"]]

    asyncio.run(run())


def test_queue_route_progression_leg_recovery_ignores_a_duplicate_for_the_same_leg(tmp_path):
    """Defense-in-depth, not a proven-reachable bug (see idx_route_progression_pending_
    actions_leg_unique's own schema comment) - proves the partial unique index + INSERT
    OR IGNORE actually work together: a second call for the identical (thread_id,
    leg_index) must not raise and must not create a second row."""
    async def run():
        db = _make_db(tmp_path)
        await db.init()
        leg = _leg_input(id_terminal=10, id_commodity=1)
        kwargs = dict(
            thread_id=1, leg_index=0, side=leg.side, id_terminal=leg.id_terminal,
            id_commodity=leg.id_commodity, terminal_name=leg.terminal_name,
            commodity_name=leg.commodity_name, display_label=leg.display_label,
            quoted_price=leg.quoted_price, quoted_scu=leg.quoted_scu, quoted_status=leg.quoted_status,
            market_scu=leg.market_scu, outcome="missing", actual_price=None, actual_scu=None, precision=None,
        )
        await db.queue_route_progression_leg_recovery(**kwargs)
        await db.queue_route_progression_leg_recovery(**kwargs)  # must not raise

        pending = await db.get_pending_route_progression_actions()
        assert len(pending) == 1, "a duplicate queue attempt for the same leg must not create a second row"

    asyncio.run(run())


def test_queue_route_progression_leg_recovery_allows_different_legs_of_the_same_thread(tmp_path):
    """The unique index must key on (thread_id, leg_index), not thread_id alone - two
    different legs of the same route genuinely can each need their own queued recovery."""
    async def run():
        db = _make_db(tmp_path)
        await db.init()
        leg = _leg_input(id_terminal=10, id_commodity=1)
        for leg_index in (0, 1):
            await db.queue_route_progression_leg_recovery(
                thread_id=1, leg_index=leg_index, side=leg.side, id_terminal=leg.id_terminal,
                id_commodity=leg.id_commodity, terminal_name=leg.terminal_name,
                commodity_name=leg.commodity_name, display_label=leg.display_label,
                quoted_price=leg.quoted_price, quoted_scu=leg.quoted_scu, quoted_status=leg.quoted_status,
                market_scu=leg.market_scu, outcome="missing", actual_price=None, actual_scu=None, precision=None,
            )

        pending = await db.get_pending_route_progression_actions()
        assert sorted(p["leg_index"] for p in pending) == [0, 1]

    asyncio.run(run())


def test_queue_route_progression_abandon_recovery_ignores_a_duplicate_for_the_same_thread(tmp_path):
    """Same defense-in-depth proof for the abandon side - a second call for the same
    thread_id must not raise and must not create a second row."""
    async def run():
        db = _make_db(tmp_path)
        await db.init()
        await db.queue_route_progression_abandon_recovery(thread_id=1, reason="first")
        await db.queue_route_progression_abandon_recovery(thread_id=1, reason="second")  # must not raise

        pending = await db.get_pending_route_progression_actions()
        assert len(pending) == 1
        assert pending[0]["reason"] == "first", "the original queued reason must survive untouched"

    asyncio.run(run())


def test_init_reconciles_legacy_duplicate_pending_actions_instead_of_crashing(tmp_path):
    """Audit-confirmed defect #4 (2026-09-15 follow-up audit): the two partial UNIQUE
    indexes above used to be created directly in SCHEMA, which executescript runs BEFORE
    any migration gets a chance to clean up - a database old enough to have queued a
    genuine duplicate before this uniqueness guard ever existed (the old, unguarded
    INSERT this codebase used before it) would crash init() outright with
    sqlite3.IntegrityError. Reproduces that pre-index state directly - drop the indexes,
    insert real duplicates the old code allowed - rather than hand-rolling an entire
    legacy schema just to get there."""
    async def run():
        db = _make_db(tmp_path)
        await db.init()
        async with db.connect() as conn:
            await conn.execute("DROP INDEX idx_route_progression_pending_actions_leg_unique")
            await conn.execute("DROP INDEX idx_route_progression_pending_actions_abandon_unique")
            await conn.execute(
                "INSERT INTO route_progression_pending_actions (thread_id, action_kind, leg_index, outcome) "
                "VALUES (1, 'leg_outcome', 0, 'matched')"
            )
            await conn.execute(
                "INSERT INTO route_progression_pending_actions (thread_id, action_kind, leg_index, outcome) "
                "VALUES (1, 'leg_outcome', 0, 'missing')"
            )
            await conn.execute(
                "INSERT INTO route_progression_pending_actions (thread_id, action_kind, reason) "
                "VALUES (2, 'abandon', 'inactive')"
            )
            await conn.execute(
                "INSERT INTO route_progression_pending_actions (thread_id, action_kind, reason) "
                "VALUES (2, 'abandon', 'inactive (retry)')"
            )
            await conn.commit()

        await db.init()  # must not raise sqlite3.IntegrityError

        pending = await db.get_pending_route_progression_actions()
        leg_outcome_rows = [row for row in pending if row["action_kind"] == "leg_outcome"]
        abandon_rows = [row for row in pending if row["action_kind"] == "abandon"]
        assert len(leg_outcome_rows) == 1, "duplicates must be reconciled down to one row per key"
        assert len(abandon_rows) == 1
        assert leg_outcome_rows[0]["outcome"] == "missing", "keeps the freshest (highest-id) row"
        assert abandon_rows[0]["reason"] == "inactive (retry)"

        # The uniqueness guard must be live again after reconciling, not just skipped.
        with pytest.raises(aiosqlite.IntegrityError):
            async with db.connect() as conn:
                await conn.execute(
                    "INSERT INTO route_progression_pending_actions (thread_id, action_kind, leg_index) "
                    "VALUES (1, 'leg_outcome', 0)"
                )
                await conn.commit()

    asyncio.run(run())


def test_init_reconciliation_prefers_the_duplicate_matching_an_already_saved_outcome(tmp_path):
    """Follow-up audit finding #5 (2026-09-15): a blind highest-id pick can discard the
    one duplicate that actually MATCHES an outcome already committed to
    route_progression_legs, in favor of a conflicting one the recovery poller's own
    guard will just reject as "already reported" - permanently losing the only payload
    able to resume that leg's unfinished downstream work (handle_leg_outcome's own
    is_same_report fall-through is exactly what a matching replay uses to finish the
    next-leg dispatch/completion step that never happened). Reproduces the audit's own
    scenario directly: a 'matched' outcome is already saved (without ever advancing),
    and the legacy duplicate queue holds ('matched', then a HIGHER-id 'missing') - the
    old highest-id rule would keep 'missing' and delete the one payload that could
    actually resume the route."""
    async def run():
        db = _make_db(tmp_path)
        await db.init()
        await _create_thread_for_legs(db, 1, [_leg_input()])
        recorded = await db.record_route_progression_leg_outcome(thread_id=1, leg_index=0, outcome="matched")
        assert recorded is True, "the outcome must actually be committed for this scenario to be meaningful"

        async with db.connect() as conn:
            await conn.execute("DROP INDEX idx_route_progression_pending_actions_leg_unique")
            await conn.execute("DROP INDEX idx_route_progression_pending_actions_abandon_unique")
            await conn.execute(
                "INSERT INTO route_progression_pending_actions (thread_id, action_kind, leg_index, outcome) "
                "VALUES (1, 'leg_outcome', 0, 'matched')"
            )
            # Inserted SECOND, so it has the higher id - a blind highest-id pick would
            # wrongly prefer this conflicting row over the one matching what's saved.
            await conn.execute(
                "INSERT INTO route_progression_pending_actions (thread_id, action_kind, leg_index, outcome) "
                "VALUES (1, 'leg_outcome', 0, 'missing')"
            )
            await conn.commit()

        await db.init()

        pending = await db.get_pending_route_progression_actions()
        leg_outcome_rows = [row for row in pending if row["action_kind"] == "leg_outcome"]
        assert len(leg_outcome_rows) == 1
        assert leg_outcome_rows[0]["outcome"] == "matched", (
            "must keep the duplicate matching the already-saved outcome, not the higher-id "
            "one that will just be rejected as conflicting - discarding it would leave "
            "nothing able to resume this leg's unfinished downstream work"
        )

    asyncio.run(run())


def test_init_reconciliation_falls_back_to_highest_id_when_nothing_is_saved_yet(tmp_path):
    """Control case, matching the pre-existing (still correct) behavior: when nothing has
    been committed to route_progression_legs yet, there's nothing to reconcile duplicates
    against - any one of them will become the first real commit once replayed, so the
    highest-id (freshest attempt) fallback is still the right, harmless default."""
    async def run():
        db = _make_db(tmp_path)
        await db.init()
        async with db.connect() as conn:
            await conn.execute("DROP INDEX idx_route_progression_pending_actions_leg_unique")
            await conn.execute("DROP INDEX idx_route_progression_pending_actions_abandon_unique")
            await conn.execute(
                "INSERT INTO route_progression_pending_actions (thread_id, action_kind, leg_index, outcome) "
                "VALUES (1, 'leg_outcome', 0, 'matched')"
            )
            await conn.execute(
                "INSERT INTO route_progression_pending_actions (thread_id, action_kind, leg_index, outcome) "
                "VALUES (1, 'leg_outcome', 0, 'missing')"
            )
            await conn.commit()

        await db.init()

        pending = await db.get_pending_route_progression_actions()
        leg_outcome_rows = [row for row in pending if row["action_kind"] == "leg_outcome"]
        assert len(leg_outcome_rows) == 1
        assert leg_outcome_rows[0]["outcome"] == "missing", "keeps the freshest (highest-id) row"

    asyncio.run(run())


def test_init_is_a_no_op_on_an_already_deduplicated_database(tmp_path):
    """Repeated initialization (every real bot restart) must stay a cheap no-op once no
    duplicates remain - proven separately from the reconciliation test above, which only
    covers the first, one-time cleanup."""
    async def run():
        db = _make_db(tmp_path)
        await db.init()
        await db.queue_route_progression_abandon_recovery(thread_id=1, reason="first")

        await db.init()
        await db.init()

        pending = await db.get_pending_route_progression_actions()
        assert len(pending) == 1
        assert pending[0]["reason"] == "first"

    asyncio.run(run())


def test_handle_leg_outcome_a_conflicting_second_report_does_not_overwrite_or_repost(tmp_path):
    """The exact scenario the audit named: two live prompts for the same leg (a duplicate
    from a retried handle_leg_outcome) report DIFFERENT outcomes. The second must be
    rejected outright - not recorded, not allowed to re-suppress/corrupt shared market
    state, and must not re-post the next leg's prompt a second time."""
    async def run():
        db = _make_db(tmp_path)
        await db.init()
        await _seed_market_row(db)
        leg0 = _leg_input(id_terminal=10, id_commodity=1, quoted_price=100.0, quoted_scu=50.0, quoted_status=3)
        leg1 = _leg_input(
            side="sell", id_terminal=20, id_commodity=1, terminal_name="Elsewhere",
            display_label="Sell Gold at Elsewhere", quoted_price=90.0, quoted_scu=40.0, quoted_status=2,
        )
        await _create_thread_for_legs(db, 1, [leg0, leg1])
        cog = RouteProgression.__new__(RouteProgression)
        cog.bot = type("FakeBot", (), {"db": db})()
        cog._active_legs = {1: [leg0, leg1]}
        channel = _fake_thread_channel()

        await cog.handle_leg_outcome(channel, 1, 0, leg0, outcome="matched")
        assert channel.send.await_count == 1, "leg 2's prompt must be posted exactly once"

        await cog.handle_leg_outcome(channel, 1, 0, leg0, outcome="missing")
        assert channel.send.await_count == 2, "the duplicate gets a rejection notice, not a second leg-2 prompt"
        rejection_text = channel.send.call_args_list[1].args[0]
        assert "already reported" in rejection_text

        stored = await db.get_route_progression_leg(1, 0)
        assert stored["outcome"] == "matched", "the winning report's outcome must survive untouched"
        result = await db.get_suppressed_sides_by_ids([(1, 10)], now=_now())
        assert (1, 10) not in result, (
            "the rejected 'missing' report must never reach terminal_market_state/suppression - "
            "'matched' (the real winner) doesn't suppress anything"
        )

    asyncio.run(run())


def test_handle_leg_outcome_a_buy_side_shortfall_suggests_a_hedge_in_the_thread(tmp_path):
    """The tracking-thread counterpart to find_hedge_cargo's own unit tests: a 'missing'
    report on a buy leg should post a hedge suggestion for a real complementary
    commodity at the SAME origin/destination pair, in addition to (not instead of) the
    next leg's prompt."""
    async def run():
        db = _make_db(tmp_path)
        await db.init()
        await _seed_market_row(db)
        await db.record_terminal_market_snapshot([
            {"id_commodity": 2, "id_terminal": 10, "commodity_name": "Cobalt", "terminal_name": "Area18 TDD",
             "price_buy": 20, "price_sell": 0, "scu_buy": 95, "scu_sell": 0, "status_buy": 1, "status_sell": None},
            {"id_commodity": 2, "id_terminal": 20, "commodity_name": "Cobalt", "terminal_name": "Elsewhere",
             "price_buy": 0, "price_sell": 50, "scu_buy": 0, "scu_sell": 80, "status_buy": None, "status_sell": 1},
        ])
        leg0 = _leg_input(id_terminal=10, id_commodity=1, quoted_price=100.0, quoted_scu=50.0, quoted_status=3)
        leg1 = _leg_input(
            side="sell", id_terminal=20, id_commodity=1, terminal_name="Elsewhere",
            display_label="Sell Gold at Elsewhere", quoted_price=90.0, quoted_scu=40.0, quoted_status=2,
        )
        await _create_thread_for_legs(db, 1, [leg0, leg1])
        cog = RouteProgression.__new__(RouteProgression)
        cog.bot = type("FakeBot", (), {"db": db})()
        cog._active_legs = {1: [leg0, leg1]}
        channel = _fake_thread_channel()

        await cog.handle_leg_outcome(channel, 1, 0, leg0, outcome="missing")

        assert channel.send.await_count == 2, "expected the hedge suggestion plus leg 2's prompt"
        hedge_message = channel.send.call_args_list[0].args[0]
        assert "Cobalt" in hedge_message, hedge_message

    asyncio.run(run())


def test_handle_leg_outcome_no_hedge_available_says_so_and_sends_the_next_leg_prompt(tmp_path):
    async def run():
        db = _make_db(tmp_path)
        await db.init()
        await _seed_market_row(db)
        leg0 = _leg_input(id_terminal=10, id_commodity=1, quoted_price=100.0, quoted_scu=50.0, quoted_status=3)
        leg1 = _leg_input(
            side="sell", id_terminal=20, id_commodity=1, terminal_name="Elsewhere",
            display_label="Sell Gold at Elsewhere", quoted_price=90.0, quoted_scu=40.0, quoted_status=2,
        )
        await _create_thread_for_legs(db, 1, [leg0, leg1])
        cog = RouteProgression.__new__(RouteProgression)
        cog.bot = type("FakeBot", (), {"db": db})()
        cog._active_legs = {1: [leg0, leg1]}
        channel = _fake_thread_channel()

        await cog.handle_leg_outcome(channel, 1, 0, leg0, outcome="missing")

        assert channel.send.await_count == 2, "expected the 'nothing found' message plus the leg-2 prompt"
        no_hedge_message = channel.send.call_args_list[0].args[0]
        assert "nothing else" in no_hedge_message, no_hedge_message

    asyncio.run(run())


def test_handle_leg_outcome_a_sell_side_shortfall_suggests_a_different_destination(tmp_path):
    """The tracking-thread counterpart to find_backup_routes' own other_destination case:
    a 'missing' report on a sell leg means the player is left holding unsold cargo, so
    this should suggest a different terminal that still buys it. Uses a 3-leg thread so
    the tested (sell) leg isn't the LAST one - reporting the final leg would complete and
    archive the thread, which _FakeThreadChannel doesn't support."""
    async def run():
        db = _make_db(tmp_path)
        await db.init()
        await _seed_market_row(db)
        await db.record_terminal_market_snapshot([
            {"id_commodity": 1, "id_terminal": 20, "commodity_name": "Gold", "terminal_name": "Elsewhere",
             "price_buy": 0, "price_sell": 0, "scu_buy": 0, "scu_sell": 0, "status_buy": None, "status_sell": 7},
            {"id_commodity": 1, "id_terminal": 30, "commodity_name": "Gold", "terminal_name": "Port Olisar",
             "price_buy": 0, "price_sell": 150, "scu_buy": 0, "scu_sell": 60, "status_buy": None, "status_sell": 1},
        ])
        leg0 = _leg_input(id_terminal=10, id_commodity=1, quoted_price=100.0, quoted_scu=50.0, quoted_status=3)
        leg1 = _leg_input(
            side="sell", id_terminal=20, id_commodity=1, terminal_name="Elsewhere",
            display_label="Sell Gold at Elsewhere", quoted_price=90.0, quoted_scu=40.0, quoted_status=2,
        )
        leg2 = _leg_input(
            side="buy", id_terminal=30, id_commodity=3, terminal_name="Third Stop",
            commodity_name="Iron", display_label="Buy Iron at Third Stop",
            quoted_price=10.0, quoted_scu=20.0, quoted_status=3,
        )
        await _create_thread_for_legs(db, 1, [leg0, leg1, leg2])
        cog = RouteProgression.__new__(RouteProgression)
        cog.bot = type("FakeBot", (), {"db": db})()
        cog._active_legs = {1: [leg0, leg1, leg2]}
        channel = _fake_thread_channel()

        await cog.handle_leg_outcome(channel, 1, 1, leg1, outcome="missing")

        assert channel.send.await_count == 2, "expected the reroute suggestion plus leg 3's prompt"
        reroute_message = channel.send.call_args_list[0].args[0]
        assert "Port Olisar" in reroute_message, reroute_message

    asyncio.run(run())


def test_handle_leg_outcome_a_sell_side_shortfall_with_no_other_buyer_says_so(tmp_path):
    async def run():
        db = _make_db(tmp_path)
        await db.init()
        await _seed_market_row(db)
        leg0 = _leg_input(id_terminal=10, id_commodity=1, quoted_price=100.0, quoted_scu=50.0, quoted_status=3)
        leg1 = _leg_input(
            side="sell", id_terminal=20, id_commodity=1, terminal_name="Elsewhere",
            display_label="Sell Gold at Elsewhere", quoted_price=90.0, quoted_scu=40.0, quoted_status=2,
        )
        leg2 = _leg_input(
            side="buy", id_terminal=30, id_commodity=3, terminal_name="Third Stop",
            commodity_name="Iron", display_label="Buy Iron at Third Stop",
            quoted_price=10.0, quoted_scu=20.0, quoted_status=3,
        )
        await _create_thread_for_legs(db, 1, [leg0, leg1, leg2])
        cog = RouteProgression.__new__(RouteProgression)
        cog.bot = type("FakeBot", (), {"db": db})()
        cog._active_legs = {1: [leg0, leg1, leg2]}
        channel = _fake_thread_channel()

        await cog.handle_leg_outcome(channel, 1, 1, leg1, outcome="missing")

        assert channel.send.await_count == 2, "expected the 'no better buyer' message plus leg 3's prompt"
        no_reroute_message = channel.send.call_args_list[0].args[0]
        assert "no better" in no_reroute_message, no_reroute_message

    asyncio.run(run())


def test_handle_leg_outcome_sell_side_reroute_offloads_the_search_to_a_worker_thread(tmp_path, monkeypatch):
    """Same regression class as /mixed-routes' and /multi-stop-route's own offload tests
    (test_route_send_shape.py): find_backup_routes always computes without_anchor internally
    (build_mixed_routes over the full market snapshot), which can be expensive enough on
    dense data to matter, and this call would otherwise run synchronously on the bot's one
    asyncio event loop. Checked directly via the actual thread it ran on, not timing, which
    can pass by accident from unrelated awaits earlier in the same handler."""
    async def run():
        db = _make_db(tmp_path)
        await db.init()
        await _seed_market_row(db)
        await db.record_terminal_market_snapshot([
            {"id_commodity": 1, "id_terminal": 20, "commodity_name": "Gold", "terminal_name": "Elsewhere",
             "price_buy": 0, "price_sell": 0, "scu_buy": 0, "scu_sell": 0, "status_buy": None, "status_sell": 7},
            {"id_commodity": 1, "id_terminal": 30, "commodity_name": "Gold", "terminal_name": "Port Olisar",
             "price_buy": 0, "price_sell": 150, "scu_buy": 0, "scu_sell": 60, "status_buy": None, "status_sell": 1},
        ])
        leg0 = _leg_input(id_terminal=10, id_commodity=1, quoted_price=100.0, quoted_scu=50.0, quoted_status=3)
        leg1 = _leg_input(
            side="sell", id_terminal=20, id_commodity=1, terminal_name="Elsewhere",
            display_label="Sell Gold at Elsewhere", quoted_price=90.0, quoted_scu=40.0, quoted_status=2,
        )
        leg2 = _leg_input(
            side="buy", id_terminal=30, id_commodity=3, terminal_name="Third Stop",
            commodity_name="Iron", display_label="Buy Iron at Third Stop",
            quoted_price=10.0, quoted_scu=20.0, quoted_status=3,
        )
        await _create_thread_for_legs(db, 1, [leg0, leg1, leg2])
        cog = RouteProgression.__new__(RouteProgression)
        cog.bot = type("FakeBot", (), {"db": db})()
        cog._active_legs = {1: [leg0, leg1, leg2]}
        channel = _fake_thread_channel()

        called_from_thread = {}
        real_find = route_progression_module.find_backup_routes

        def spy(*args, **kwargs):
            called_from_thread["thread"] = threading.current_thread()
            return real_find(*args, **kwargs)

        monkeypatch.setattr(route_progression_module, "find_backup_routes", spy)

        await cog.handle_leg_outcome(channel, 1, 1, leg1, outcome="missing")

        assert called_from_thread.get("thread") is not None, "find_backup_routes was never called"
        assert called_from_thread["thread"] is not threading.main_thread(), (
            "find_backup_routes ran on the main/event-loop thread - it must be offloaded via "
            "asyncio.to_thread so it can't block the bot's one event loop"
        )

    asyncio.run(run())


def test_handle_leg_outcome_sell_side_reroute_honors_the_originating_routes_filters(tmp_path, monkeypatch):
    """Audit-confirmed defect: _suggest_sell_shortfall_reroute used to call find_backup_routes
    with every filter at its default (off/None), regardless of what the tracked route's own
    search actually required - a space-only or capital-ship-only route's reroute suggestion
    could point at a terminal that violates those constraints. The filters are now carried
    through TrackableRoute -> route_snapshot and read back at reroute time via
    _get_route_filters."""
    async def run():
        db = _make_db(tmp_path)
        await db.init()
        await _seed_market_row(db)
        leg0 = _leg_input(id_terminal=10, id_commodity=1, quoted_price=100.0, quoted_scu=50.0, quoted_status=3)
        leg1 = _leg_input(
            side="sell", id_terminal=20, id_commodity=1, terminal_name="Elsewhere",
            display_label="Sell Gold at Elsewhere", quoted_price=90.0, quoted_scu=40.0, quoted_status=2,
        )
        leg2 = _leg_input(
            side="buy", id_terminal=30, id_commodity=3, terminal_name="Third Stop",
            commodity_name="Iron", display_label="Buy Iron at Third Stop",
            quoted_price=10.0, quoted_scu=20.0, quoted_status=3,
        )
        await _create_thread_for_legs(
            db, 1, [leg0, leg1, leg2],
            space_only=True, capital_access_only=True, auto_load_only=True, system="Stanton",
        )
        cog = RouteProgression.__new__(RouteProgression)
        cog.bot = type("FakeBot", (), {"db": db})()
        cog._active_legs = {1: [leg0, leg1, leg2]}
        channel = _fake_thread_channel()

        captured_kwargs = {}
        real_find = route_progression_module.find_backup_routes

        def spy(*args, **kwargs):
            captured_kwargs.update(kwargs)
            return real_find(*args, **kwargs)

        monkeypatch.setattr(route_progression_module, "find_backup_routes", spy)

        await cog.handle_leg_outcome(channel, 1, 1, leg1, outcome="missing")

        assert captured_kwargs, "find_backup_routes was never called"
        assert captured_kwargs["space_only"] is True
        assert captured_kwargs["capital_access_only"] is True
        assert captured_kwargs["auto_load_only"] is True
        assert captured_kwargs["system"] == "Stanton"

    asyncio.run(run())


def test_handle_leg_outcome_sell_side_reroute_prices_against_the_nearest_preceding_buy(tmp_path, monkeypatch):
    """Audit-confirmed defect: the paired buy leg used to be found with a bare 'first buy row
    anywhere in the thread matching this commodity' lookup, ignoring leg_index - a route that
    revisits the same commodity on a later hop would silently price the shortfall against an
    earlier, different purchase. Two buy legs for the same commodity (different prices) at
    leg_index 0 and 2; the sell-side shortfall at leg_index 3 must pair with the leg 2 buy
    (the nearest preceding one), not the leg 0 buy."""
    async def run():
        db = _make_db(tmp_path)
        await db.init()
        await _seed_market_row(db)
        leg0 = _leg_input(id_terminal=10, id_commodity=1, quoted_price=100.0, quoted_scu=50.0, quoted_status=3)
        leg1 = _leg_input(
            side="sell", id_terminal=20, id_commodity=1, terminal_name="Elsewhere",
            display_label="Sell Gold at Elsewhere", quoted_price=90.0, quoted_scu=40.0, quoted_status=2,
        )
        leg2 = _leg_input(
            id_terminal=30, id_commodity=1, terminal_name="Third Stop",
            display_label="Buy Gold at Third Stop (revisit)", quoted_price=200.0, quoted_scu=30.0, quoted_status=3,
        )
        leg3 = _leg_input(
            side="sell", id_terminal=40, id_commodity=1, terminal_name="Fourth Stop",
            display_label="Sell Gold at Fourth Stop", quoted_price=250.0, quoted_scu=30.0, quoted_status=2,
        )
        leg4 = _leg_input(
            side="buy", id_terminal=50, id_commodity=3, terminal_name="Fifth Stop",
            commodity_name="Iron", display_label="Buy Iron at Fifth Stop",
            quoted_price=10.0, quoted_scu=20.0, quoted_status=3,
        )
        await _create_thread_for_legs(db, 1, [leg0, leg1, leg2, leg3, leg4])
        cog = RouteProgression.__new__(RouteProgression)
        cog.bot = type("FakeBot", (), {"db": db})()
        cog._active_legs = {1: [leg0, leg1, leg2, leg3, leg4]}
        channel = _fake_thread_channel()

        captured_kwargs = {}
        real_find = route_progression_module.find_backup_routes

        def spy(*args, **kwargs):
            captured_kwargs.update(kwargs)
            return real_find(*args, **kwargs)

        monkeypatch.setattr(route_progression_module, "find_backup_routes", spy)

        await cog.handle_leg_outcome(channel, 1, 3, leg3, outcome="missing")

        assert captured_kwargs, "find_backup_routes was never called"
        assert captured_kwargs["anchor_buy_price"] == 200.0, (
            "must price against the nearest preceding buy leg (leg 2, 200.0), not the first "
            "matching buy anywhere in the thread (leg 0, 100.0)"
        )

    asyncio.run(run())


def test_handle_leg_outcome_a_matched_outcome_does_not_suggest_a_hedge(tmp_path):
    async def run():
        db = _make_db(tmp_path)
        await db.init()
        await _seed_market_row(db)
        await db.record_terminal_market_snapshot([
            {"id_commodity": 2, "id_terminal": 10, "commodity_name": "Cobalt", "terminal_name": "Area18 TDD",
             "price_buy": 20, "price_sell": 0, "scu_buy": 95, "scu_sell": 0, "status_buy": 1, "status_sell": None},
            {"id_commodity": 2, "id_terminal": 20, "commodity_name": "Cobalt", "terminal_name": "Elsewhere",
             "price_buy": 0, "price_sell": 50, "scu_buy": 0, "scu_sell": 80, "status_buy": None, "status_sell": 1},
        ])
        leg0 = _leg_input(id_terminal=10, id_commodity=1, quoted_price=100.0, quoted_scu=50.0, quoted_status=3)
        leg1 = _leg_input(
            side="sell", id_terminal=20, id_commodity=1, terminal_name="Elsewhere",
            display_label="Sell Gold at Elsewhere", quoted_price=90.0, quoted_scu=40.0, quoted_status=2,
        )
        await _create_thread_for_legs(db, 1, [leg0, leg1])
        cog = RouteProgression.__new__(RouteProgression)
        cog.bot = type("FakeBot", (), {"db": db})()
        cog._active_legs = {1: [leg0, leg1]}
        channel = _fake_thread_channel()

        await cog.handle_leg_outcome(channel, 1, 0, leg0, outcome="matched")

        assert channel.send.await_count == 1, "expected only leg 2's prompt, no hedge suggestion"

    asyncio.run(run())


# -- Hedge reporting: route_progression_hedges, _record_hedge_report, _post_pending_hedge_sell_prompt --

async def _seed_hedge(db: Database, **overrides) -> int:
    fields = dict(
        thread_id=1, origin_leg_index=0, destination_leg_index=1,
        id_commodity=2, commodity_name="Cobalt",
        id_terminal_origin=10, terminal_name_origin="Area18 TDD",
        id_terminal_destination=20, terminal_name_destination="Elsewhere",
        quoted_price_buy=20.0, quoted_scu=80.0, quoted_price_sell=50.0,
        market_scu_buy=95.0, market_scu_sell=80.0,
        status_buy=1, status_sell=1,
    )
    fields.update(overrides)
    return await db.create_route_progression_hedge(**fields)


def test_record_hedge_report_writes_a_less_outcome_and_marks_the_hedge(tmp_path):
    async def run():
        db = _make_db(tmp_path)
        await db.init()
        hedge_id = await _seed_hedge(db)
        cog = RouteProgression.__new__(RouteProgression)
        cog.bot = type("FakeBot", (), {"db": db})()

        recorded = await cog._record_hedge_report(
            hedge_id=hedge_id, side="buy", actual_price=22.0, actual_scu=50.0
        )

        assert recorded is True
        hedge = await db.get_route_progression_hedge(hedge_id)
        assert hedge["buy_outcome"] == "less"
        assert hedge["buy_actual_scu"] == 50.0
        assert hedge["buy_reported_at"] is not None
        assert hedge["sell_outcome"] is None

        async with db.connect() as conn:
            cursor = await conn.execute(
                "SELECT * FROM terminal_market_state WHERE id_commodity = 2 AND id_terminal = 10"
            )
            state = dict(await cursor.fetchone())
        assert state["source"] == "player_report"

    asyncio.run(run())


def test_record_hedge_report_is_idempotent_per_side(tmp_path):
    async def run():
        db = _make_db(tmp_path)
        await db.init()
        hedge_id = await _seed_hedge(db)
        cog = RouteProgression.__new__(RouteProgression)
        cog.bot = type("FakeBot", (), {"db": db})()

        first = await cog._record_hedge_report(hedge_id=hedge_id, side="buy", actual_price=22.0, actual_scu=50.0)
        second = await cog._record_hedge_report(hedge_id=hedge_id, side="buy", actual_price=99.0, actual_scu=1.0)

        assert first is True
        assert second is False, "a second report for the same side must be rejected"
        hedge = await db.get_route_progression_hedge(hedge_id)
        assert hedge["buy_actual_scu"] == 50.0, "the winning report must not be overwritten"

    asyncio.run(run())


def test_record_hedge_report_a_zero_scu_is_missing(tmp_path):
    async def run():
        db = _make_db(tmp_path)
        await db.init()
        hedge_id = await _seed_hedge(db)
        cog = RouteProgression.__new__(RouteProgression)
        cog.bot = type("FakeBot", (), {"db": db})()

        await cog._record_hedge_report(hedge_id=hedge_id, side="buy", actual_price=None, actual_scu=0.0)

        hedge = await db.get_route_progression_hedge(hedge_id)
        assert hedge["buy_outcome"] == "missing"

    asyncio.run(run())


def test_record_hedge_report_buy_and_sell_are_independent_sides(tmp_path):
    async def run():
        db = _make_db(tmp_path)
        await db.init()
        hedge_id = await _seed_hedge(db)
        cog = RouteProgression.__new__(RouteProgression)
        cog.bot = type("FakeBot", (), {"db": db})()

        await cog._record_hedge_report(hedge_id=hedge_id, side="buy", actual_price=20.0, actual_scu=80.0)
        recorded_sell = await cog._record_hedge_report(hedge_id=hedge_id, side="sell", actual_price=50.0, actual_scu=80.0)

        assert recorded_sell is True
        hedge = await db.get_route_progression_hedge(hedge_id)
        assert hedge["buy_outcome"] == "matched"
        assert hedge["sell_outcome"] == "matched"

        async with db.connect() as conn:
            cursor = await conn.execute(
                "SELECT * FROM terminal_market_state WHERE id_commodity = 2 AND id_terminal = 20"
            )
            state = dict(await cursor.fetchone())
        assert state["price_sell"] == 50.0

    asyncio.run(run())


def test_record_hedge_report_a_confirmed_empty_sell_suppresses_that_side(tmp_path):
    async def run():
        db = _make_db(tmp_path)
        await db.init()
        hedge_id = await _seed_hedge(db)
        await db.record_terminal_market_snapshot([
            {"id_commodity": 2, "id_terminal": 20, "commodity_name": "Cobalt", "terminal_name": "Elsewhere",
             "price_buy": 0, "price_sell": 50, "scu_buy": 0, "scu_sell": 80, "status_buy": None, "status_sell": 1},
        ])
        cog = RouteProgression.__new__(RouteProgression)
        cog.bot = type("FakeBot", (), {"db": db})()

        await cog._record_hedge_report(hedge_id=hedge_id, side="sell", actual_price=None, actual_scu=0.0)

        result = await db.get_suppressed_sides_by_ids([(2, 20)], now=_now())
        assert result == {(2, 20): {"buy": False, "sell": True}}

    asyncio.run(run())


def test_record_hedge_report_a_positive_sell_does_not_suppress(tmp_path):
    async def run():
        db = _make_db(tmp_path)
        await db.init()
        hedge_id = await _seed_hedge(db)
        await db.record_terminal_market_snapshot([
            {"id_commodity": 2, "id_terminal": 20, "commodity_name": "Cobalt", "terminal_name": "Elsewhere",
             "price_buy": 0, "price_sell": 50, "scu_buy": 0, "scu_sell": 80, "status_buy": None, "status_sell": 1},
        ])
        cog = RouteProgression.__new__(RouteProgression)
        cog.bot = type("FakeBot", (), {"db": db})()

        await cog._record_hedge_report(hedge_id=hedge_id, side="sell", actual_price=50.0, actual_scu=80.0)

        result = await db.get_suppressed_sides_by_ids([(2, 20)], now=_now())
        assert (2, 20) not in result

    asyncio.run(run())


def test_suppress_hedge_market_side_never_marks_the_anchor_legs_own_suppression_marker(tmp_path):
    """The exact bug suppress_hedge_market_side's own docstring warns against: reusing
    suppress_terminal_market_side with the anchor route's destination_leg_index would
    incorrectly mark THAT leg's suppression_applied_at for a suppression that's really
    about the hedge's own commodity/terminal, not the anchor's."""
    async def run():
        db = _make_db(tmp_path)
        await db.init()
        leg0 = _leg_input(id_terminal=10, id_commodity=1, quoted_price=100.0, quoted_scu=50.0, quoted_status=3)
        leg1 = _leg_input(
            side="sell", id_terminal=20, id_commodity=1, terminal_name="Elsewhere",
            display_label="Sell Gold at Elsewhere", quoted_price=90.0, quoted_scu=40.0, quoted_status=2,
        )
        await _create_thread_for_legs(db, 1, [leg0, leg1])
        hedge_id = await _seed_hedge(db, destination_leg_index=1)
        await db.record_terminal_market_snapshot([
            {"id_commodity": 2, "id_terminal": 20, "commodity_name": "Cobalt", "terminal_name": "Elsewhere",
             "price_buy": 0, "price_sell": 50, "scu_buy": 0, "scu_sell": 80, "status_buy": None, "status_sell": 1},
        ])
        cog = RouteProgression.__new__(RouteProgression)
        cog.bot = type("FakeBot", (), {"db": db})()

        await cog._record_hedge_report(hedge_id=hedge_id, side="sell", actual_price=None, actual_scu=0.0)

        anchor_leg = await db.get_route_progression_leg(1, 1)
        assert anchor_leg["suppression_applied_at"] is None, (
            "the hedge's suppression must never mark the anchor's OWN leg row"
        )

    asyncio.run(run())


def test_get_pending_hedge_sell_for_leg_requires_a_confirmed_buy_side(tmp_path):
    async def run():
        db = _make_db(tmp_path)
        await db.init()
        hedge_id = await _seed_hedge(db)

        assert await db.get_pending_hedge_sell_for_leg(1, 1) is None, "buy side not yet confirmed"

        await db.record_hedge_side_outcome(hedge_id, "buy", outcome="matched", actual_price=20.0, actual_scu=80.0)
        pending = await db.get_pending_hedge_sell_for_leg(1, 1)
        assert pending is not None and pending["id"] == hedge_id

        await db.record_hedge_side_outcome(hedge_id, "sell", outcome="matched", actual_price=50.0, actual_scu=80.0)
        assert await db.get_pending_hedge_sell_for_leg(1, 1) is None, "sell side already confirmed"

    asyncio.run(run())


def test_post_pending_hedge_sell_prompt_posts_a_companion_message(tmp_path):
    async def run():
        db = _make_db(tmp_path)
        await db.init()
        hedge_id = await _seed_hedge(db)
        await db.record_hedge_side_outcome(hedge_id, "buy", outcome="matched", actual_price=20.0, actual_scu=80.0)
        cog = RouteProgression.__new__(RouteProgression)
        cog.bot = type("FakeBot", (), {"db": db})()
        channel = _fake_thread_channel()

        await cog._post_pending_hedge_sell_prompt(channel, 1, 1)

        assert channel.send.await_count == 1
        message_text = channel.send.call_args_list[0].args[0]
        assert "Cobalt" in message_text
        view = channel.send.call_args_list[0].kwargs["view"]
        assert view.side == "sell"
        assert view.hedge_id == hedge_id

    asyncio.run(run())


def test_post_pending_hedge_sell_prompt_silent_without_a_confirmed_buy(tmp_path):
    async def run():
        db = _make_db(tmp_path)
        await db.init()
        await _seed_hedge(db)
        cog = RouteProgression.__new__(RouteProgression)
        cog.bot = type("FakeBot", (), {"db": db})()
        channel = _fake_thread_channel()

        await cog._post_pending_hedge_sell_prompt(channel, 1, 1)

        assert channel.send.await_count == 0

    asyncio.run(run())


def _fake_message():
    return NS(edit=AsyncMock())


def test_record_hedge_report_a_failed_market_write_leaves_the_side_retryable(tmp_path):
    """One transient failure in the market write (a database lock, say) must not cost the player
    their report. The side is only marked reported AFTER that write lands, so a retry succeeds;
    marking it first meant every retry was told 'already reported' with nothing ever recorded."""
    async def run():
        db = _make_db(tmp_path)
        await db.init()
        hedge_id = await _seed_hedge(db)
        cog = RouteProgression.__new__(RouteProgression)
        cog.bot = type("FakeBot", (), {"db": db})()

        real_write = db.record_hedge_report_market_update
        calls = {"n": 0}

        async def flaky(row):
            calls["n"] += 1
            if calls["n"] == 1:
                raise sqlite3.OperationalError("database is locked")
            return await real_write(row)

        db.record_hedge_report_market_update = flaky

        with pytest.raises(sqlite3.OperationalError):
            await cog._record_hedge_report(hedge_id=hedge_id, side="buy", actual_price=22.0, actual_scu=50.0)
        assert (await db.get_route_progression_hedge(hedge_id))["buy_outcome"] is None, "must stay reportable"

        retried = await cog._record_hedge_report(hedge_id=hedge_id, side="buy", actual_price=22.0, actual_scu=50.0)
        assert retried is True
        hedge = await db.get_route_progression_hedge(hedge_id)
        assert hedge["buy_outcome"] == "less" and hedge["buy_actual_scu"] == 50.0
        async with db.connect() as conn:
            cursor = await conn.execute(
                "SELECT * FROM terminal_market_state WHERE id_commodity = 2 AND id_terminal = 10"
            )
            state = dict(await cursor.fetchone())
        assert state["source"] == "player_report"

    asyncio.run(run())


def test_record_hedge_report_a_repeat_report_never_rewrites_market_data(tmp_path):
    """The early 'already reported' check must come BEFORE any market write - otherwise a second
    submit with different numbers would overwrite shared market state and only then be told no."""
    async def run():
        db = _make_db(tmp_path)
        await db.init()
        hedge_id = await _seed_hedge(db)
        cog = RouteProgression.__new__(RouteProgression)
        cog.bot = type("FakeBot", (), {"db": db})()

        await cog._record_hedge_report(hedge_id=hedge_id, side="buy", actual_price=22.0, actual_scu=50.0)
        writes = []
        real_write = db.record_hedge_report_market_update

        async def spy(row):
            writes.append(row)
            return await real_write(row)

        db.record_hedge_report_market_update = spy
        second = await cog._record_hedge_report(hedge_id=hedge_id, side="buy", actual_price=99.0, actual_scu=1.0)

        assert second is False
        assert writes == [], "a rejected repeat report must not touch market state"

    asyncio.run(run())


def test_the_hedge_suggestion_view_is_given_its_message_so_the_button_can_be_disabled(tmp_path):
    async def run():
        db = _make_db(tmp_path)
        await db.init()
        await _seed_market_row(db)
        await db.record_terminal_market_snapshot([
            {"id_commodity": 2, "id_terminal": 10, "commodity_name": "Cobalt", "terminal_name": "Area18 TDD",
             "price_buy": 20, "price_sell": 0, "scu_buy": 95, "scu_sell": 0, "status_buy": 1, "status_sell": None},
            {"id_commodity": 2, "id_terminal": 20, "commodity_name": "Cobalt", "terminal_name": "Elsewhere",
             "price_buy": 0, "price_sell": 50, "scu_buy": 0, "scu_sell": 80, "status_buy": None, "status_sell": 1},
        ])
        leg0 = _leg_input(id_terminal=10, id_commodity=1, quoted_price=100.0, quoted_scu=50.0, quoted_status=3)
        leg1 = _leg_input(
            side="sell", id_terminal=20, id_commodity=1, terminal_name="Elsewhere",
            display_label="Sell Gold at Elsewhere", quoted_price=90.0, quoted_scu=40.0, quoted_status=2,
        )
        await _create_thread_for_legs(db, 1, [leg0, leg1])
        cog = RouteProgression.__new__(RouteProgression)
        cog.bot = type("FakeBot", (), {"db": db})()
        cog._active_legs = {1: [leg0, leg1]}
        channel = _fake_thread_channel()
        hedge_message, prompt_message = _fake_message(), _fake_message()
        channel.send.side_effect = [hedge_message, prompt_message]

        await cog.handle_leg_outcome(channel, 1, 0, leg0, outcome="missing")

        hedge_view = channel.send.call_args_list[0].kwargs["view"]
        assert isinstance(hedge_view, HedgeReportView)
        assert hedge_view.message is hedge_message, "without its message the view can never disable its button"
        await hedge_view.disable_in_background()
        hedge_message.edit.assert_awaited_once()
        assert all(item.disabled for item in hedge_view.children)

    asyncio.run(run())


def test_the_pending_hedge_sell_prompt_view_is_given_its_message(tmp_path):
    async def run():
        db = _make_db(tmp_path)
        await db.init()
        hedge_id = await _seed_hedge(db)
        await db.record_hedge_side_outcome(hedge_id, "buy", outcome="matched", actual_price=20.0, actual_scu=80.0)
        cog = RouteProgression.__new__(RouteProgression)
        cog.bot = type("FakeBot", (), {"db": db})()
        channel = _fake_thread_channel()
        message = _fake_message()
        channel.send.return_value = message

        await cog._post_pending_hedge_sell_prompt(channel, 1, 1)

        assert channel.send.call_args_list[0].kwargs["view"].message is message

    asyncio.run(run())


def test_hedge_report_view_disable_swallows_any_exception_not_just_http():
    """Matches LegOutcomeView's own audited behaviour: the report is already recorded by the time
    this cosmetic edit runs, so nothing non-HTTP going wrong in it may escape."""
    async def run():
        view = HedgeReportView(cog=None, hedge_id=1, side="buy")
        view.message = NS(edit=AsyncMock(side_effect=RuntimeError("boom")))
        await view.disable_in_background()  # must not raise
        assert all(item.disabled for item in view.children)

    asyncio.run(run())


def _hedge_modal(cog, hedge_id, *, scu, price=""):
    view = HedgeReportView(cog=cog, hedge_id=hedge_id, side="buy")
    modal = HedgeReportModal(cog=cog, hedge_id=hedge_id, side="buy", view=view)
    modal.scu_input._value = scu
    modal.price_input._value = price
    interaction = NS(response=NS(send_message=AsyncMock()))
    return modal, interaction


def test_hedge_report_modal_rejects_input_that_is_not_a_safe_number_and_writes_nothing(tmp_path):
    """Player-typed numbers go straight into shared market state, so float() parses that are valid
    but unsafe (inf, nan, negatives) must be rejected here, the same way ActualAmountModal does."""
    async def run():
        db = _make_db(tmp_path)
        await db.init()
        hedge_id = await _seed_hedge(db)
        cog = RouteProgression.__new__(RouteProgression)
        cog.bot = type("FakeBot", (), {"db": db})()

        for scu, price in (("abc", ""), ("inf", ""), ("nan", ""), ("-5", ""), ("10", "abc"), ("10", "-1"), ("10", "inf")):
            modal, interaction = _hedge_modal(cog, hedge_id, scu=scu, price=price)
            await modal.on_submit(interaction)
            interaction.response.send_message.assert_awaited_once()
            assert interaction.response.send_message.call_args.kwargs.get("ephemeral") is True, (scu, price)
            assert "try again" in interaction.response.send_message.call_args.args[0], (scu, price)

        assert (await db.get_route_progression_hedge(hedge_id))["buy_outcome"] is None, "nothing may be recorded"

    asyncio.run(run())


def test_hedge_report_modal_records_a_valid_report_and_confirms_it(tmp_path):
    async def run():
        db = _make_db(tmp_path)
        await db.init()
        hedge_id = await _seed_hedge(db)
        cog = RouteProgression.__new__(RouteProgression)
        cog.bot = type("FakeBot", (), {"db": db})()

        modal, interaction = _hedge_modal(cog, hedge_id, scu="50", price="22")
        await modal.on_submit(interaction)

        assert "thanks" in interaction.response.send_message.call_args.args[0].lower()
        hedge = await db.get_route_progression_hedge(hedge_id)
        assert hedge["buy_outcome"] == "less" and hedge["buy_actual_scu"] == 50.0

        again, interaction2 = _hedge_modal(cog, hedge_id, scu="70")
        await again.on_submit(interaction2)
        assert "already reported" in interaction2.response.send_message.call_args.args[0]

    asyncio.run(run())


def test_handle_leg_outcome_same_report_retry_does_not_clobber_newer_market_data(tmp_path):
    """Carry-forward defect from the 2026-09-13 audit, still reproducible before this fix:
    a same-report replay (exactly what a delivery-failure retry, or the durable recovery
    queue picking the leg back up later, produces) used to reapply ITS OWN report's
    market-state values unconditionally - even long after something else (another leg's
    report, a fresh UEX collector snapshot) had already written something genuinely newer
    for the same (commodity, terminal) pair. Reproduced directly: record the outcome once
    (the real first write), let a newer snapshot land, then replay the IDENTICAL report a
    second time - record_route_progression_leg_outcome's own idempotency means this hits
    the same-report-retried fallthrough, not a fresh write - and confirm the newer data
    survives untouched instead of reverting to the original report's now-stale price."""
    async def run():
        db = _make_db(tmp_path)
        await db.init()
        await _seed_market_row(db)
        leg0 = _leg_input(id_terminal=10, id_commodity=1, quoted_price=100.0, quoted_scu=50.0, quoted_status=3)
        leg1 = _leg_input(
            side="sell", id_terminal=20, id_commodity=1, terminal_name="Elsewhere",
            display_label="Sell Gold at Elsewhere", quoted_price=90.0, quoted_scu=40.0, quoted_status=2,
        )
        # Two legs, not one - a single-leg thread's first handle_leg_outcome call
        # completes the route, which calls channel.edit() to archive it; the fake channel
        # only stubs send(). Reporting only leg 0 here sidesteps that entirely and matches
        # the conflicting-report test's own established pattern above.
        await _create_thread_for_legs(db, 1, [leg0, leg1])
        cog = RouteProgression.__new__(RouteProgression)
        cog.bot = type("FakeBot", (), {"db": db, "user": NS(id=1), "add_view": Mock()})()
        cog._active_legs = {1: [leg0, leg1]}
        channel = _fake_thread_channel()
        # The first handle_leg_outcome call below advances to leg 1 and genuinely sends
        # its prompt (channel.send succeeds) - the replay's own leg-1 dispatch then finds
        # that claim already held and, per finding #3's fix, reconciles against history
        # rather than trusting the claim blindly. Give it a matching message so that
        # reconciliation resolves as "confirmed already sent" instead of raising, which
        # is what a genuinely successful first delivery looks like.
        channel.history = _fake_history([_fake_sent_message(author_id=1, title="Leg 2: Sell Gold at Elsewhere")])

        await cog.handle_leg_outcome(channel, 1, 0, leg0, outcome="matched")

        async def _price_buy() -> float:
            async with db.connect() as conn:
                cursor = await conn.execute(
                    "SELECT price_buy FROM terminal_market_state WHERE id_commodity = 1 AND id_terminal = 10"
                )
                return (await cursor.fetchone())["price_buy"]

        assert await _price_buy() == 100.0, "the original report's own write must land as usual"

        # Something else updates the SAME pair with a genuinely newer price in the
        # meantime - a fresh UEX collector snapshot, or a different leg's own report.
        await db.record_terminal_market_snapshot([dict(
            id_commodity=1, id_terminal=10, commodity_name="Gold", terminal_name="Area18 TDD",
            price_buy=999.0, price_sell=90, scu_buy=50, scu_sell=40, status_buy=3, status_sell=2,
        )])

        # A retry replays the IDENTICAL report.
        await cog.handle_leg_outcome(channel, 1, 0, leg0, outcome="matched")

        assert await _price_buy() == 999.0, (
            "the replay must not clobber the newer price with its own now-stale report"
        )

    asyncio.run(run())


# -- suppression retried independently of the market-update marker (follow-up audit ------
# finding #2, 2026-09-15) --------------------------------------------------------------
# Real gap: market_update_applied_at used to gate BOTH the market-state write and the
# separate suppress_terminal_market_side call - if suppression alone failed after the
# marker had already committed, market_update_already_applied being True on every later
# replay skipped the whole block, suppression included, so a depleted terminal never
# actually got suppressed no matter how many times the report was retried.

def test_handle_leg_outcome_retries_suppression_even_when_the_market_update_already_applied(tmp_path):
    async def run():
        db = _make_db(tmp_path)
        await db.init()
        await _seed_market_row(db)
        leg0 = _leg_input(id_terminal=10, id_commodity=1, quoted_price=100.0, quoted_scu=50.0, quoted_status=3)
        leg1 = _leg_input(
            side="sell", id_terminal=20, id_commodity=1, terminal_name="Elsewhere",
            display_label="Sell Gold at Elsewhere", quoted_price=90.0, quoted_scu=40.0, quoted_status=2,
        )
        await _create_thread_for_legs(db, 1, [leg0, leg1])
        cog = RouteProgression.__new__(RouteProgression)
        cog.bot = type("FakeBot", (), {"db": db, "user": NS(id=1), "add_view": Mock()})()
        cog._active_legs = {1: [leg0, leg1]}
        channel = _fake_thread_channel()

        # Suppression fails on its first attempt only (the market-state write itself
        # succeeds normally on that same first attempt).
        real_suppress = db.suppress_terminal_market_side
        call_count = {"n": 0}

        async def flaky_suppress(*args, **kwargs):
            call_count["n"] += 1
            if call_count["n"] == 1:
                raise RuntimeError("transient failure")
            return await real_suppress(*args, **kwargs)
        db.suppress_terminal_market_side = flaky_suppress

        async def _suppressed_until():
            async with db.connect() as conn:
                cursor = await conn.execute(
                    "SELECT buy_suppressed_until FROM terminal_market_state "
                    "WHERE id_commodity = 1 AND id_terminal = 10"
                )
                row = await cursor.fetchone()
                return row["buy_suppressed_until"] if row else None

        # A "missing" report confirms depletion (scu=0, empty buy status) and never even
        # reaches leg 1's dispatch - the suppression failure raises first.
        try:
            await cog.handle_leg_outcome(channel, 1, 0, leg0, outcome="missing")
        except RuntimeError:
            pass
        else:
            raise AssertionError("the simulated suppression failure must propagate")

        assert await _suppressed_until() is None, "suppression must not have landed on the failed attempt"
        stored = await db.get_route_progression_leg(1, 0)
        assert stored["market_update_applied_at"] is not None, "the market write itself must have succeeded"
        assert stored["suppression_applied_at"] is None, "the failed suppression must not be marked applied"

        # A replay of the SAME report (is_same_report fall-through) must still retry
        # suppression, even though the market update's own marker is already set.
        await cog.handle_leg_outcome(channel, 1, 0, leg0, outcome="missing")

        assert await _suppressed_until() is not None, (
            "the replay must actually retry suppression, not skip it just because the "
            "market update's own marker was already applied"
        )
        assert call_count["n"] == 2, "suppression must have been attempted exactly twice: once failed, once retried"

    asyncio.run(run())


def test_handle_leg_outcome_ignores_a_report_for_a_route_already_abandoned_with_no_prior_outcome(tmp_path):
    """Audit-confirmed defect #3, the other structurally different reason
    record_route_progression_leg_outcome can reject a write: the thread was abandoned by a
    racing action before this leg's outcome was ever recorded by anyone. Must not be
    misreported as 'already reported (likely duplicate)' (a stored outcome of None can
    never equal this call's own outcome string, so treating that as a duplicate-report
    conflict would give a wrong, confusing message) - and must not drive any market-state
    write or next-leg prompt for a route that isn't being tracked anymore."""
    async def run():
        db = _make_db(tmp_path)
        await db.init()
        await _seed_market_row(db)
        leg0 = _leg_input(id_terminal=10, id_commodity=1, quoted_price=100.0, quoted_scu=50.0, quoted_status=3)
        leg1 = _leg_input(
            side="sell", id_terminal=20, id_commodity=1, terminal_name="Elsewhere",
            display_label="Sell Gold at Elsewhere", quoted_price=90.0, quoted_scu=40.0, quoted_status=2,
        )
        await _create_thread_for_legs(db, 1, [leg0, leg1])
        assert await db.set_route_progression_thread_status(1, "abandoned") is True

        cog = RouteProgression.__new__(RouteProgression)
        cog.bot = type("FakeBot", (), {"db": db})()
        cog._active_legs = {1: [leg0, leg1]}
        channel = _fake_thread_channel()

        await cog.handle_leg_outcome(channel, 1, 0, leg0, outcome="matched")

        channel.send.assert_awaited_once()
        message = channel.send.call_args.args[0]
        assert "no longer being tracked" in message
        assert "already reported" not in message, "must not be misreported as a duplicate-report conflict"
        stored = await db.get_route_progression_leg(1, 0)
        assert stored["outcome"] is None, "no outcome should have been recorded for an abandoned route"
        result = await db.get_suppressed_sides_by_ids([(1, 10)], now=_now())
        assert (1, 10) not in result, "market-state must not be touched for a report that lost this race"

    asyncio.run(run())


def test_handle_leg_outcome_completion_stays_silent_if_the_thread_was_abandoned_in_the_race(tmp_path, monkeypatch):
    """Simulates the narrow window between claim_route_progression_advance winning the
    completion claim and set_route_progression_thread_status actually committing - without
    checking its return value, a completion racing a concurrent abandonment would still
    send 'Route complete!' and archive a thread the OTHER action already closed out as
    abandoned, a visible contradiction."""
    async def run():
        db = _make_db(tmp_path)
        await db.init()
        await _seed_market_row(db)
        leg0 = _leg_input(id_terminal=10, id_commodity=1)
        await _create_thread_for_legs(db, 1, [leg0])

        cog = RouteProgression.__new__(RouteProgression)
        cog.bot = type("FakeBot", (), {"db": db})()
        cog._active_legs = {1: [leg0]}
        channel = _fake_thread_channel()

        # Force the exact race outcome: the claim wins (status was still in_progress a
        # moment ago), but the status-set itself loses to a concurrent abandonment.
        monkeypatch.setattr(db, "set_route_progression_thread_status", AsyncMock(return_value=False))

        await cog.handle_leg_outcome(channel, 1, 0, leg0, outcome="matched")

        channel.send.assert_not_awaited()
        thread = await db.get_route_progression_thread(1)
        assert thread["advanced_to_index"] == 1, "the claim itself still committed - only the status write raced"

    asyncio.run(run())


def test_abandon_thread_stays_silent_if_the_route_was_already_completed_in_the_race(tmp_path, monkeypatch):
    """Mirror of the completion-side test above, for AbandonConfirmView's own commit path -
    must not send 'was abandoned' (and archive) a thread that a racing completion already
    closed out as completed."""
    async def run():
        db = _make_db(tmp_path)
        await db.init()
        leg0 = _leg_input(id_terminal=10, id_commodity=1)
        await _create_thread_for_legs(db, 1, [leg0])

        cog = RouteProgression.__new__(RouteProgression)
        cog.bot = type("FakeBot", (), {"db": db})()
        cog._active_legs = {1: [leg0]}
        channel = _fake_thread_channel()

        monkeypatch.setattr(db, "set_route_progression_thread_status", AsyncMock(return_value=False))

        await cog.abandon_thread(channel, 1, reason="test")

        channel.send.assert_not_awaited()
        assert 1 not in cog._active_legs, "the in-memory cache entry must still be cleared either way"

    asyncio.run(run())


def test_handle_leg_outcome_retrying_the_identical_report_still_finishes_advancing(tmp_path):
    """Simulates the other half of the same audit finding: an earlier attempt already
    durably recorded the outcome but crashed before posting the next leg's prompt (e.g. a
    Discord hiccup right after the DB write). A retry with the SAME outcome/values must
    still complete that missing step, not silently bail out just because
    record_route_progression_leg_outcome's write is now a no-op."""
    async def run():
        db = _make_db(tmp_path)
        await db.init()
        await _seed_market_row(db)
        leg0 = _leg_input(id_terminal=10, id_commodity=1, quoted_price=100.0, quoted_scu=50.0, quoted_status=3)
        leg1 = _leg_input(
            side="sell", id_terminal=20, id_commodity=1, terminal_name="Elsewhere",
            display_label="Sell Gold at Elsewhere", quoted_price=90.0, quoted_scu=40.0, quoted_status=2,
        )
        await _create_thread_for_legs(db, 1, [leg0, leg1])
        # Pre-seed exactly what a partially-succeeded earlier attempt would have left
        # behind: the outcome already recorded, but nothing advanced past leg 0 yet.
        assert await db.record_route_progression_leg_outcome(thread_id=1, leg_index=0, outcome="matched") is True

        cog = RouteProgression.__new__(RouteProgression)
        cog.bot = type("FakeBot", (), {"db": db})()
        cog._active_legs = {1: [leg0, leg1]}
        channel = _fake_thread_channel()

        await cog.handle_leg_outcome(channel, 1, 0, leg0, outcome="matched")

        channel.send.assert_awaited_once()
        assert "already reported" not in channel.send.call_args.args
        thread = await db.get_route_progression_thread(1)
        assert thread["advanced_to_index"] == 1, "leg 2's prompt must have been claimed/posted"

    asyncio.run(run())


def test_get_leg_falls_back_to_the_persisted_snapshot_when_active_legs_has_nothing(tmp_path):
    """The restart-safety half of the recovery fix: _get_leg must reconstruct a leg from
    route_snapshot (including market_scu, which lives nowhere else) when _active_legs
    doesn't have the thread - simulating a bot restart wiping that in-memory cache."""
    async def run():
        db = _make_db(tmp_path)
        await db.init()
        leg = _leg_input(
            id_terminal=10, id_commodity=1, quoted_price=100.0, quoted_scu=50.0,
            quoted_status=3, market_scu=77.0,
        )
        await _create_thread_for_legs(db, 1, [leg])
        cog = RouteProgression.__new__(RouteProgression)
        cog.bot = type("FakeBot", (), {"db": db})()
        cog._active_legs = {}  # nothing cached - as if the bot just restarted

        reconstructed = await cog._get_leg(1, 0)

        assert reconstructed is not None
        assert reconstructed.market_scu == 77.0
        assert reconstructed.terminal_name == leg.terminal_name
        assert reconstructed.display_label == leg.display_label

    asyncio.run(run())


def test_recovery_queue_completes_an_exhausted_leg_outcome_with_no_active_legs_cache(tmp_path):
    """End-to-end for finding #1: _record_leg_outcome_durably queues a durable recovery
    action once retries are exhausted, and retry_pending_route_progression_actions later
    finishes it - reconstructed entirely from the DB, with _active_legs left empty the
    whole time to simulate a bot restart between the failure and the retry."""
    async def run():
        db = _make_db(tmp_path)
        await db.init()
        await _seed_market_row(db)
        leg = _leg_input(id_terminal=10, id_commodity=1, quoted_price=100.0, quoted_scu=50.0, quoted_status=3)
        await _create_thread_for_legs(db, 1, [leg])

        cog = RouteProgression.__new__(RouteProgression)
        cog.bot = type("FakeBot", (), {"db": db, "get_channel": lambda self, thread_id: "not-a-thread"})()
        cog._active_legs = {}

        async def always_fails(*args, **kwargs):
            raise RuntimeError("simulated total outage")

        cog.handle_leg_outcome = always_fails
        channel = _fake_thread_channel()
        await cog._record_leg_outcome_durably(channel, 1, 0, leg, outcome="missing")

        pending = await db.get_pending_route_progression_actions()
        assert len(pending) == 1 and pending[0]["action_kind"] == "leg_outcome"
        assert (await db.get_route_progression_leg(1, 0))["outcome"] is None, "not recorded yet"

        del cog.handle_leg_outcome  # restore the real bound method for the poller below
        await cog.retry_pending_route_progression_actions.coro(cog)

        assert await db.get_pending_route_progression_actions() == []
        stored = await db.get_route_progression_leg(1, 0)
        assert stored["outcome"] == "missing"
        result = await db.get_suppressed_sides_by_ids([(1, 10)], now=_now())
        assert result == {(1, 10): {"buy": True, "sell": False}}

    asyncio.run(run())


def test_recovery_queue_completes_an_exhausted_abandon(tmp_path):
    async def run():
        db = _make_db(tmp_path)
        await db.init()
        leg = _leg_input(id_terminal=10, id_commodity=1)
        await _create_thread_for_legs(db, 1, [leg])

        cog = RouteProgression.__new__(RouteProgression)
        cog.bot = type("FakeBot", (), {"db": db, "get_channel": lambda self, thread_id: "not-a-thread"})()
        cog._active_legs = {1: [leg]}

        async def always_fails(*args, **kwargs):
            raise RuntimeError("simulated total outage")

        cog.abandon_thread = always_fails
        channel = _fake_thread_channel()
        await cog._abandon_thread_durably(channel, 1, reason="you asked to stop tracking it")

        pending = await db.get_pending_route_progression_actions()
        assert len(pending) == 1 and pending[0]["action_kind"] == "abandon"

        del cog.abandon_thread
        await cog.retry_pending_route_progression_actions.coro(cog)

        assert await db.get_pending_route_progression_actions() == []
        thread = await db.get_route_progression_thread(1)
        assert thread["status"] == "abandoned"

    asyncio.run(run())


def test_recovery_queue_discards_a_pending_action_for_a_thread_already_resolved(tmp_path):
    """If the thread's fate was already decided some other way (e.g. the user reported
    the leg again successfully through a fresh prompt before the recovery poller got to
    it, completing the route) a stale queued action must be dropped, not replayed against
    an already-finished thread."""
    async def run():
        db = _make_db(tmp_path)
        await db.init()
        leg = _leg_input(id_terminal=10, id_commodity=1)
        await _create_thread_for_legs(db, 1, [leg])
        await db.set_route_progression_thread_status(1, "completed")
        await db.queue_route_progression_abandon_recovery(thread_id=1, reason="stale")

        cog = RouteProgression.__new__(RouteProgression)
        cog.bot = type("FakeBot", (), {"db": db, "get_channel": lambda self, thread_id: "not-a-thread"})()
        cog._active_legs = {}

        await cog.retry_pending_route_progression_actions.coro(cog)

        assert await db.get_pending_route_progression_actions() == []
        thread = await db.get_route_progression_thread(1)
        assert thread["status"] == "completed", "must not be clobbered back to 'abandoned'"

    asyncio.run(run())


# -- Coordinated audit of 165d20d: four confirmed P2 defects in the recovery/claim design -
# 1. advanced_to_index was committed BEFORE the send/status write it guards - a definite
#    failure there left the claim falsely consumed, so a retry silently did nothing.
#    Fixed with release_route_progression_advance_claim, called on definite failure.
# 2. The recovery poller converted an unreachable channel to None and still called
#    handle_leg_outcome for a non-final leg - the outcome got recorded, the next-leg
#    prompt was silently skipped, and the only durable record of that gap was deleted.
#    Fixed by requiring a real discord.Thread before running a non-final leg's recovery.
# 3. A transient aiosqlite error escaping the recovery loop's body permanently killed the
#    tasks.loop (sqlite errors aren't in discord.ext.tasks.Loop's reconnect set). Fixed
#    with cycle-level and per-row exception containment, including around the
#    failure-accounting call itself.
# 4. A failed recovery-queue insert was logged and swallowed, but the user was still told
#    "queued... no further action is needed" - losing the report/abandon with no trace.
#    Fixed by branching the notice on whether the queue write actually succeeded.

def test_release_route_progression_advance_claim_only_releases_the_matching_index(tmp_path):
    async def run():
        db = _make_db(tmp_path)
        await db.init()
        await db.create_route_progression_thread(
            thread_id=1, user_id=1, guild_id=1, route_kind="best_route", route_snapshot={},
            legs=[{"side": "buy", "id_terminal": 10, "id_commodity": 1}],
        )
        assert await db.claim_route_progression_advance(1, to_index=0) is True

        assert await db.release_route_progression_advance_claim(1, claimed_index=1, revert_to=-1) is False, (
            "must not release when the row's current value doesn't match claimed_index"
        )
        assert await db.release_route_progression_advance_claim(1, claimed_index=0, revert_to=-1) is True

        assert await db.claim_route_progression_advance(1, to_index=0) is True, (
            "releasing must let a retry win the same index again"
        )

    asyncio.run(run())


def test_post_leg_prompt_send_failure_releases_the_claim_so_a_retry_can_resend(tmp_path):
    """Audit-confirmed defect #1: _post_leg_prompt committed advanced_to_index before
    thread.send ran, so a definite send failure left the claim consumed and a retry
    silently returned without ever resending the prompt."""
    async def run():
        db = _make_db(tmp_path)
        await db.init()
        leg = _leg_input()
        await _create_thread_for_legs(db, 1, [leg])
        cog = RouteProgression.__new__(RouteProgression)
        cog.bot = type("FakeBot", (), {"db": db})()
        cog._active_legs = {}
        channel = _fake_thread_channel()
        channel.send.side_effect = discord.HTTPException(NS(status=500, reason="lost"), "send failed")

        try:
            await cog._post_leg_prompt(channel, 1, 0, leg)
        except discord.HTTPException:
            pass
        else:
            raise AssertionError("the simulated send failure must propagate")

        channel.send = AsyncMock()
        await cog._post_leg_prompt(channel, 1, 0, leg)

        assert channel.send.await_count == 1, "the retry must actually resend now that the claim was released"

    asyncio.run(run())


def _fake_history(messages: list):
    """A stand-in for discord.Thread.history() - a callable that returns a fresh async
    iterator over `messages` each time it's invoked, matching the real method's own
    signature (called as `thread.history(limit=...)`)."""
    def history(*, limit=None, **kwargs):
        async def gen():
            for message in messages:
                yield message
        return gen()
    return history


def _fake_sent_message(*, author_id: int, title: str, message_id: int = 999):
    return NS(id=message_id, author=NS(id=author_id), embeds=[NS(title=title)])


# -- _post_leg_prompt ambiguous-send reconciliation (2026-09-15 follow-up audit finding #3) -
# Real gap: the broadened `except Exception:` above (audit-confirmed defect #1, fixed
# earlier this session) released the durable claim for EVERY send failure, including an
# ambiguous transport-level one where the message may have actually reached Discord and
# only the confirmation was lost - releasing in that case lets a retry post a genuine
# duplicate live prompt. These tests prove the reconciliation added to close that gap:
# discord.HTTPException (a real rejection response) still releases immediately with no
# history lookup; anything else first checks Discord's own message history before
# deciding, and only releases when that check confirms the message is genuinely absent.

def test_post_leg_prompt_ambiguous_failure_confirmed_absent_releases_the_claim(tmp_path):
    """The message really didn't send (history confirms it) - safe to release and retry,
    same outcome as a definite discord.HTTPException failure, just reached via a real
    reconciliation check instead of assuming it from the exception type alone."""
    async def run():
        db = _make_db(tmp_path)
        await db.init()
        leg = _leg_input()
        await _create_thread_for_legs(db, 1, [leg])
        cog = RouteProgression.__new__(RouteProgression)
        cog.bot = type("FakeBot", (), {"db": db, "user": NS(id=1), "add_view": Mock()})()
        cog._active_legs = {}
        channel = _fake_thread_channel()
        channel.send.side_effect = asyncio.TimeoutError("network hiccup")
        channel.history = _fake_history([])  # nothing in recent history at all

        try:
            await cog._post_leg_prompt(channel, 1, 0, leg)
        except asyncio.TimeoutError:
            pass
        else:
            raise AssertionError("the simulated send failure must propagate")

        channel.send = AsyncMock()
        await cog._post_leg_prompt(channel, 1, 0, leg)

        assert channel.send.await_count == 1, "the retry must actually resend now that the claim was released"

    asyncio.run(run())


def test_post_leg_prompt_ambiguous_failure_confirmed_sent_holds_the_claim_and_reattaches_view(tmp_path):
    """The message actually did send - only the confirmation was lost. Must NOT release
    the claim (a retry would duplicate a genuinely live prompt) and must NOT propagate the
    exception (this is a success, just a late-discovered one) - and since discord.py never
    registered the view's buttons against a message it doesn't know exists, they must be
    explicitly re-attached via bot.add_view so they still work."""
    async def run():
        db = _make_db(tmp_path)
        await db.init()
        leg = _leg_input()
        await _create_thread_for_legs(db, 1, [leg])
        cog = RouteProgression.__new__(RouteProgression)
        add_view = Mock()
        cog.bot = type("FakeBot", (), {"db": db, "user": NS(id=1), "add_view": add_view})()
        cog._active_legs = {}
        channel = _fake_thread_channel()
        channel.send.side_effect = asyncio.TimeoutError("network hiccup")
        sent_message = _fake_sent_message(author_id=1, title="Leg 1: Buy Gold at Area18 TDD")
        channel.history = _fake_history([sent_message])

        await cog._post_leg_prompt(channel, 1, 0, leg)  # must not raise

        add_view.assert_called_once()
        assert add_view.call_args.kwargs["message_id"] == sent_message.id

        # The claim must still be held - a second call for the same leg must not resend.
        channel.send = AsyncMock()
        await cog._post_leg_prompt(channel, 1, 0, leg)
        channel.send.assert_not_awaited()

    asyncio.run(run())


def test_post_leg_prompt_ambiguous_failure_unreconcilable_holds_the_claim_but_still_raises(tmp_path):
    """Reconciliation itself fails too (e.g. the thread became unreachable) - genuinely
    can't tell whether the message sent. Per this project's "quarantine ambiguous outcomes,
    never blindly retry" convention, the claim must NOT be released (that would risk a
    duplicate if it actually did send) - but the original exception must still propagate,
    so this is logged/retried rather than silently swallowed as if it were a success."""
    async def run():
        db = _make_db(tmp_path)
        await db.init()
        leg = _leg_input()
        await _create_thread_for_legs(db, 1, [leg])
        cog = RouteProgression.__new__(RouteProgression)
        cog.bot = type("FakeBot", (), {"db": db, "user": NS(id=1), "add_view": Mock()})()
        cog._active_legs = {}
        channel = _fake_thread_channel()
        channel.send.side_effect = asyncio.TimeoutError("network hiccup")

        def broken_history(*, limit=None, **kwargs):
            raise discord.Forbidden(NS(status=403, reason="x"), "lost channel access")
        channel.history = broken_history

        try:
            await cog._post_leg_prompt(channel, 1, 0, leg)
        except asyncio.TimeoutError:
            pass
        else:
            raise AssertionError("the original send failure must still propagate")

        # The claim must still be held (not released) - see the three follow-up tests
        # below for what a SUBSEQUENT call must do with that held claim (2026-09-15
        # follow-up audit finding #3): it must never again be trusted as silent proof of
        # success, the way this test's own earlier version wrongly asserted.

    asyncio.run(run())


# -- retrying a held-but-unresolved claim (2026-09-15 follow-up audit finding #3) --------
# Real gap found on top of the reconciliation above: once a claim was held from an
# ambiguous, never-resolved first attempt, _post_leg_prompt's claim-already-held branch
# used to trust that as proof of success and just return - so a RETRY (from
# _record_leg_outcome_durably's own loop, or the recovery poller) silently reported
# success without ever delivering the prompt, without ever reaching the durable-queue
# safety net, and without ever recovering once history became reachable again. These
# three tests prove the fix: still-unresolvable keeps raising (so retries keep counting
# and eventually queue for recovery), genuinely-absent-now actually sends, and
# found-now reattaches the view exactly like the original send-failure path does.

def test_post_leg_prompt_retry_of_held_unresolved_claim_keeps_raising_while_still_unresolvable(tmp_path):
    async def run():
        db = _make_db(tmp_path)
        await db.init()
        leg = _leg_input()
        await _create_thread_for_legs(db, 1, [leg])
        cog = RouteProgression.__new__(RouteProgression)
        cog.bot = type("FakeBot", (), {"db": db, "user": NS(id=1), "add_view": Mock()})()
        cog._active_legs = {}
        channel = _fake_thread_channel()
        channel.send.side_effect = asyncio.TimeoutError("network hiccup")

        def broken_history(*, limit=None, **kwargs):
            raise discord.Forbidden(NS(status=403, reason="x"), "lost channel access")
        channel.history = broken_history

        for attempt in range(3):
            try:
                await cog._post_leg_prompt(channel, 1, 0, leg)
            except Exception:
                pass
            else:
                raise AssertionError(f"attempt {attempt} must still raise - history is still unreachable")

        # Only the very first attempt actually calls thread.send (and fails) - every
        # later attempt finds the claim already held and re-verifies via history
        # instead, without ever attempting a real send while still unresolved.
        assert channel.send.await_count == 1

    asyncio.run(run())


def test_post_leg_prompt_retry_of_held_unresolved_claim_sends_once_confirmed_genuinely_absent(tmp_path):
    """History was unreachable on the first attempt but becomes checkable again on a
    retry, and confirms the prompt genuinely never sent - the claim is already validly
    held at this leg_index, so the retry must go ahead and send now (recovering) rather
    than treating the still-held claim as if it already meant success."""
    async def run():
        db = _make_db(tmp_path)
        await db.init()
        leg = _leg_input()
        await _create_thread_for_legs(db, 1, [leg])
        cog = RouteProgression.__new__(RouteProgression)
        cog.bot = type("FakeBot", (), {"db": db, "user": NS(id=1), "add_view": Mock()})()
        cog._active_legs = {}
        channel = _fake_thread_channel()
        channel.send.side_effect = asyncio.TimeoutError("network hiccup")

        def broken_history(*, limit=None, **kwargs):
            raise discord.Forbidden(NS(status=403, reason="x"), "lost channel access")
        channel.history = broken_history

        try:
            await cog._post_leg_prompt(channel, 1, 0, leg)
        except asyncio.TimeoutError:
            pass
        else:
            raise AssertionError("the first attempt must still raise")

        channel.send = AsyncMock()
        channel.history = _fake_history([])  # now reachable, and genuinely empty
        await cog._post_leg_prompt(channel, 1, 0, leg)

        channel.send.assert_awaited_once(), "must actually deliver the prompt now that it's confirmed absent"

    asyncio.run(run())


def test_post_leg_prompt_retry_of_held_unresolved_claim_reattaches_view_once_confirmed_sent(tmp_path):
    """History was unreachable on the first attempt but becomes checkable again on a
    retry, and this time finds the message really did send the first time - must
    reattach the view (so its buttons still work) and must not send a duplicate."""
    async def run():
        db = _make_db(tmp_path)
        await db.init()
        leg = _leg_input()
        await _create_thread_for_legs(db, 1, [leg])
        cog = RouteProgression.__new__(RouteProgression)
        add_view = Mock()
        cog.bot = type("FakeBot", (), {"db": db, "user": NS(id=1), "add_view": add_view})()
        cog._active_legs = {}
        channel = _fake_thread_channel()
        channel.send.side_effect = asyncio.TimeoutError("network hiccup")

        def broken_history(*, limit=None, **kwargs):
            raise discord.Forbidden(NS(status=403, reason="x"), "lost channel access")
        channel.history = broken_history

        try:
            await cog._post_leg_prompt(channel, 1, 0, leg)
        except asyncio.TimeoutError:
            pass
        else:
            raise AssertionError("the first attempt must still raise")

        channel.send = AsyncMock()
        sent_message = _fake_sent_message(author_id=1, title="Leg 1: Buy Gold at Area18 TDD")
        channel.history = _fake_history([sent_message])
        await cog._post_leg_prompt(channel, 1, 0, leg)  # must not raise

        channel.send.assert_not_awaited(), "must not send a duplicate once confirmed already sent"
        add_view.assert_called_once()
        assert add_view.call_args.kwargs["message_id"] == sent_message.id

    asyncio.run(run())


def test_record_leg_outcome_durably_eventually_queues_recovery_when_prompt_stays_genuinely_ambiguous(
    monkeypatch, tmp_path
):
    """End-to-end reproduction of the audit's own finding #3 scenario: the next-leg send
    and history reconciliation both fail on every attempt. Before this fix, the second
    in-process retry saw the claim held from attempt 1 and returned normally, so
    _record_leg_outcome_durably reported success after only 2 attempts without ever
    reaching its own durable-queue safety net - the route silently stalled forever with
    nothing recorded anywhere except a log line. After the fix, every attempt keeps
    raising while genuinely unresolved, so the wrapper exhausts its retries and actually
    queues a recovery action."""
    async def run():
        monkeypatch.setattr("bot.cogs.route_progression.POST_ACK_RETRY_DELAY_SECONDS", 0)
        db = _make_db(tmp_path)
        await db.init()
        leg0 = _leg_input()
        leg1 = _leg_input(display_label="Sell Gold at Levski")
        await _create_thread_for_legs(db, 1, [leg0, leg1])
        cog = RouteProgression.__new__(RouteProgression)
        cog.bot = type("FakeBot", (), {"db": db, "user": NS(id=1), "add_view": Mock()})()
        cog._active_legs = {1: [leg0, leg1]}
        channel = _fake_thread_channel()

        async def flaky_send(*args, **kwargs):
            # Only the leg-1 prompt send (embed=/view=) is the thing that's genuinely
            # ambiguous here - the wrapper's own plain-text recovery notice at the end
            # must still go through, or this test can't tell "queued for recovery" apart
            # from "notifying about it also happened to fail."
            if "embed" in kwargs:
                raise asyncio.TimeoutError("network hiccup")
            return NS(id=999)
        channel.send = AsyncMock(side_effect=flaky_send)

        def broken_history(*, limit=None, **kwargs):
            raise discord.Forbidden(NS(status=403, reason="x"), "lost channel access")
        channel.history = broken_history

        handled = await cog._record_leg_outcome_durably(channel, 1, 0, leg0, outcome="matched")

        assert handled is True, "must report handled - the outcome itself was saved and recovery was queued"
        pending = await db.get_pending_route_progression_actions()
        assert len(pending) == 1, (
            "the durable-queue safety net must actually engage instead of a hollow "
            "'claim already held' success after only 2 attempts"
        )
        assert pending[0]["action_kind"] == "leg_outcome"
        assert pending[0]["leg_index"] == 0
        stored = await db.get_route_progression_leg(1, 0)
        assert stored is not None and stored["outcome"] == "matched", (
            "the leg-0 outcome itself must still be saved even though leg 1's prompt never delivered"
        )

    asyncio.run(run())


# -- partial-failure recovery return semantics (2026-09-15 follow-up audit finding #4) ---
# Real gap: _record_leg_outcome_durably's every retry re-runs handle_leg_outcome's WHOLE
# call, but that call's own outcome write is only its first step - a later step (the
# next-leg prompt, market/suppression writes) can keep failing on every attempt even
# though the outcome itself was durably saved on attempt 1. If the durable recovery queue
# insert ALSO then failed, the wrapper used to report "not handled" unconditionally,
# telling its caller to release the claim and reopen the view - letting a second,
# different report through a different button/modal, which the DB's own conflict guard
# correctly rejects, permanently abandoning the real, already-saved report's unfinished
# downstream work. These tests prove the fix: an already-saved outcome is recognized even
# when queuing failed, and the view is not reopened for it.

def test_record_leg_outcome_durably_reports_handled_when_outcome_was_saved_despite_queue_failure(
    monkeypatch, tmp_path
):
    async def run():
        from bot.cogs.route_progression import RouteProgression
        monkeypatch.setattr("bot.cogs.route_progression.POST_ACK_RETRY_DELAY_SECONDS", 0)
        db = _make_db(tmp_path)
        await db.init()
        leg0 = _leg_input()
        leg1 = _leg_input(display_label="Sell Gold at Levski")
        await _create_thread_for_legs(db, 1, [leg0, leg1])
        cog = RouteProgression.__new__(RouteProgression)
        cog.bot = type("FakeBot", (), {"db": db, "user": NS(id=1), "add_view": Mock()})()
        cog._active_legs = {1: [leg0, leg1]}
        channel = _fake_thread_channel()

        async def flaky_send(*args, **kwargs):
            if "embed" in kwargs:
                raise asyncio.TimeoutError("network hiccup")
            return NS(id=999)
        channel.send = AsyncMock(side_effect=flaky_send)

        def broken_history(*, limit=None, **kwargs):
            raise discord.Forbidden(NS(status=403, reason="x"), "lost channel access")
        channel.history = broken_history

        async def failing_queue(*args, **kwargs):
            raise RuntimeError("db unreachable")
        db.queue_route_progression_leg_recovery = failing_queue

        handled = await cog._record_leg_outcome_durably(channel, 1, 0, leg0, outcome="matched")

        assert handled is True, (
            "must report handled - the outcome was already saved even though nothing "
            "could be automatically queued to finish the rest"
        )
        pending = await db.get_pending_route_progression_actions()
        assert len(pending) == 0, "the failed queue insert must not have silently succeeded"
        stored = await db.get_route_progression_leg(1, 0)
        assert stored is not None and stored["outcome"] == "matched"

    asyncio.run(run())


def test_record_leg_outcome_durably_reports_not_handled_when_nothing_was_ever_saved(monkeypatch, tmp_path):
    """Control case: the outcome write itself never commits (handle_leg_outcome fails
    before ever reaching the DB), so once the queue also fails there is genuinely nothing
    saved - the wrapper must still report False so the caller releases the claim and lets
    the user try again, exactly as before this fix."""
    async def run():
        from bot.cogs.route_progression import RouteProgression
        monkeypatch.setattr("bot.cogs.route_progression.POST_ACK_RETRY_DELAY_SECONDS", 0)
        cog = RouteProgression.__new__(RouteProgression)

        async def always_fails(*args, **kwargs):
            raise RuntimeError("permanent failure before anything is saved")
        cog.handle_leg_outcome = always_fails

        class _FakeDB:
            async def queue_route_progression_leg_recovery(self, **kwargs):
                raise RuntimeError("db unreachable")

            async def get_route_progression_leg(self, thread_id, leg_index):
                return None

        cog.bot = type("FakeBot", (), {"db": _FakeDB()})()
        channel = _fake_thread_channel()

        handled = await cog._record_leg_outcome_durably(channel, 1, 0, _leg_input(), outcome="matched")

        assert handled is False, "nothing was ever saved - must still report not handled"

    asyncio.run(run())


def test_matched_button_does_not_reopen_the_view_when_the_outcome_was_already_saved(monkeypatch, tmp_path):
    """End-to-end through the real button callback (matching the audit's own reproduction
    technique): 'Matched quote' is clicked, the outcome commits, but the next-leg prompt
    delivery AND the durable recovery queue both then fail. Before this fix, the button's
    own `if not handled: release_claim(); reenable_in_background()` would reopen the
    view - the audit's reproduction showed a subsequent 'Less / not there' submission
    then getting rejected as conflicting, permanently stranding the route with no next
    prompt and no recovery job. After the fix, the view stays resolved because the report
    itself is recognized as already saved."""
    async def run():
        monkeypatch.setattr("bot.cogs.route_progression.POST_ACK_RETRY_DELAY_SECONDS", 0)
        db = _make_db(tmp_path)
        await db.init()
        leg0 = _leg_input()
        leg1 = _leg_input(display_label="Sell Gold at Levski")
        await _create_thread_for_legs(db, 1, [leg0, leg1])
        cog = RouteProgression.__new__(RouteProgression)
        cog.bot = type("FakeBot", (), {"db": db, "user": NS(id=1), "add_view": Mock()})()
        cog._active_legs = {1: [leg0, leg1]}
        channel = _fake_thread_channel()

        async def flaky_send(*args, **kwargs):
            if "embed" in kwargs:
                raise asyncio.TimeoutError("network hiccup")
            return NS(id=999)
        channel.send = AsyncMock(side_effect=flaky_send)

        def broken_history(*, limit=None, **kwargs):
            raise discord.Forbidden(NS(status=403, reason="x"), "lost channel access")
        channel.history = broken_history

        async def failing_queue(*args, **kwargs):
            raise RuntimeError("db unreachable")
        db.queue_route_progression_leg_recovery = failing_queue

        view = LegOutcomeView(cog=cog, thread_id=1, leg_index=0, leg=leg0)
        view.message = _FakeMessage()
        interaction = _FakeInteraction()
        interaction.channel = channel

        await view.matched.callback(interaction)

        assert view.resolved is True, (
            "the view must stay resolved - reopening it would let a conflicting second "
            "report through for a leg that's already genuinely saved"
        )
        stored = await db.get_route_progression_leg(1, 0)
        assert stored is not None and stored["outcome"] == "matched"

    asyncio.run(run())


def test_completion_status_failure_releases_the_claim_so_a_retry_can_finish(tmp_path):
    """Audit-confirmed defect #1's other half: the completion claim (to_index=total_legs)
    committed before set_route_progression_thread_status ran, so a DB failure there left
    the thread stuck in_progress forever - the retry saw the claim as already won and
    returned early instead of finishing the status write."""
    async def run():
        db = _make_db(tmp_path)
        await db.init()
        leg = _leg_input(id_terminal=10, id_commodity=1)
        await _create_thread_for_legs(db, 1, [leg])
        cog = RouteProgression.__new__(RouteProgression)
        cog.bot = type("FakeBot", (), {"db": db})()
        cog._active_legs = {}

        real_set_status = db.set_route_progression_thread_status
        db.set_route_progression_thread_status = AsyncMock(side_effect=RuntimeError("temporary DB failure"))
        try:
            await cog.handle_leg_outcome(None, 1, 0, leg, outcome="matched")
        except RuntimeError:
            pass
        else:
            raise AssertionError("the simulated status failure must propagate")

        db.set_route_progression_thread_status = real_set_status
        await cog.handle_leg_outcome(None, 1, 0, leg, outcome="matched")

        thread = await db.get_route_progression_thread(1)
        assert thread["status"] == "completed", "the retry must finish completion now that the claim was released"

    asyncio.run(run())


def test_recovery_keeps_action_when_next_prompt_cannot_be_delivered(tmp_path):
    """Audit-confirmed defect #2: the poller converted an unreachable channel to None and
    still called handle_leg_outcome for a non-final leg - the outcome got recorded, the
    next-leg prompt was silently skipped (channel isn't a discord.Thread), and the poller
    then deleted the only durable record telling a future tick to deliver that prompt."""
    async def run():
        db = _make_db(tmp_path)
        await db.init()
        leg0 = _leg_input(id_terminal=10, id_commodity=1)
        leg1 = _leg_input(
            side="sell", id_terminal=20, id_commodity=1,
            terminal_name="Elsewhere", display_label="Sell Gold at Elsewhere",
        )
        await _create_thread_for_legs(db, 1, [leg0, leg1])
        await db.queue_route_progression_leg_recovery(
            thread_id=1, leg_index=0, side=leg0.side, id_terminal=leg0.id_terminal,
            id_commodity=leg0.id_commodity, terminal_name=leg0.terminal_name,
            commodity_name=leg0.commodity_name, display_label=leg0.display_label,
            quoted_price=leg0.quoted_price, quoted_scu=leg0.quoted_scu, quoted_status=leg0.quoted_status,
            market_scu=leg0.market_scu, outcome="matched", actual_price=None, actual_scu=None, precision=None,
        )

        async def failing_fetch_channel(self, thread_id):
            raise discord.HTTPException(NS(status=503, reason="unavailable"), "temporary outage")

        cog = RouteProgression.__new__(RouteProgression)
        cog.bot = type(
            "FakeBot", (),
            {"db": db, "get_channel": lambda self, thread_id: None, "fetch_channel": failing_fetch_channel},
        )()
        cog._active_legs = {}

        await cog.retry_pending_route_progression_actions.coro(cog)

        pending = await db.get_pending_route_progression_actions()
        assert len(pending) == 1, "must stay queued until the next prompt can actually be delivered"
        stored = await db.get_route_progression_leg(1, 0)
        assert stored["outcome"] is None, "must not record the outcome before the next prompt can be delivered"

    asyncio.run(run())


def test_recovery_completes_a_final_leg_even_when_the_channel_is_unreachable(tmp_path):
    """The fix for defect #2 must not overcorrect: completion doesn't depend on a real
    channel the way a next-leg prompt does (only the closing message/archive step, whose
    own failure is already caught elsewhere) - so a final leg's recovery must still
    succeed even when the thread can't be reached, exactly as it did before this fix."""
    async def run():
        db = _make_db(tmp_path)
        await db.init()
        leg = _leg_input(id_terminal=10, id_commodity=1)
        await _create_thread_for_legs(db, 1, [leg])
        await db.queue_route_progression_leg_recovery(
            thread_id=1, leg_index=0, side=leg.side, id_terminal=leg.id_terminal,
            id_commodity=leg.id_commodity, terminal_name=leg.terminal_name,
            commodity_name=leg.commodity_name, display_label=leg.display_label,
            quoted_price=leg.quoted_price, quoted_scu=leg.quoted_scu, quoted_status=leg.quoted_status,
            market_scu=leg.market_scu, outcome="matched", actual_price=None, actual_scu=None, precision=None,
        )

        cog = RouteProgression.__new__(RouteProgression)
        cog.bot = type("FakeBot", (), {"db": db, "get_channel": lambda self, thread_id: "not-a-thread"})()
        cog._active_legs = {}

        await cog.retry_pending_route_progression_actions.coro(cog)

        assert await db.get_pending_route_progression_actions() == []
        stored = await db.get_route_progression_leg(1, 0)
        assert stored["outcome"] == "matched"
        thread = await db.get_route_progression_thread(1)
        assert thread["status"] == "completed"

    asyncio.run(run())


def test_recovery_cycle_contains_a_transient_database_failure_loading_pending_actions():
    """Audit-confirmed defect #3: an aiosqlite.OperationalError (e.g. a lock beyond the
    busy timeout) escaping the initial pending-actions read used to terminate this
    tasks.loop permanently, since sqlite errors aren't in discord.ext.tasks.Loop's own
    reconnect exception set."""
    async def run():
        db = type(
            "FailingDB", (),
            {"get_pending_route_progression_actions": AsyncMock(
                side_effect=aiosqlite.OperationalError("database is locked")
            )},
        )()
        cog = RouteProgression.__new__(RouteProgression)
        cog.bot = type("FakeBot", (), {"db": db})()
        cog._active_legs = {}

        await cog.retry_pending_route_progression_actions.coro(cog)  # must not raise

    asyncio.run(run())


def test_recovery_cycle_continues_past_a_per_row_database_failure(tmp_path):
    """A failure looking up ONE row's thread must not escape and stop the whole cycle -
    it must be recorded as a failed attempt (itself made failure-safe) so the row stays
    queued for the next tick, exactly like any other failed recovery attempt."""
    async def run():
        db = _make_db(tmp_path)
        await db.init()
        leg = _leg_input(id_terminal=10, id_commodity=1)
        await db.queue_route_progression_leg_recovery(
            thread_id=1, leg_index=0, side=leg.side, id_terminal=leg.id_terminal,
            id_commodity=leg.id_commodity, terminal_name=leg.terminal_name,
            commodity_name=leg.commodity_name, display_label=leg.display_label,
            quoted_price=leg.quoted_price, quoted_scu=leg.quoted_scu, quoted_status=leg.quoted_status,
            market_scu=leg.market_scu, outcome="matched", actual_price=None, actual_scu=None, precision=None,
        )
        real_get_thread = db.get_route_progression_thread
        db.get_route_progression_thread = AsyncMock(side_effect=aiosqlite.OperationalError("database is locked"))

        cog = RouteProgression.__new__(RouteProgression)
        cog.bot = type("FakeBot", (), {"db": db})()
        cog._active_legs = {}

        await cog.retry_pending_route_progression_actions.coro(cog)  # must not raise

        db.get_route_progression_thread = real_get_thread
        pending = await db.get_pending_route_progression_actions()
        assert len(pending) == 1 and pending[0]["attempts"] == 1, (
            "the row must stay queued with its attempt counted, not be lost or crash the cycle"
        )

    asyncio.run(run())


def test_failed_leg_outcome_queue_write_does_not_claim_that_background_recovery_is_queued(monkeypatch):
    """Audit-confirmed defect #4: if the recovery-queue insert itself fails during the
    same outage that exhausted the post-ack retries, the user was still told the report
    was queued and no further action was needed - losing the report with no trace."""
    async def run():
        db = type(
            "FailingDB", (),
            {
                "queue_route_progression_leg_recovery": AsyncMock(side_effect=RuntimeError("DB offline")),
                # Nothing was ever saved in this scenario (handle_leg_outcome itself
                # always fails before reaching any real write) - finding #4's
                # already-saved check must correctly find nothing here too.
                "get_route_progression_leg": AsyncMock(return_value=None),
            },
        )()
        cog = RouteProgression.__new__(RouteProgression)
        cog.bot = type("FakeBot", (), {"db": db})()
        cog.handle_leg_outcome = AsyncMock(side_effect=RuntimeError("DB offline"))
        monkeypatch.setattr("bot.cogs.route_progression.POST_ACK_RETRY_DELAY_SECONDS", 0)
        channel = _fake_thread_channel()

        await cog._record_leg_outcome_durably(channel, 1, 0, _leg_input(), outcome="matched")

        channel.send.assert_awaited_once()
        notice = channel.send.call_args.args[0]
        assert "queued" not in notice.lower(), (
            "must not claim the report was queued when the recovery-queue write itself failed"
        )

    asyncio.run(run())


def test_failed_abandon_queue_write_does_not_claim_that_background_recovery_is_queued(monkeypatch):
    """Same fix as above, applied to _abandon_thread_durably's identical pattern."""
    async def run():
        db = type(
            "FailingDB", (),
            {"queue_route_progression_abandon_recovery": AsyncMock(side_effect=RuntimeError("DB offline"))},
        )()
        cog = RouteProgression.__new__(RouteProgression)
        cog.bot = type("FakeBot", (), {"db": db})()
        cog.abandon_thread = AsyncMock(side_effect=RuntimeError("DB offline"))
        monkeypatch.setattr("bot.cogs.route_progression.POST_ACK_RETRY_DELAY_SECONDS", 0)
        channel = _fake_thread_channel()

        await cog._abandon_thread_durably(channel, 1, reason="test")

        channel.send.assert_awaited_once()
        notice = channel.send.call_args.args[0]
        assert "queued" not in notice.lower(), (
            "must not claim the abandon was queued when the recovery-queue write itself failed"
        )

    asyncio.run(run())
