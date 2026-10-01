"""Every name-filtered /commodities_prices lookup uses only the commodity asked for.

UEX's commodity_name filter is a case-insensitive SUBSTRING match - confirmed live: 'Gold' also
returns Golden Medmon (71,000 against real Gold's ~31,000) and 'Tin' returns Astatine (listed
first) and HexaPolyMesh Coating. /price's own tests are in test_price_command.py and the
refinery advisor's in test_refinery.py; this file covers the other callers: /best-route, price
and stock alerts, the trending/top-routes refresh, and /commodity-history. The fake UEX here
matches names by substring, as UEX does. Ported from aiv2 commit c4f1aa6.
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock

import httpx
from cryptography.fernet import Fernet

from bot.cogs import trends as trends_module
from bot.cogs.alerts import Alerts
from bot.cogs.prices import Prices, ambiguous_commodity_text
from bot.cogs.stock_alerts import StockAlerts
from bot.cogs.trends import Trends
from bot.db.database import Database
from bot.uex.client import UexClient

# Astatine first, as UEX listed it live for 'Tin'.
ALL_PRICE_ROWS = [
    {"id_terminal": 1, "terminal_name": "Port Tressler", "id_commodity": 9, "commodity_name": "Astatine",
     "price_sell": 2000.0, "price_buy": 1500.0, "scu_buy": 80, "scu_sell": 80, "status_buy": 3,
     "status_sell": 3, "scu_sell_users_rows": 40},
    {"id_terminal": 2, "terminal_name": "Area 18", "id_commodity": 70, "commodity_name": "HexaPolyMesh Coating",
     "price_sell": 900.0, "scu_sell_users_rows": 2},
    {"id_terminal": 3, "terminal_name": "Everus Harbor", "id_commodity": 77, "commodity_name": "Tin",
     "price_buy": 120.0, "scu_buy": 500, "status_buy": 3, "scu_sell_users_rows": 3},
    {"id_terminal": 6, "terminal_name": "Baijini Point", "id_commodity": 77, "commodity_name": "Tin",
     "price_sell": 150.0, "scu_sell": 500, "status_sell": 3},
    {"id_terminal": 4, "terminal_name": "Ashland", "id_commodity": 50, "commodity_name": "Golden Medmon",
     "price_sell": 71000.0, "price_buy": 50000.0, "scu_buy": 20, "scu_sell_users_rows": 90},
    {"id_terminal": 5, "terminal_name": "CBD Lorville", "id_commodity": 33, "commodity_name": "Gold",
     "price_sell": 31000.0, "price_buy": 24000.0, "scu_buy": 0, "scu_sell_users_rows": 12},
]
CATALOG = [
    {"id": 9, "name": "Astatine", "is_buyable": 1, "is_sellable": 1},
    {"id": 70, "name": "HexaPolyMesh Coating", "is_buyable": 0, "is_sellable": 1},
    {"id": 77, "name": "Tin", "is_buyable": 1, "is_sellable": 1},
    {"id": 50, "name": "Golden Medmon", "is_buyable": 1, "is_sellable": 1},
    {"id": 33, "name": "Gold", "is_buyable": 1, "is_sellable": 1},
]


def _matching(needle: str) -> list[dict]:
    """/commodities_prices?commodity_name=X as UEX really answers it: every row whose name CONTAINS X."""
    return [dict(r) for r in ALL_PRICE_ROWS if needle.lower() in r["commodity_name"].lower()]


async def _substring_prices(**kwargs) -> list[dict]:
    return _matching(kwargs["commodity_name"])


class _FakeFollowup:
    def __init__(self):
        self.sent = []

    async def send(self, *args, **kwargs):
        self.sent.append((args, kwargs))


class _FakeInteraction:
    def __init__(self, user_id: int = 1) -> None:
        self.user = NS(id=user_id)
        self.response = NS(defer=AsyncMock())
        self.followup = _FakeFollowup()


def _sent_text(interaction) -> str:
    """Everything the command sent, plain text and embeds alike, for 'never mentions X' checks."""
    parts = []
    for args, kwargs in interaction.followup.sent:
        parts.extend(str(a) for a in args)
        embeds = [kwargs["embed"]] if kwargs.get("embed") else list(kwargs.get("embeds") or [])
        for embed in embeds:
            parts.append(str(embed.title))
            parts.append(str(embed.footer.text))
            parts.extend(f"{f.name}\n{f.value}" for f in embed.fields)
    return "\n".join(parts)


# -- /best-route ----------------------------------------------------------------------------------------------------

async def _best_route(tmp_path, commodity: str) -> tuple[_FakeInteraction, list[str]]:
    """Runs the real /best-route callback against a fake UEX that substring-matches, as UEX does. Returns the
    interaction and the id_commodity of every /commodities_routes request it made."""
    db = Database(tmp_path / "best_route_exact.sqlite3", Fernet(Fernet.generate_key()))
    await db.init()
    route_requests: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if "commodities_prices" in path:
            return httpx.Response(200, json={"status": "ok",
                                             "data": _matching(request.url.params["commodity_name"])})
        if "commodities_routes" in path:
            route_requests.append(request.url.params.get("id_commodity"))
        return httpx.Response(200, json={"status": "ok", "data": []})

    client = UexClient(app_token="test", base_url="https://uex.test")
    await client._client.aclose()
    client._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    cog = Prices.__new__(Prices)
    cog.bot = NS(db=db, uex=client)
    interaction = _FakeInteraction()
    try:
        await cog.best_route.callback(cog, interaction, commodity=commodity)
    finally:
        await client.aclose()
    return interaction, route_requests


def test_best_route_for_tin_routes_tin_even_when_uex_lists_astatine_first(tmp_path):
    """Live bug: /best-route took rows[0]'s id_commodity, so 'Tin' routed Astatine."""
    async def run():
        interaction, route_requests = await _best_route(tmp_path, "Tin")

        assert route_requests == ["77"], route_requests
        shown = _sent_text(interaction)
        assert "Everus Harbor" in shown and "Baijini Point" in shown
        assert "Astatine" not in shown and "Port Tressler" not in shown and "Area 18" not in shown

    asyncio.run(run())


