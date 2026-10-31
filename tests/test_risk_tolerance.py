"""The saved risk tolerance is gone; the "⚠️ Cargo risk" labels on routes stay.

/set-trading-preferences had a risk-tolerance option (Low, Medium, High) that, since
PROJECT_CONTEXT.md entry 104, left flagged goods out of route suggestions. It was removed on
2026-09-30 (entry 114): only UEX's illegal flag ever changed a result, so Low and Medium
behaved the same, and on the Pi 5 players had it unset, 5 on High (the same as off), 1 on
Medium and none on Low. The labels already tell players what they're hauling.

What these check:
- /set-trading-preferences has no risk-tolerance option.
- A value saved before the removal stays in its column (the schema is additive-only), and
  nothing reads it: no route command, hedge suggestion or tracking thread leaves goods out
  because of it, and no reply mentions it.
- Flagged cargo on every route command that labelled it still carries its label.

Tests that need a saved value write it straight into the column, the way an old row looks,
so they also run against the code from before the removal, and fail there because goods are
left out or the setting is shown.
"""
from __future__ import annotations

import asyncio
import inspect
from datetime import datetime, timezone
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock

import httpx
import pytest
from cryptography.fernet import Fernet

from bot.cogs import prices as prices_module
from bot.cogs.prices import Prices
from bot.cogs.route_progression import RouteProgression
from bot.cogs.trading_preferences import TradingPreferences
from bot.cogs.trends import Trends
from bot.db.database import Database
from bot.uex.client import UexClient
from bot.uex.commodity_risk import RISK_FLAG_KEYS
from bot.uex.trading_preferences import DEFAULT_TRADING_PREFERENCES, describe_active_preferences
from bot.uex.trends import ScoredRouteEntry
from tests.route_results import route_results
from tests.test_route_messages import USER, _client, _db, _Interaction

GOLD, WIDOW, QUANTAINIUM = 5, 6, 7
NO_FLAGS = {key: 0 for key in RISK_FLAG_KEYS}
COMMODITIES = [
    {"id": GOLD, "name": "Gold", **NO_FLAGS},
    {"id": WIDOW, "name": "WiDoW", **NO_FLAGS, "is_illegal": 1},
    {"id": QUANTAINIUM, "name": "Quantainium", **NO_FLAGS, "is_explosive": 1},
]
# The fixtures in tests/test_route_send_shape.py trade Stileron (id 1) and Cobalt (id 2).
STILERON_AND_ILLEGAL_COBALT = [
    {"id": 1, "name": "Stileron", **NO_FLAGS},
    {"id": 2, "name": "Cobalt", **NO_FLAGS, "is_illegal": 1},
]
ILLEGAL_LABEL = "Cargo risk: restricted in some jurisdictions"
OLD_LEVELS = ["low", "medium"]  # the two that left goods out; "high" left out nothing


async def save_old_risk_tolerance(db: Database, user_id: int, tolerance: str) -> None:
    """A risk tolerance saved before the removal. The column is still there, so write it
    directly: the code no longer has a way to."""
    async with db.connect() as conn:
        await conn.execute(
            "INSERT INTO user_trading_preferences (user_id, risk_tolerance) VALUES (?, ?) "
            "ON CONFLICT(user_id) DO UPDATE SET risk_tolerance = excluded.risk_tolerance",
            (user_id, tolerance),
        )
        await conn.commit()


async def stored_risk_tolerance(db: Database, user_id: int) -> str | None:
    async with db.connect() as conn:
        cursor = await conn.execute(
            "SELECT risk_tolerance FROM user_trading_preferences WHERE user_id = ?", (user_id,))
        row = await cursor.fetchone()
    return row[0] if row else None


def _all_text(sent) -> str:
    """Everything a player could read across the sends: plain text plus the route results."""
    calls = [(item.args, item.kwargs) if hasattr(item, "kwargs") else item
             for item in getattr(sent, "call_args_list", sent)]
    parts = [str(args[0]) for args, _ in calls if args]
    parts += [kwargs["content"] for _, kwargs in calls if kwargs.get("content")]
    parts += [kwargs["embed"].description or "" for _, kwargs in calls if kwargs.get("embed") is not None]
    results = route_results(sent)
    if results:
        parts.append(results.all_text())
    return "\n".join(parts)


# ---- the option and the stored value ------------------------------------------------------

