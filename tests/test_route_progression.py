"""Tests for Recommendation Outcome Tracking (Phase 1): the pure outcome-to-terminal-state
mapping in bot/uex/route_progression.py, the Database CRUD methods it's built on, and the
leg-outcome View/Modal claim logic in bot/cogs/route_progression.py."""
from __future__ import annotations

import asyncio
from types import SimpleNamespace as NS

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
from bot.db.database import Database
from bot.uex.route_progression import is_reportable_amount, terminal_state_update_for_outcome


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


class _FakeCog:
    def __init__(self):
        self.calls = []
        self.abandon_calls = []

    async def handle_leg_outcome(self, channel, thread_id, leg_index, leg, **kwargs):
        self.calls.append((thread_id, leg_index, leg, kwargs))

    async def abandon_thread(self, channel, thread_id, **kwargs):
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
