"""Regression tests: /best-route and /top-routes must filter their FULL candidate list
before truncating to a display size, not after. Both previously ranked-then-sliced to a
small top-N by profit/score first and applied auto-load-only/system filtering only to
what survived that cut - a route that would pass the filter but wasn't in the top N was
silently unreachable, no matter how the filter itself was implemented.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone

from cryptography.fernet import Fernet
import httpx

from bot.cogs.prices import Prices
from bot.cogs.trends import Trends
from bot.db.database import Database
from bot.uex.client import UexClient
from bot.uex.trends import ScoredRouteEntry


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


def _catch_all_transport(rows: list[dict]) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        if "commodities_prices" in request.url.path:
            return httpx.Response(200, json={"status": "ok", "data": rows})
        return httpx.Response(200, json={"status": "ok", "data": []})

    return httpx.MockTransport(handler)


def test_best_route_fallback_auto_load_filter_finds_a_lower_ranked_route(tmp_path):
    """15 decoy buy/sell pairs (all far more profitable, none auto-load-capable at both
    ends) outrank the one pair that IS auto-load-capable at both ends. Before the fix,
    /best-route's fallback branch ranked all pairs by profit, sliced to MAX_FIELD_ROWS (5),
    and only then filtered - the auto-load pair, ranked far below 5th, was discarded
    before the filter ever ran."""
    async def run():
        db = Database(tmp_path / "best_route.sqlite3", Fernet(Fernet.generate_key()))
        await db.init()
        terminal_refs = [{"id": 7, "name": "BA", "is_auto_load": True}, {"id": 8, "name": "SA", "is_auto_load": True}]
        terminal_refs += [{"id": i, "name": f"T{i}", "is_auto_load": False} for i in range(1, 7)]
        await db.upsert_terminal_reference(terminal_refs)

        def row(id_terminal, name, price_buy, price_sell, scu):
            return {
                "id_terminal": id_terminal, "terminal_name": name, "id_commodity": 1,
                "commodity_name": "Cobalt", "price_buy": price_buy, "price_sell": price_sell,
                "scu_buy": scu if price_buy else 0, "scu_sell": scu if price_sell else 0,
                "status_buy": 1 if price_buy else None, "status_sell": 1 if price_sell else None,
            }

        rows = [
            row(1, "B1", 1, 0, 100), row(2, "B2", 2, 0, 100), row(3, "B3", 3, 0, 100),
            row(4, "S1", 0, 500, 100), row(5, "S2", 0, 400, 100), row(6, "S3", 0, 300, 100),
            row(7, "BA", 100, 0, 100), row(8, "SA", 0, 110, 100),
        ]

        client = UexClient(app_token="test", base_url="https://uex.test")
        await client._client.aclose()
        client._client = httpx.AsyncClient(transport=_catch_all_transport(rows))

        bot = type("FakeBot", (), {})()
        bot.db = db
        bot.uex = client
        cog = Prices.__new__(Prices)
        cog.bot = bot
        interaction = _FakeInteraction(111)

        try:
            await cog.best_route.callback(cog, interaction, commodity="Cobalt", auto_load_only=True)

            assert interaction.followup.sent, "expected at least one followup"
            _, kwargs = interaction.followup.sent[0]
            embed = kwargs.get("embed")
            assert embed is not None, f"expected an embed response, got: {interaction.followup.sent}"
            field_names = " ".join(f.name for f in embed.fields)
            assert "BA" in field_names and "SA" in field_names, (
                f"expected the auto-load-capable BA->SA route in the response, got fields: {field_names}"
            )
        finally:
            await client.aclose()

    asyncio.run(run())


def test_best_route_fallback_pool_is_not_capped_at_a_fixed_size(tmp_path):
    """Regression: the fallback's first fix widened the candidate pool from
    MAX_FIELD_ROWS (5) to a fixed ROUTE_FILTER_CANDIDATE_POOL (25) before filtering -
    better, but still a cap. A commodity traded at more than 25 terminals (real on live
    UEX data for dozens of commodities) could still have its only auto-load-capable pair
    excluded before the filter ever ran, since best_routes' own `limit` caps which
    buy-side terminals get considered at all, not just how many final routes come back.
    30 decoy buy terminals (prices 1-30, cheaper than the auto-load buy terminal at 31)
    push it past any fixed cap on the buy side specifically."""
    async def run():
        db = Database(tmp_path / "best_route_wide.sqlite3", Fernet(Fernet.generate_key()))
        await db.init()
        terminal_refs = [{"id": 7, "name": "BA", "is_auto_load": True}, {"id": 8, "name": "SA", "is_auto_load": True}]
        terminal_refs += [{"id": 100 + i, "name": f"Decoy{i}", "is_auto_load": False} for i in range(30)]
        terminal_refs += [{"id": 200, "name": "DecoySell", "is_auto_load": False}]
        await db.upsert_terminal_reference(terminal_refs)

        rows = [
            {"id_terminal": 100 + i, "terminal_name": f"Decoy{i}", "id_commodity": 1, "commodity_name": "Cobalt",
             "price_buy": i + 1, "price_sell": 0, "scu_buy": 100, "scu_sell": 0, "status_buy": 1, "status_sell": None}
            for i in range(30)
        ]
        rows.append({"id_terminal": 200, "terminal_name": "DecoySell", "id_commodity": 1, "commodity_name": "Cobalt",
                      "price_buy": 0, "price_sell": 1000, "scu_buy": 0, "scu_sell": 100, "status_buy": None, "status_sell": 1})
        rows.append({"id_terminal": 7, "terminal_name": "BA", "id_commodity": 1, "commodity_name": "Cobalt",
                     "price_buy": 31, "price_sell": 0, "scu_buy": 100, "scu_sell": 0, "status_buy": 1, "status_sell": None})
        rows.append({"id_terminal": 8, "terminal_name": "SA", "id_commodity": 1, "commodity_name": "Cobalt",
                     "price_buy": 0, "price_sell": 50, "scu_buy": 0, "scu_sell": 100, "status_buy": None, "status_sell": 1})

        client = UexClient(app_token="test", base_url="https://uex.test")
        await client._client.aclose()
        client._client = httpx.AsyncClient(transport=_catch_all_transport(rows))

        bot = type("FakeBot", (), {})()
        bot.db = db
        bot.uex = client
        cog = Prices.__new__(Prices)
        cog.bot = bot
        interaction = _FakeInteraction(111)

        try:
            await cog.best_route.callback(cog, interaction, commodity="Cobalt", auto_load_only=True)

            assert interaction.followup.sent, "expected at least one followup"
            _, kwargs = interaction.followup.sent[0]
            embed = kwargs.get("embed")
            assert embed is not None, f"expected an embed response, got: {interaction.followup.sent}"
            field_names = " ".join(f.name for f in embed.fields)
            assert "BA" in field_names and "SA" in field_names, (
                f"expected the auto-load-capable BA->SA route in the response, got fields: {field_names}"
            )
        finally:
            await client.aclose()

    asyncio.run(run())


def test_top_routes_auto_load_filter_finds_a_lower_scored_route(tmp_path):
    """10 decoy entries (score 91-100, none auto-load-capable) outrank the one entry that
    IS auto-load-capable at both ends (score 50). This directly injects the full 11-entry
    pool into cog._top_scored_routes rather than running the real refresh_trending() loop
    (which needs live UEX calls across every tradeable commodity, impractical here), so it
    proves _send_ranked_routes correctly filters a full candidate pool and applies
    display_limit afterward, not that refresh_trending's storage-limit change itself
    (TOP_SCORED_ROUTES_KEEP -> len(route_candidates)) behaves correctly - that one-line
    change is simple enough to be verified by reading it, not by an automated test that
    runs the real loop."""
    async def run():
        db = Database(tmp_path / "top_routes.sqlite3", Fernet(Fernet.generate_key()))
        await db.init()
        terminal_refs = [{"id": 900, "name": "AutoOrigin", "is_auto_load": True},
                          {"id": 901, "name": "AutoDest", "is_auto_load": True}]
        terminal_refs += [
            {"id": 200 + i, "name": f"DecoyOrigin{i}", "is_auto_load": False} for i in range(10)
        ]
        terminal_refs += [
            {"id": 300 + i, "name": f"DecoyDest{i}", "is_auto_load": False} for i in range(10)
        ]
        await db.upsert_terminal_reference(terminal_refs)

        decoys = [
            ScoredRouteEntry(
                commodity_name=f"Decoy{i}", id_commodity=100 + i,
                origin_terminal_name=f"DecoyOrigin{i}", destination_terminal_name=f"DecoyDest{i}",
                price_origin=10.0, price_destination=200.0, price_margin=None, price_roi=None,
                distance=None, score=100 - i, scu_origin=50, scu_destination=50,
                status_origin=1, status_destination=1,
                origin_terminal_id=200 + i, destination_terminal_id=300 + i,
            )
            for i in range(10)
        ]
        auto_load_entry = ScoredRouteEntry(
            commodity_name="AutoLoadable", id_commodity=999,
            origin_terminal_name="AutoOrigin", destination_terminal_name="AutoDest",
            price_origin=10.0, price_destination=60.0, price_margin=None, price_roi=None,
            distance=None, score=50, scu_origin=50, scu_destination=50,
            status_origin=1, status_destination=1,
            origin_terminal_id=900, destination_terminal_id=901,
        )

        client = UexClient(app_token="test", base_url="https://uex.test")
        await client._client.aclose()
        client._client = httpx.AsyncClient(transport=_catch_all_transport([]))

        bot = type("FakeBot", (), {})()
        bot.db = db
        bot.uex = client
        cog = Trends.__new__(Trends)
        cog.bot = bot
        cog._top_scored_routes_lock = asyncio.Lock()
        cog._top_scored_routes = [*decoys, auto_load_entry]
        cog._top_scored_routes_updated_at = datetime.now(timezone.utc)
        interaction = _FakeInteraction(111)

        try:
            await cog.top_routes.callback(cog, interaction, auto_load_only=True)

            assert interaction.followup.sent, "expected at least one followup"
            _, kwargs = interaction.followup.sent[0]
            embed = kwargs.get("embed")
            assert embed is not None, f"expected an embed response, got: {interaction.followup.sent}"
            field_names = " ".join(f.name for f in embed.fields)
            assert "AutoOrigin" in field_names and "AutoDest" in field_names, (
                f"expected the auto-load-capable route in the response, got fields: {field_names}"
            )
        finally:
            await client.aclose()

    asyncio.run(run())
