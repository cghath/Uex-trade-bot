"""Trades cog regression test: /uex-trades must keep every followup ephemeral - discord.py
does not inherit ephemeral from response.defer(), each followup.send() needs it explicitly,
and this command previously omitted it despite deferring ephemeral (a real privacy leak:
trade history posted publicly in the channel)."""
from __future__ import annotations

import asyncio

from cryptography.fernet import Fernet
import httpx

from bot.cogs.trades import Trades
from bot.db.database import Database
from bot.uex.client import UexClient


def _make_db(tmp_path) -> Database:
    return Database(tmp_path / "trades.sqlite3", Fernet(Fernet.generate_key()))


class _FakeResponse:
    async def defer(self, **kwargs):
        pass

    async def send_message(self, *args, **kwargs):
        raise AssertionError("send_message should not be used once deferred")


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


def test_uex_trades_keeps_every_followup_ephemeral(tmp_path):
    async def run():
        db = _make_db(tmp_path)
        await db.init()
        user_id = 111
        await db.set_user_secret_key(user_id, "sk_test")

        def handler(request: httpx.Request) -> httpx.Response:
            assert "user_trades" in request.url.path
            return httpx.Response(200, json={"status": "ok", "data": [
                {"operation": "sell", "scu": 10, "commodity_name": "Laranite", "price": "150", "date_added": "x"},
            ]})

        client = UexClient(app_token="test", base_url="https://uex.test")
        await client._client.aclose()
        client._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))

        bot = type("FakeBot", (), {})()
        bot.db = db
        bot.uex = client
        cog = Trades.__new__(Trades)
        cog.bot = bot
        interaction = _FakeInteraction(user_id)

        try:
            await cog.uex_trades.callback(cog, interaction)

            assert interaction.followup.sent, "expected at least one followup"
            for _, kwargs in interaction.followup.sent:
                assert kwargs.get("ephemeral") is True
        finally:
            await client.aclose()

    asyncio.run(run())


def test_uex_trades_unlinked_message_is_ephemeral(tmp_path):
    async def run():
        db = _make_db(tmp_path)
        await db.init()
        client = UexClient(app_token="test", base_url="https://uex.test")

        bot = type("FakeBot", (), {})()
        bot.db = db
        bot.uex = client
        cog = Trades.__new__(Trades)
        cog.bot = bot
        interaction = _FakeInteraction(222)

        try:
            await cog.uex_trades.callback(cog, interaction)

            (_, kwargs), = interaction.followup.sent
            assert kwargs.get("ephemeral") is True
        finally:
            await client.aclose()

    asyncio.run(run())
