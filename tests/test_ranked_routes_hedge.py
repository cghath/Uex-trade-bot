"""Hedge protection on the ranked route lists (/top-routes, /routes-from, /route-on-the-way).

/best-route already warned when a haul would use ALL the stock/demand on record and
suggested a second commodity for the same trip (find_hedge_cargo). The three ranked lists
share one send path, _send_ranked_routes, which showed the same stock-limited numbers with
no warning and no hedge. These run that shared path end to end against a real Database, so
the market rows the hedge reads (get_mixed_route_market_rows) are genuine, not mocked.
/routes-from and /route-on-the-way differ from /top-routes only in where their entries come
from, so the shared function is exercised directly plus one real /top-routes callback.
"""
from __future__ import annotations

import asyncio
from dataclasses import replace
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock

from cryptography.fernet import Fernet

from bot.cogs.trends import Trends
from bot.db.database import Database
from bot.uex.trends import ScoredRouteEntry

SHIP_SCU = 100


def _interaction():
    return NS(user=NS(id=1), response=NS(defer=AsyncMock()), followup=NS(send=AsyncMock()))


def _route(commodity_id=1, name="Taranite", *, origin_id=1, destination_id=101, scu=21, price_origin=100.0):
    return ScoredRouteEntry(
        commodity_name=name, id_commodity=commodity_id,
        origin_terminal_name=f"Origin {origin_id}", destination_terminal_name=f"Destination {destination_id}",
        price_origin=price_origin, price_destination=price_origin + 100, price_margin=50, price_roi=100,
        distance=10, score=100, scu_origin=scu, scu_destination=scu, status_origin=1, status_destination=1,
        origin_terminal_id=origin_id, destination_terminal_id=destination_id,
    )


async def _make_cog(tmp_path, *, seed_hedge=True):
    db = Database(tmp_path / "ranked_hedge.sqlite3", Fernet(Fernet.generate_key()))
    await db.init()
    if seed_hedge:
        # Cobalt trades at the SAME origin/destination pair as the Taranite anchor route. The
        # anchor itself is on record there too, and MORE profitable per SCU than Cobalt, so it
        # would win the hedge slot if find_hedge_cargo were ever allowed to suggest it.
        await db.record_terminal_market_snapshot([
            {"id_commodity": 1, "id_terminal": 1, "commodity_name": "Taranite", "terminal_name": "Origin 1",
             "price_buy": 100, "price_sell": 0, "scu_buy": 21, "scu_sell": 0, "status_buy": 1, "status_sell": None},
            {"id_commodity": 1, "id_terminal": 101, "commodity_name": "Taranite", "terminal_name": "Destination 1",
             "price_buy": 0, "price_sell": 200, "scu_buy": 0, "scu_sell": 21, "status_buy": None, "status_sell": 1},
            {"id_commodity": 2, "id_terminal": 1, "commodity_name": "Cobalt", "terminal_name": "Origin 1",
             "price_buy": 20, "price_sell": 0, "scu_buy": 95, "scu_sell": 0, "status_buy": 1, "status_sell": None},
            {"id_commodity": 2, "id_terminal": 101, "commodity_name": "Cobalt", "terminal_name": "Destination 1",
             "price_buy": 0, "price_sell": 50, "scu_buy": 0, "scu_sell": 80, "status_buy": None, "status_sell": 1},
        ])
    db.get_mixed_route_market_rows = AsyncMock(wraps=db.get_mixed_route_market_rows)
    uex = NS(
        get_vehicles=AsyncMock(return_value=[dict(name="Ship", scu=SHIP_SCU)]),
        get_commodities_status=AsyncMock(return_value={}),
    )
    cog = Trends.__new__(Trends)
    cog.bot = NS(db=db, uex=uex, get_cog=lambda name: None)
    return cog, db


async def _send(cog, entries, **kwargs):
    inter = _interaction()
    await cog._send_ranked_routes(
        inter, entries=entries, updated_at=None, ship="Ship", title="Top routes",
        footer_note="Collected data", log_label="test", display_limit=10, **kwargs,
    )
    return inter


def _route_text(inter) -> str:
    """Every field value of every route embed (the first embed-bearing call is the intro)."""
    embeds = [call.kwargs["embed"] for call in inter.followup.send.call_args_list if call.kwargs.get("embed")]
    return "\n".join(field.value or "" for embed in embeds[1:] for field in embed.fields)


def _route_embeds(inter):
    return [call.kwargs["embed"] for call in inter.followup.send.call_args_list if call.kwargs.get("embed")][1:]


def test_a_stock_limited_route_warns_and_suggests_a_hedge_at_the_same_terminal_pair(tmp_path):
    async def run():
        cog, _ = await _make_cog(tmp_path)
        inter = await _send(cog, [_route()])
        text = _route_text(inter)
        assert "Uses the entire stock/demand currently on record" in text, text
        hedge_lines = [line for line in text.splitlines() if line.startswith("Hedge:")]
        assert len(hedge_lines) == 1 and "Cobalt" in hedge_lines[0], text
        assert "79 SCU" in hedge_lines[0], "fills the ship's spare 79 SCU (100 - 21)"
        assert "Taranite" not in hedge_lines[0], "the anchor commodity is never its own hedge"

    asyncio.run(run())


