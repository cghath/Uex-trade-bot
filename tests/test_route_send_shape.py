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

from cryptography.fernet import Fernet
import httpx

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
