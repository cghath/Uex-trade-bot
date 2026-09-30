"""Audit UX-9: the saved risk tolerance (/set-trading-preferences) was stored and shown but
did nothing - a player with "Medium - avoid illegal or buggy goods" still got illegal
goods in every route. Route commands now leave out what each level says it avoids.

- Low: illegal, explosive, volatile (quantum or over time) and buggy goods.
- Medium: illegal and buggy goods.
- High, or no preference: nothing.

/best-route is the exception: the player names the commodity, so it's shown anyway, with a
note that it's outside their tolerance."""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock

import pytest
from cryptography.fernet import Fernet

from bot.cogs import prices as prices_module
from bot.cogs.prices import Prices
from bot.cogs.route_progression import RouteProgression
from bot.cogs.trends import Trends
from bot.uex.commodity_risk import (
    RISK_FLAG_KEYS,
    outside_risk_tolerance,
    within_risk_tolerance,
)
from bot.uex.trading_preferences import (
    DEFAULT_TRADING_PREFERENCES,
    describe_active_preferences,
    format_trading_preferences,
    risk_tolerance_hint,
)
from bot.uex.trends import ScoredRouteEntry
from tests.route_results import route_results
from tests.test_route_messages import USER, _client, _db, _Interaction
from tests.test_route_progression import (
    _create_thread_for_legs,
    _fake_thread_channel,
    _leg_input,
    _make_db,
    _seed_market_row,
)

GOLD, WIDOW, QUANTAINIUM = 5, 6, 7
COMMODITIES = [
    {"id": GOLD, "name": "Gold", **{key: 0 for key in RISK_FLAG_KEYS}},
    {"id": WIDOW, "name": "WiDoW", **{key: 0 for key in RISK_FLAG_KEYS}, "is_illegal": 1},
    {"id": QUANTAINIUM, "name": "Quantainium", **{key: 0 for key in RISK_FLAG_KEYS}, "is_explosive": 1},
]


# ---- what each level leaves out --------------------------------------------------------

@pytest.mark.parametrize("flag", RISK_FLAG_KEYS)
def test_low_leaves_out_every_risk_flag(flag):
    assert outside_risk_tolerance({flag: 1}, "low")


@pytest.mark.parametrize("flag, left_out", [
    ("is_illegal", True), ("is_buggy", True),
    ("is_explosive", False), ("is_volatile_qt", False), ("is_volatile_time", False),
])
def test_medium_leaves_out_only_illegal_and_buggy_goods(flag, left_out):
    assert outside_risk_tolerance({flag: 1}, "medium") is left_out


@pytest.mark.parametrize("tolerance", ["high", None])
def test_high_or_no_preference_leaves_out_nothing(tolerance):
    assert not outside_risk_tolerance({key: 1 for key in RISK_FLAG_KEYS}, tolerance)


def test_a_commodity_with_no_collected_flags_is_kept():
    """Routes already warn that its risk is unknown; hiding it would be a guess."""
    assert not outside_risk_tolerance(None, "low")
    assert not outside_risk_tolerance({"is_illegal": None}, "low")


def test_market_rows_are_filtered_by_their_own_flags():
    rows = [{"id_commodity": GOLD, "is_illegal": 0}, {"id_commodity": WIDOW, "is_illegal": 1}]
    assert [r["id_commodity"] for r in within_risk_tolerance(rows, "medium")] == [GOLD]
    assert within_risk_tolerance(rows, "high") is rows


def test_the_preference_no_longer_says_it_isnt_enforced():
    assert describe_active_preferences(risk_tolerance="medium") == "Filters: risk tolerance: medium (saved)"
    shown = format_trading_preferences(dict(DEFAULT_TRADING_PREFERENCES, risk_tolerance="medium"))
    assert "Risk tolerance: **medium** (route suggestions skip illegal and buggy goods)" in shown
    assert "not yet enforced" not in shown
    assert risk_tolerance_hint("low") == (
        " Your saved risk tolerance (low) skips illegal, explosive, volatile and buggy goods - "
        "change it with /set-trading-preferences.")
    assert risk_tolerance_hint("high") == ""


# ---- /top-routes (and /routes-from, /route-on-the-way, which share its ranking) ----------

def _entry(id_commodity: int, name: str) -> ScoredRouteEntry:
    return ScoredRouteEntry(
        commodity_name=name, id_commodity=id_commodity, origin_terminal_name="Origin",
        destination_terminal_name="Dest", price_origin=10.0, price_destination=60.0, price_margin=None,
        price_roi=None, distance=None, score=50, scu_origin=50, scu_destination=50, status_origin=1,
        status_destination=1, origin_terminal_id=1, destination_terminal_id=2,
    )


def _everything_sent(interaction: _Interaction) -> str:
    results = route_results(interaction.sent)
    return "\n".join([interaction.texts(), results.all_text() if results else ""])


async def _top_routes(tmp_path, tolerance, entries):
    db = await _db(tmp_path)
    await db.upsert_commodity_reference(COMMODITIES)
    await db.set_trading_preferences(USER, risk_tolerance=tolerance)
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
    return _everything_sent(interaction)


