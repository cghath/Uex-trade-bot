"""Route commands say the real reason nothing (or no cargo math) came back.

Audit findings UX-2 and MSG-4: a saved /set-trading-preferences filter that ruled out every
route read as "nothing exists right now", with no hint the player's own setting was the
cause; and a missing ship, a saved ship that no longer matches one, and UEX's ship list
failing to load all read "set a default ship" - even to a player who had one.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from unittest.mock import AsyncMock

import httpx
from cryptography.fernet import Fernet

from bot.cogs.prices import Prices
from bot.cogs.trends import Trends
from bot.db.database import Database
from bot.uex.client import UexClient
from bot.uex.exceptions import UexApiError
from bot.uex.route_presentation import missing_ship_cargo_line, missing_ship_note
from bot.uex.trading_preferences import saved_filter_labels, saved_filters_hint
from bot.uex.trends import ScoredRouteEntry

USER = 111


# -- the helpers ---------------------------------------------------------------------------

def test_missing_ship_note_names_the_real_cause():
    assert "set a default ship" in missing_ship_note(None, lookup_failed=False)
    assert "ship list didn't load" in missing_ship_note("Caterpillar", lookup_failed=True)
    assert "'Catterpilar' didn't match a single ship" in missing_ship_note("Catterpilar", lookup_failed=False)
    assert "set a ship" in missing_ship_cargo_line(None, lookup_failed=False)
    assert "didn't load" in missing_ship_cargo_line("Caterpillar", lookup_failed=True)
    assert "'Catterpilar' didn't match" in missing_ship_cargo_line("Catterpilar", lookup_failed=False)


def test_saved_filters_hint_wording():
    assert saved_filters_hint([]) == ""
    one = saved_filters_hint(saved_filter_labels(auto_load_only=True))
    assert "Your saved auto-load-only setting is on" in one and "override it" in one
    two = saved_filters_hint(saved_filter_labels(auto_load_only=True, system="Pyro"))
    assert "auto-load-only and system Pyro settings are on" in two and "override them" in two


# -- end to end through the real commands ----------------------------------------------------

class _Interaction:
    def __init__(self):
        self.user = type("U", (), {"id": USER})()
        self.response = type("R", (), {"defer": AsyncMock()})()
        self.sent: list[tuple[tuple, dict]] = []

        async def send(*args, **kwargs):
            self.sent.append((args, kwargs))

        self.followup = type("F", (), {"send": staticmethod(send)})()

    def texts(self) -> str:
        parts = [str(a[0]) for a, _ in self.sent if a]
        parts += [kw["content"] for _, kw in self.sent if kw.get("content")]
        parts += [kw["embed"].footer.text or "" for _, kw in self.sent if kw.get("embed")]
        return "\n".join(parts)


async def _client(rows: list[dict]) -> UexClient:
    def handler(request: httpx.Request) -> httpx.Response:
        data = rows if "commodities_prices" in request.url.path else []
        return httpx.Response(200, json={"status": "ok", "data": data})

    client = UexClient(app_token="test", base_url="https://uex.test")
    await client._client.aclose()
    client._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return client


async def _db(tmp_path) -> Database:
    db = Database(tmp_path / "routes.sqlite3", Fernet(Fernet.generate_key()))
    await db.init()
    await db.upsert_terminal_reference([
        {"id": 1, "name": "Origin", "is_auto_load": False}, {"id": 2, "name": "Dest", "is_auto_load": False},
    ])
    return db


def _entry() -> ScoredRouteEntry:
    return ScoredRouteEntry(
        commodity_name="Gold", id_commodity=5, origin_terminal_name="Origin", destination_terminal_name="Dest",
        price_origin=10.0, price_destination=60.0, price_margin=None, price_roi=None, distance=None, score=50,
        scu_origin=50, scu_destination=50, status_origin=1, status_destination=1,
        origin_terminal_id=1, destination_terminal_id=2,
    )


async def _top_routes(tmp_path, *, prefs: dict, ship_name: str | None = None, vehicles_error: bool = False, **options):
    db = await _db(tmp_path)
    if prefs:
        await db.set_trading_preferences(USER, **prefs)
    if ship_name:
        await db.set_default_ship(USER, ship_name)
    client = await _client([])
    if vehicles_error:
        client.get_vehicles = AsyncMock(side_effect=UexApiError("UEX is down"))
    cog = Trends.__new__(Trends)
    cog.bot = type("B", (), {"db": db, "uex": client, "get_cog": staticmethod(lambda name: None)})()
    cog._top_scored_routes_lock = asyncio.Lock()
    cog._top_scored_routes = [_entry()]
    cog._top_scored_routes_updated_at = datetime.now(timezone.utc)
    interaction = _Interaction()
    try:
        await cog.top_routes.callback(cog, interaction, **options)
    finally:
        await client.aclose()
    return interaction.texts()


def test_top_routes_names_a_saved_auto_load_filter_that_ruled_everything_out(tmp_path):
    text = asyncio.run(_top_routes(tmp_path, prefs={"auto_load_only": True}))
    assert "No routes with auto-load at both ends found right now." in text
    assert "Your saved auto-load-only setting is on" in text
    assert "try again" not in text, "it isn't a data-collection problem"


def test_top_routes_doesnt_blame_saved_settings_for_an_option_passed_on_the_command(tmp_path):
    text = asyncio.run(_top_routes(tmp_path, prefs={}, auto_load_only=True))
    assert "No routes with auto-load at both ends found right now." in text
    assert "saved" not in text


def test_top_routes_says_when_a_saved_ship_no_longer_matches(tmp_path):
    text = asyncio.run(_top_routes(tmp_path, prefs={}, ship_name="Retired Ship"))
    assert "'Retired Ship' didn't match a single ship" in text
    assert "set a default ship" not in text


def test_top_routes_says_when_uex_ship_list_is_down(tmp_path):
    text = asyncio.run(_top_routes(tmp_path, prefs={}, ship_name="Caterpillar", vehicles_error=True))
    assert "UEX's ship list didn't load" in text
    assert "set a default ship" not in text


def test_best_route_names_a_saved_system_filter_that_ruled_everything_out(tmp_path):
    async def run():
        db = await _db(tmp_path)
        await db.set_trading_preferences(USER, preferred_system="Pyro")
        rows = [
            {"id_terminal": 1, "terminal_name": "Origin", "id_commodity": 5, "commodity_name": "Gold",
             "price_buy": 10, "price_sell": 0, "scu_buy": 100, "scu_sell": 0, "status_buy": 3, "status_sell": None},
            {"id_terminal": 2, "terminal_name": "Dest", "id_commodity": 5, "commodity_name": "Gold",
             "price_buy": 0, "price_sell": 60, "scu_buy": 0, "scu_sell": 100, "status_buy": None, "status_sell": 5},
        ]
        client = await _client(rows)
        cog = Prices.__new__(Prices)
        cog.bot = type("B", (), {"db": db, "uex": client})()
        interaction = _Interaction()
        try:
            await cog.best_route.callback(cog, interaction, commodity="Gold")
        finally:
            await client.aclose()
        return interaction.texts()

    text = asyncio.run(run())
    assert "No routes confirmed entirely within Pyro" in text
    assert "Your saved system Pyro setting is on" in text


def test_mixed_routes_names_every_saved_filter_when_nothing_fits(monkeypatch):
    from types import SimpleNamespace as NS

    from bot.cogs import prices as prices_module
    from bot.uex.trading_preferences import DEFAULT_TRADING_PREFERENCES

    monkeypatch.setattr(prices_module, "build_mixed_routes", lambda *args, **kwargs: [])

    async def run():
        prefs = dict(DEFAULT_TRADING_PREFERENCES, capital_ship_access=True, auto_load_only=True)
        db = NS(get_default_ship=AsyncMock(return_value="Ship"), get_trading_preferences=AsyncMock(return_value=prefs),
                get_mixed_route_market_rows=AsyncMock(return_value=[]))
        uex = NS(get_vehicles=AsyncMock(return_value=[dict(name="Ship", scu=100, pad_type="M")]),
                 get_space_stations=AsyncMock(return_value=[]))
        cog = Prices.__new__(Prices)
        cog.bot = NS(db=db, uex=uex)
        interaction = _Interaction()
        await cog.mixed_routes.callback(cog, interaction)
        return interaction.texts()

    text = asyncio.run(run())
    assert "with auto-load at both ends" in text, "auto-load is checked at both terminals, not just the origin"
    assert "Your saved capital-ship access and auto-load-only settings are on" in text
