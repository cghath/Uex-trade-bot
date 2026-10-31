"""Notifications are only marked done once they're settled (bot/delivery.py).

Audit findings REL-4 and REL-7: several alert paths caught a failed send, logged it, and
then marked the notification seen (or the alert fired) anyway, losing it after a one-off
Discord hiccup. And a negotiation message over Discord's 2,000-char limit, or to a user
with closed DMs, was re-sent every 5 minutes forever. A temporary failure must leave the
notification pending; a refusal Discord will always repeat must settle it.
"""
import asyncio
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, MagicMock

import aiohttp
import discord

from bot import delivery
from bot.cogs.alerts import Alerts
from bot.cogs.marketplace_alerts import MarketplaceAlerts
from bot.cogs.negotiation_alerts import NegotiationAlerts
from bot.cogs.scanner import Scanner
from bot.cogs.stock_alerts import StockAlerts
from bot.delivery import Delivery


def _http_error(status: int) -> discord.HTTPException:
    response = NS(status=status, reason="x")
    if status == 403:
        return discord.Forbidden(response, {"message": "Cannot send messages to this user", "code": 50007})
    return discord.HTTPException(response, {"message": "x"})


def _target(*, fail_with=None):
    """A channel or user whose send() raises `fail_with` (if given) and records calls."""
    target = NS(sent=[])

    async def send(content=None, **kwargs):
        target.sent.append((content, kwargs))
        if fail_with is not None:
            raise fail_with

    target.send = send
    return target


def _bot(*, channel=None, user=None, db=None, uex=None):
    return NS(get_channel=MagicMock(return_value=channel), get_user=MagicMock(return_value=user),
              fetch_user=AsyncMock(return_value=user), db=db, uex=uex)


# -- the building blocks ---------------------------------------------------------------------

def test_send_errors_split_into_retry_and_undeliverable():
    assert delivery.classify_send_error(_http_error(503)) is Delivery.RETRY
    assert delivery.classify_send_error(_http_error(429)) is Delivery.RETRY
    assert delivery.classify_send_error(aiohttp.ClientConnectionError()) is Delivery.RETRY
    assert delivery.classify_send_error(asyncio.TimeoutError()) is Delivery.RETRY
    assert delivery.classify_send_error(_http_error(403)) is Delivery.UNDELIVERABLE, "closed DMs / missing permissions"
    assert delivery.classify_send_error(_http_error(404)) is Delivery.UNDELIVERABLE, "unknown user or channel"
    assert delivery.classify_send_error(_http_error(400)) is Delivery.UNDELIVERABLE, "e.g. message too long"


def test_fit_message_trims_only_the_body_and_says_so():
    short = delivery.fit_message("Prefix: ", "hello")
    assert short == "Prefix: hello"
    long = delivery.fit_message("Prefix: ", "x" * 65_535)
    assert len(long) <= delivery.MAX_MESSAGE_CHARS
    assert long.startswith("Prefix: xxx") and long.endswith("read the rest on UEX)*")


def test_channel_then_dm_outcomes():
    async def run(channel_error, dm_error, *, has_channel=True):
        channel = _target(fail_with=channel_error) if has_channel else None
        user = _target(fail_with=dm_error)
        outcome = await delivery.send_to_channel_or_dm(_bot(channel=channel, user=user), 5, 10, "hi", label="test")
        return outcome, user.sent

    assert asyncio.run(run(None, None))[0] is Delivery.DELIVERED
    outcome, dms = asyncio.run(run(_http_error(403), None))
    assert outcome is Delivery.DELIVERED and [content for content, _ in dms] == ["hi"], \
        "a refused channel post falls back to a DM"
    assert asyncio.run(run(_http_error(403), _http_error(503)))[0] is Delivery.RETRY
    assert asyncio.run(run(_http_error(503), _http_error(403)))[0] is Delivery.RETRY
    assert asyncio.run(run(_http_error(403), _http_error(403)))[0] is Delivery.UNDELIVERABLE
    assert asyncio.run(run(None, _http_error(403), has_channel=False))[0] is Delivery.UNDELIVERABLE