def test_best_route_asks_which_one_when_the_name_is_several_commodities(tmp_path):
    async def run():
        interaction, route_requests = await _best_route(tmp_path, "Gol")

        assert interaction.followup.sent == [((ambiguous_commodity_text("Gol", ["Gold", "Golden Medmon"]),), {})]
        assert route_requests == []

    asyncio.run(run())


# -- price alerts ---------------------------------------------------------------------------------------------------

def test_a_gold_price_alert_ignores_golden_medmons_price():
    """A 'Gold sells for at least 50,000' alert fired on Golden Medmon's 71,000."""
    async def run():
        alerts = [
            {"id": 1, "user_id": 10, "channel_id": 5, "commodity_name": "Gold",
             "direction": "sell_at_least", "target_price": 50000},
            {"id": 2, "user_id": 10, "channel_id": 5, "commodity_name": "Gold",
             "direction": "sell_at_least", "target_price": 30000},
        ]
        cog = Alerts.__new__(Alerts)
        cog.bot = NS(db=NS(list_active_alerts=AsyncMock(return_value=alerts)),
                     uex=NS(get_commodities_prices=AsyncMock(side_effect=_substring_prices)))
        cog._fire_alert = AsyncMock()

        await cog._poll_alerts_once()

        fired = [(call.args[0]["id"], call.args[1]) for call in cog._fire_alert.await_args_list]
        assert fired == [(2, "best sell price is now **31000.00 aUEC/unit**")]

    asyncio.run(run())


def test_a_price_alert_on_a_name_matching_several_commodities_is_skipped_not_guessed():
    async def run():
        alert = {"id": 3, "user_id": 10, "channel_id": 5, "commodity_name": "Gol",
                 "direction": "sell_at_least", "target_price": 1}
        cog = Alerts.__new__(Alerts)
        cog.bot = NS(db=NS(list_active_alerts=AsyncMock(return_value=[alert])),
                     uex=NS(get_commodities_prices=AsyncMock(side_effect=_substring_prices)))
        cog._fire_alert = AsyncMock()

        await cog._poll_alerts_once()

        cog._fire_alert.assert_not_awaited()

    asyncio.run(run())


# -- stock alerts ---------------------------------------------------------------------------------------------------

