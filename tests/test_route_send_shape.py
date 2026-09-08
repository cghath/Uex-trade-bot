"""Regression test for a real incident: a fix meant for /multi-stop-route's embed-size
bug was misapplied during editing and landed in /mixed-routes instead (both commands end
with structurally similar boilerplate, and the edit's anchor text matched the wrong
one). /mixed-routes ended up with dead code referencing undefined variables in an
unreachable except branch, while /multi-stop-route still batched all routes into one
oversized message - the exact shape that caused the original "stuck thinking" bug, live,
through a full test run and a deploy, undetected. These tests pin down the actual
send-call shape each command must use, not just whether a response was sent at all.
"""
from __future__ import annotations

import asyncio
import threading
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock

from cryptography.fernet import Fernet
import discord
import httpx

from bot.cogs import prices as prices_module
from bot.cogs.prices import Prices
from bot.cogs.route_progression import RouteProgression, RouteTrackingView
from bot.db.database import Database
from bot.uex.client import UexClient
from bot.uex.mixed_routes import MixedCargoItem, MixedRoute
from bot.uex.multi_stop_routes import MultiStopLeg, MultiStopRoute
from bot.uex.trading_preferences import DEFAULT_TRADING_PREFERENCES


class _FakeResponse:
    async def defer(self, **kwargs):
        pass


class _FakeFollowup:
    def __init__(self):
        self.sent = []

    async def send(self, *args, **kwargs):
        self.sent.append((args, kwargs))


class _FakeInteraction:
    def __init__(self, user_id):
        self.user = type("U", (), {"id": user_id})()
        self.response = _FakeResponse()
        self.followup = _FakeFollowup()


_MULTI_STOP_ROWS = [
    {"id_commodity": 1, "id_terminal": 1, "commodity_name": "Stileron", "terminal_name": "Origin",
     "price_buy": 100, "price_sell": 0, "scu_buy": 10, "scu_sell": 0, "status_buy": 1, "status_sell": None},
    {"id_commodity": 1, "id_terminal": 2, "commodity_name": "Stileron", "terminal_name": "Midpoint",
     "price_buy": 0, "price_sell": 150, "scu_buy": 0, "scu_sell": 10, "status_buy": None, "status_sell": 1},
    {"id_commodity": 2, "id_terminal": 2, "commodity_name": "Cobalt", "terminal_name": "Midpoint",
     "price_buy": 50, "price_sell": 0, "scu_buy": 10, "scu_sell": 0, "status_buy": 1, "status_sell": None},
    {"id_commodity": 2, "id_terminal": 3, "commodity_name": "Cobalt", "terminal_name": "Final",
     "price_buy": 0, "price_sell": 90, "scu_buy": 0, "scu_sell": 10, "status_buy": None, "status_sell": 1},
]

# /mixed-routes needs 2+ commodities profitable at the SAME origin/destination pair
# (unlike multi-stop, which chains separate pairs) - a different shape of fixture.
_MIXED_ROUTES_ROWS = [
    {"id_commodity": 1, "id_terminal": 1, "commodity_name": "Stileron", "terminal_name": "Origin",
     "price_buy": 100, "price_sell": 0, "scu_buy": 4, "scu_sell": 0, "status_buy": 1, "status_sell": None},
    {"id_commodity": 1, "id_terminal": 2, "commodity_name": "Stileron", "terminal_name": "Destination",
     "price_buy": 0, "price_sell": 200, "scu_buy": 0, "scu_sell": 10, "status_buy": None, "status_sell": 1},
    {"id_commodity": 2, "id_terminal": 1, "commodity_name": "Cobalt", "terminal_name": "Origin",
     "price_buy": 20, "price_sell": 0, "scu_buy": 95, "scu_sell": 0, "status_buy": 1, "status_sell": None},
    {"id_commodity": 2, "id_terminal": 2, "commodity_name": "Cobalt", "terminal_name": "Destination",
     "price_buy": 0, "price_sell": 50, "scu_buy": 0, "scu_sell": 80, "status_buy": None, "status_sell": 1},
]


def _transport() -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if "vehicles" in path:
            return httpx.Response(200, json={"status": "ok", "data": [{"name": "TestShip", "scu": 10, "pad_type": "M"}]})
        if "terminals_distances" in path:
            return httpx.Response(200, json={"status": "ok", "data": {"distance": 1.0}})
        return httpx.Response(200, json={"status": "ok", "data": []})

    return httpx.MockTransport(handler)


async def _run_command(tmp_path, db_name, market_rows, coro_factory):
    db = Database(tmp_path / db_name, Fernet(Fernet.generate_key()))
    await db.init()
    await db.record_terminal_market_snapshot(market_rows)

    client = UexClient(app_token="test", base_url="https://uex.test")
    await client._client.aclose()
    client._client = httpx.AsyncClient(transport=_transport())

    bot = type("FakeBot", (), {})()
    bot.db = db
    bot.uex = client
    bot.get_cog = lambda name: None
    cog = Prices.__new__(Prices)
    cog.bot = bot
    interaction = _FakeInteraction(111)

    try:
        await coro_factory(cog, interaction)
        return interaction
    finally:
        await client.aclose()


def test_multi_stop_route_sends_one_message_per_route_not_batched(tmp_path):
    async def run():
        interaction = await _run_command(
            tmp_path, "multi_stop.sqlite3", _MULTI_STOP_ROWS,
            lambda cog, interaction: cog.multi_stop_route.callback(cog, interaction, ship="TestShip"),
        )
        assert interaction.followup.sent, "expected at least one followup"
        for args, kwargs in interaction.followup.sent:
            assert "embeds" not in kwargs, (
                "must send one embed per route via 'embed=', not a batched 'embeds=' list - "
                "that batched shape is what caused the original stuck-thinking bug"
            )
            assert kwargs.get("embed") is not None or "content" in kwargs

    asyncio.run(run())


def test_diminishing_returns_sends_a_chart_embed_with_a_plateau_note(tmp_path):
    async def run():
        interaction = await _run_command(
            tmp_path, "diminishing_returns.sqlite3", _MULTI_STOP_ROWS,
            lambda cog, interaction: cog.diminishing_returns.callback(cog, interaction, ship="TestShip"),
        )
        assert interaction.followup.sent, "expected at least one followup"
        # First followup is the "running a sweep" status message; the sweep's own real
        # stock/demand (10 SCU per leg in this fixture) should plateau within a couple of
        # budget checkpoints, so the final followup carries the actual chart embed.
        final_args, final_kwargs = interaction.followup.sent[-1]
        assert "embed" in final_kwargs, interaction.followup.sent
        assert "file" in final_kwargs
        embed = final_kwargs["embed"]
        assert "Diminishing returns begin around" in embed.description

    asyncio.run(run())


class _EmbedTooLargeFollowup(_FakeFollowup):
    """Simulates Discord rejecting the embed (too large) so the plain-text fallback path
    in multi_stop_route actually runs, the same way a real oversized route would."""

    async def send(self, *args, **kwargs):
        if "embed" in kwargs:
            response = type("R", (), {"status": 400, "reason": "Bad Request", "headers": {}})()
            raise discord.HTTPException(response, {"message": "Embed size exceeds maximum size of 6000"})
        await super().send(*args, **kwargs)


