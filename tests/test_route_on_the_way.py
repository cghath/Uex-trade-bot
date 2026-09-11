"""Tests for /route-on-the-way: like /routes-from, filters the SAME background-refreshed
candidate pool /top-routes reads from - but down to routes matching BOTH a resolved origin
AND a resolved destination, not just the origin."""
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
    return Database(tmp_path / "route_on_the_way.sqlite3", Fernet(Fernet.generate_key()))


async def _seed_terminals(db: Database) -> None:
    await db.upsert_terminal_reference([
        {"id": 1, "name": "Area18"},
        {"id": 2, "name": "Port Tressler"},
        {"id": 3, "name": "Baijini Point"},
    ])


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


def _make_cog(bot) -> Trends:
    cog = Trends.__new__(Trends)
    cog.bot = bot
    return cog


def test_route_on_the_way_reports_when_the_origin_cannot_be_resolved(tmp_path):
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
        cog = _make_cog(bot)
        interaction = _FakeInteraction(1)

        try:
            await cog.route_on_the_way.callback(
                cog, interaction, origin="Nowhere Station", destination="Area18"
            )
        finally:
            await client.aclose()

        assert interaction.response.messages, "expected an immediate response"
        assert "couldn't find" in interaction.response.messages[0][0][0].lower()
        assert not interaction.followup.sent

    asyncio.run(run())


def test_route_on_the_way_reports_when_the_destination_cannot_be_resolved(tmp_path):
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
        cog = _make_cog(bot)
        interaction = _FakeInteraction(1)

        try:
            await cog.route_on_the_way.callback(
                cog, interaction, origin="Area18", destination="Nowhere Station"
            )
        finally:
            await client.aclose()

        assert interaction.response.messages, "expected an immediate response"
        assert "couldn't find" in interaction.response.messages[0][0][0].lower()
        assert not interaction.followup.sent

    asyncio.run(run())


def test_route_on_the_way_rejects_the_same_terminal_for_both(tmp_path):
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
        cog = _make_cog(bot)
        interaction = _FakeInteraction(1)

        try:
            await cog.route_on_the_way.callback(cog, interaction, origin="Area18", destination="Area18")
        finally:
            await client.aclose()

        assert "same terminal" in interaction.response.messages[0][0][0].lower()
        assert not interaction.followup.sent

    asyncio.run(run())


def test_route_on_the_way_reports_when_no_route_matches_both_ends(tmp_path):
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
        cog = _make_cog(bot)
        cog._top_scored_routes_lock = asyncio.Lock()
        # A real route exists from Area18, but not to Port Tressler specifically.
        cog._top_scored_routes = [_entry(id_commodity=1, origin_id=1, origin_name="Area18",
                                          destination_id=3, destination_name="Baijini Point", score=100)]
        cog._top_scored_routes_updated_at = datetime.now(timezone.utc)
        interaction = _FakeInteraction(1)

        try:
            await cog.route_on_the_way.callback(
                cog, interaction, origin="Area18", destination="Port Tressler"
            )
        finally:
            await client.aclose()

        assert interaction.response.messages, "expected an immediate response"
        assert "no profitable routes" in interaction.response.messages[0][0][0].lower()
        assert not interaction.followup.sent

    asyncio.run(run())


def test_route_on_the_way_filters_the_shared_pool_to_both_resolved_ends(tmp_path):
    """The same candidate pool /top-routes reads from - filtering must require BOTH the
    origin and destination to match, not just one of them."""
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
        cog = _make_cog(bot)
        cog._top_scored_routes_lock = asyncio.Lock()
        cog._top_scored_routes = [
            # Correct match: Area18 -> Port Tressler.
            _entry(id_commodity=1, origin_id=1, origin_name="Area18", destination_id=2,
                   destination_name="Port Tressler", score=100),
            # Same origin, wrong destination - must be excluded.
            _entry(id_commodity=2, origin_id=1, origin_name="Area18", destination_id=3,
                   destination_name="Baijini Point", score=200),
            # Same destination, wrong origin - must be excluded.
            _entry(id_commodity=3, origin_id=3, origin_name="Baijini Point", destination_id=2,
                   destination_name="Port Tressler", score=300),
        ]
        cog._top_scored_routes_updated_at = datetime.now(timezone.utc)
        interaction = _FakeInteraction(1)

        try:
            await cog.route_on_the_way.callback(
                cog, interaction, origin="Area18", destination="Port Tressler"
            )
        finally:
            await client.aclose()

        assert interaction.followup.sent, "expected at least one followup"
        commodity_names = " ".join(
            kwargs["embed"].title for _, kwargs in interaction.followup.sent
            if kwargs.get("embed") and kwargs["embed"].title
        )
        assert "Commodity 1" in commodity_names
        assert "Commodity 2" not in commodity_names
        assert "Commodity 3" not in commodity_names

    asyncio.run(run())


