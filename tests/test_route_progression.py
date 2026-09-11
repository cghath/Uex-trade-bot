"""Tests for Recommendation Outcome Tracking (Phase 1): the pure outcome-to-terminal-state
mapping in bot/uex/route_progression.py, the Database CRUD methods it's built on, and the
leg-outcome View/Modal claim logic in bot/cogs/route_progression.py."""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock

import aiosqlite
from cryptography.fernet import Fernet
import discord
import pytest

from bot.cogs.route_progression import (
    AbandonConfirmView,
    ActualAmountModal,
    LegOutcomeView,
    MoreOutcomeFollowupView,
    RouteLegInput,
    RouteProgression,
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
        await db.record_player_report_market_update({
            "id_commodity": 1, "id_terminal": 10, "commodity_name": "Gold",
            "terminal_name": "Area18 TDD", "price_buy": 100, "scu_buy": 50, "status_buy": 3,
        })
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
        await db.record_player_report_market_update({
            "id_commodity": 1, "id_terminal": 10, "commodity_name": "Gold",
            "terminal_name": "Area18 TDD", "price_sell": 95, "scu_sell": 20, "status_sell": None,
        })
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
        await db.record_player_report_market_update({
            "id_commodity": 1, "id_terminal": 10, "commodity_name": "Gold",
            "terminal_name": "Area18 TDD", "price_buy": None, "scu_buy": 0.0, "status_buy": 1,
        })
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
        await db.record_player_report_market_update({
            "id_commodity": 1, "id_terminal": 10, "commodity_name": "Gold",
            "terminal_name": "Area18 TDD", "price_buy": 100.0, "scu_buy": 50.0, "status_buy": 3,
        })
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
        await db.suppress_terminal_market_side(id_commodity=1, id_terminal=10, side="buy", until=until)

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
        await db.suppress_terminal_market_side(id_commodity=1, id_terminal=10, side="buy", until=expired_until)

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
        await db.suppress_terminal_market_side(id_commodity=1, id_terminal=10, side="buy", until=until)
        await db.suppress_terminal_market_side(id_commodity=2, id_terminal=20, side="sell", until=until)

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
        await db.suppress_terminal_market_side(id_commodity=1, id_terminal=10, side="buy", until=until)

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
        await db.suppress_terminal_market_side(id_commodity=1, id_terminal=10, side="buy", until=expired_until)

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
    def __init__(self, *, fail_edit: bool = False, fail_send_message: bool = False):
        self.messages = []
        self.modals = []
        self.edited_views = []
        self._fail_edit = fail_edit
        self._fail_send_message = fail_send_message

    async def send_modal(self, modal):
        self.modals.append(modal)

    async def send_message(self, *args, **kwargs):
        if self._fail_send_message:
            raise discord.HTTPException(NS(status=500, reason="test"), "test")
        self.messages.append((args, kwargs))

    async def edit_message(self, **kwargs):
        if self._fail_edit:
            raise discord.HTTPException(NS(status=500, reason="test"), "test")
        self.edited_views.append(kwargs)


class _FakeInteraction:
    def __init__(self, *, fail_edit: bool = False, fail_send_message: bool = False):
        self.response = _FakeResponse(fail_edit=fail_edit, fail_send_message=fail_send_message)
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
    further down for that."""
    def __init__(self):
        self.calls = []
        self.abandon_calls = []

    async def _record_leg_outcome_durably(self, channel, thread_id, leg_index, leg, **kwargs):
        self.calls.append((thread_id, leg_index, leg, kwargs))

    async def _abandon_thread_durably(self, channel, thread_id, **kwargs):
        self.abandon_calls.append((thread_id, kwargs))


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


async def _create_thread_for_legs(db: Database, thread_id: int, legs: list[RouteLegInput]) -> None:
    await db.create_route_progression_thread(
        thread_id=thread_id, user_id=1, guild_id=1, route_kind="best_route",
        route_snapshot={"title": "test route", "legs": [_leg_dict(leg) for leg in legs]},
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