def test_multi_stop_route_fallback_preserves_warnings(tmp_path):
    """Regression: the fallback text (sent when the real embed is rejected as too large)
    only carried summary_lines (investment/revenue/profit/ROI/distance/confidence) -
    warnings (risk flags, stock/demand limits, practical notes) were silently dropped.
    A stock-limited leg (5 SCU available vs a 10-SCU ship) must produce a real warning
    that survives into the fallback content, not just the profit figures."""
    async def run():
        db = Database(tmp_path / "multi_stop_fallback.sqlite3", Fernet(Fernet.generate_key()))
        await db.init()
        rows = [
            {"id_commodity": 1, "id_terminal": 1, "commodity_name": "Stileron", "terminal_name": "Origin",
             "price_buy": 100, "price_sell": 0, "scu_buy": 5, "scu_sell": 0, "status_buy": 1, "status_sell": None},
            {"id_commodity": 1, "id_terminal": 2, "commodity_name": "Stileron", "terminal_name": "Midpoint",
             "price_buy": 0, "price_sell": 150, "scu_buy": 0, "scu_sell": 5, "status_buy": None, "status_sell": 1},
            {"id_commodity": 2, "id_terminal": 2, "commodity_name": "Cobalt", "terminal_name": "Midpoint",
             "price_buy": 50, "price_sell": 0, "scu_buy": 10, "scu_sell": 0, "status_buy": 1, "status_sell": None},
            {"id_commodity": 2, "id_terminal": 3, "commodity_name": "Cobalt", "terminal_name": "Final",
             "price_buy": 0, "price_sell": 90, "scu_buy": 0, "scu_sell": 10, "status_buy": None, "status_sell": 1},
        ]
        await db.record_terminal_market_snapshot(rows)

        client = UexClient(app_token="test", base_url="https://uex.test")
        await client._client.aclose()
        client._client = httpx.AsyncClient(transport=_transport())

        bot = type("FakeBot", (), {})()
        bot.db = db
        bot.uex = client
        bot.get_cog = lambda name: None
        cog = Prices.__new__(Prices)
        cog.bot = bot
        interaction = _FakeInteraction(111)
        interaction.followup = _EmbedTooLargeFollowup()

        try:
            await cog.multi_stop_route.callback(cog, interaction, ship="TestShip")

            assert interaction.followup.sent, "expected at least one followup"
            for args, kwargs in interaction.followup.sent:
                assert "embed" not in kwargs, "the embed send should have been rejected, not succeeded"
            fallback_text = "\n".join(kwargs["content"] for _, kwargs in interaction.followup.sent)
            assert "Stileron: limited by stock" in fallback_text, (
                f"expected the stock-limit explanation to survive into the fallback, got: {fallback_text!r}"
            )
        finally:
            await client.aclose()

    asyncio.run(run())


def test_mixed_routes_discloses_when_cargo_allocation_is_approximate(tmp_path):
    """A ship above EXACT_SEARCH_MAX_CAPACITY only gets a capped exact solve plus a
    heuristic for the rest (see mixed_routes.allocate_pair_cargo) - the "five best"
    recommendation isn't a proven optimum in that case, and the footer must say so."""
    async def run():
        db = Database(tmp_path / "mixed_routes_disclosure.sqlite3", Fernet(Fernet.generate_key()))
        await db.init()
        await db.record_terminal_market_snapshot(_MIXED_ROUTES_ROWS)

        def handler(request: httpx.Request) -> httpx.Response:
            path = request.url.path
            if "vehicles" in path:
                return httpx.Response(200, json={"status": "ok", "data": [{"name": "BigShip", "scu": 30, "pad_type": "M"}]})
            return httpx.Response(200, json={"status": "ok", "data": []})

        client = UexClient(app_token="test", base_url="https://uex.test")
        await client._client.aclose()
        client._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))

        bot = type("FakeBot", (), {})()
        bot.db = db
        bot.uex = client
        bot.get_cog = lambda name: None
        cog = Prices.__new__(Prices)
        cog.bot = bot
        interaction = _FakeInteraction(111)

        try:
            await cog.mixed_routes.callback(cog, interaction, ship="BigShip")

            assert interaction.followup.sent, "expected at least one followup"
            _, kwargs = interaction.followup.sent[0]
            embed = kwargs.get("embed")
            assert embed is not None, f"expected an embed response, got: {interaction.followup.sent}"
            footer_text = embed.footer.text or ""
            assert "approximate" in footer_text.lower(), f"expected an approximation disclosure, got footer: {footer_text!r}"
        finally:
            await client.aclose()

    asyncio.run(run())


def test_mixed_routes_offloads_cargo_allocation_to_a_worker_thread(tmp_path, monkeypatch):
    """Regression: cargo allocation (see allocate_pair_cargo) can run a real, sometimes
    expensive combinatorial search - a dense enough market snapshot measured at ~15s for
    an 8-terminal/8-commodity case. Calling it directly on the coroutine handling the
    interaction would run that on the bot's one asyncio event loop thread, freezing every
    other interaction and background poller for as long as it takes. Checked directly
    (not via timing, which can pass by accident from unrelated awaits earlier in the
    command): the actual thread build_mixed_routes runs on must not be the main/event-loop
    thread, meaning the call went through asyncio.to_thread."""
    async def run():
        called_from_thread = {}
        real_build = prices_module.build_mixed_routes

        def spy(*args, **kwargs):
            called_from_thread["thread"] = threading.current_thread()
            return real_build(*args, **kwargs)

        monkeypatch.setattr(prices_module, "build_mixed_routes", spy)

        interaction = await _run_command(
            tmp_path, "mixed_routes_thread.sqlite3", _MIXED_ROUTES_ROWS,
            lambda cog, interaction: cog.mixed_routes.callback(cog, interaction, ship="TestShip"),
        )
        assert interaction.followup.sent, "expected at least one followup"
        assert called_from_thread.get("thread") is not None, "build_mixed_routes was never called"
        assert called_from_thread["thread"] is not threading.main_thread(), (
            "build_mixed_routes ran on the main/event-loop thread - it must be offloaded "
            "via asyncio.to_thread so it can't block the bot's one event loop"
        )

    asyncio.run(run())


def test_multi_stop_route_offloads_cargo_allocation_to_a_worker_thread(tmp_path, monkeypatch):
    """Same regression as the /mixed-routes version above, for /multi-stop-route's
    build_multi_stop_routes - its DFS can call the same exact allocator far more often
    per command, making the offload matter even more here."""
    async def run():
        called_from_thread = {}
        real_build = prices_module.build_multi_stop_routes

        def spy(*args, **kwargs):
            called_from_thread["thread"] = threading.current_thread()
            return real_build(*args, **kwargs)

        monkeypatch.setattr(prices_module, "build_multi_stop_routes", spy)

        interaction = await _run_command(
            tmp_path, "multi_stop_thread.sqlite3", _MULTI_STOP_ROWS,
            lambda cog, interaction: cog.multi_stop_route.callback(cog, interaction, ship="TestShip"),
        )
        assert interaction.followup.sent, "expected at least one followup"
        assert called_from_thread.get("thread") is not None, "build_multi_stop_routes was never called"
        assert called_from_thread["thread"] is not threading.main_thread(), (
            "build_multi_stop_routes ran on the main/event-loop thread - it must be "
            "offloaded via asyncio.to_thread so it can't block the bot's one event loop"
        )

    asyncio.run(run())


def test_multi_stop_route_fallback_preserves_approximation_disclosure(tmp_path):
    """Regression: the "cargo allocation is approximate" disclosure lived only in the
    embed footer - a route whose allocation is approximate but whose embed is rejected as
    too large silently lost that disclosure in the plain-text fallback."""
    async def run():
        db = Database(tmp_path / "multi_stop_fallback_disclosure.sqlite3", Fernet(Fernet.generate_key()))
        await db.init()
        await db.record_terminal_market_snapshot(_MULTI_STOP_ROWS)

        def handler(request: httpx.Request) -> httpx.Response:
            path = request.url.path
            if "vehicles" in path:
                # scu (30) exceeds EXACT_SEARCH_MAX_CAPACITY (25), making this route's
                # cargo allocation approximate, not proven-optimal.
                return httpx.Response(200, json={"status": "ok", "data": [{"name": "BigShip", "scu": 30, "pad_type": "M"}]})
            if "terminals_distances" in path:
                return httpx.Response(200, json={"status": "ok", "data": {"distance": 1.0}})
            return httpx.Response(200, json={"status": "ok", "data": []})

        client = UexClient(app_token="test", base_url="https://uex.test")
        await client._client.aclose()
        client._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))

        bot = type("FakeBot", (), {})()
        bot.db = db
        bot.uex = client
        bot.get_cog = lambda name: None
        cog = Prices.__new__(Prices)
        cog.bot = bot
        interaction = _FakeInteraction(111)
        interaction.followup = _EmbedTooLargeFollowup()

        try:
            await cog.multi_stop_route.callback(cog, interaction, ship="BigShip")

            fallback_text = "\n".join(kwargs["content"] for _, kwargs in interaction.followup.sent)
            assert "approximate" in fallback_text.lower(), (
                f"expected the approximation disclosure to survive into the fallback, got: {fallback_text!r}"
            )
        finally:
            await client.aclose()

    asyncio.run(run())