def test_a_gold_stock_alert_ignores_golden_medmon_restocks():
    """Golden Medmon in stock at Ashland used to be reported as 'Gold is back in stock'."""
    async def run():
        alert = {"id": 7, "user_id": 10, "channel_id": 5, "commodity_name": "Gold", "scope": "personal",
                 "ship_query": None}
        db = NS(list_active_stock_alerts=AsyncMock(return_value=[alert]),
                get_stock_alert_terminal_state=AsyncMock(return_value={}),
                upsert_stock_alert_terminal_state=AsyncMock(),
                get_default_ship=AsyncMock(return_value=None))
        cog = StockAlerts.__new__(StockAlerts)
        cog.bot = NS(db=db, uex=NS(get_commodities_prices=AsyncMock(side_effect=_substring_prices)))
        cog._notify_stock_alert = AsyncMock()

        await cog._poll_stock_alerts_once()

        cog._notify_stock_alert.assert_not_awaited()
        assert [call.args[1] for call in db.upsert_stock_alert_terminal_state.await_args_list] == [5]

    asyncio.run(run())


# -- the trending / top-routes refresh loop -------------------------------------------------------------------------

def _trends_cog(uex) -> Trends:
    cog = Trends.__new__(Trends)
    cog.bot = NS(uex=uex)
    cog._trending, cog._trending_updated_at, cog._trending_lock = [], None, asyncio.Lock()
    cog._top_scored_routes, cog._top_scored_routes_updated_at = [], None
    cog._top_scored_routes_lock = asyncio.Lock()
    cog._top_in_stock_routes, cog._top_in_stock_routes_updated_at = [], None
    cog._top_in_stock_routes_lock = asyncio.Lock()
    return cog


def test_trending_refresh_keeps_each_commodity_to_its_own_rows(monkeypatch):
    """Gold's trending best sell was Golden Medmon's 71,000, and Tin's routes were Astatine's (fetched twice)."""
    monkeypatch.setattr(trends_module, "_TRENDING_CALL_DELAY", 0)

    async def run():
        uex = NS(get_commodities=AsyncMock(return_value=CATALOG),
                 get_commodities_prices=AsyncMock(side_effect=_substring_prices),
                 get_commodities_routes=AsyncMock(return_value=[]))
        cog = _trends_cog(uex)

        await cog._refresh_trending_once()

        best_sell = {e.commodity_name: e.best_sell_price for e in cog.get_trending_snapshot()}
        assert best_sell["Gold"] == 31000.0
        assert best_sell["Tin"] == 150.0
        route_ids = [call.kwargs["id_commodity"] for call in uex.get_commodities_routes.await_args_list]
        assert sorted(route_ids) == [9, 33, 50, 70, 77], "each commodity's routes fetched once, by its own id"

    asyncio.run(run())


# -- /commodity-history ---------------------------------------------------------------------------------------------

def test_commodity_history_charts_the_commodity_asked_for():
    """Golden Medmon's busier terminal won the 'most traded' default and was charted under Gold's name."""
    async def run():
        uex = NS(get_commodities_prices=AsyncMock(side_effect=_substring_prices),
                 get_commodities_prices_history=AsyncMock(return_value=[]))
        cog = _trends_cog(uex)
        interaction = _FakeInteraction()

        await cog.commodity_history.callback(cog, interaction, commodity="Gold")

        uex.get_commodities_prices_history.assert_awaited_once_with(id_terminal=5, id_commodity=33)
        assert interaction.followup.sent[0][0][0] == "No historical price data for Gold at CBD Lorville yet."

    asyncio.run(run())


def test_commodity_history_asks_which_one_when_the_name_is_several_commodities():
    async def run():
        uex = NS(get_commodities_prices=AsyncMock(side_effect=_substring_prices),
                 get_commodities_prices_history=AsyncMock(return_value=[]))
        cog = _trends_cog(uex)
        interaction = _FakeInteraction()

        await cog.commodity_history.callback(cog, interaction, commodity="Gol")

        assert interaction.followup.sent == [((ambiguous_commodity_text("Gol", ["Gold", "Golden Medmon"]),), {})]
        uex.get_commodities_prices_history.assert_not_awaited()

    asyncio.run(run())
