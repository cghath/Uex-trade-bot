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
    """deferred/send_message raise on a second call, matching real discord.py's
    InteractionResponded - so a caller that defers twice (e.g. a command that deferred
    itself but forgot already_deferred=True calling _send_ranked_routes) fails loudly
    instead of silently passing."""

    def __init__(self):
        self.messages = []
        self.deferred = False

    async def defer(self, **kwargs):
        if self.deferred:
            raise RuntimeError("interaction already responded to (double defer)")
        self.deferred = True

    async def send_message(self, *args, **kwargs):
        if self.deferred:
            raise RuntimeError("interaction already responded to")
        self.messages.append((args, kwargs))
        self.deferred = True


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


def test_routes_from_defers_before_the_terminal_lookup(tmp_path):
    """A slow resolve_terminal_id_by_name must not risk Discord's ~3s initial-response
    deadline - the command has to acknowledge the interaction first."""
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

        real_resolve = db.resolve_terminal_id_by_name
        seen_deferred = []

        async def spying_resolve(name):
            seen_deferred.append(interaction.response.deferred)
            return await real_resolve(name)

        db.resolve_terminal_id_by_name = spying_resolve

        try:
            await cog.routes_from.callback(cog, interaction, location="Area18")
        finally:
            await client.aclose()

        assert seen_deferred == [True], "the interaction must already be deferred by the time the DB is queried"

    asyncio.run(run())


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

        assert not interaction.response.messages, "must defer, not respond directly"
        assert len(interaction.followup.sent) == 1, "must not proceed to ranking a location it couldn't resolve"
        assert "couldn't find" in interaction.followup.sent[0][0][0].lower()

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

        assert not interaction.response.messages, "must defer, not respond directly"
        assert len(interaction.followup.sent) == 1
        assert "no profitable routes" in interaction.followup.sent[0][0][0].lower()

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


def test_routes_from_shows_the_budget_in_the_footer_and_caps_the_cargo_estimate(tmp_path):
    """Consistency fix: /routes-from shared the same underlying candidate pool and
    cargo/budget machinery as /route-on-the-way but never accepted a budget option at
    all - unlike /mixed-routes, /multi-stop-route, and /route-on-the-way, which all cap
    the cargo estimate by budget and disclose it."""
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
            _entry(id_commodity=1, origin_id=1, origin_name="Area18", destination_id=2,
                   destination_name="Port Tressler", score=100),
        ]
        cog._top_scored_routes_updated_at = datetime.now(timezone.utc)
        interaction = _FakeInteraction(1)

        try:
            await cog.routes_from.callback(cog, interaction, location="Area18", budget=1000.0)
        finally:
            await client.aclose()

        embeds = [kwargs["embed"] for _, kwargs in interaction.followup.sent if kwargs.get("embed")]
        intro_footer = embeds[0].footer.text
        assert "budget 1,000 aUEC" in intro_footer

        route_embed = next(e for e in embeds if e.title and "Commodity 1" in e.title)
        route_text = route_embed.fields[0].value
        assert "limited by your budget" in route_text

    asyncio.run(run())


def test_routes_from_falls_back_to_a_saved_budget_preference(tmp_path):
    async def run():
        db = _make_db(tmp_path)
        await db.init()
        await _seed_terminals(db)
        await db.set_trading_preferences(1, budget=1000.0)
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
            _entry(id_commodity=1, origin_id=1, origin_name="Area18", destination_id=2,
                   destination_name="Port Tressler", score=100),
        ]
        cog._top_scored_routes_updated_at = datetime.now(timezone.utc)
        interaction = _FakeInteraction(1)

        try:
            await cog.routes_from.callback(cog, interaction, location="Area18")
        finally:
            await client.aclose()

        embeds = [kwargs["embed"] for _, kwargs in interaction.followup.sent if kwargs.get("embed")]
        assert "budget 1,000 aUEC" in embeds[0].footer.text

    asyncio.run(run())