def test_a_ship_limited_route_gets_no_warning_hedge_or_market_lookup(tmp_path):
    async def run():
        cog, db = await _make_cog(tmp_path)
        inter = await _send(cog, [_route(scu=500)])
        text = _route_text(inter)
        assert "Cargo: **100 SCU**" in text and "limited by" in text, text
        assert "Uses the entire stock" not in text and "Hedge:" not in text, text
        db.get_mixed_route_market_rows.assert_not_awaited()

    asyncio.run(run())


def test_the_market_snapshot_is_loaded_once_however_many_routes_need_a_hedge(tmp_path):
    async def run():
        cog, db = await _make_cog(tmp_path)
        entries = [
            _route(commodity_id=1, name="Taranite"),
            _route(commodity_id=3, name="Laranite", origin_id=1, destination_id=101),
            _route(commodity_id=4, name="Agricium", origin_id=1, destination_id=101),
        ]
        inter = await _send(cog, entries)
        assert _route_text(inter).count("Uses the entire stock") == 3
        assert db.get_mixed_route_market_rows.await_count == 1

    asyncio.run(run())


def test_a_route_with_no_terminal_ids_is_warned_but_never_looks_up_a_hedge(tmp_path):
    async def run():
        cog, db = await _make_cog(tmp_path)
        route = replace(_route(), origin_terminal_id=None, destination_terminal_id=None)
        inter = await _send(cog, [route])
        text = _route_text(inter)
        assert "Uses the entire stock/demand currently on record" in text and "Hedge:" not in text, text
        db.get_mixed_route_market_rows.assert_not_awaited()

    asyncio.run(run())


def test_a_budget_caps_what_the_hedge_may_spend(tmp_path):
    """Anchor: 21 SCU x 100 = 2,100 aUEC. A 2,300 aUEC budget leaves 200 spare, and Cobalt
    costs 20 per SCU - so the hedge may only be 10 SCU, not the 79 SCU of free cargo space."""
    async def run():
        cog, _ = await _make_cog(tmp_path)
        inter = await _send(cog, [_route()], budget=2300.0)
        hedge_lines = [line for line in _route_text(inter).splitlines() if line.startswith("Hedge:")]
        assert len(hedge_lines) == 1 and "10 SCU" in hedge_lines[0], hedge_lines

    asyncio.run(run())


def test_a_route_limited_by_budget_gets_no_warning_or_hedge(tmp_path):
    async def run():
        cog, db = await _make_cog(tmp_path)
        inter = await _send(cog, [_route()], budget=1000.0)
        text = _route_text(inter)
        assert "limited by your budget" in text, text
        assert "Uses the entire stock" not in text and "Hedge:" not in text, text
        db.get_mixed_route_market_rows.assert_not_awaited()

    asyncio.run(run())


def test_a_hedge_lookup_failure_still_sends_every_route(tmp_path):
    """The hedge is additive: a locked database must cost the player the suggestion, not
    their routes."""
    async def run():
        cog, db = await _make_cog(tmp_path)
        db.get_mixed_route_market_rows = AsyncMock(side_effect=RuntimeError("database is locked"))
        inter = await _send(cog, [_route(), _route(commodity_id=3, name="Laranite")])
        text = _route_text(inter)
        assert len(_route_embeds(inter)) == 2
        assert text.count("Uses the entire stock/demand currently on record") == 2
        assert "Hedge:" not in text

    asyncio.run(run())


def test_no_other_commodity_at_the_pair_means_a_warning_but_no_hedge_line(tmp_path):
    async def run():
        cog, _ = await _make_cog(tmp_path, seed_hedge=False)
        inter = await _send(cog, [_route()])
        text = _route_text(inter)
        assert "Uses the entire stock/demand currently on record" in text and "Hedge:" not in text, text

    asyncio.run(run())


def test_the_top_routes_command_shows_the_hedge_through_its_real_callback(tmp_path):
    async def run():
        cog, _ = await _make_cog(tmp_path)
        cog._top_scored_routes = [_route()]
        cog._top_scored_routes_updated_at = None
        cog._top_scored_routes_lock = asyncio.Lock()
        inter = _interaction()
        await cog.top_routes.callback(cog, inter, ship="Ship")
        text = _route_text(inter)
        assert "Hedge:" in text and "Cobalt" in text, text

    asyncio.run(run())


def test_hedged_routes_still_fit_discords_embed_limits(tmp_path):
    async def run():
        cog, _ = await _make_cog(tmp_path)
        inter = await _send(cog, [_route(commodity_id=i, name=f"Commodity {i}") for i in range(1, 11)])
        embeds = _route_embeds(inter)
        assert len(embeds) == 10
        assert all(len(embed) <= 6000 for embed in embeds)
        assert all(len(field.value) <= 1024 for embed in embeds for field in embed.fields)

    asyncio.run(run())