def test_route_on_the_way_shows_the_budget_in_the_footer_and_caps_the_cargo_estimate(tmp_path):
    """User-requested: /route-on-the-way's budget option should both cap the estimated
    haul (via estimate_route_cargo's new budget parameter) and disclose the budget
    itself, not just silently apply it."""
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
        cog = _make_cog(bot)
        cog._top_scored_routes_lock = asyncio.Lock()
        cog._top_scored_routes = [
            _entry(id_commodity=1, origin_id=1, origin_name="Area18", destination_id=2,
                   destination_name="Port Tressler", score=100),
        ]
        cog._top_scored_routes_updated_at = datetime.now(timezone.utc)
        interaction = _FakeInteraction(1)

        try:
            await cog.route_on_the_way.callback(
                cog, interaction, origin="Area18", destination="Port Tressler", budget=1000.0
            )
        finally:
            await client.aclose()

        embeds = [kwargs["embed"] for _, kwargs in interaction.followup.sent if kwargs.get("embed")]
        intro_footer = embeds[0].footer.text
        assert "budget 1,000 aUEC" in intro_footer

        route_embed = next(e for e in embeds if e.title and "Commodity 1" in e.title)
        route_text = route_embed.fields[0].value
        assert "limited by your budget" in route_text

    asyncio.run(run())


def test_route_on_the_way_falls_back_to_a_saved_budget_preference(tmp_path):
    """No budget passed on the call itself - only the saved /set-trading-preferences
    default should apply, same fallback pattern as auto-load-only/system."""
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
        cog = _make_cog(bot)
        cog._top_scored_routes_lock = asyncio.Lock()
        cog._top_scored_routes = [
            _entry(id_commodity=1, origin_id=1, origin_name="Area18", destination_id=2,
                   destination_name="Port Tressler", score=100),
        ]
        cog._top_scored_routes_updated_at = datetime.now(timezone.utc)
        interaction = _FakeInteraction(1)

        try:
            await cog.route_on_the_way.callback(cog, interaction, origin="Area18", destination="Port Tressler")
        finally:
            await client.aclose()

        embeds = [kwargs["embed"] for _, kwargs in interaction.followup.sent if kwargs.get("embed")]
        assert "budget 1,000 aUEC" in embeds[0].footer.text

    asyncio.run(run())


def test_route_on_the_way_direction_matters(tmp_path):
    """A route in the reverse direction (destination -> origin) must not satisfy a query
    for origin -> destination - the command is direction-specific, matching how the
    player actually asked the question ('from where I am to where I'm going')."""
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
        cog = _make_cog(bot)
        cog._top_scored_routes_lock = asyncio.Lock()
        # Only the reverse direction (Port Tressler -> Area18) exists in the pool.
        cog._top_scored_routes = [
            _entry(id_commodity=1, origin_id=2, origin_name="Port Tressler", destination_id=1,
                   destination_name="Area18", score=100),
        ]
        cog._top_scored_routes_updated_at = datetime.now(timezone.utc)
        interaction = _FakeInteraction(1)

        try:
            await cog.route_on_the_way.callback(
                cog, interaction, origin="Area18", destination="Port Tressler"
            )
        finally:
            await client.aclose()

        assert "no profitable routes" in interaction.response.messages[0][0][0].lower()
        assert not interaction.followup.sent

    asyncio.run(run())
