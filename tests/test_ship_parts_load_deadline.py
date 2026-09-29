"""Audit REL-6: a hanging Star Citizen Wiki made each lookup take ~96s (3 attempts x 30s),
a detail miss made two, and batches ran one after another, so a cold /ship-parts-finder
category could outlast Discord's 15-minute interaction window and never show. A browse
now stops starting new lookups after LOAD_TIME_BUDGET_SECONDS: what's still running
finishes in the background and fills the cache, and the parts left unanswered are counted
in the browser's "wiki didn't respond" note."""
import asyncio
import inspect
import time
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock

from bot.cogs import ship_parts_finder
from bot.cogs.ship_parts_finder import DETAIL_BATCH_SIZE, LOAD_TIME_BUDGET_SECONDS, ShipPartsFinder
from bot.uex.ship_parts import ShipPort

PORT = ShipPort(name="hardpoint_power_plant", port_type="PowerPlant", size_min=1, size_max=1)


def _catalog(n):
    return [{"id": i, "uuid": f"u{i}", "category": "Power Plants", "size": "1", "name": f"Plant {i}"}
            for i in range(1, n + 1)]


def _prices(n):
    return [{"id_item": i, "price_buy": 1000 + i, "id_terminal": 5, "terminal_name": "Shop 5"}
            for i in range(1, n + 1)]


def _cog(n, *, detail_delay, distance_delay=0.0):
    calls = {"detail": 0}

    async def get_item_detail(uuid):
        calls["detail"] += 1
        await asyncio.sleep(detail_delay)
        return {"uuid": uuid, "name": f"Plant {uuid[1:]}", "size": 1, "type": "PowerPlant"}

    async def get_terminal_distance(origin, destination):
        await asyncio.sleep(distance_delay)
        return {"distance": 12.5}

    uex = NS(get_item_catalog=AsyncMock(return_value=_catalog(n)),
             get_items_prices_all=AsyncMock(return_value=_prices(n)),
             get_terminal_distance=get_terminal_distance)
    wiki = NS(get_item_detail=get_item_detail, find_item_detail_by_name=AsyncMock(return_value=None),
              find_item_variants_by_name=AsyncMock(return_value=[]))
    db = NS(get_terminal_references_by_ids=AsyncMock(return_value={}))
    return ShipPartsFinder(NS(uex=uex, db=db), wiki_client=wiki, start_refresh=False), calls


def test_a_hanging_wiki_returns_partial_results_within_the_budget():
    async def run():
        cog, _ = _cog(6, detail_delay=1.0)
        started = time.monotonic()
        parts = await cog.candidates_for_port(PORT, category="Power Plants", time_budget=0.05)
        return parts, time.monotonic() - started

    parts, took = asyncio.run(run())
    assert took < 0.9
    assert parts.wiki_unavailable == 6, "every part the wiki didn't answer in time is counted in the note"
    assert len(parts) == 6, "parts still show from UEX's own data rather than vanishing"


def test_lookups_cut_off_by_the_budget_still_fill_the_cache():
    async def run():
        cog, calls = _cog(3, detail_delay=0.2)
        first = await cog.candidates_for_port(PORT, category="Power Plants", time_budget=0.05)
        await asyncio.sleep(0.4)  # the cut-off lookups finish in the background
        second = await cog.candidates_for_port(PORT, category="Power Plants", time_budget=0.05)
        return first, second, calls

    first, second, calls = asyncio.run(run())
    assert first.wiki_unavailable == 3
    assert second.wiki_unavailable == 0 and all(p.get("_detail_loaded") for p in second)
    assert calls["detail"] == 3, "the second browse was served from the cache the first one filled"


def test_no_new_batch_starts_once_the_budget_is_spent():
    async def run():
        cog, calls = _cog(3 * DETAIL_BATCH_SIZE, detail_delay=0.3)
        await cog.candidates_for_port(PORT, category="Power Plants", time_budget=0.05)
        return calls

    calls = asyncio.run(run())
    assert calls["detail"] == DETAIL_BATCH_SIZE, "only the first batch was ever sent to the wiki"


def test_slow_distances_come_back_unknown_instead_of_holding_up_the_list():
    async def run():
        cog, _ = _cog(2, detail_delay=0.0, distance_delay=1.0)
        started = time.monotonic()
        parts = await cog.candidates_for_port(PORT, category="Power Plants", origin_id=9, time_budget=0.05)
        return parts, time.monotonic() - started

    parts, took = asyncio.run(run())
    assert took < 0.9
    assert [p["_distance_gm"] for p in parts] == [None, None]


def test_without_a_budget_every_lookup_is_waited_for():
    async def run():
        cog, _ = _cog(3, detail_delay=0.1)
        return await cog.candidates_for_port(PORT, category="Power Plants", time_budget=None)

    parts = asyncio.run(run())
    assert parts.wiki_unavailable == 0 and all(p.get("_detail_loaded") for p in parts)


def test_a_normal_load_is_unaffected_by_the_budget():
    async def run():
        cog, _ = _cog(20, detail_delay=0.0)
        return await cog.candidates_for_port(PORT, category="Power Plants")

    parts = asyncio.run(run())
    assert len(parts) == 20 and parts.wiki_unavailable == 0


def test_interactive_loads_get_the_budget_by_default():
    default = inspect.signature(ShipPartsFinder.candidates_for_port).parameters["time_budget"].default
    assert default == LOAD_TIME_BUDGET_SECONDS
    # Well inside Discord's 15-minute interaction window, with room for the final edit.
    assert LOAD_TIME_BUDGET_SECONDS < 60
    source = inspect.getsource(ship_parts_finder.PartsBrowserView)
    assert "time_budget" not in source, "the browser's loads must not opt out of the budget"
