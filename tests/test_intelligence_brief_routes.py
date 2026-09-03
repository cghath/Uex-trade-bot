"""Tests for /intelligence-brief's route recommendations, which share cargo allocation
(build_mixed_routes/allocate_pair_cargo) with /mixed-routes and /multi-stop-route - two
fixes applied to those commands (offloading the allocation off the event loop, and
disclosing when it's only an approximation) were never applied to this third caller.
"""
from __future__ import annotations

import asyncio
import threading

from cryptography.fernet import Fernet
import httpx

from bot.cogs import intelligence_brief as intelligence_brief_module
from bot.cogs.intelligence_brief import IntelligenceBrief
from bot.db.database import Database
from bot.uex.client import UexClient


def _row(commodity_id, terminal_id, name, terminal, **values):
    return {
        "id_commodity": commodity_id,
        "id_terminal": terminal_id,
        "commodity_name": name,
        "terminal_name": terminal,
        "price_buy": None,
        "price_sell": None,
        "scu_buy": None,
        "scu_sell": None,
        "status_sell": 1,
        **values,
    }


# /mixed-routes needs 2+ commodities profitable at the SAME origin/destination pair.
_MIXED_ROUTES_ROWS = [
    _row(1, 1, "Stileron", "Origin", price_buy=100, scu_buy=4),
    _row(1, 2, "Stileron", "Destination", price_sell=200, scu_sell=10),
    _row(2, 1, "Cobalt", "Origin", price_buy=20, scu_buy=95),
    _row(2, 2, "Cobalt", "Destination", price_sell=50, scu_sell=80),
]


async def _make_cog(tmp_path, db_name: str, market_rows: list[dict], ship_scu: float):
    db = Database(tmp_path / db_name, Fernet(Fernet.generate_key()))
    await db.init()
    await db.record_terminal_market_snapshot(market_rows)

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if "vehicles" in path:
            return httpx.Response(
                200, json={"status": "ok", "data": [{"name": "TestShip", "scu": ship_scu, "pad_type": "M"}]}
            )
        return httpx.Response(200, json={"status": "ok", "data": []})

    client = UexClient(app_token="test", base_url="https://uex.test")
    await client._client.aclose()
    client._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))

    bot = type("FakeBot", (), {})()
    bot.db = db
    bot.uex = client
    cog = IntelligenceBrief.__new__(IntelligenceBrief)
    cog.bot = bot
    return cog, client


def test_routes_embed_offloads_cargo_allocation_to_a_worker_thread(tmp_path, monkeypatch):
    """Regression: build_mixed_routes can run a real, sometimes expensive combinatorial
    search (see allocate_pair_cargo) - calling it directly on _routes_embed's coroutine
    would block the bot's one asyncio event loop for as long as it takes, same bug fixed
    for /mixed-routes and /multi-stop-route. Checked directly (not via timing, which can
    pass by accident from unrelated awaits elsewhere): the actual thread
    build_mixed_routes runs on must not be the main/event-loop thread."""
    async def run():
        cog, client = await _make_cog(tmp_path, "brief_thread.sqlite3", _MIXED_ROUTES_ROWS, ship_scu=10)
        called_from_thread = {}
        real_build = intelligence_brief_module.build_mixed_routes

        def spy(*args, **kwargs):
            called_from_thread["thread"] = threading.current_thread()
            return real_build(*args, **kwargs)

        monkeypatch.setattr(intelligence_brief_module, "build_mixed_routes", spy)
        try:
            embed = await cog._routes_embed("TestShip", None, False)
        finally:
            await client.aclose()

        assert embed.fields, "expected at least one route field"
        assert called_from_thread.get("thread") is not None, "build_mixed_routes was never called"
        assert called_from_thread["thread"] is not threading.main_thread(), (
            "build_mixed_routes ran on the main/event-loop thread - it must be offloaded "
            "via asyncio.to_thread so it can't block the bot's one event loop"
        )

    asyncio.run(run())


def test_routes_embed_discloses_when_cargo_allocation_is_approximate(tmp_path):
    """Regression: /intelligence-brief never checked route.is_exact, so a route
    recommendation could be an unproven approximation (see allocate_pair_cargo) with no
    warning at all, unlike /mixed-routes' footer disclosure for the same case."""
    async def run():
        # ship_scu=30 exceeds EXACT_SEARCH_MAX_CAPACITY (25), making this route's cargo
        # allocation approximate, not proven-optimal.
        cog, client = await _make_cog(tmp_path, "brief_disclosure.sqlite3", _MIXED_ROUTES_ROWS, ship_scu=30)
        try:
            embed = await cog._routes_embed("TestShip", None, False)
        finally:
            await client.aclose()

        assert embed.fields, "expected at least one route field"
        assert any("approximate" in (field.value or "").lower() for field in embed.fields), (
            f"expected an approximation disclosure, got fields: {[f.value for f in embed.fields]!r}"
        )

    asyncio.run(run())
