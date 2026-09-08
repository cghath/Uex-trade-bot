"""Tests for /intelligence-brief's route recommendations, which share cargo allocation
(build_mixed_routes/allocate_pair_cargo) with /mixed-routes and /multi-stop-route - two
fixes applied to those commands (offloading the allocation off the event loop, and
disclosing when it's only an approximation) were never applied to this third caller.
"""
from __future__ import annotations

import asyncio
import threading

from cryptography.fernet import Fernet
import discord
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


class _FakeResponse:
    async def defer(self, **kwargs):
        pass


class _FakeFollowup:
    def __init__(self):
        self.sent = []

    async def send(self, *args, **kwargs):
        self.sent.append((args, kwargs))


class _EmbedBatchTooLargeFollowup(_FakeFollowup):
    """Simulates Discord rejecting the combined embeds=[...] send as too large, the same
    way a real /intelligence-brief with rich risk/health data would - see the audit
    finding this pins down."""

    async def send(self, *args, **kwargs):
        if "embeds" in kwargs:
            response = type("R", (), {"status": 400, "reason": "Bad Request", "headers": {}})()
            raise discord.HTTPException(response, {"message": "Embed size exceeds maximum size of 6000"})
        await super().send(*args, **kwargs)


class _FakeInteraction:
    def __init__(self, user_id):
        self.user = type("U", (), {"id": user_id})()
        self.response = _FakeResponse()
        self.followup = _EmbedBatchTooLargeFollowup()


def test_intelligence_brief_falls_back_to_plain_text_when_the_combined_batch_is_too_large(tmp_path):
    """Audit fix: _routes_embed's own internal chunking only protects ITS own length, not
    the combined total across all embeds sent together in this command's one message -
    the exact "stuck thinking forever" bug class every sibling route command already
    learned to guard against (see /mixed-routes' and /multi-stop-route's own fallback
    tests), confirmed missing here by two independent audit findings. The command must
    catch the rejected batched send and fall back to plain text preserving the essential
    content, not raise an uncaught discord.HTTPException or silently drop the reply."""
    async def run():
        db = Database(tmp_path / "brief_fallback.sqlite3", Fernet(Fernet.generate_key()))
        await db.init()

        bot = type("FakeBot", (), {})()
        bot.db = db
        cog = IntelligenceBrief.__new__(IntelligenceBrief)
        cog.bot = bot
        interaction = _FakeInteraction(1)

        # No ship supplied and no saved default - skips _routes_embed/bot.uex entirely,
        # isolating this test to the combined-send guard itself rather than route data.
        await cog.intelligence_brief.callback(cog, interaction, ship=None, budget=None, space_only=False)

        assert interaction.followup.sent, "expected at least one followup"
        for _, kwargs in interaction.followup.sent:
            assert "embeds" not in kwargs, "the batched embed send should have been rejected, not succeeded"
        fallback_text = "\n".join(kwargs["content"] for _, kwargs in interaction.followup.sent)
        assert "Intelligence Brief" in fallback_text, fallback_text
        assert "24-Hour Supply" in fallback_text, fallback_text

    asyncio.run(run())


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


def test_routes_embed_now_shows_limiting_factors_health_and_confidence(tmp_path):
    """Centralized Route Presentation: /intelligence-brief's route recommendations used to
    show only a bare risk-label summary - no limiting-factor explanation (Load-Limiting
    Explanations shipped for /mixed-routes and /multi-stop-route but was never applied
    here), no terminal-health warnings, and no confidence rating at all, unlike every
    sibling route command. Now built from the same shared bot.uex.route_presentation
    helpers those commands use."""
    async def run():
        cog, client = await _make_cog(tmp_path, "brief_limiting_factors.sqlite3", _MIXED_ROUTES_ROWS, ship_scu=10)
        try:
            embed = await cog._routes_embed("TestShip", None, False)
        finally:
            await client.aclose()

        assert embed.fields, "expected at least one route field"
        combined = "\n".join(field.value or "" for field in embed.fields)
        assert "limited by" in combined, combined
        assert "Confidence:" in combined, combined

    asyncio.run(run())


def test_routes_embed_discloses_truncation_instead_of_silently_dropping_routes(tmp_path, monkeypatch):
    """Centralized Route Presentation: /intelligence-brief had NO Discord-size protection
    at all before - every sibling route command already learned this lesson the hard way
    (see PROJECT_CONTEXT.md's embed-budget entries) but it was never applied here. This
    forces the shared add_chunked_fields call to reject one route deterministically and
    confirms the command discloses the omission rather than raising or silently vanishing
    a route."""
    async def run():
        cog, client = await _make_cog(tmp_path, "brief_truncation.sqlite3", _MIXED_ROUTES_ROWS, ship_scu=10)
        call_count = {"n": 0}
        real_add_chunked_fields = intelligence_brief_module.add_chunked_fields

        def flaky_add_chunked_fields(embed, *, name, lines):
            call_count["n"] += 1
            if call_count["n"] == 1:
                return False
            return real_add_chunked_fields(embed, name=name, lines=lines)

        monkeypatch.setattr(intelligence_brief_module, "add_chunked_fields", flaky_add_chunked_fields)
        try:
            embed = await cog._routes_embed("TestShip", None, False)
        finally:
            await client.aclose()

        assert "omitted" in (embed.footer.text or "").lower(), embed.footer.text

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