# -- price alerts: one-shot, used up only once settled ---------------------------------------

def _price_alert_cog(channel, user):
    db = NS(deactivate_alert=AsyncMock())
    cog = Alerts.__new__(Alerts)
    cog.bot = _bot(channel=channel, user=user, db=db)
    return cog, db


def test_price_alert_stays_active_after_a_temporary_failure():
    cog, db = _price_alert_cog(_target(fail_with=_http_error(503)), _target(fail_with=_http_error(503)))
    alert = {"id": 3, "user_id": 10, "channel_id": 5, "commodity_name": "Gold"}
    asyncio.run(cog._fire_alert(alert, "best sell price is now 10"))
    db.deactivate_alert.assert_not_awaited()


def test_price_alert_is_deactivated_once_delivered_or_refused_outright():
    for channel, user in [(_target(), _target()), (_target(fail_with=_http_error(403)), _target(fail_with=_http_error(403)))]:
        cog, db = _price_alert_cog(channel, user)
        asyncio.run(cog._fire_alert({"id": 3, "user_id": 10, "channel_id": 5, "commodity_name": "Gold"}, "x"))
        db.deactivate_alert.assert_awaited_once_with(3)


# -- stock alerts: a restock whose notification failed temporarily is detected again ---------

def test_stock_alert_keeps_an_unsent_restock_pending():
    async def run(send_error):
        alert = {"id": 7, "user_id": 10, "channel_id": 5, "commodity_name": "Gold", "scope": "personal",
                 "ship_query": None}
        db = NS(list_active_stock_alerts=AsyncMock(return_value=[alert]),
                get_stock_alert_terminal_state=AsyncMock(return_value={}),
                upsert_stock_alert_terminal_state=AsyncMock(),
                get_default_ship=AsyncMock(return_value=None))
        uex = NS(get_commodities_prices=AsyncMock(return_value=[
            {"id_terminal": 1, "terminal_name": "A", "price_buy": 5, "scu_buy": 100},   # restocked
            {"id_terminal": 2, "terminal_name": "B", "price_buy": 5, "scu_buy": 0},     # still empty
        ]))
        cog = StockAlerts.__new__(StockAlerts)
        cog.bot = _bot(user=_target(fail_with=send_error), db=db, uex=uex)
        await cog._poll_stock_alerts_once()
        return sorted(call.args[1] for call in db.upsert_stock_alert_terminal_state.await_args_list)

    assert asyncio.run(run(_http_error(503))) == [2], "terminal 1's restock stays unrecorded, so it's retried"
    assert asyncio.run(run(None)) == [1, 2]
    assert asyncio.run(run(_http_error(403))) == [1, 2], "a refused DM won't be retried forever"


# -- marketplace alerts: a listing is only seen once its DM is settled -----------------------

def _marketplace_cog(send_error):
    alert = {"id": 4, "user_id": 10, "keyword": "laranite", "operation": "sell", "target_price": None,
             "min_quality": None, "max_quality": None}
    db = NS(get_seen_marketplace_listing_ids=AsyncMock(return_value=set()), mark_marketplace_listing_seen=AsyncMock())
    uex = NS(get_marketplace_listings=AsyncMock(return_value=[
        {"id": 101, "title": "Laranite", "price": "100", "in_stock": 1},
        {"id": 102, "title": "Laranite", "price": "90", "in_stock": 1},
    ]))
    user = _target(fail_with=send_error)
    cog = MarketplaceAlerts.__new__(MarketplaceAlerts)
    cog.bot = _bot(user=user, db=db, uex=uex)
    return cog, db, user, alert


def test_marketplace_alert_leaves_listings_unseen_after_a_temporary_failure():
    cog, db, user, alert = _marketplace_cog(_http_error(503))
    asyncio.run(cog._poll_alert_group("laranite", "sell", [alert], []))
    db.mark_marketplace_listing_seen.assert_not_awaited()
    assert len(user.sent) == 1, "stops after the first failure instead of failing on every listing"


