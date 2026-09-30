"""Audit REL-5: the 45-minute trending/top-routes refresh skipped every commodity it
couldn't fetch and still replaced the cached snapshots, stamped "refreshed just now". A UEX
blip mid-refresh left /trending, /top-routes, /routes-from and /route-on-the-way silently
incomplete until the next cycle. A refresh missing too much now keeps the previous
snapshot (while that one is more complete and under 2 hours old), and any partial snapshot
that is shown says so."""
import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace as NS

import httpx
from cryptography.fernet import Fernet

from bot.cogs import trends as trends_cog
from bot.cogs.trends import REFRESH_KEEP_PREVIOUS_MAX_AGE, REFRESH_MAX_FAILED_SHARE, Trends
from bot.db.database import Database
from bot.uex.client import UexClient
from bot.uex.exceptions import UexApiError
from bot.uex.trends import (
    RefreshGap, ScoredRouteEntry, partial_refresh_hint, partial_refresh_note, should_replace_snapshot,
)
from tests.route_results import route_results

HOUR = timedelta(hours=1)


def _replace(new, previous, age):
    return should_replace_snapshot(new, previous, age, max_failed_share=REFRESH_MAX_FAILED_SHARE,
                                   max_keep_age=REFRESH_KEEP_PREVIOUS_MAX_AGE)


# ---- the policy ----------------------------------------------------------------------------

def test_a_refresh_with_nothing_before_it_is_always_used():
    assert _replace(RefreshGap(15, 20), None, None)


def test_a_refresh_within_the_failure_threshold_replaces_the_snapshot():
    assert _replace(RefreshGap(2, 20), RefreshGap(0, 20), HOUR)


def test_a_mostly_failed_refresh_keeps_a_more_complete_recent_snapshot():
    assert not _replace(RefreshGap(6, 20), RefreshGap(0, 20), HOUR)


def test_a_mostly_failed_refresh_still_beats_a_worse_one():
    assert _replace(RefreshGap(6, 20), RefreshGap(10, 20), HOUR)


def test_a_kept_snapshot_gives_way_once_it_is_too_old():
    assert _replace(RefreshGap(6, 20), RefreshGap(0, 20), REFRESH_KEEP_PREVIOUS_MAX_AGE)


def test_an_empty_commodity_list_never_wipes_a_snapshot():
    assert not _replace(RefreshGap(0, 0), RefreshGap(0, 20), HOUR)


def test_the_notes_say_how_much_is_missing_and_stay_silent_when_nothing_is():
    assert partial_refresh_note(RefreshGap(0, 20)) == "" and partial_refresh_hint(RefreshGap(0, 20)) == ""
    assert partial_refresh_note(RefreshGap(0, 0)) == ""
    assert partial_refresh_note(RefreshGap(3, 20)) == "partial refresh: 3 of 20 commodities couldn't be fetched"
    assert partial_refresh_hint(RefreshGap(3, 20)) == (
        " Some routes may be missing: the last refresh couldn't fetch 3 of 20 commodities."
    )


# ---- the refresh loop --------------------------------------------------------------------

COMMODITIES = [{"id": i, "name": f"Commodity {i}", "is_buyable": 1} for i in range(1, 21)]


def _price_rows(commodity_name):
    i = int(commodity_name.split()[-1])
    return [{"id_commodity": i, "scu_buy_users_rows": i, "price_buy": 10, "price_sell": 20}]


def _route_rows(id_commodity):
    return [{"id_terminal_origin": 1, "id_terminal_destination": 2, "price_origin": 10,
             "price_destination": 20 + id_commodity, "scu_origin": 50, "scu_destination": 50,
             "profit": 100 + id_commodity, "status_origin": 1, "status_destination": 1}]


def _uex(*, price_fails=(), route_fails=(), commodities=COMMODITIES):
    async def get_commodities_prices(*, commodity_name):
        if int(commodity_name.split()[-1]) in price_fails:
            raise UexApiError("503 x3")
        return _price_rows(commodity_name)

    async def get_commodities_routes(*, id_commodity):
        if id_commodity in route_fails:
            raise UexApiError("timed out")
        return _route_rows(id_commodity)

    async def get_commodities():
        return commodities

    return NS(get_commodities=get_commodities, get_commodities_prices=get_commodities_prices,
              get_commodities_routes=get_commodities_routes)


