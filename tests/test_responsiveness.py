"""Audit REL-14 and REL-12: nothing a slash command does should freeze the bot's one
event loop or blow Discord's ~3s autocomplete deadline.

- Charts are drawn in a worker thread (asyncio.to_thread), on matplotlib's Figure API
  rather than pyplot, whose global figure registry isn't thread-safe.
- Every UEX-backed autocomplete answers within bot/autocomplete.py's budget, showing no
  suggestions (not an error) when UEX is slow or down.
"""
import asyncio
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace as NS

import pytest

from bot import autocomplete
from bot.autocomplete import fetch_within
from bot.cogs import liquidity, prices
from bot.cogs.liquidity import LiquidityCog
from bot.cogs.marketplace import category_autocomplete
from bot.cogs.mining_locations import mineable_commodity_autocomplete
from bot.cogs.prices import commodity_name_autocomplete
from bot.cogs.refinery import raw_commodity_autocomplete
from bot.cogs.ships import ship_name_autocomplete
from bot.uex import charts
from bot.uex.exceptions import UexApiError
from tests.test_route_send_shape import _MULTI_STOP_ROWS, _run_command

PNG_MAGIC = b"\x89PNG\r\n\x1a\n"

# ---- charts -------------------------------------------------------------------------


def test_charts_module_does_not_use_pyplot():
    assert not hasattr(charts, "plt"), "pyplot's global state isn't safe to use from worker threads"


def _history_rows(n=6):
    return [
        {"date_added": 1_790_000_000 + i * 3600, "price_buy": 100 + i, "price_sell": 120 + i}
        for i in range(1, n)
    ]


def test_charts_render_correctly_from_several_threads_at_once():
    def draw(i):
        return charts.render_price_history_chart(
            commodity_name=f"Gold {i}", terminal_name="Area18", history_rows=_history_rows(),
        )

    with ThreadPoolExecutor(max_workers=4) as pool:
        buffers = list(pool.map(draw, range(8)))
    assert all(b is not None and b.getvalue().startswith(PNG_MAGIC) for b in buffers)


class _Followup:
    def __init__(self):
        self.sent = []

    async def send(self, *args, **kwargs):
        self.sent.append((args, kwargs))


class _Response:
    async def defer(self, *args, **kwargs):
        pass


def test_liquidity_trends_draws_its_chart_off_the_event_loop(monkeypatch):
    seen = {}

    def fake_render(**kwargs):
        seen["thread"] = threading.current_thread()
        return None

    monkeypatch.setattr(liquidity, "render_liquidity_history_chart", fake_render)
    history = [
        {"item_name": "Gold", "score": score, "negotiations_success": 1, "negotiations_open": 0,
         "id_item": 7, "recorded_hour": f"2026-09-28 0{i}:00:00"}
        for i, score in enumerate((40.0, 55.0))
    ]

    async def get_liquidity_history(item):
        return history

    cog = LiquidityCog.__new__(LiquidityCog)
    cog.db = NS(get_liquidity_history=get_liquidity_history)
    interaction = NS(response=_Response(), followup=_Followup())
    asyncio.run(LiquidityCog.liquidity_trends.callback(cog, interaction, item="Gold"))

    assert seen["thread"] is not threading.main_thread()
    assert interaction.followup.sent


def test_diminishing_returns_draws_its_chart_off_the_event_loop(tmp_path, monkeypatch):
    seen = {}
    real_render = prices.render_budget_curve_chart

    def recording_render(*args, **kwargs):
        seen["thread"] = threading.current_thread()
        return real_render(*args, **kwargs)

    monkeypatch.setattr(prices, "render_budget_curve_chart", recording_render)
    asyncio.run(_run_command(
        tmp_path, "responsiveness.sqlite3", _MULTI_STOP_ROWS,
        lambda cog, interaction: cog.diminishing_returns.callback(cog, interaction, ship="TestShip"),
    ))
    assert seen["thread"] is not threading.main_thread()


# ---- autocompletes ------------------------------------------------------------------

_ROWS = [
    {"id": 3, "name": "Cutlass Black", "is_raw": 1, "is_refinable": 1, "is_buyable": 1},
    {"id": 4, "name": "Agricium (Ore)", "is_raw": 1, "is_refinable": 1, "is_buyable": 1},
]


def _uex(behavior):
    """A fake UexClient whose every method behaves the same way: 'ok', 'slow', 'down',
    or 'bug' (an exception that isn't UEX's fault and must surface)."""
    async def fetch(*args, **kwargs):
        if behavior == "slow":
            await asyncio.sleep(1.0)
        elif behavior == "down":
            raise UexApiError("UEX is down")
        elif behavior == "bug":
            raise KeyError("not a UEX failure")
        return _ROWS

    return NS(get_vehicles=fetch, get_commodities=fetch, get_categories=fetch)


AUTOCOMPLETES = [
    (ship_name_autocomplete, "cut", "Cutlass Black"),
    (commodity_name_autocomplete, "agri", "Agricium (Ore)"),
    (mineable_commodity_autocomplete, "agri", "Agricium (Ore)"),
    (raw_commodity_autocomplete, "agri", "Agricium (Ore)"),
    (category_autocomplete, "cut", "Cutlass Black"),
]


def _interaction(behavior):
    return NS(client=NS(uex=_uex(behavior)), namespace=NS(type="item"))


@pytest.mark.parametrize("handler,typed,expected", AUTOCOMPLETES, ids=lambda v: getattr(v, "__name__", ""))
def test_autocomplete_answers_normally_when_uex_is_quick(handler, typed, expected):
    choices = asyncio.run(handler(_interaction("ok"), typed))
    assert [c.name for c in choices] == [expected]


@pytest.mark.parametrize("handler,typed,expected", AUTOCOMPLETES, ids=lambda v: getattr(v, "__name__", ""))
def test_autocomplete_gives_up_before_discords_deadline_when_uex_is_slow(handler, typed, expected, monkeypatch):
    monkeypatch.setattr(autocomplete, "AUTOCOMPLETE_BUDGET_SECONDS", 0.05)
    started = time.monotonic()
    assert asyncio.run(handler(_interaction("slow"), typed)) == []
    assert time.monotonic() - started < 0.9


@pytest.mark.parametrize("handler,typed,expected", AUTOCOMPLETES, ids=lambda v: getattr(v, "__name__", ""))
def test_autocomplete_shows_nothing_when_uex_is_down(handler, typed, expected):
    assert asyncio.run(handler(_interaction("down"), typed)) == []


def test_fetch_within_raises_a_real_bug_instead_of_hiding_it():
    async def buggy():
        raise KeyError("not a UEX failure")

    with pytest.raises(KeyError):
        asyncio.run(fetch_within(buggy()))