def test_multi_stop_route_falls_back_to_plain_text_when_only_the_warnings_section_overflows(monkeypatch):
    """Follow-up review finding: _add_chunked_fields' A08 fix (see test_prices_chunked_
    fields.py) made adding a logical field all-or-nothing, which is exactly right for a
    per-leg field - but /multi-stop-route also uses it for one call covering the ENTIRE
    accumulated warnings section, and ignored its return value. If the leg fields + route
    summary already consume most of the budget, the warnings section can fail to fit
    entirely - the function then adds NOTHING, the route embed (legs + summary, no
    warnings) is still small enough to send successfully, and every cargo-risk/cross-
    system warning silently vanishes with no exception ever raised to trigger the existing
    too-large fallback. Fixed by checking the warnings call's own return value and
    manually entering the same plain-text fallback (which independently rebuilds the full
    warning list) when it comes back False, exactly as if the whole embed had been
    rejected. Reproduced here with a controlled 3-leg, 3-commodity-per-leg route - not
    real UEX data - built to force this specific budget interaction, per the review's own
    approach."""
    async def run():
        legs = []
        for leg in range(3):
            source = dict(
                scu_buy=10, status_buy=1, is_illegal=1, is_explosive=1, is_volatile_time=1,
                is_volatile_qt=1, is_buggy=1, max_container_size=8, has_freight_elevator=0,
                has_loading_dock=0, is_player_owned=1, is_refuel=1, is_repair=1, is_cargo_center=1,
                star_system_name="Stanton" if leg % 2 == 0 else "Pyro",
            )
            destination = dict(
                source, scu_sell=10, status_sell=1,
                star_system_name="Pyro" if leg % 2 == 0 else "Stanton",
            )
            cargo = tuple(
                MixedCargoItem(
                    i, f"Commodity {i}", 10, 100, 200, 10, 1000, 1000, source, destination,
                    limiting_factors=("stock", "demand"),
                )
                for i in range(1, 4)
            )
            legs.append(MultiStopLeg(leg + 1, f"Station {leg + 1}", leg + 2, f"Station {leg + 2}", cargo, 3000, 6000, 3000, True))
        route = MultiStopRoute(tuple(legs), 3000, 12000, 9000)
        monkeypatch.setattr(prices_module, "build_multi_stop_routes", lambda *a, **k: [route])

        bot = type("FakeBot", (), {})()
        bot.db = type("FakeDb", (), {})()
        bot.db.get_default_ship = AsyncMock(return_value="Ship")
        bot.db.get_trading_preferences = AsyncMock(return_value=dict(DEFAULT_TRADING_PREFERENCES))
        bot.db.get_mixed_route_market_rows = AsyncMock(return_value=[])
        bot.db.get_terminal_data_health_by_ids = AsyncMock(return_value={
            i: dict(
                terminal_name=f"Station {i}", last_update_days=5, last_update_days_limit=3,
                last_update_days_percentage=0, prices_updated_percentage=0,
            )
            for i in range(1, 5)
        })
        bot.uex = type("FakeUex", (), {})()
        bot.uex.get_vehicles = AsyncMock(return_value=[dict(name="Ship", scu=100)])
        bot.uex.get_terminal_distance = AsyncMock(return_value=dict(distance=10))
        bot.get_cog = lambda name: None
        cog = Prices.__new__(Prices)
        cog.bot = bot
        cog._get_status_lookup = AsyncMock(return_value={
            "buy": {1: dict(name_short="High Supply")}, "sell": {1: dict(name_short="Low Inventory")},
        })
        interaction = _FakeInteraction(1)

        await cog.multi_stop_route.callback(cog, interaction)

        assert interaction.followup.sent, "expected at least one followup"
        for _, kwargs in interaction.followup.sent:
            assert "embed" not in kwargs, "an embed missing its warnings must not be sent as if complete"
        fallback_text = "\n".join(kwargs.get("content", "") for _, kwargs in interaction.followup.sent)
        assert "Cargo risk:" in fallback_text, fallback_text
        assert "crosses systems" in fallback_text, fallback_text

    asyncio.run(run())


def test_multi_stop_route_attaches_a_track_button_with_flattened_legs(monkeypatch):
    """/multi-stop-route wiring: a chain leg carries several commodities at once
    (allocate_pair_cargo's mixed load), unlike /best-route's single-commodity leg - each
    is flattened into one buy + one sell progression-leg per commodity per hop, in order,
    so the existing leg-by-leg cog can walk a chain exactly the same way it already walks
    /best-route's simpler 2-leg case."""
    async def run():
        source = dict(scu_buy=10, status_buy=1, star_system_name="Stanton")
        destination = dict(scu_sell=10, status_sell=1, star_system_name="Stanton")
        cargo = (
            MixedCargoItem(1, "Gold", 5, 100, 200, 10, 500, 500, source, destination, limiting_factors=("stock",)),
            MixedCargoItem(2, "Cobalt", 3, 50, 90, 10, 150, 120, source, destination, limiting_factors=("stock",)),
        )
        leg = MultiStopLeg(10, "Station A", 20, "Station B", cargo, 650, 1270, 620, True)
        route = MultiStopRoute((leg,), 650, 1270, 620)
        monkeypatch.setattr(prices_module, "build_multi_stop_routes", lambda *a, **k: [route])

        bot = type("FakeBot", (), {})()
        bot.db = type("FakeDb", (), {})()
        bot.db.get_default_ship = AsyncMock(return_value="Ship")
        bot.db.get_trading_preferences = AsyncMock(return_value=dict(DEFAULT_TRADING_PREFERENCES))
        bot.db.get_mixed_route_market_rows = AsyncMock(return_value=[])
        bot.db.get_terminal_data_health_by_ids = AsyncMock(return_value={})
        bot.uex = type("FakeUex", (), {})()
        bot.uex.get_vehicles = AsyncMock(return_value=[dict(name="Ship", scu=100)])
        bot.uex.get_terminal_distance = AsyncMock(return_value=dict(distance=10))
        tracking_cog = RouteProgression.__new__(RouteProgression)
        bot.get_cog = lambda name: tracking_cog if name == "RouteProgression" else None
        cog = Prices.__new__(Prices)
        cog.bot = bot
        cog._get_status_lookup = AsyncMock(return_value={"buy": {}, "sell": {}})
        interaction = _FakeInteraction(1)

        await cog.multi_stop_route.callback(cog, interaction)

        assert interaction.followup.sent, "expected at least one followup"
        _, kwargs = interaction.followup.sent[0]
        view = kwargs.get("view")
        assert isinstance(view, RouteTrackingView)
        assert len(view.children) == 1, "one route -> one tracking button"

        legs = view.routes[0].legs
        assert [progression_leg.display_label for progression_leg in legs] == [
            "Buy Gold at Station A", "Buy Cobalt at Station A",
            "Sell Gold at Station B", "Sell Cobalt at Station B",
        ], legs
        assert legs[0].id_terminal == 10 and legs[0].side == "buy"
        assert legs[2].id_terminal == 20 and legs[2].side == "sell"
        assert legs[0].quoted_scu == 5 and legs[0].quoted_price == 100
        # market_scu (the real quoted market availability) travels separately from
        # quoted_scu (this hop's cargo allocation) - see terminal_state_update_for_outcome.
        assert legs[0].market_scu == 10 and legs[1].market_scu == 10

    asyncio.run(run())