def test_set_trading_preferences_has_no_risk_tolerance_option():
    command = TradingPreferences.set_trading_preferences
    assert [param.name for param in command.parameters] == [
        "ship", "budget", "space_only", "capital_ship_access", "auto_load_only", "system",
    ]
    assert "risk" not in " ".join(str(param.description) for param in command.parameters).lower()
    assert "risk_tolerance" not in inspect.signature(describe_active_preferences).parameters


def test_an_old_saved_value_stays_in_its_column_and_nothing_reads_or_writes_it(tmp_path):
    async def run():
        db = Database(tmp_path / "prefs.sqlite3", Fernet(Fernet.generate_key()))
        await db.init()
        async with db.connect() as conn:
            cursor = await conn.execute("PRAGMA table_info(user_trading_preferences)")
            columns = [row[1] for row in await cursor.fetchall()]
        assert "risk_tolerance" in columns, "additive-only: the column stays"

        await save_old_risk_tolerance(db, USER, "low")
        assert await db.get_trading_preferences(USER) == DEFAULT_TRADING_PREFERENCES

        prefs = await db.set_trading_preferences(USER, space_only=True)
        assert prefs == dict(DEFAULT_TRADING_PREFERENCES, space_only=True)
        assert await stored_risk_tolerance(db, USER) == "low", "an update leaves the old value alone"
        await db.set_trading_preferences(2, auto_load_only=True)
        assert await stored_risk_tolerance(db, 2) is None, "a new row never gets one"
        with pytest.raises(TypeError):
            await db.set_trading_preferences(USER, risk_tolerance="low")

        await db.init()  # a restart
        assert await db.get_trading_preferences(USER) == dict(DEFAULT_TRADING_PREFERENCES, space_only=True)
        assert await db.clear_trading_preferences(USER)
        assert await db.get_trading_preferences(USER) == DEFAULT_TRADING_PREFERENCES

    asyncio.run(run())


def test_the_preference_replies_no_longer_mention_it(tmp_path):
    async def run():
        db = Database(tmp_path / "prefs.sqlite3", Fernet(Fernet.generate_key()))
        await db.init()
        await save_old_risk_tolerance(db, USER, "medium")
        cog = TradingPreferences.__new__(TradingPreferences)
        cog.bot = NS(db=db)

        def interaction():
            return NS(user=NS(id=USER), response=NS(send_message=AsyncMock(), defer=AsyncMock()),
                      followup=NS(send=AsyncMock()))

        shown, updated = interaction(), interaction()
        await cog.my_trading_preferences.callback(cog, shown)
        await cog.set_trading_preferences.callback(cog, updated, space_only=True)
        return shown.response.send_message.call_args.args[0], updated.followup.send.call_args.args[0]

    shown, updated = asyncio.run(run())
    assert "Your trading preferences" in shown and "Trading preferences updated." in updated
    for text in (shown, updated):
        assert "Preferred system:" in text
        assert "risk" not in text.lower(), text


# ---- /top-routes --------------------------------------------------------------------------

def _entry(id_commodity: int, name: str) -> ScoredRouteEntry:
    return ScoredRouteEntry(
        commodity_name=name, id_commodity=id_commodity, origin_terminal_name="Origin",
        destination_terminal_name="Dest", price_origin=10.0, price_destination=60.0, price_margin=None,
        price_roi=None, distance=None, score=50, scu_origin=50, scu_destination=50, status_origin=1,
        status_destination=1, origin_terminal_id=1, destination_terminal_id=2,
    )


async def _top_routes(tmp_path, tolerance, entries):
    db = await _db(tmp_path)
    await db.upsert_commodity_reference(COMMODITIES)
    await save_old_risk_tolerance(db, USER, tolerance)
    client = await _client([])
    cog = Trends.__new__(Trends)
    cog.bot = NS(db=db, uex=client, get_cog=lambda name: None)
    cog._top_scored_routes_lock = asyncio.Lock()
    cog._top_scored_routes = entries
    cog._top_scored_routes_updated_at = datetime.now(timezone.utc)
    interaction = _Interaction()
    try:
        await cog.top_routes.callback(cog, interaction)
    finally:
        await client.aclose()
    return _all_text(interaction.sent)


@pytest.mark.parametrize("tolerance", OLD_LEVELS)
def test_top_routes_leaves_nothing_out_and_still_labels_flagged_cargo(tmp_path, tolerance):
    entries = [_entry(WIDOW, "WiDoW"), _entry(QUANTAINIUM, "Quantainium"), _entry(GOLD, "Gold")]
    text = asyncio.run(_top_routes(tmp_path, tolerance, entries))
    assert "WiDoW" in text and "Quantainium" in text and "Gold" in text
    assert f"⚠️ {ILLEGAL_LABEL}" in text and "⚠️ Cargo risk: explosion risk" in text
    assert "risk tolerance" not in text.lower()


