"""Tests for /routes-from: the terminal name lookup it's built on (Database.
search_terminals_by_name/resolve_terminal_id_by_name) and the command itself, which
filters the SAME background-refreshed candidate pool /top-routes reads from down to one
origin terminal rather than computing its own ranking."""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone

from cryptography.fernet import Fernet
import httpx

from bot.cogs.trends import Trends
from bot.db.database import Database
from bot.uex.client import UexClient
from bot.uex.trends import ScoredRouteEntry


def _make_db(tmp_path) -> Database:
    return Database(tmp_path / "routes_from.sqlite3", Fernet(Fernet.generate_key()))


# -- Database.search_terminals_by_name / resolve_terminal_id_by_name --------------------

async def _seed_terminals(db: Database) -> None:
    await db.upsert_terminal_reference([
        {"id": 1, "name": "Area18"},
        {"id": 2, "name": "Port Tressler"},
        {"id": 3, "name": "Area18 Alternate Yard"},
    ])


def test_search_terminals_by_name_matches_substrings(tmp_path):
    async def run():
        db = _make_db(tmp_path)
        await db.init()
        await _seed_terminals(db)
        results = await db.search_terminals_by_name("area18")
        names = {row["terminal_name"] for row in results}
        assert names == {"Area18", "Area18 Alternate Yard"}

    asyncio.run(run())


def test_search_terminals_by_name_with_empty_query_returns_nothing(tmp_path):
    async def run():
        db = _make_db(tmp_path)
        await db.init()
        await _seed_terminals(db)
        assert await db.search_terminals_by_name("   ") == []

    asyncio.run(run())


def test_resolve_terminal_id_by_name_exact_match(tmp_path):
    async def run():
        db = _make_db(tmp_path)
        await db.init()
        await _seed_terminals(db)
        assert await db.resolve_terminal_id_by_name("Area18") == (1, "Area18")
        assert await db.resolve_terminal_id_by_name("area18") == (1, "Area18"), "case-insensitive"

    asyncio.run(run())


def test_resolve_terminal_id_by_name_unique_substring_match(tmp_path):
    async def run():
        db = _make_db(tmp_path)
        await db.init()
        await _seed_terminals(db)
        assert await db.resolve_terminal_id_by_name("Tressler") == (2, "Port Tressler")

    asyncio.run(run())


def test_resolve_terminal_id_by_name_ambiguous_substring_is_refused(tmp_path):
    """Never guesses between candidates - matching find_item_id_by_name's established
    tiered/gated pattern (bot/uex/marketplace.py)."""
    async def run():
        db = _make_db(tmp_path)
        await db.init()
        await _seed_terminals(db)
        assert await db.resolve_terminal_id_by_name("Area18") is not None  # exact still resolves
        assert await db.resolve_terminal_id_by_name("Area") is None  # matches both Area18 entries

    asyncio.run(run())


def test_resolve_terminal_id_by_name_no_match(tmp_path):
    async def run():
        db = _make_db(tmp_path)
        await db.init()
        await _seed_terminals(db)
        assert await db.resolve_terminal_id_by_name("Nonexistent Station") is None

    asyncio.run(run())


# -- /routes-from command -----------------------------------------------------------------

class _FakeResponse:
    def __init__(self):
        self.messages = []

    async def defer(self, **kwargs):
        pass

    async def send_message(self, *args, **kwargs):
        self.messages.append((args, kwargs))


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


def _entry(*, id_commodity, origin_id, origin_name, destination_id, destination_name, score) -> ScoredRouteEntry:
    return ScoredRouteEntry(
        commodity_name=f"Commodity {id_commodity}", id_commodity=id_commodity,
        origin_terminal_name=origin_name, destination_terminal_name=destination_name,
        price_origin=100, price_destination=200, price_margin=50, price_roi=100,
        distance=10, score=score, scu_origin=50, scu_destination=50,
        status_origin=1, status_destination=1,
        origin_terminal_id=origin_id, destination_terminal_id=destination_id,
    )


