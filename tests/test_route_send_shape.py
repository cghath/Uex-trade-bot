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

from cryptography.fernet import Fernet
import discord
import httpx

from bot.cogs import prices as prices_module
from bot.cogs.prices import Prices
from bot.db.database import Database
from bot.uex.client import UexClient


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
            assert "stock limits this load to 5 SCU" in fallback_text, (
                f"expected the stock-limit warning to survive into the fallback, got: {fallback_text!r}"
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
        cog = Prices.__new__(Prices)
        cog.bot = bot
        interaction = _FakeInteraction(111)

        try:
            await cog.mixed_routes.callback(cog, interaction, ship="BigShip")

            assert interaction.followup.sent, "expected at least one followup"
            _, kwargs = interaction.followup.sent[0]
            embeds = kwargs.get("embeds") or []
            assert embeds, f"expected at least one embed, got: {interaction.followup.sent}"
            footer_text = embeds[0].footer.text or ""
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


def test_mixed_routes_still_sends_one_batched_message(tmp_path):
    """/mixed-routes' embeds are small enough that batching is fine and intentional -
    this pins that down so a future fix doesn't accidentally swap the two commands'
    send shape again."""
    async def run():
        interaction = await _run_command(
            tmp_path, "mixed_routes.sqlite3", _MIXED_ROUTES_ROWS,
            lambda cog, interaction: cog.mixed_routes.callback(cog, interaction, ship="TestShip"),
        )
        assert len(interaction.followup.sent) == 1, "expected exactly one batched followup"
        _, kwargs = interaction.followup.sent[0]
        assert "embeds" in kwargs and isinstance(kwargs["embeds"], list)

    asyncio.run(run())