def _cog(uex):
    cog = Trends.__new__(Trends)
    cog.bot = NS(uex=uex)
    cog._trending, cog._trending_updated_at, cog._trending_lock = [], None, asyncio.Lock()
    cog._top_scored_routes, cog._top_scored_routes_updated_at = [], None
    cog._top_scored_routes_lock = asyncio.Lock()
    cog._top_in_stock_routes, cog._top_in_stock_routes_updated_at = [], None
    cog._top_in_stock_routes_lock = asyncio.Lock()
    return cog


def _no_delay(monkeypatch):
    async def instant(_seconds):
        return None
    monkeypatch.setattr(trends_cog.asyncio, "sleep", instant)


def test_a_clean_refresh_records_no_gap(monkeypatch):
    _no_delay(monkeypatch)
    cog = _cog(_uex())
    asyncio.run(cog._refresh_trending_once())
    assert len(cog._top_scored_routes) == 20 and len(cog._trending) == 20
    assert cog._top_scored_routes_gap == RefreshGap(0, 20) and cog._trending_gap == RefreshGap(0, 20)


def test_a_uex_blip_mid_refresh_keeps_the_previous_complete_snapshot(monkeypatch, caplog):
    _no_delay(monkeypatch)

    async def run():
        cog = _cog(_uex())
        await cog._refresh_trending_once()
        good_routes, good_at = list(cog._top_scored_routes), cog._top_scored_routes_updated_at
        cog.bot.uex = _uex(price_fails=set(range(1, 7)))  # 6 of 20 = 30% fail
        await cog._refresh_trending_once()
        return cog, good_routes, good_at

    cog, good_routes, good_at = asyncio.run(run())
    assert cog._top_scored_routes == good_routes and cog._top_scored_routes_updated_at == good_at
    assert cog._top_in_stock_routes_gap == RefreshGap(0, 20) and len(cog._trending) == 20
    assert "kept the previous snapshot: couldn't fetch 6 of 20" in caplog.text


def test_a_small_gap_is_used_and_recorded(monkeypatch):
    _no_delay(monkeypatch)

    async def run():
        cog = _cog(_uex())
        await cog._refresh_trending_once()
        cog.bot.uex = _uex(price_fails={3})
        await cog._refresh_trending_once()
        return cog

    cog = asyncio.run(run())
    assert len(cog._top_scored_routes) == 19 and cog._top_scored_routes_gap == RefreshGap(1, 20)


def test_a_route_fetch_failure_only_marks_the_route_lists(monkeypatch):
    _no_delay(monkeypatch)
    cog = _cog(_uex(route_fails={4, 5}))
    asyncio.run(cog._refresh_trending_once())
    assert cog._trending_gap == RefreshGap(0, 20)
    assert cog._top_scored_routes_gap == RefreshGap(2, 20) == cog._top_in_stock_routes_gap


def test_the_first_refresh_after_a_start_is_used_even_if_partial(monkeypatch):
    _no_delay(monkeypatch)
    cog = _cog(_uex(price_fails=set(range(1, 11))))
    asyncio.run(cog._refresh_trending_once())
    assert len(cog._top_scored_routes) == 10 and cog._top_scored_routes_gap == RefreshGap(10, 20)


def test_an_empty_commodity_list_keeps_every_snapshot(monkeypatch):
    _no_delay(monkeypatch)

    async def run():
        cog = _cog(_uex())
        await cog._refresh_trending_once()
        cog.bot.uex = _uex(commodities=[])
        await cog._refresh_trending_once()
        return cog

    cog = asyncio.run(run())
    assert len(cog._trending) == 20 and len(cog._top_scored_routes) == 20 and len(cog._top_in_stock_routes) == 20


def test_a_kept_snapshot_gives_way_to_a_partial_one_after_two_hours(monkeypatch):
    _no_delay(monkeypatch)

    async def run():
        cog = _cog(_uex())
        await cog._refresh_trending_once()
        cog._top_scored_routes_updated_at -= REFRESH_KEEP_PREVIOUS_MAX_AGE
        cog.bot.uex = _uex(price_fails=set(range(1, 7)))
        await cog._refresh_trending_once()
        return cog

    cog = asyncio.run(run())
    assert len(cog._top_scored_routes) == 14 and cog._top_scored_routes_gap == RefreshGap(6, 20)