@pytest.mark.parametrize("tolerance", OLD_LEVELS)
def test_top_routes_with_only_flagged_goods_still_shows_them(tmp_path, tolerance):
    text = asyncio.run(_top_routes(tmp_path, tolerance, [_entry(WIDOW, "WiDoW"), _entry(QUANTAINIUM, "Quantainium")]))
    assert "No routes" not in text
    assert "WiDoW" in text and "Quantainium" in text


def test_top_routes_hedge_line_offers_flagged_cargo_with_its_label(tmp_path):
    from tests.test_ranked_routes_hedge import _make_cog, _route, _route_text

    async def run():
        cog, db = await _make_cog(tmp_path)
        await db.upsert_commodity_reference([{"id": 2, "name": "Cobalt", **NO_FLAGS, "is_illegal": 1}])
        await save_old_risk_tolerance(db, 1, "medium")
        cog._top_scored_routes_lock = asyncio.Lock()
        cog._top_scored_routes = [_route()]
        cog._top_scored_routes_updated_at = datetime.now(timezone.utc)
        interaction = NS(user=NS(id=1), response=NS(defer=AsyncMock()), followup=NS(send=AsyncMock()))
        await cog.top_routes.callback(cog, interaction, ship="Ship")
        return _route_text(interaction)

    text = asyncio.run(run())
    hedges = [line for line in text.splitlines() if line.startswith("Hedge:")]
    # labelled as risky, not just offered (workflow audit 2026-09-30: no label, so it looked unflagged)
    assert hedges and "Cobalt" in hedges[0] and ILLEGAL_LABEL in hedges[0], text


# ---- /best-route --------------------------------------------------------------------------

@pytest.mark.parametrize("tolerance", OLD_LEVELS)
def test_best_route_shows_the_label_and_no_tolerance_note(tmp_path, tolerance):
    async def run():
        db = await _db(tmp_path)
        await db.upsert_commodity_reference(COMMODITIES)
        await save_old_risk_tolerance(db, USER, tolerance)
        rows = [
            {"id_terminal": 1, "terminal_name": "Origin", "id_commodity": WIDOW, "commodity_name": "WiDoW",
             "price_buy": 10, "price_sell": 0, "scu_buy": 100, "scu_sell": 0, "status_buy": 3, "status_sell": None},
            {"id_terminal": 2, "terminal_name": "Dest", "id_commodity": WIDOW, "commodity_name": "WiDoW",
             "price_buy": 0, "price_sell": 60, "scu_buy": 0, "scu_sell": 100, "status_buy": None, "status_sell": 5},
        ]
        client = await _client(rows)
        cog = Prices.__new__(Prices)
        cog.bot = NS(db=db, uex=client)
        interaction = _Interaction()
        try:
            await cog.best_route.callback(cog, interaction, commodity="WiDoW")
        finally:
            await client.aclose()
        return _all_text(interaction.sent)

    text = asyncio.run(run())
    assert "Origin" in text and "Dest" in text
    assert f"⚠️ {ILLEGAL_LABEL}" in text
    assert "risk tolerance" not in text.lower()


