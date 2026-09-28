"""Background task loops must survive any single failure.

discord.ext.tasks only restarts a loop after a narrow set of network errors, so any other
exception escaping a loop body (sqlite3.OperationalError "database is locked", a TypeError
from an odd UEX row) stops that loop until the bot restarts, silently. Each test here starts
the real decorated loop, injects a failure into its first step, and checks the task is still
alive afterwards - the same check as the marketplace collector's own test in test_marketplace.py.
"""
import asyncio
import sqlite3
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from bot.cogs.alerts import Alerts
from bot.cogs.digest import Digest
from bot.cogs.intelligence import Intelligence
from bot.cogs.marketplace_alerts import MarketplaceAlerts
from bot.cogs.negotiation_alerts import NegotiationAlerts
from bot.cogs.route_progression import RouteProgression
from bot.cogs.scanner import Scanner
from bot.cogs.stock_alerts import StockAlerts
from bot.cogs.trends import Trends


class _Raising:
    """Every attribute is an async method that records its call, then raises `exc`."""

    def __init__(self, exc: BaseException):
        self._exc = exc
        self.calls: list[str] = []

    def __getattr__(self, name):
        async def method(*_args, **_kwargs):
            self.calls.append(name)
            raise self._exc

        return method


def _locked_db() -> _Raising:
    return _Raising(sqlite3.OperationalError("database is locked"))


def _cog(cls, *, db, uex=None):
    bot = SimpleNamespace(db=db, uex=uex or SimpleNamespace(), wait_until_ready=AsyncMock())
    cog = cls.__new__(cls)
    cog.bot = bot
    return cog


def _fuel_setup():
    uex = SimpleNamespace(get_terminals=AsyncMock(return_value=[]), get_fuel_prices=AsyncMock(return_value=[]))
    db = _locked_db()
    return _cog(Intelligence, db=db, uex=uex), "snapshot_fuel_prices", db


def _trends_setup():
    uex = _Raising(RuntimeError("unexpected payload shape"))
    return _cog(Trends, db=SimpleNamespace(), uex=uex), "refresh_trending", uex


def _db_first(cls, loop_name):
    def setup():
        db = _locked_db()
        return _cog(cls, db=db), loop_name, db
    return setup


LOOP_SETUPS = {
    "price alerts": _db_first(Alerts, "poll_alerts"),
    "stock alerts": _db_first(StockAlerts, "poll_stock_alerts"),
    "digest": _db_first(Digest, "post_scheduled_digests"),
    "marketplace alerts": _db_first(MarketplaceAlerts, "poll_marketplace_alerts"),
    "negotiation alerts": _db_first(NegotiationAlerts, "poll_negotiation_messages"),
    "scanner": _db_first(Scanner, "poll_scanner"),
    "abandoned threads": _db_first(RouteProgression, "poll_abandoned_threads"),
    "fuel collector": _fuel_setup,
    "trending refresh": _trends_setup,
}


@pytest.mark.parametrize("name", list(LOOP_SETUPS))
def test_loop_survives_a_failure_in_its_first_step(name):
    async def run():
        cog, loop_name, failing = LOOP_SETUPS[name]()
        loop = getattr(cog, loop_name)
        loop.start()
        try:
            for _ in range(200):  # wait for the first iteration to hit the injected failure
                if failing.calls:
                    break
                await asyncio.sleep(0.01)
            assert failing.calls, "the loop never reached the injected failure"
            await asyncio.sleep(0.05)  # let an escaped exception finish tearing the task down
            assert not loop.failed(), f"{loop_name} stopped for good after one error"
        finally:
            loop.cancel()

    asyncio.run(run())


def test_one_failing_price_alert_does_not_block_the_next():
    async def run():
        alerts = [
            {"id": 1, "user_id": 10, "channel_id": 99, "commodity_name": "Gold",
             "direction": "sell_at_least", "target_price": 1},
            {"id": 2, "user_id": 20, "channel_id": 99, "commodity_name": "Gold",
             "direction": "sell_at_least", "target_price": 1},
        ]

        async def deactivate(alert_id):
            if alert_id == 1:
                raise sqlite3.OperationalError("database is locked")

        db = SimpleNamespace(list_active_alerts=AsyncMock(return_value=alerts),
                             deactivate_alert=AsyncMock(side_effect=deactivate))
        uex = SimpleNamespace(get_commodities_prices=AsyncMock(return_value=[{"price_sell": 50, "price_buy": 40}]))
        cog = _cog(Alerts, db=db, uex=uex)
        channel = SimpleNamespace(send=AsyncMock())
        cog.bot.get_channel = MagicMock(return_value=channel)

        await cog.poll_alerts.coro(cog)

        sent = [call.args[0] for call in channel.send.await_args_list]
        assert any("#2" in message for message in sent), sent
        assert [call.args[0] for call in db.deactivate_alert.await_args_list] == [1, 2]

    asyncio.run(run())


def test_one_failing_negotiation_user_does_not_block_the_next():
    async def run():
        async def secret_key(user_id):
            if user_id == 1:
                raise sqlite3.OperationalError("database is locked")
            return None  # unlinked - nothing further to poll

        db = SimpleNamespace(list_negotiation_alert_user_ids=AsyncMock(return_value=[1, 2]),
                             get_user_secret_key=AsyncMock(side_effect=secret_key))
        cog = _cog(NegotiationAlerts, db=db)

        await cog.poll_negotiation_messages.coro(cog)

        assert [call.args[0] for call in db.get_user_secret_key.await_args_list] == [1, 2]

    asyncio.run(run())


def test_one_failing_scanner_watcher_does_not_block_the_next():
    async def run():
        async def seen_ids(user_id):
            if user_id == 1:
                raise sqlite3.OperationalError("database is locked")
            return set()

        db = SimpleNamespace(
            list_scanner_watchers=AsyncMock(return_value=[{"user_id": 1, "channel_id": 5},
                                                          {"user_id": 2, "channel_id": 5}]),
            get_seen_scanner_listing_ids=AsyncMock(side_effect=seen_ids),
            mark_scanner_listing_seen=AsyncMock(),
        )
        cog = _cog(Scanner, db=db)
        cog.bot.get_channel = MagicMock(return_value=SimpleNamespace())
        steal = SimpleNamespace(listing_id=77)
        cog._find_current_steals = AsyncMock(return_value=[steal])
        cog._notify = AsyncMock()

        await cog.poll_scanner.coro(cog)

        assert [call.args[1] for call in cog._notify.await_args_list] == [2]
        db.mark_scanner_listing_seen.assert_awaited_once_with(2, 77)

    asyncio.run(run())