def test_best_route_discloses_when_routes_are_truncated_for_size(tmp_path, monkeypatch):
    """Second follow-up review finding: /best-route's primary branch (UEX's own
    /commodities_routes data) calls the atomic _add_chunked_fields for each ranked route
    but never checks its return value or discloses a truncation, unlike /top-routes
    (trends.py) which stops and appends an "N more omitted" footer note. A route that
    silently failed to fit would just vanish with no visible sign anything was omitted.
    Forces the second of three ranked routes to fail deterministically."""
    async def run():
        db = Database(tmp_path / "best_route_budget.sqlite3", Fernet(Fernet.generate_key()))
        await db.init()

        def handler(request: httpx.Request) -> httpx.Response:
            path = request.url.path
            if "commodities_prices" in path:
                return httpx.Response(200, json={"status": "ok", "data": [
                    {"id_commodity": 1, "commodity_name": "Gold"}
                ]})
            if "commodities_routes" in path:
                return httpx.Response(200, json={"status": "ok", "data": [
                    {
                        "id_terminal_origin": i, "id_terminal_destination": i + 100,
                        "origin_terminal_name": f"Origin {i}", "destination_terminal_name": f"Destination {i}",
                        "price_origin": 100, "price_destination": 200, "price_margin": 50, "price_roi": 100,
                        "distance": 5, "score": 100 - i, "scu_origin": 10, "scu_destination": 10,
                        "status_origin": 1, "status_destination": 1, "profit": 100 - i,
                    }
                    for i in range(1, 4)
                ]})
            return httpx.Response(200, json={"status": "ok", "data": []})

        client = UexClient(app_token="test", base_url="https://uex.test")
        await client._client.aclose()
        client._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))

        bot = type("FakeBot", (), {})()
        bot.db = db
        bot.uex = client
        bot.get_cog = lambda name: None
        cog = Prices.__new__(Prices)
        cog.bot = bot
        interaction = _FakeInteraction(1)

        call_count = {"n": 0}
        real_add_chunked_fields = prices_module._add_chunked_fields

        def flaky_add_chunked_fields(embed, *, name, lines):
            call_count["n"] += 1
            if call_count["n"] == 2:
                return False
            return real_add_chunked_fields(embed, name=name, lines=lines)

        monkeypatch.setattr(prices_module, "_add_chunked_fields", flaky_add_chunked_fields)

        try:
            await cog.best_route.callback(cog, interaction, commodity="Gold")
        finally:
            await client.aclose()

        # Each route is now its own message: intro (no fields), route 1, route 3 (route 2
        # skipped - continue, no send at all), then a trailing "omitted" note.
        assert len(interaction.followup.sent) == 4, interaction.followup.sent
        _, route1_kwargs = interaction.followup.sent[1]
        assert route1_kwargs["embed"].title == "Origin 1 → Destination 1"
        _, route3_kwargs = interaction.followup.sent[2]
        assert route3_kwargs["embed"].title == "Origin 3 → Destination 3"
        omitted_args, _ = interaction.followup.sent[3]
        assert "omitted" in omitted_args[0].lower(), omitted_args

    asyncio.run(run())


def test_best_route_primary_branch_now_warns_on_a_cross_system_route(tmp_path):
    """Centralized Route Presentation: /best-route's primary branch (UEX's own
    /commodities_routes data, with a real distance figure) never had a cross-system
    warning at all - only its OWN fallback branch (no real distance data) did, and
    /top-routes had neither. Now both get one via the shared
    bot.uex.route_presentation.travel_warning."""
    async def run():
        db = Database(tmp_path / "best_route_cross_system.sqlite3", Fernet(Fernet.generate_key()))
        await db.init()
        await db.upsert_terminal_reference([
            {"id": 1, "name": "Origin 1", "star_system_name": "Stanton"},
            {"id": 101, "name": "Destination 1", "star_system_name": "Pyro"},
        ])

        def handler(request: httpx.Request) -> httpx.Response:
            path = request.url.path
            if "commodities_prices" in path:
                return httpx.Response(200, json={"status": "ok", "data": [
                    {"id_commodity": 1, "commodity_name": "Gold"}
                ]})
            if "commodities_routes" in path:
                return httpx.Response(200, json={"status": "ok", "data": [
                    {
                        "id_terminal_origin": 1, "id_terminal_destination": 101,
                        "origin_terminal_name": "Origin 1", "destination_terminal_name": "Destination 1",
                        "price_origin": 100, "price_destination": 200, "price_margin": 50, "price_roi": 100,
                        "distance": 5, "score": 100, "scu_origin": 10, "scu_destination": 10,
                        "status_origin": 1, "status_destination": 1, "profit": 100,
                    }
                ]})
            return httpx.Response(200, json={"status": "ok", "data": []})

        client = UexClient(app_token="test", base_url="https://uex.test")
        await client._client.aclose()
        client._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))

        bot = type("FakeBot", (), {})()
        bot.db = db
        bot.uex = client
        bot.get_cog = lambda name: None
        cog = Prices.__new__(Prices)
        cog.bot = bot
        interaction = _FakeInteraction(1)

        try:
            await cog.best_route.callback(cog, interaction, commodity="Gold")
        finally:
            await client.aclose()

        assert len(interaction.followup.sent) >= 2, "expected the intro plus at least one route message"
        _, kwargs = interaction.followup.sent[1]
        embed = kwargs["embed"]
        assert any("crosses systems" in (f.value or "") for f in embed.fields), embed.fields

    asyncio.run(run())


def test_best_route_attaches_a_route_tracking_view_when_the_cog_is_loaded(tmp_path):
    """/best-route's primary branch attaches a 'Track this route' button per route shown,
    but only when RouteProgression actually loaded - a cog load failure elsewhere must not
    break /best-route itself (see the bare bot.get_cog=lambda name: None fixtures on every
    other test in this file, which exercise the no-cog fallback instead)."""
    async def run():
        db = Database(tmp_path / "best_route_tracking_view.sqlite3", Fernet(Fernet.generate_key()))
        await db.init()

        def handler(request: httpx.Request) -> httpx.Response:
            path = request.url.path
            if "commodities_prices" in path:
                return httpx.Response(200, json={"status": "ok", "data": [
                    {"id_commodity": 1, "commodity_name": "Gold"}
                ]})
            if "commodities_routes" in path:
                return httpx.Response(200, json={"status": "ok", "data": [
                    {
                        "id_terminal_origin": 1, "id_terminal_destination": 101,
                        "origin_terminal_name": "Origin 1", "destination_terminal_name": "Destination 1",
                        "price_origin": 100, "price_destination": 200, "price_margin": 50, "price_roi": 100,
                        "distance": 5, "score": 100, "scu_origin": 10, "scu_destination": 10,
                        "status_origin": 1, "status_destination": 1, "profit": 100,
                    }
                ]})
            return httpx.Response(200, json={"status": "ok", "data": []})

        client = UexClient(app_token="test", base_url="https://uex.test")
        await client._client.aclose()
        client._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))

        tracking_cog = RouteProgression.__new__(RouteProgression)
        bot = type("FakeBot", (), {})()
        bot.db = db
        bot.uex = client
        bot.get_cog = lambda name: tracking_cog if name == "RouteProgression" else None
        cog = Prices.__new__(Prices)
        cog.bot = bot
        interaction = _FakeInteraction(1)

        try:
            await cog.best_route.callback(cog, interaction, commodity="Gold")
        finally:
            await client.aclose()

        assert len(interaction.followup.sent) >= 2, "expected the intro plus the route message"
        _, kwargs = interaction.followup.sent[1]
        view = kwargs.get("view")
        assert isinstance(view, RouteTrackingView)
        assert len(view.children) == 1, "one route shown -> one tracking button"

    asyncio.run(run())