@pytest.mark.parametrize("branch", ["uex_routes", "price_rows"])
def test_best_route_hedge_line_offers_flagged_cargo_with_its_label(tmp_path, branch):
    from tests.test_route_send_shape import _all_embed_text, _FakeInteraction, _fallback_best_route_cog

    async def run():
        db = Database(tmp_path / f"{branch}.sqlite3", Fernet(Fernet.generate_key()))
        await db.init()
        await db.upsert_commodity_reference([{"id": 2, "name": "Cobalt", **NO_FLAGS, "is_illegal": 1}])
        await save_old_risk_tolerance(db, 1, "medium")
        sell_terminal = 3 if branch == "price_rows" else 101
        await db.record_terminal_market_snapshot([
            {"id_commodity": 2, "id_terminal": 1, "commodity_name": "Cobalt", "terminal_name": "Buy",
             "price_buy": 20, "price_sell": 0, "scu_buy": 95, "scu_sell": 0, "status_buy": 1, "status_sell": None},
            {"id_commodity": 2, "id_terminal": sell_terminal, "commodity_name": "Cobalt", "terminal_name": "Sell",
             "price_buy": 0, "price_sell": 50, "scu_buy": 0, "scu_sell": 80, "status_buy": None, "status_sell": 1},
        ])
        cog, client, handler = _fallback_best_route_cog(
            db, vehicles=[{"name": "Ironclad Assault", "scu": 1440}], buy_scu=21, sell_scu=21)
        if branch == "uex_routes":
            fallback = handler

            def handler(request):
                if "commodities_routes" in request.url.path:
                    return httpx.Response(200, json={"status": "ok", "data": [{
                        "id_terminal_origin": 1, "id_terminal_destination": 101,
                        "origin_terminal_name": "Buy", "destination_terminal_name": "Sell",
                        "price_origin": 100, "price_destination": 200, "price_margin": 50, "price_roi": 100,
                        "distance": 5, "score": 100, "scu_origin": 21, "scu_destination": 21,
                        "status_origin": 1, "status_destination": 1, "profit": 100}]})
                return fallback(request)
        await client._client.aclose()
        client._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        interaction = _FakeInteraction(1)
        try:
            await cog.best_route.callback(cog, interaction, commodity="Taranite", ship="Ironclad Assault")
        finally:
            await client.aclose()
        return _all_embed_text(interaction)

    text = asyncio.run(run())
    hedges = [line for line in text.splitlines() if line.startswith("Hedge:")]
    # labelled as risky, not just offered (workflow audit 2026-09-30: no label, so it looked unflagged)
    assert hedges and "Cobalt" in hedges[0] and ILLEGAL_LABEL in hedges[0], text


# ---- the mixed-cargo commands -------------------------------------------------------------

POOL = [
    {"id_commodity": GOLD, "is_illegal": 0, "is_explosive": 0},
    {"id_commodity": WIDOW, "is_illegal": 1, "is_explosive": 0},
    {"id_commodity": QUANTAINIUM, "is_illegal": 0, "is_explosive": 1},
]


@pytest.mark.parametrize("command, builder, options", [
    ("mixed_routes", "build_mixed_routes", {}),
    ("multi_stop_route", "build_multi_stop_routes", {}),
    ("multi_stop_route", "build_multi_stop_routes", {"origin": "Origin"}),
    ("diminishing_returns", "sweep_budget_curve", {}),
])
def test_mixed_cargo_commands_search_every_commodity(tmp_path, monkeypatch, command, builder, options):
    searched = []

    def capture(rows, *args, **kwargs):
        searched.append(sorted(row["id_commodity"] for row in rows))
        return []

    monkeypatch.setattr(prices_module, builder, capture)

    async def run():
        saved = Database(tmp_path / "prefs.sqlite3", Fernet(Fernet.generate_key()))
        await saved.init()
        await save_old_risk_tolerance(saved, USER, "low")  # Low left out the most
        db = NS(get_default_ship=AsyncMock(return_value="Ship"),
                get_trading_preferences=saved.get_trading_preferences,
                get_mixed_route_market_rows=AsyncMock(return_value=[dict(row) for row in POOL]),
                resolve_terminal_id_by_name=AsyncMock(return_value=(1, "Origin")))
        uex = NS(get_vehicles=AsyncMock(return_value=[dict(name="Ship", scu=100, pad_type="M")]))
        cog = Prices.__new__(Prices)
        cog.bot = NS(db=db, uex=uex)
        interaction = _Interaction()
        await getattr(Prices, command).callback(cog, interaction, **options)
        return interaction.texts()

    text = asyncio.run(run())
    assert searched and all(ids == sorted([GOLD, WIDOW, QUANTAINIUM]) for ids in searched), searched
    assert "risk tolerance" not in text.lower()


async def _run_prices_command(tmp_path, market_rows, run_command):
    """A real Prices command against a real database and a fake UEX, with an old "medium"
    saved for the player and Cobalt flagged illegal."""
    from tests.test_route_send_shape import _FakeInteraction, _transport

    db = Database(tmp_path / "routes.sqlite3", Fernet(Fernet.generate_key()))
    await db.init()
    await db.record_terminal_market_snapshot(market_rows)
    await db.upsert_commodity_reference(STILERON_AND_ILLEGAL_COBALT)
    await save_old_risk_tolerance(db, USER, "medium")
    client = UexClient(app_token="test", base_url="https://uex.test")
    await client._client.aclose()
    client._client = httpx.AsyncClient(transport=_transport())
    cog = Prices.__new__(Prices)
    cog.bot = NS(db=db, uex=client, get_cog=lambda name: None)
    interaction = _FakeInteraction(USER)
    try:
        await run_command(cog, interaction)
    finally:
        await client.aclose()
    return _all_text(interaction.followup.sent)