def test_marketplace_alert_settles_a_refused_dm_without_trying_every_listing():
    cog, db, user, alert = _marketplace_cog(_http_error(403))
    asyncio.run(cog._poll_alert_group("laranite", "sell", [alert], []))
    assert len(user.sent) == 1
    db.mark_marketplace_listing_seen.assert_awaited_once()


# -- scanner: DM fallback, and seen only once settled ----------------------------------------

def _scanner_cog(channel, user):
    db = NS(get_seen_scanner_listing_ids=AsyncMock(return_value=set()), mark_scanner_listing_seen=AsyncMock())
    cog = Scanner.__new__(Scanner)
    cog.bot = _bot(channel=channel, user=user, db=db)
    return cog, db


def _steal(listing_id):
    return NS(listing_id=listing_id, item_name="Quantanium", id_item=1, listing_title="Q", listing_price=10.0,
              fair_price=20.0, currency="UEC", discount_pct=50.0, seller="pilot")


def test_scanner_falls_back_to_a_dm_when_its_channel_refuses():
    user = _target()
    cog, db = _scanner_cog(_target(fail_with=_http_error(403)), user)
    asyncio.run(cog._notify_watcher({"user_id": 10, "channel_id": 5}, [_steal(1)]))
    assert len(user.sent) == 1
    db.mark_scanner_listing_seen.assert_awaited_once_with(10, 1)


def test_scanner_leaves_deals_unseen_after_a_temporary_failure():
    cog, db = _scanner_cog(_target(fail_with=_http_error(503)), _target(fail_with=_http_error(503)))
    asyncio.run(cog._notify_watcher({"user_id": 10, "channel_id": 5}, [_steal(1), _steal(2)]))
    db.mark_scanner_listing_seen.assert_not_awaited()


# -- negotiation alerts: long messages fit, refused DMs aren't retried forever ---------------

def _negotiation_cog(user, messages):
    seen = set()
    db = NS(is_negotiation_message_seen=AsyncMock(side_effect=lambda uid, mid: (uid, mid) in seen),
            mark_negotiation_message_seen=AsyncMock(side_effect=lambda uid, mid: seen.add((uid, mid))))
    uex = NS(get_marketplace_negotiations_messages=AsyncMock(return_value=messages),
             get_marketplace_listings=AsyncMock(return_value=[]))
    cog = NegotiationAlerts.__new__(NegotiationAlerts)
    cog.bot = _bot(user=user, db=db, uex=uex)
    return cog, seen


NEGOTIATION = {"id": 7, "id_listing": 999, "is_listing_advertiser": 1, "advertiser_username": "Alice",
               "client_username": "Bob", "listing_title": "Laranite"}


def test_a_long_negotiation_message_is_trimmed_to_fit_a_dm():
    user = _target()
    cog, seen = _negotiation_cog(user, [{"id": 1, "date_added": 1, "message": "x" * 5000, "user_username": "Bob"}])
    assert asyncio.run(cog._check_negotiation(10, "sk", NEGOTIATION, 7)) is True
    (content, _), = user.sent
    assert len(content) <= 2000 and content.endswith("read the rest on UEX)*")
    assert (10, 1) in seen


def test_closed_dms_settle_the_negotiation_instead_of_retrying_every_5_minutes():
    user = _target(fail_with=_http_error(403))
    messages = [{"id": 1, "date_added": 1, "message": "hi", "user_username": "Bob"},
                {"id": 2, "date_added": 2, "message": "still there?", "user_username": "Bob"}]
    cog, seen = _negotiation_cog(user, messages)
    advanced = asyncio.run(cog._check_negotiation(10, "sk", NEGOTIATION, 7))
    assert advanced is True, "the checkpoint moves on"
    assert seen == {(10, 1), (10, 2)}
    assert len(user.sent) == 1, "one refused DM, not one per message"


def test_a_temporary_dm_failure_still_leaves_the_message_for_the_next_poll():
    cog, seen = _negotiation_cog(_target(fail_with=_http_error(503)),
                                 [{"id": 1, "date_added": 1, "message": "hi", "user_username": "Bob"}])
    assert asyncio.run(cog._check_negotiation(10, "sk", NEGOTIATION, 7)) is False
    assert seen == set()