def test_best_route_primary_branch_discloses_missing_distance_instead_of_silence(tmp_path):
    """Audit fix: travel_warning's has_real_distance was hardcoded True for every route in
    this branch regardless of whether THAT route's own UEX row actually had a distance
    figure - a same-system route with distance=None got neither a real distance line nor
    any travel-time disclaimer, silently indistinguishable from a route where distance
    genuinely doesn't matter."""
    async def run():
        db = Database(tmp_path / "best_route_no_distance.sqlite3", Fernet(Fernet.generate_key()))
        await db.init()
        await db.upsert_terminal_reference([
            {"id": 1, "name": "Origin 1", "star_system_name": "Stanton"},
            {"id": 101, "name": "Destination 1", "star_system_name": "Stanton"},
        ])

        def handler(request: httpx.Request) -> httpx.Response:
            path = request.url.path
            if "commodities_prices" in path:
                return httpx.Response(200, json={"status": "ok", "data": [
                    {"id_commodity": 1, "commodity_name": "Gold"}
                ]})
            if "commodities_routes" in path:
                return httpx.Response(200, json={"status": "ok", "data": [
                    {
                        "id_terminal_origin": 1, "id_terminal_destination": 101,
                        "origin_terminal_name": "Origin 1", "destination_terminal_name": "Destination 1",
                        "price_origin": 100, "price_destination": 200, "price_margin": 50, "price_roi": 100,
                        "distance": None, "score": 100, "scu_origin": 10, "scu_destination": 10,
                        "status_origin": 1, "status_destination": 1, "profit": 100,
                    }
                ]})
            return httpx.Response(200, json={"status": "ok", "data": []})

        client = UexClient(app_token="test", base_url="https://uex.test")
        await client._client.aclose()
        client._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))

        bot = type("FakeBot", (), {})()
        bot.db = db
        bot.uex = client
        bot.get_cog = lambda name: None
        cog = Prices.__new__(Prices)
        cog.bot = bot
        interaction = _FakeInteraction(1)

        try:
            await cog.best_route.callback(cog, interaction, commodity="Gold")
        finally:
            await client.aclose()

        assert len(interaction.followup.sent) >= 2, "expected the intro plus the route message"
        _, kwargs = interaction.followup.sent[1]
        embed = kwargs["embed"]
        combined = "\n".join(f.value or "" for f in embed.fields)
        assert "GM" not in combined, combined
        assert "not included in this ranking" in combined, combined

    asyncio.run(run())


def test_best_route_fallback_branch_shows_evidence_levels_for_missing_stock_and_demand(tmp_path):
    """Evidence-Level Labels: /best-route's fallback branch (no UEX /commodities_routes
    data for this commodity) never showed a raw stock/demand figure at all before - a
    missing scu_buy/scu_sell was invisible, indistinguishable from a route that simply
    doesn't mention it. The buy side here has collected observation history to infer
    from (a real recorded state change, well past MIN_HISTORY_HOURS, anchored to a
    terminal_market_state.last_seen row like real collected data always has - not a
    single stale point extrapolated to wall-clock now, which no longer qualifies as
    "inferred" after this audit's fix); the sell side has no observations at all."""
    async def run():
        db = Database(tmp_path / "best_route_evidence.sqlite3", Fernet(Fernet.generate_key()))
        await db.init()
        # Two backdated observations bridging a real state change, plus the matching
        # terminal_market_state row real collected data always has (last_seen anchors
        # coverage, not wall-clock now) - inserted directly rather than via
        # record_terminal_market_snapshot, which always stamps timestamps as
        # datetime('now') and can't backdate them.
        async with db.connect() as sqlite:
            await sqlite.execute(
                """INSERT INTO terminal_market_observations
                   (id_commodity, id_terminal, observed_at, commodity_name, terminal_name,
                    price_buy, scu_buy, status_buy)
                   VALUES (1, 1, datetime('now', '-72 hours'), 'Gold', 'Buy A', 10, 50, 1)"""
            )
            await sqlite.execute(
                """INSERT INTO terminal_market_observations
                   (id_commodity, id_terminal, observed_at, commodity_name, terminal_name,
                    price_buy, scu_buy, status_buy)
                   VALUES (1, 1, datetime('now', '-48 hours'), 'Gold', 'Buy A', 10, 0, 1)"""
            )
            await sqlite.execute(
                """INSERT INTO terminal_market_state
                   (id_commodity, id_terminal, commodity_name, terminal_name, last_seen)
                   VALUES (1, 1, 'Gold', 'Buy A', datetime('now', '-2 hours'))"""
            )
            await sqlite.commit()

        def handler(request: httpx.Request) -> httpx.Response:
            path = request.url.path
            if "commodities_prices" in path:
                return httpx.Response(200, json={"status": "ok", "data": [
                    {"id_commodity": 1, "commodity_name": "Gold", "id_terminal": 1, "terminal_name": "Buy A",
                     "price_buy": 10, "price_sell": 0},
                    {"id_commodity": 1, "commodity_name": "Gold", "id_terminal": 3, "terminal_name": "Sell A",
                     "price_buy": 0, "price_sell": 100},
                ]})
            if "commodities_routes" in path:
                return httpx.Response(200, json={"status": "ok", "data": []})
            return httpx.Response(200, json={"status": "ok", "data": []})

        client = UexClient(app_token="test", base_url="https://uex.test")
        await client._client.aclose()
        client._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))

        bot = type("FakeBot", (), {})()
        bot.db = db
        bot.uex = client
        bot.get_cog = lambda name: None
        cog = Prices.__new__(Prices)
        cog.bot = bot
        interaction = _FakeInteraction(1)

        try:
            await cog.best_route.callback(cog, interaction, commodity="Gold")
        finally:
            await client.aclose()

        assert interaction.followup.sent, "expected at least one followup"
        _, kwargs = interaction.followup.sent[0]
        embed = kwargs["embed"]
        combined = "\n".join(field.value or "" for field in embed.fields)
        assert "historically available" in combined, combined
        assert "no information reported" in combined, combined

    asyncio.run(run())


def test_best_route_fallback_branch_shows_investment(tmp_path):
    """CargoEstimate now carries an investment figure alongside Run profit (bot/uex/
    ships.py) - the fallback branch (no UEX /commodities_routes data) must show it too,
    not just the primary branch below."""
    async def run():
        db = Database(tmp_path / "best_route_fallback_investment.sqlite3", Fernet(Fernet.generate_key()))
        await db.init()

        def handler(request: httpx.Request) -> httpx.Response:
            path = request.url.path
            if "commodities_prices" in path:
                return httpx.Response(200, json={"status": "ok", "data": [
                    {"id_commodity": 1, "commodity_name": "Gold", "id_terminal": 1, "terminal_name": "Buy A",
                     "price_buy": 100, "price_sell": 0, "scu_buy": 50, "scu_sell": 0},
                    {"id_commodity": 1, "commodity_name": "Gold", "id_terminal": 3, "terminal_name": "Sell A",
                     "price_buy": 0, "price_sell": 200, "scu_buy": 0, "scu_sell": 50},
                ]})
            if "commodities_routes" in path:
                return httpx.Response(200, json={"status": "ok", "data": []})
            return httpx.Response(200, json={"status": "ok", "data": []})

        client = UexClient(app_token="test", base_url="https://uex.test")
        await client._client.aclose()
        client._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))

        bot = type("FakeBot", (), {})()
        bot.db = db
        bot.uex = client
        bot.get_cog = lambda name: None
        cog = Prices.__new__(Prices)
        cog.bot = bot
        interaction = _FakeInteraction(1)

        try:
            await cog.best_route.callback(cog, interaction, commodity="Gold")
        finally:
            await client.aclose()

        assert interaction.followup.sent, "expected at least one followup"
        _, kwargs = interaction.followup.sent[0]
        embed = kwargs["embed"]
        combined = "\n".join(field.value or "" for field in embed.fields)
        assert "Investment:" in combined, combined

    asyncio.run(run())