# ---- what players see --------------------------------------------------------------------

class _Followup:
    def __init__(self):
        self.sent = []

    async def send(self, *args, **kwargs):
        self.sent.append((args, kwargs))


def _interaction():
    async def defer(**kwargs):
        return None

    async def send_message(*args, **kwargs):
        raise AssertionError("expected a deferred response")

    return NS(user=NS(id=111), response=NS(defer=defer, send_message=send_message), followup=_Followup())


def test_top_routes_footer_says_when_the_snapshot_is_partial(tmp_path):
    async def run():
        db = Database(tmp_path / "partial.sqlite3", Fernet(Fernet.generate_key()))
        await db.init()
        client = UexClient(app_token="test", base_url="https://uex.test")
        await client._client.aclose()
        client._client = httpx.AsyncClient(transport=httpx.MockTransport(
            lambda request: httpx.Response(200, json={"status": "ok", "data": []})))
        cog = _cog(client)
        cog.bot = NS(db=db, uex=client, get_cog=lambda name: None)
        cog._top_scored_routes = [ScoredRouteEntry(
            commodity_name="Gold", id_commodity=1, origin_terminal_name="Origin",
            destination_terminal_name="Destination", price_origin=100.0, price_destination=200.0,
            price_margin=100.0, price_roi=100.0, distance=10.0, score=100, scu_origin=50,
            scu_destination=50, status_origin=1, status_destination=1,
            origin_terminal_id=1, destination_terminal_id=2,
        )]
        cog._top_scored_routes_updated_at = datetime.now(timezone.utc)
        cog._top_scored_routes_gap = RefreshGap(3, 20)
        interaction = _interaction()
        try:
            await cog.top_routes.callback(cog, interaction)
        finally:
            await client.aclose()
        return interaction

    interaction = asyncio.run(run())
    header = route_results(interaction.followup.sent).header
    assert "partial refresh: 3 of 20 commodities couldn't be fetched" in header


def test_trending_footer_says_when_the_snapshot_is_partial():
    async def run():
        sent = {}

        async def send_message(*args, **kwargs):
            sent.update(kwargs)

        cog = _cog(_uex())
        cog._trending = [NS(commodity_name="Gold", total_trips_15d=12, avg_volatility=None, best_sell_price=100)]
        cog._trending_updated_at = datetime.now(timezone.utc)
        cog._trending_gap = RefreshGap(2, 20)
        await cog.trending.callback(cog, NS(response=NS(send_message=send_message)))
        return sent

    footer = asyncio.run(run())["embed"].footer.text
    assert "partial refresh: 2 of 20 commodities couldn't be fetched" in footer


def test_an_empty_result_says_routes_may_be_missing_from_a_partial_refresh(tmp_path):
    async def run():
        db = Database(tmp_path / "partial_empty.sqlite3", Fernet(Fernet.generate_key()))
        await db.init()
        client = UexClient(app_token="test", base_url="https://uex.test")
        await client._client.aclose()
        client._client = httpx.AsyncClient(transport=httpx.MockTransport(
            lambda request: httpx.Response(200, json={"status": "ok", "data": []})))
        cog = _cog(client)
        cog.bot = NS(db=db, uex=client, get_cog=lambda name: None)
        cog._top_scored_routes = [ScoredRouteEntry(
            commodity_name="Gold", id_commodity=1, origin_terminal_name="Origin",
            destination_terminal_name="Destination", price_origin=100.0, price_destination=200.0,
            price_margin=100.0, price_roi=100.0, distance=10.0, score=100, scu_origin=50,
            scu_destination=50, status_origin=1, status_destination=1,
            origin_terminal_id=1, destination_terminal_id=2,
        )]
        cog._top_scored_routes_updated_at = datetime.now(timezone.utc)
        cog._top_scored_routes_gap = RefreshGap(3, 20)
        interaction = _interaction()
        try:
            # Neither terminal is known to support auto-load, so the filter empties the list.
            await cog.top_routes.callback(cog, interaction, auto_load_only=True)
        finally:
            await client.aclose()
        return interaction

    message = asyncio.run(run()).followup.sent[-1][0][0]
    assert message.startswith("No routes with auto-load at both ends found right now.")
    assert "the last refresh couldn't fetch 3 of 20 commodities" in message
