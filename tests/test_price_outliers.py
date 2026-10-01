"""Cross-terminal price-outlier warnings (bot/uex/price_outliers.py).

Added after a real UEX data-entry error - Rayari Kaltag showing Fresh Food at 2,614 aUEC/SCU
while every other terminal agreed around 21,614 - went unflagged by every existing warning
and confidence signal. Ported from aiv2 commit a6bd024 (itself from production's never-
merged feature/price-outlier-detection branch), onto today's commands: /best-route (both
branches), /mixed-routes, /multi-stop-route and /intelligence-brief.
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock

import httpx
from cryptography.fernet import Fernet

from bot.cogs.intelligence_brief import IntelligenceBrief
from bot.cogs.prices import Prices
from bot.db.database import Database
from bot.uex.client import UexClient
from bot.uex.mixed_routes import MixedCargoItem
from bot.uex.price_outliers import (
    MIN_SIBLINGS_FOR_COMPARISON,
    OUTLIER_RATIO_THRESHOLD,
    find_price_outlier,
    format_price_outlier_warning,
    index_commodity_prices,
)
from bot.uex.route_presentation import cargo_item_warnings, price_outlier_warnings
from bot.uex.trading_preferences import DEFAULT_TRADING_PREFERENCES
from tests.route_results import route_results


def _rows(*entries: tuple[int, int, float, float]) -> list[dict]:
    """(id_commodity, id_terminal, price_buy, price_sell) -> market-row-shaped dicts."""
    return [dict(id_commodity=c, id_terminal=t, price_buy=buy, price_sell=sell) for c, t, buy, sell in entries]


# The real incident's shape: one terminal at 2,614, its siblings around 21,614.
FRESH_FOOD = _rows((120, 72, 2614, 0), (120, 39, 21614, 0), (120, 40, 21500, 0), (120, 41, 21700, 0))


# -- the check itself -----------------------------------------------------------------------

def test_index_groups_by_commodity_and_side():
    index = index_commodity_prices(_rows((1, 10, 100, 150), (1, 11, 110, 160), (2, 10, 500, 600)))
    assert sorted(index[(1, "buy")]) == [(10, 100), (11, 110)]
    assert sorted(index[(1, "sell")]) == [(10, 150), (11, 160)]
    assert index[(2, "buy")] == [(10, 500)]


def test_index_skips_missing_and_non_positive_prices():
    rows = [
        dict(id_commodity=1, id_terminal=10, price_buy=0, price_sell=None),
        dict(id_commodity=1, id_terminal=11, price_buy=None, price_sell=-5),
        dict(id_commodity=None, id_terminal=12, price_buy=100, price_sell=150),
        dict(id_commodity=1, id_terminal=None, price_buy=100, price_sell=150),
    ]
    assert index_commodity_prices(rows) == {}


def test_the_real_fresh_food_error_is_flagged():
    outlier = find_price_outlier(index_commodity_prices(FRESH_FOOD), id_commodity=120, id_terminal=72,
                                 side="buy", price=2614)
    assert outlier is not None
    assert (outlier.sibling_count, outlier.sibling_median) == (3, 21614)
    assert outlier.ratio > OUTLIER_RATIO_THRESHOLD


def test_a_price_far_above_the_others_is_flagged_too():
    index = index_commodity_prices(_rows((1, 10, 100000, 0), (1, 11, 1000, 0), (1, 12, 1100, 0), (1, 13, 900, 0)))
    outlier = find_price_outlier(index, id_commodity=1, id_terminal=10, side="buy", price=100000)
    assert outlier is not None and outlier.price > outlier.sibling_median


def test_ordinary_variance_is_not_flagged():
    # 2x the others' median is real terminal-to-terminal variance, not the ~8x error.
    index = index_commodity_prices(_rows((1, 10, 2000, 0), (1, 11, 1000, 0), (1, 12, 1050, 0), (1, 13, 950, 0)))
    assert find_price_outlier(index, id_commodity=1, id_terminal=10, side="buy", price=2000) is None


def test_too_few_other_terminals_means_cant_tell():
    index = index_commodity_prices(_rows((1, 10, 2614, 0), (1, 39, 21614, 0)))
    assert len(index[(1, "buy")]) - 1 < MIN_SIBLINGS_FOR_COMPARISON
    assert find_price_outlier(index, id_commodity=1, id_terminal=10, side="buy", price=2614) is None


def test_the_checked_terminal_never_counts_as_its_own_sibling():
    # Two real siblings plus terminal 10's own row: still too few to compare.
    index = index_commodity_prices(_rows((1, 10, 2614, 0), (1, 11, 21614, 0), (1, 12, 21500, 0)))
    assert find_price_outlier(index, id_commodity=1, id_terminal=10, side="buy", price=2614) is None


def test_buy_and_sell_are_checked_separately():
    index = index_commodity_prices(_rows(
        (1, 10, 2614, 500), (1, 11, 21614, 510), (1, 12, 21500, 490), (1, 13, 21700, 505)))
    assert find_price_outlier(index, id_commodity=1, id_terminal=10, side="buy", price=2614) is not None
    assert find_price_outlier(index, id_commodity=1, id_terminal=10, side="sell", price=500) is None


def test_an_unknown_commodity_or_price_is_never_flagged():
    index = index_commodity_prices(FRESH_FOOD)
    assert find_price_outlier(index, id_commodity=None, id_terminal=72, side="buy", price=2614) is None
    assert find_price_outlier(index, id_commodity=120, id_terminal=72, side="buy", price=None) is None


def test_the_warning_names_direction_and_median_but_not_which_is_right():
    outlier = find_price_outlier(index_commodity_prices(FRESH_FOOD), id_commodity=120, id_terminal=72,
                                 side="buy", price=2614)
    assert format_price_outlier_warning(outlier, label="origin buy") == (
        "origin buy price 2,614 is 8.3x below the median of 3 other terminals (21,614) for this commodity"
        " - could be a real deal or a data error, verify before committing")


# -- the shared warning lines (bot/uex/route_presentation.py) -------------------------------

def _item(**overrides) -> MixedCargoItem:
    source = dict(id_terminal=72, scu_buy=10, status_buy=1)
    destination = dict(id_terminal=39, scu_sell=10, status_sell=1)
    source.update(overrides.pop("source", {}))
    destination.update(overrides.pop("destination", {}))
    defaults = dict(
        id_commodity=120, commodity_name="Fresh Food", quantity_scu=10, buy_price=2614, sell_price=21600,
        available_scu=10, investment=26140, profit=189860, source=source, destination=destination,
        limiting_factors=("stock",),
    )
    defaults.update(overrides)
    return MixedCargoItem(**defaults)


_NO_STATUS = {"buy": {}, "sell": {}}


def test_cargo_warnings_flag_an_outlier_buy_price_by_commodity():
    lines = cargo_item_warnings(_item(), status_lookup=_NO_STATUS,
                                price_outlier_index=index_commodity_prices(FRESH_FOOD))
    assert "⚠️ Fresh Food origin buy price 2,614 is 8.3x below the median of 3 other terminals" in "\n".join(lines)


def test_cargo_warnings_flag_an_outlier_sell_price_with_the_leg_prefix():
    index = index_commodity_prices(_rows(
        (120, 39, 0, 200000), (120, 40, 0, 20000), (120, 41, 0, 21000), (120, 42, 0, 19000)))
    lines = cargo_item_warnings(_item(sell_price=200000), status_lookup=_NO_STATUS, prefix="Leg 2 ",
                                price_outlier_index=index)
    assert any(line.startswith("Leg 2 ⚠️ Fresh Food destination sell price 200,000 is") for line in lines), lines


def test_cargo_warnings_skip_the_check_without_an_index_or_when_prices_agree():
    assert not any("other terminals" in line for line in cargo_item_warnings(_item(), status_lookup=_NO_STATUS))
    agreeing = index_commodity_prices(_rows((120, 72, 21600, 0), (120, 39, 21614, 0), (120, 40, 21500, 0),
                                            (120, 41, 21700, 0)))
    lines = cargo_item_warnings(_item(buy_price=21600), status_lookup=_NO_STATUS, price_outlier_index=agreeing)
    assert not any("other terminals" in line for line in lines)


def test_price_outlier_warnings_checks_each_side_it_has_a_terminal_for():
    index = index_commodity_prices(FRESH_FOOD)
    assert price_outlier_warnings(index, id_commodity=120, origin_id=72, buy_price=2614,
                                  destination_id=None, sell_price=99) == [
        "⚠️ " + format_price_outlier_warning(
            find_price_outlier(index, id_commodity=120, id_terminal=72, side="buy", price=2614), label="origin buy")]
    assert price_outlier_warnings(index, id_commodity=120, origin_id=None, buy_price=2614,
                                  destination_id=None, sell_price=None) == []


# -- through the real commands ------------------------------------------------------------

class _Followup:
    def __init__(self):
        self.sent = []

    async def send(self, *args, **kwargs):
        self.sent.append((args, kwargs))


class _Interaction:
    def __init__(self):
        self.user = type("U", (), {"id": 1})()
        self.response = NS(defer=AsyncMock())
        self.followup = _Followup()


async def _prices_cog(tmp_path, handler, market_rows=None) -> tuple[Prices, UexClient]:
    db = Database(tmp_path / "outliers.sqlite3", Fernet(Fernet.generate_key()))
    await db.init()
    if market_rows:
        await db.record_terminal_market_snapshot(market_rows)
    client = UexClient(app_token="test", base_url="https://uex.test")
    await client._client.aclose()
    client._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    bot = type("FakeBot", (), {})()
    bot.db, bot.uex, bot.get_cog = db, client, (lambda name: None)
    cog = Prices.__new__(Prices)
    cog.bot = bot
    return cog, client


def _ok(data):
    return httpx.Response(200, json={"status": "ok", "data": data})


# The Fresh Food error at terminal 1, three agreeing siblings, and a buyer at terminal 5.
_FRESH_FOOD_PRICES = [
    {"id_commodity": 1, "commodity_name": "Fresh Food", "id_terminal": 1, "terminal_name": "Rayari Kaltag",
     "price_buy": 2614, "price_sell": 0, "scu_buy": 10, "status_buy": 3},
    *({"id_commodity": 1, "commodity_name": "Fresh Food", "id_terminal": t, "terminal_name": f"Sibling {t}",
       "price_buy": p, "price_sell": 0, "scu_buy": 10, "status_buy": 3}
      for t, p in ((2, 21614), (3, 21500), (4, 21700))),
    {"id_commodity": 1, "commodity_name": "Fresh Food", "id_terminal": 5, "terminal_name": "Buyer",
     "price_buy": 0, "price_sell": 30000, "scu_sell": 10, "status_sell": 3},
]


def test_best_route_primary_branch_flags_the_outlier(tmp_path):
    def handler(request):
        if "commodities_prices" in request.url.path:
            return _ok(_FRESH_FOOD_PRICES)
        if "commodities_routes" in request.url.path:
            return _ok([{
                "id_terminal_origin": 1, "id_terminal_destination": 5, "origin_terminal_name": "Rayari Kaltag",
                "destination_terminal_name": "Buyer", "price_origin": 2614, "price_destination": 30000,
                "price_margin": 90, "price_roi": 1000, "distance": 5, "score": 100, "scu_origin": 10,
                "scu_destination": 10, "status_origin": 3, "status_destination": 3, "profit": 273860,
            }])
        return _ok([])

    async def run():
        cog, client = await _prices_cog(tmp_path, handler)
        interaction = _Interaction()
        try:
            await cog.best_route.callback(cog, interaction, commodity="Fresh Food")
        finally:
            await client.aclose()
        return route_results(interaction.followup.sent)

    results = asyncio.run(run())
    assert results and results.embeds
    assert "⚠️ origin buy price 2,614 is 8.3x below the median of 3 other terminals (21,614)" in results.field_text()


def test_best_route_fallback_branch_flags_the_outlier(tmp_path):
    def handler(request):
        if "commodities_prices" in request.url.path:
            return _ok(_FRESH_FOOD_PRICES)
        return _ok([])  # no /commodities_routes data: the buy/sell-pairing fallback

    async def run():
        cog, client = await _prices_cog(tmp_path, handler)
        interaction = _Interaction()
        try:
            await cog.best_route.callback(cog, interaction, commodity="Fresh Food")
        finally:
            await client.aclose()
        return route_results(interaction.followup.sent)

    results = asyncio.run(run())
    assert results and results.embeds
    assert "⚠️ origin buy price 2,614 is 8.3x below" in results.field_text()


def _vehicles(request):
    if "vehicles" in request.url.path:
        return _ok([{"name": "TestShip", "scu": 10, "pad_type": "M"}])
    if "terminals_distances" in request.url.path:
        return _ok({"distance": 1.0})
    return _ok([])


def _market(commodity, terminal, name, terminal_name, **values):
    return {"id_commodity": commodity, "id_terminal": terminal, "commodity_name": name,
            "terminal_name": terminal_name, "price_buy": 0, "price_sell": 0, "scu_buy": 0, "scu_sell": 0,
            "status_buy": None, "status_sell": None, **values}


# Two commodities from Origin (1) to Destination (2), Stileron's buy price the error; the
# siblings only trade Stileron, so no other mixed load competes.
_MIXED_OUTLIER_ROWS = [
    _market(1, 1, "Stileron", "Origin", price_buy=2614, scu_buy=4, status_buy=1),
    _market(1, 2, "Stileron", "Destination", price_sell=30000, scu_sell=10, status_sell=1),
    _market(2, 1, "Cobalt", "Origin", price_buy=20, scu_buy=95, status_buy=1),
    _market(2, 2, "Cobalt", "Destination", price_sell=50, scu_sell=80, status_sell=1),
    *(_market(1, t, "Stileron", f"Sibling {t}", price_buy=p, scu_buy=10, status_buy=1)
      for t, p in ((20, 21614), (21, 21500), (22, 21700))),
]


def test_mixed_routes_flags_the_outlier(tmp_path):
    async def run():
        cog, client = await _prices_cog(tmp_path, _vehicles, _MIXED_OUTLIER_ROWS)
        interaction = _Interaction()
        try:
            await cog.mixed_routes.callback(cog, interaction, ship="TestShip")
        finally:
            await client.aclose()
        return route_results(interaction.followup.sent)

    results = asyncio.run(run())
    assert results and results.pages
    assert "⚠️ Stileron origin buy price 2,614 is 8.3x below the median of 3 other terminals" in results.all_text()


_MULTI_STOP_OUTLIER_ROWS = [
    _market(1, 1, "Stileron", "Origin", price_buy=2614, scu_buy=10, status_buy=1),
    _market(1, 2, "Stileron", "Midpoint", price_sell=30000, scu_sell=10, status_sell=1),
    _market(2, 2, "Cobalt", "Midpoint", price_buy=50, scu_buy=10, status_buy=1),
    _market(2, 3, "Cobalt", "Final", price_sell=90, scu_sell=10, status_sell=1),
    *(_market(1, t, "Stileron", f"Sibling {t}", price_buy=p, scu_buy=10, status_buy=1)
      for t, p in ((20, 21614), (21, 21500), (22, 21700))),
]


def test_multi_stop_route_flags_the_outlier_on_its_leg(tmp_path):
    async def run():
        cog, client = await _prices_cog(tmp_path, _vehicles, _MULTI_STOP_OUTLIER_ROWS)
        interaction = _Interaction()
        try:
            await cog.multi_stop_route.callback(cog, interaction, ship="TestShip")
        finally:
            await client.aclose()
        return route_results(interaction.followup.sent)

    results = asyncio.run(run())
    assert results and results.pages
    text = results.all_text()
    assert "Leg 1 ⚠️ Stileron origin buy price 2,614 is 8.3x below" in text, text


def test_intelligence_brief_routes_flag_the_outlier(tmp_path):
    async def run():
        db = Database(tmp_path / "brief_outliers.sqlite3", Fernet(Fernet.generate_key()))
        await db.init()
        await db.record_terminal_market_snapshot(_MIXED_OUTLIER_ROWS)
        client = UexClient(app_token="test", base_url="https://uex.test")
        await client._client.aclose()
        client._client = httpx.AsyncClient(transport=httpx.MockTransport(_vehicles))
        cog = IntelligenceBrief.__new__(IntelligenceBrief)
        cog.bot = type("FakeBot", (), {"db": db, "uex": client})()
        try:
            return await cog._routes_embed("TestShip", dict(DEFAULT_TRADING_PREFERENCES), budget=None,
                                           space_only=False)
        finally:
            await client.aclose()

    embed = asyncio.run(run())
    combined = "\n".join(field.value or "" for field in embed.fields)
    assert "⚠️ Stileron origin buy price 2,614 is 8.3x below" in combined, combined