def test_best_route_primary_branch_shows_investment(tmp_path):
    async def run():
        db = Database(tmp_path / "best_route_primary_investment.sqlite3", Fernet(Fernet.generate_key()))
        await db.init()

        def handler(request: httpx.Request) -> httpx.Response:
            path = request.url.path
            if "commodities_prices" in path:
                return httpx.Response(200, json={"status": "ok", "data": [
                    {"id_commodity": 1, "commodity_name": "Gold"}
                ]})
            if "commodities_routes" in path:
                return httpx.Response(200, json={"status": "ok", "data": [
                    {
                        "id_terminal_origin": 1, "id_terminal_destination": 101,
                        "origin_terminal_name": "Origin 1", "destination_terminal_name": "Destination 1",
                        "price_origin": 100, "price_destination": 200, "price_margin": 50, "price_roi": 100,
                        "distance": 5, "score": 100, "scu_origin": 10, "scu_destination": 10,
                        "status_origin": 1, "status_destination": 1, "profit": 100,
                    }
                ]})
            return httpx.Response(200, json={"status": "ok", "data": []})

        client = UexClient(app_token="test", base_url="https://uex.test")
        await client._client.aclose()
        client._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))

        bot = type("FakeBot", (), {})()
        bot.db = db
        bot.uex = client
        bot.get_cog = lambda name: None
        cog = Prices.__new__(Prices)
        cog.bot = bot
        interaction = _FakeInteraction(1)

        try:
            await cog.best_route.callback(cog, interaction, commodity="Gold")
        finally:
            await client.aclose()

        assert len(interaction.followup.sent) >= 2, "expected the intro plus the route message"
        _, kwargs = interaction.followup.sent[1]
        embed = kwargs["embed"]
        combined = "\n".join(f.value or "" for f in embed.fields)
        assert "Investment:" in combined, combined

    asyncio.run(run())


def test_best_route_fallback_branch_anchors_history_to_last_seen_not_wall_clock(tmp_path):
    """Audit fix: the 'inferred' tier used to anchor observation coverage to wall-clock
    now() instead of the collector's own terminal_market_state.last_seen - a pair with
    only 2 REAL hours of confirmed collector coverage (last_seen frozen shortly after the
    second observation, meaning the collector hasn't rechecked this pair since) would
    still render as "historically available" with a large observed-hours figure, because
    extending to wall-clock now() (years later, in this fixture) silently counted the
    entire unconfirmed gap as continued observation. With the fix anchoring to last_seen,
    this same pair correctly has only ~2 real observed hours - not enough to infer from."""
    async def run():
        db = Database(tmp_path / "best_route_stale_anchor.sqlite3", Fernet(Fernet.generate_key()))
        await db.init()
        # Two real observations 2 hours apart, from years before "now" - if the anchor
        # were wall-clock now(), this would extend to several years of "observed" time.
        # last_seen is frozen at the same moment as the second observation: the collector
        # confirmed this state once more and then never rechecked it again.
        async with db.connect() as sqlite:
            await sqlite.execute(
                """INSERT INTO terminal_market_observations
                   (id_commodity, id_terminal, observed_at, commodity_name, terminal_name,
                    price_buy, scu_buy, status_buy)
                   VALUES (1, 1, '2020-01-01 00:00:00', 'Gold', 'Buy A', 10, 50, 1)"""
            )
            await sqlite.execute(
                """INSERT INTO terminal_market_observations
                   (id_commodity, id_terminal, observed_at, commodity_name, terminal_name,
                    price_buy, scu_buy, status_buy)
                   VALUES (1, 1, '2020-01-01 02:00:00', 'Gold', 'Buy A', 10, 0, 1)"""
            )
            await sqlite.execute(
                """INSERT INTO terminal_market_state
                   (id_commodity, id_terminal, commodity_name, terminal_name, last_seen)
                   VALUES (1, 1, 'Gold', 'Buy A', '2020-01-01 02:00:00')"""
            )
            await sqlite.commit()

        def handler(request: httpx.Request) -> httpx.Response:
            path = request.url.path
            if "commodities_prices" in path:
                return httpx.Response(200, json={"status": "ok", "data": [
                    {"id_commodity": 1, "commodity_name": "Gold", "id_terminal": 1, "terminal_name": "Buy A",
                     "price_buy": 10, "price_sell": 0},
                    {"id_commodity": 1, "commodity_name": "Gold", "id_terminal": 3, "terminal_name": "Sell A",
                     "price_buy": 0, "price_sell": 100},
                ]})
            if "commodities_routes" in path:
                return httpx.Response(200, json={"status": "ok", "data": []})
            return httpx.Response(200, json={"status": "ok", "data": []})

        client = UexClient(app_token="test", base_url="https://uex.test")
        await client._client.aclose()
        client._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))

        bot = type("FakeBot", (), {})()
        bot.db = db
        bot.uex = client
        bot.get_cog = lambda name: None
        cog = Prices.__new__(Prices)
        cog.bot = bot
        interaction = _FakeInteraction(1)

        try:
            await cog.best_route.callback(cog, interaction, commodity="Gold")
        finally:
            await client.aclose()

        assert interaction.followup.sent, "expected at least one followup"
        _, kwargs = interaction.followup.sent[0]
        embed = kwargs["embed"]
        combined = "\n".join(field.value or "" for field in embed.fields)
        assert "historically available" not in combined, combined
        assert "no information reported" in combined, combined

    asyncio.run(run())


def test_best_route_fallback_branch_discloses_when_routes_are_truncated_for_size(tmp_path, monkeypatch):
    """Same finding as the primary-branch test above, for /best-route's OTHER branch -
    the one used when UEX has no /commodities_routes data for this commodity and the bot
    derives routes itself from raw /commodities_prices rows via best_routes(). This is a
    materially different code path (different data source, different loop variables), so
    it needed its own independent check rather than assuming the primary branch's fix
    covered it."""
    async def run():
        db = Database(tmp_path / "best_route_fallback_budget.sqlite3", Fernet(Fernet.generate_key()))
        await db.init()

        def handler(request: httpx.Request) -> httpx.Response:
            path = request.url.path
            if "commodities_prices" in path:
                return httpx.Response(200, json={"status": "ok", "data": [
                    {"id_commodity": 1, "commodity_name": "Gold", "id_terminal": 1, "terminal_name": "Buy A",
                     "price_buy": 10, "price_sell": 0},
                    {"id_commodity": 1, "commodity_name": "Gold", "id_terminal": 2, "terminal_name": "Buy B",
                     "price_buy": 20, "price_sell": 0},
                    {"id_commodity": 1, "commodity_name": "Gold", "id_terminal": 3, "terminal_name": "Sell A",
                     "price_buy": 0, "price_sell": 100},
                    {"id_commodity": 1, "commodity_name": "Gold", "id_terminal": 4, "terminal_name": "Sell B",
                     "price_buy": 0, "price_sell": 90},
                ]})
            if "commodities_routes" in path:
                return httpx.Response(200, json={"status": "ok", "data": []})
            return httpx.Response(200, json={"status": "ok", "data": []})

        client = UexClient(app_token="test", base_url="https://uex.test")
        await client._client.aclose()
        client._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))

        bot = type("FakeBot", (), {})()
        bot.db = db
        bot.uex = client
        bot.get_cog = lambda name: None
        cog = Prices.__new__(Prices)
        cog.bot = bot
        interaction = _FakeInteraction(1)

        call_count = {"n": 0}
        real_add_chunked_fields = prices_module._add_chunked_fields

        def flaky_add_chunked_fields(embed, *, name, lines):
            call_count["n"] += 1
            if call_count["n"] == 2:
                return False
            return real_add_chunked_fields(embed, name=name, lines=lines)

        monkeypatch.setattr(prices_module, "_add_chunked_fields", flaky_add_chunked_fields)

        try:
            await cog.best_route.callback(cog, interaction, commodity="Gold")
        finally:
            await client.aclose()

        assert interaction.followup.sent, "expected at least one followup"
        _, kwargs = interaction.followup.sent[0]
        embed = kwargs["embed"]
        assert len(embed.fields) == 1, f"expected exactly the first route's field, got {len(embed.fields)}"
        assert "omitted" in (embed.footer.text or "").lower(), (embed.footer.text,)

    asyncio.run(run())