@pytest.mark.parametrize("command", ["mixed_routes", "multi_stop_route"])
def test_mixed_cargo_routes_carry_flagged_cargo_with_its_label(tmp_path, command):
    """Real market rows and the real route search: Cobalt, flagged illegal, is part of the
    route and labelled. Under the old "medium" it was left out, and no route came back."""
    from tests.test_route_send_shape import _MIXED_ROUTES_ROWS, _MULTI_STOP_ROWS

    rows = _MIXED_ROUTES_ROWS if command == "mixed_routes" else _MULTI_STOP_ROWS
    text = asyncio.run(_run_prices_command(
        tmp_path, rows, lambda cog, interaction: getattr(cog, command).callback(cog, interaction, ship="TestShip")))
    assert "Cobalt" in text and ILLEGAL_LABEL in text, text
    assert "risk tolerance" not in text.lower()


def test_intelligence_brief_routes_carry_flagged_cargo_with_its_label(tmp_path):
    from tests.test_intelligence_brief_routes import _MIXED_ROUTES_ROWS, _make_cog

    async def run():
        cog, client = await _make_cog(tmp_path, "brief.sqlite3", _MIXED_ROUTES_ROWS, ship_scu=10)
        db = cog.bot.db
        await db.upsert_commodity_reference(STILERON_AND_ILLEGAL_COBALT)
        await save_old_risk_tolerance(db, USER, "medium")
        try:
            embed = await cog._routes_embed(
                "TestShip", await db.get_trading_preferences(USER), budget=None, space_only=None)
        finally:
            await client.aclose()
        return "\n".join([embed.description or "", embed.footer.text or "",
                          *(f"{field.name}\n{field.value}" for field in embed.fields)])

    text = asyncio.run(run())
    assert "Cobalt" in text and ILLEGAL_LABEL in text, text
    assert "risk tolerance" not in text.lower()


# ---- a tracking thread's hedge suggestion -------------------------------------------------

def test_a_tracking_threads_hedge_offers_flagged_cargo_with_its_label(tmp_path):
    from tests.test_route_progression import (
        _create_thread_for_legs,
        _fake_thread_channel,
        _leg_input,
        _make_db,
        _seed_market_row,
    )

    async def run():
        db = _make_db(tmp_path)
        await db.init()
        await _seed_market_row(db)
        await db.upsert_commodity_reference([{"id": 2, "name": "WiDoW", **NO_FLAGS, "is_illegal": 1}])
        await save_old_risk_tolerance(db, 1, "medium")  # user 1 owns the thread
        await db.record_terminal_market_snapshot([
            {"id_commodity": 2, "id_terminal": 10, "commodity_name": "WiDoW", "terminal_name": "Area18 TDD",
             "price_buy": 20, "price_sell": 0, "scu_buy": 95, "scu_sell": 0, "status_buy": 1, "status_sell": None},
            {"id_commodity": 2, "id_terminal": 20, "commodity_name": "WiDoW", "terminal_name": "Elsewhere",
             "price_buy": 0, "price_sell": 50, "scu_buy": 0, "scu_sell": 80, "status_buy": None, "status_sell": 1},
        ])
        leg0 = _leg_input(id_terminal=10, id_commodity=1, quoted_price=100.0, quoted_scu=50.0, quoted_status=3)
        leg1 = _leg_input(side="sell", id_terminal=20, id_commodity=1, terminal_name="Elsewhere",
                          display_label="Sell Gold at Elsewhere", quoted_price=90.0, quoted_scu=40.0, quoted_status=2)
        await _create_thread_for_legs(db, 1, [leg0, leg1])
        cog = RouteProgression.__new__(RouteProgression)
        cog.bot = NS(db=db)
        cog._active_legs = {1: [leg0, leg1]}
        channel = _fake_thread_channel()
        await cog.handle_leg_outcome(channel, 1, 0, leg0, outcome="missing")
        return channel.send.call_args_list[0].args[0]

    message = asyncio.run(run())
    assert "WiDoW" in message and "this could fill it" in message, message
    assert ILLEGAL_LABEL in message, message
    assert "risk tolerance" not in message.lower()
