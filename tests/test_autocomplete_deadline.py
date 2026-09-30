"""bot/autocomplete.py's gather_within, and the two UEX-backed autocompletes that use it:
Discord drops an autocomplete answer after ~3s, so a slow UEX response must turn into "no
suggestions" in time, while the slow fetch still finishes and warms the cache."""
import asyncio
import time
from types import SimpleNamespace as NS

from bot import autocomplete
from bot.autocomplete import gather_within
from bot.cogs.item_finder import sold_item_name_autocomplete
from bot.cogs.ship_shops import listed_ship_autocomplete
from bot.uex.exceptions import UexApiError


async def _value(value, delay=0.0):
    await asyncio.sleep(delay)
    return value


async def _raise(exc):
    raise exc


def test_gather_within_returns_results_exceptions_and_timeouts_in_order():
    async def run():
        slow_done = asyncio.Event()

        async def slow():
            await asyncio.sleep(0.2)
            slow_done.set()
            return "late"

        outcomes = await gather_within(_value("fast"), _raise(UexApiError("down")), slow(), timeout=0.05)
        # The slow fetch was not cancelled - it keeps running so it can fill the cache.
        await asyncio.wait_for(slow_done.wait(), timeout=1)
        return outcomes

    fast, failed, slow = asyncio.run(run())
    assert fast == "fast"
    assert isinstance(failed, UexApiError)
    assert isinstance(slow, TimeoutError)


def _slow_uex(delay):
    async def slow_rows():
        await asyncio.sleep(delay)
        return [{"id": 52, "name": "Cutlass Black", "id_vehicle": 52, "item_name": "Arrowhead"}]
    return NS(
        get_vehicles=slow_rows, get_vehicle_purchase_prices_all=slow_rows,
        get_vehicle_rental_prices_all=slow_rows, get_items_prices_all=slow_rows,
    )


def test_ship_autocomplete_answers_with_nothing_before_a_slow_uex_blocks_discords_deadline(monkeypatch):
    monkeypatch.setattr(autocomplete, "AUTOCOMPLETE_BUDGET_SECONDS", 0.05)
    started = time.monotonic()
    choices = asyncio.run(listed_ship_autocomplete(NS(client=NS(uex=_slow_uex(1.0))), "cut"))
    assert choices == []
    assert time.monotonic() - started < 0.9


def test_item_autocomplete_answers_with_nothing_before_a_slow_uex_blocks_discords_deadline(monkeypatch):
    monkeypatch.setattr(autocomplete, "AUTOCOMPLETE_BUDGET_SECONDS", 0.05)
    started = time.monotonic()
    choices = asyncio.run(sold_item_name_autocomplete(NS(client=NS(uex=_slow_uex(1.0))), "arrow"))
    assert choices == []
    assert time.monotonic() - started < 0.9


def test_both_autocompletes_still_answer_normally_within_the_budget():
    uex = _slow_uex(0.0)
    assert [c.name for c in asyncio.run(listed_ship_autocomplete(NS(client=NS(uex=uex)), "cut"))] == ["Cutlass Black"]
    assert [c.name for c in asyncio.run(sold_item_name_autocomplete(NS(client=NS(uex=uex)), "arrow"))] == ["Arrowhead"]