def test_multi_stop_route_falls_back_to_plain_text_when_a_leg_field_does_not_fit(tmp_path, monkeypatch):
    """Second follow-up review finding: /multi-stop-route's per-leg loop called the
    atomic _add_chunked_fields for each leg's own field but never checked its return
    value - unlike the warnings-section call right after the loop, which the previous
    review round already fixed to check it. If a middle leg's field silently failed to
    fit (while an earlier and/or later leg's smaller field still fit into the same
    remaining budget), the route embed's title and "Route summary" field would both still
    unconditionally describe ALL legs (built from route.legs and route.investment/
    revenue/profit, not from which leg fields actually got added) while the embed itself
    visibly showed fewer legs than it claimed - a self-contradictory result that also
    never triggered the existing too-large fallback, since a route missing one leg's
    field is smaller, not bigger, and sends "successfully." Forces the SECOND leg's field
    to fail deterministically rather than depending on exact byte counts."""
    async def run():
        call_count = {"n": 0}
        real_add_chunked_fields = prices_module._add_chunked_fields

        def flaky_add_chunked_fields(embed, *, name, lines):
            if name.startswith("Leg 2"):
                return False
            return real_add_chunked_fields(embed, name=name, lines=lines)

        monkeypatch.setattr(prices_module, "_add_chunked_fields", flaky_add_chunked_fields)

        interaction = await _run_command(
            tmp_path, "multi_stop_leg_drop.sqlite3", _MULTI_STOP_ROWS,
            lambda cog, interaction: cog.multi_stop_route.callback(cog, interaction, ship="TestShip"),
        )

        assert interaction.followup.sent, "expected at least one followup"
        for args, kwargs in interaction.followup.sent:
            assert "embed" not in kwargs, "a route embed missing one of its legs must not be sent as if complete"

    asyncio.run(run())


def test_mixed_routes_sends_one_message_per_route(tmp_path):
    """Each route gets its own message with its own embed and its own "Track this route"
    button directly beneath it (matching /best-route, /top-routes, and /multi-stop-route),
    not bundled together into one multi-embed message the way /mixed-routes used to."""
    async def run():
        interaction = await _run_command(
            tmp_path, "mixed_routes.sqlite3", _MIXED_ROUTES_ROWS,
            lambda cog, interaction: cog.mixed_routes.callback(cog, interaction, ship="TestShip"),
        )
        assert interaction.followup.sent, "expected at least one followup"
        for _, kwargs in interaction.followup.sent:
            assert "embeds" not in kwargs, "must no longer batch multiple embeds into one message"
            assert isinstance(kwargs.get("embed"), discord.Embed)

    asyncio.run(run())


def test_mixed_routes_attaches_a_track_button_with_flattened_legs(monkeypatch):
    """/mixed-routes wiring: like /multi-stop-route, one route carries several commodities
    at once (allocate_pair_cargo's mixed load) at a single origin/destination pair, not a
    chain of hops - flattened into one buy + one sell progression-leg per commodity, in
    order, so the existing leg-by-leg cog can walk it unchanged."""
    async def run():
        source = dict(scu_buy=10, status_buy=1, star_system_name="Stanton")
        destination = dict(scu_sell=10, status_sell=1, star_system_name="Stanton")
        cargo = (
            MixedCargoItem(1, "Gold", 5, 100, 200, 10, 500, 500, source, destination),
            MixedCargoItem(2, "Cobalt", 3, 50, 90, 10, 150, 120, source, destination),
        )
        route = NS(
            origin_name="Station A", destination_name="Station B", origin_id=10, destination_id=20,
            cargo=cargo, cargo_scu=8, investment=650, revenue=1270, profit=620, roi_pct=95.4, is_exact=True,
        )
        monkeypatch.setattr(prices_module, "build_mixed_routes", lambda *a, **k: [route])

        db = NS(
            get_default_ship=AsyncMock(return_value="Ship"),
            get_trading_preferences=AsyncMock(return_value=dict(DEFAULT_TRADING_PREFERENCES)),
            get_mixed_route_market_rows=AsyncMock(return_value=[]),
            get_terminal_data_health_by_ids=AsyncMock(return_value={}),
        )
        uex = NS(get_vehicles=AsyncMock(return_value=[dict(name="Ship", scu=100)]))
        tracking_cog = RouteProgression.__new__(RouteProgression)
        cog = Prices.__new__(Prices)
        cog.bot = NS(db=db, uex=uex, get_cog=lambda name: tracking_cog if name == "RouteProgression" else None)
        cog._get_status_lookup = AsyncMock(return_value={"buy": {}, "sell": {}})
        interaction = _FakeInteraction(1)

        await cog.mixed_routes.callback(cog, interaction)

        assert interaction.followup.sent, "expected at least one followup"
        _, kwargs = interaction.followup.sent[0]
        view = kwargs.get("view")
        assert isinstance(view, RouteTrackingView)
        assert len(view.children) == 1, "one route -> one tracking button"

        legs = view.routes[0].legs
        assert [leg.display_label for leg in legs] == [
            "Buy Gold at Station A", "Buy Cobalt at Station A",
            "Sell Gold at Station B", "Sell Cobalt at Station B",
        ], legs
        assert legs[0].id_terminal == 10 and legs[0].side == "buy"
        assert legs[2].id_terminal == 20 and legs[2].side == "sell"
        assert legs[0].quoted_scu == 5 and legs[0].quoted_price == 100
        # market_scu carries the real quoted market availability (available_scu)
        # separately from quoted_scu (the ship/budget-capped cargo allocation) - a
        # "matched" report must confirm the former, not silently shrink the terminal
        # to the size of this one cargo run. See terminal_state_update_for_outcome.
        assert legs[0].market_scu == 10 and legs[1].market_scu == 10

    asyncio.run(run())