def test_top_routes_leaves_out_goods_outside_the_saved_tolerance(tmp_path):
    entries = [_entry(WIDOW, "WiDoW"), _entry(QUANTAINIUM, "Quantainium"), _entry(GOLD, "Gold")]
    medium = asyncio.run(_top_routes(tmp_path / "m", "medium", entries))
    assert "Gold" in medium and "Quantainium" in medium and "WiDoW" not in medium
    assert "risk tolerance: medium" in medium
    high = asyncio.run(_top_routes(tmp_path / "h", "high", entries))
    assert "WiDoW" in high


def test_top_routes_says_the_saved_tolerance_is_why_nothing_came_back(tmp_path):
    text = asyncio.run(_top_routes(tmp_path, "low", [_entry(WIDOW, "WiDoW"), _entry(QUANTAINIUM, "Quantainium")]))
    assert "No routes within your risk tolerance found right now." in text
    assert "Your saved risk tolerance (low) skips" in text


# ---- /best-route: named, so shown, with a note -------------------------------------------

def test_best_route_shows_a_named_commodity_outside_the_tolerance_with_a_note(tmp_path):
    async def run():
        db = await _db(tmp_path)
        await db.upsert_commodity_reference(COMMODITIES)
        await db.set_trading_preferences(USER, risk_tolerance="medium")
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
        return _everything_sent(interaction)

    text = asyncio.run(run())
    assert "Origin" in text and "Dest" in text, "the route itself is still shown"
    assert "Outside your saved risk tolerance (medium) - other route commands leave it out." in text


# ---- the mixed-cargo commands filter their whole pool ------------------------------------

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
def test_mixed_cargo_commands_search_only_goods_within_the_tolerance(monkeypatch, command, builder, options):
    searched = []

    def capture(rows, *args, **kwargs):
        searched.append([row["id_commodity"] for row in rows])
        return []

    monkeypatch.setattr(prices_module, builder, capture)

    async def run():
        prefs = dict(DEFAULT_TRADING_PREFERENCES, risk_tolerance="low")
        db = NS(get_default_ship=AsyncMock(return_value="Ship"), get_trading_preferences=AsyncMock(return_value=prefs),
                get_mixed_route_market_rows=AsyncMock(return_value=[dict(row) for row in POOL]),
                resolve_terminal_id_by_name=AsyncMock(return_value=(1, "Origin")))
        uex = NS(get_vehicles=AsyncMock(return_value=[dict(name="Ship", scu=100, pad_type="M")]))
        cog = Prices.__new__(Prices)
        cog.bot = NS(db=db, uex=uex)
        interaction = _Interaction()
        await getattr(Prices, command).callback(cog, interaction, **options)
        return interaction.texts()

    text = asyncio.run(run())
    assert searched and all(ids == [GOLD] for ids in searched)
    assert "Your saved risk tolerance (low) skips" in text


# ---- a tracking thread's hedge suggestion ------------------------------------------------

def test_a_tracking_threads_hedge_respects_the_owners_tolerance(tmp_path):
    async def run():
        db = _make_db(tmp_path)
        await db.init()
        await _seed_market_row(db)
        await db.upsert_commodity_reference([{"id": 2, "name": "WiDoW", "is_illegal": 1}])
        await db.set_trading_preferences(1, risk_tolerance="medium")  # user 1 owns the thread
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
    assert "WiDoW" not in message
    assert "nothing else trades" in message and "Your saved risk tolerance (medium) skips" in message


# ---- "Hedge:" lines offer a different commodity, so they respect the tolerance too --------

COBALT_ILLEGAL = [{"id": 2, "name": "Cobalt", **{key: 0 for key in RISK_FLAG_KEYS}, "is_illegal": 1}]


def test_a_ranked_routes_hedge_line_respects_the_tolerance(tmp_path):
    from tests.test_ranked_routes_hedge import _make_cog, _route, _route_text, _send

    async def run(tolerance):
        cog, db = await _make_cog(tmp_path / tolerance)
        await db.upsert_commodity_reference(COBALT_ILLEGAL)
        return _route_text(await _send(cog, [_route()], risk_tolerance=tolerance))

    assert "Hedge:" in asyncio.run(run("high"))
    text = asyncio.run(run("medium"))
    assert "Uses the entire stock/demand currently on record" in text and "Hedge:" not in text


@pytest.mark.parametrize("branch", ["uex_routes", "price_rows"])
def test_best_routes_hedge_line_respects_the_tolerance(tmp_path, branch):
    import httpx

    from bot.db.database import Database
    from tests.test_route_send_shape import (
        _all_embed_text,
        _FakeInteraction,
        _fallback_best_route_cog,
    )

    async def run(tolerance):
        db = Database(tmp_path / f"{branch}_{tolerance}.sqlite3", Fernet(Fernet.generate_key()))
        await db.init()
        await db.upsert_commodity_reference(COBALT_ILLEGAL)
        await db.set_trading_preferences(1, risk_tolerance=tolerance)
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

    assert "Hedge:" in asyncio.run(run("high"))
    text = asyncio.run(run("medium"))
    assert "/mixed-routes" in text and "Hedge:" not in text