def test_routes_from_reports_when_the_location_cannot_be_resolved(tmp_path):
    async def run():
        db = _make_db(tmp_path)
        await db.init()
        client = UexClient(app_token="test", base_url="https://uex.test")
        await client._client.aclose()
        client._client = httpx.AsyncClient(transport=httpx.MockTransport(
            lambda request: httpx.Response(200, json={"status": "ok", "data": []})
        ))

        bot = type("FakeBot", (), {})()
        bot.db = db
        bot.uex = client
        bot.get_cog = lambda name: None
        cog = Trends.__new__(Trends)
        cog.bot = bot
        interaction = _FakeInteraction(1)

        try:
            await cog.routes_from.callback(cog, interaction, location="Nowhere Station")
        finally:
            await client.aclose()

        assert interaction.response.messages, "expected an immediate response"
        assert "couldn't find" in interaction.response.messages[0][0][0].lower()
        assert not interaction.followup.sent, "must not proceed to ranking a location it couldn't resolve"

    asyncio.run(run())


def test_routes_from_reports_when_no_routes_originate_there(tmp_path):
    async def run():
        db = _make_db(tmp_path)
        await db.init()
        await _seed_terminals(db)
        client = UexClient(app_token="test", base_url="https://uex.test")
        await client._client.aclose()
        client._client = httpx.AsyncClient(transport=httpx.MockTransport(
            lambda request: httpx.Response(200, json={"status": "ok", "data": []})
        ))

        bot = type("FakeBot", (), {})()
        bot.db = db
        bot.uex = client
        bot.get_cog = lambda name: None
        cog = Trends.__new__(Trends)
        cog.bot = bot
        cog._top_scored_routes_lock = asyncio.Lock()
        cog._top_scored_routes = [_entry(id_commodity=1, origin_id=2, origin_name="Port Tressler",
                                          destination_id=3, destination_name="Elsewhere", score=100)]
        cog._top_scored_routes_updated_at = datetime.now(timezone.utc)
        interaction = _FakeInteraction(1)

        try:
            # Area18 (id 1) has no routes in the pool - only Port Tressler (id 2) does.
            await cog.routes_from.callback(cog, interaction, location="Area18")
        finally:
            await client.aclose()

        assert interaction.response.messages, "expected an immediate response"
        assert "no profitable routes" in interaction.response.messages[0][0][0].lower()
        assert not interaction.followup.sent

    asyncio.run(run())


def test_routes_from_filters_the_shared_pool_to_the_resolved_origin(tmp_path):
    """The same candidate pool /top-routes reads from - filtering must only keep routes
    whose origin matches, not just re-rank everything."""
    async def run():
        db = _make_db(tmp_path)
        await db.init()
        await _seed_terminals(db)
        client = UexClient(app_token="test", base_url="https://uex.test")
        await client._client.aclose()
        client._client = httpx.AsyncClient(transport=httpx.MockTransport(
            lambda request: httpx.Response(200, json={"status": "ok", "data": []})
        ))

        bot = type("FakeBot", (), {})()
        bot.db = db
        bot.uex = client
        bot.get_cog = lambda name: None
        cog = Trends.__new__(Trends)
        cog.bot = bot
        cog._top_scored_routes_lock = asyncio.Lock()
        cog._top_scored_routes = [
            _entry(id_commodity=1, origin_id=1, origin_name="Area18", destination_id=3,
                   destination_name="Destination A", score=100),
            _entry(id_commodity=2, origin_id=2, origin_name="Port Tressler", destination_id=3,
                   destination_name="Destination B", score=200),
        ]
        cog._top_scored_routes_updated_at = datetime.now(timezone.utc)
        interaction = _FakeInteraction(1)

        try:
            await cog.routes_from.callback(cog, interaction, location="Area18")
        finally:
            await client.aclose()

        assert interaction.followup.sent, "expected at least one followup"
        titles = " ".join(
            kwargs["embed"].title for _, kwargs in interaction.followup.sent
            if kwargs.get("embed") and kwargs["embed"].title
        )
        assert "Destination A" in titles
        assert "Destination B" not in titles, "a route from a different origin must not appear"

    asyncio.run(run())