def test_mixed_routes_matched_report_writes_real_market_stock_not_the_allocated_cargo_amount(tmp_path):
    """End-to-end regression for a real defect: /mixed-routes allocates a SHARE of a
    ship's cargo per commodity, capped by capacity/budget - far less than the terminal's
    real stock. A player confirming that allocation "matched the quote" must not shrink
    terminal_market_state down to the size of their own purchase."""
    async def run():
        db = Database(tmp_path / "mixed_matched.sqlite3", Fernet(Fernet.generate_key()))
        await db.init()
        await db.record_terminal_market_snapshot(_MIXED_ROUTES_ROWS)
        # Cobalt's real market_available = min(scu_buy origin=95, scu_sell destination=80)
        # = 80, from _MIXED_ROUTES_ROWS - the figure a "matched" report must confirm.
        real_available_scu = 80

        client = UexClient(app_token="test", base_url="https://uex.test")
        await client._client.aclose()
        client._client = httpx.AsyncClient(transport=_transport())

        tracker = RouteProgression.__new__(RouteProgression)
        bot = type("FakeBot", (), {})()
        bot.db = db
        bot.uex = client
        bot.get_cog = lambda name: tracker if name == "RouteProgression" else None
        tracker.bot = bot
        tracker._active_legs = {}
        cog = Prices.__new__(Prices)
        cog.bot = bot
        interaction = _FakeInteraction(111)

        try:
            await cog.mixed_routes.callback(cog, interaction, ship="TestShip")
            views = [kwargs["view"] for _, kwargs in interaction.followup.sent if kwargs.get("view")]
            assert views, "expected a tracking view on the real command output"
            route = views[0].routes[0]
            cobalt_buy_leg = next(leg for leg in route.legs if leg.side == "buy" and leg.id_commodity == 2)
            # The ship's small cargo pool means the allocation is nowhere near the real stock.
            assert cobalt_buy_leg.quoted_scu < real_available_scu
            assert cobalt_buy_leg.market_scu == real_available_scu

            await db.create_route_progression_thread(
                thread_id=555, user_id=111, guild_id=1, route_kind="mixed_routes",
                route_snapshot={}, legs=[vars(cobalt_buy_leg)],
            )
            tracker._active_legs[555] = [cobalt_buy_leg]
            await tracker.handle_leg_outcome(None, 555, 0, cobalt_buy_leg, outcome="matched")

            async with db.connect() as conn:
                cursor = await conn.execute(
                    "SELECT scu_buy FROM terminal_market_state WHERE id_commodity = 2 AND id_terminal = 1"
                )
                row = await cursor.fetchone()
            assert row["scu_buy"] == real_available_scu, (
                cobalt_buy_leg.quoted_scu, cobalt_buy_leg.market_scu, row["scu_buy"]
            )
        finally:
            await client.aclose()

    asyncio.run(run())


def test_mixed_routes_a_route_that_does_not_fit_falls_back_on_its_own(monkeypatch):
    """Second follow-up review finding (original): Discord enforces its 6,000-char
    embed-text limit as a SUM across every embed in one message, not per individual embed -
    /mixed-routes used to batch up to 5 embeds into one message with no protection against
    this. Now that each route is its own message (matching /multi-stop-route's shape),
    that failure mode is structurally gone - the remaining, narrower case is ONE route's
    own content not fitting ITS OWN embed, which must fall back to plain text for just
    that route, without dragging the other routes' real embeds down with it (the old
    shared-batch behavior's real cost)."""
    async def run():
        source = dict(
            scu_buy=10, status_buy=1, max_container_size=8, has_freight_elevator=0,
            has_loading_dock=0, is_player_owned=1, is_refuel=1, is_repair=1, is_cargo_center=1,
            star_system_name="Stanton",
        )
        destination = dict(source, scu_sell=10, status_sell=1, star_system_name="Stanton")
        routes = []
        for r in range(1, 4):
            cargo = (MixedCargoItem(r, f"Commodity {r}", 10, 100, 200, 10, 1000, 1000, source, destination),)
            routes.append(NS(
                origin_name=f"Origin {r}", destination_name=f"Destination {r}",
                origin_id=2 * r - 1, destination_id=2 * r, cargo=cargo,
                cargo_scu=10, investment=1000, revenue=2000, profit=1000, roi_pct=100.0, is_exact=True,
            ))

        db = NS(
            get_default_ship=AsyncMock(return_value="Ship"),
            get_trading_preferences=AsyncMock(return_value=dict(DEFAULT_TRADING_PREFERENCES)),
            get_mixed_route_market_rows=AsyncMock(return_value=[]),
            get_terminal_data_health_by_ids=AsyncMock(return_value={}),
        )
        uex = NS(get_vehicles=AsyncMock(return_value=[dict(name="Ship", scu=100)]))
        cog = Prices.__new__(Prices)
        cog.bot = NS(db=db, uex=uex, get_cog=lambda name: None)
        cog._get_status_lookup = AsyncMock(return_value={"buy": {}, "sell": {}})
        interaction = _FakeInteraction(1)

        # Force the SECOND route's warnings section to fail to fit, deterministically.
        real_add_chunked_fields = prices_module._add_chunked_fields
        call_count = {"n": 0}

        def flaky_add_chunked_fields(embed, *, name, lines):
            call_count["n"] += 1
            if call_count["n"] == 2:
                return False
            return real_add_chunked_fields(embed, name=name, lines=lines)

        monkeypatch.setattr(prices_module, "build_mixed_routes", lambda *a, **k: routes)
        monkeypatch.setattr(prices_module, "_add_chunked_fields", flaky_add_chunked_fields)
        await cog.mixed_routes.callback(cog, interaction)

        assert interaction.followup.sent, "expected at least one followup"
        embed_titles = [kwargs["embed"].title for _, kwargs in interaction.followup.sent if kwargs.get("embed")]
        plain_texts = [kwargs["content"] for _, kwargs in interaction.followup.sent if kwargs.get("content")]
        assert any("Origin 1" in title for title in embed_titles), embed_titles
        assert any("Origin 3" in title for title in embed_titles), embed_titles
        assert not any("Origin 2" in title for title in embed_titles), (
            "route 2's own embed should have been skipped, not sent incomplete", embed_titles
        )
        assert any("Origin 2" in text for text in plain_texts), (
            "route 2 must still appear via its own plain-text fallback", plain_texts
        )

    asyncio.run(run())


def test_mixed_routes_fallback_preserves_the_approximation_disclosure(monkeypatch):
    """Follow-up review finding: /mixed-routes' plain-text fallback (added earlier this
    session to handle the combined-batch-too-large case) copied the route heading,
    cargo/financial lines, and warnings, but never included the footer - which is where
    the route.is_exact approximation disclosure (and the budget/space-only/capital-access
    notes) actually live. An approximate route silently lost that qualification the
    moment the batch send was rejected or a route's own warnings didn't fit. The
    multi-stop-route fallback already includes its equivalent disclosure explicitly;
    /mixed-routes' fallback just never carried its footer at all."""
    async def run():
        source = dict(scu_buy=10, status_buy=1)
        destination = dict(scu_sell=10, status_sell=1)
        cargo = (MixedCargoItem(1, "Ore", 10, 100, 200, 10, 1000, 1000, source, destination),)
        # is_exact=False - this route's cargo allocation is only the heuristic
        # approximation, so the footer must say so.
        route = MixedRoute(1, "Origin", 2, "Destination", cargo, 10, 1000, 2000, 1000, False)
        monkeypatch.setattr(prices_module, "build_mixed_routes", lambda *a, **k: [route])

        db = NS(
            get_default_ship=AsyncMock(return_value="Ship"),
            get_trading_preferences=AsyncMock(return_value=dict(DEFAULT_TRADING_PREFERENCES)),
            get_mixed_route_market_rows=AsyncMock(return_value=[]),
            get_terminal_data_health_by_ids=AsyncMock(return_value={}),
        )
        uex = NS(get_vehicles=AsyncMock(return_value=[dict(name="Ship", scu=100)]))
        cog = Prices.__new__(Prices)
        cog.bot = NS(db=db, uex=uex, get_cog=lambda name: None)
        cog._get_status_lookup = AsyncMock(return_value={})

        delivered = []

        async def send(**kwargs):
            if "embed" in kwargs:
                raise discord.HTTPException(NS(status=400, reason="Bad Request", headers={}), "Embed too large")
            delivered.append(kwargs.get("content", ""))

        interaction = NS(user=NS(id=1), response=NS(defer=AsyncMock()), followup=NS(send=send))
        await cog.mixed_routes.callback(cog, interaction)

        fallback_text = "\n".join(delivered)
        assert "approximate" in fallback_text.lower(), fallback_text

    asyncio.run(run())
