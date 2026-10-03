"""2026-10-03 range audit UX-1 and UX-2.

UX-1: alerts DM by default, and a DM Discord refuses (closed DMs) was the end of it - nothing
arrived, and a one-shot price alert was used up. Now it posts in the channel the alert was set
in, pinging only its owner (the owner's choice). A temporary failure still waits for the next
poll instead.

UX-2: a new marketplace alert announced the listings already up as "new", five per poll. Its
first poll now records them without a word, so only listings posted after it notify.
"""
import asyncio
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, MagicMock

import discord
from cryptography.fernet import Fernet

from bot import delivery
from bot.cogs.alerts import Alerts
from bot.cogs.marketplace_alerts import MarketplaceAlerts
from bot.db.database import Database
from bot.delivery import Delivery


def _http_error(status: int) -> discord.HTTPException:
    response = NS(status=status, reason="x")
    if status == 403:
        return discord.Forbidden(response, {"message": "Cannot send messages to this user", "code": 50007})
    return discord.HTTPException(response, {"message": "x"})


def _target(*, fail_with=None):
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


ALERT = {"id": 3, "user_id": 10, "channel_id": 5, "scope": "personal"}


def _send(channel, user, alert=ALERT):
    return asyncio.run(delivery.send_alert(_bot(channel=channel, user=user), alert, "price alert #3 fired",
                                           label="test"))


# -- UX-1 ------------------------------------------------------------------------------------

def test_closed_dms_post_the_alert_in_its_channel_pinging_only_its_owner():
    channel = _target()
    assert _send(channel, _target(fail_with=_http_error(403))) is Delivery.DELIVERED
    (content, kwargs), = channel.sent
    assert content.startswith("<@10> price alert #3 fired") and delivery.DM_FALLBACK_NOTE in content
    mentions = kwargs["allowed_mentions"]
    assert mentions.everyone is False and mentions.roles is False and [u.id for u in mentions.users] == [10]


def test_a_working_dm_or_a_temporary_failure_never_posts_in_the_channel():
    for user, expected in ((_target(), Delivery.DELIVERED), (_target(fail_with=_http_error(503)), Delivery.RETRY)):
        channel = _target()
        assert _send(channel, user) is expected
        assert channel.sent == []


def test_no_channel_to_fall_back_to_or_it_refuses_too_is_undeliverable():
    closed = _target(fail_with=_http_error(403))
    assert _send(None, closed) is Delivery.UNDELIVERABLE, "the bot can't see the channel"
    assert _send(_target(), closed, {**ALERT, "channel_id": None}) is Delivery.UNDELIVERABLE
    assert _send(_target(fail_with=_http_error(403)), closed) is Delivery.UNDELIVERABLE
    assert _send(_target(fail_with=_http_error(503)), closed) is Delivery.RETRY, "the channel may work next time"


def test_a_price_alert_for_a_player_with_closed_dms_arrives_in_its_channel():
    channel = _target()
    db = NS(deactivate_alert=AsyncMock())
    cog = Alerts.__new__(Alerts)
    cog.bot = _bot(channel=channel, user=_target(fail_with=_http_error(403)), db=db)
    asyncio.run(cog._fire_alert({**ALERT, "commodity_name": "Gold"}, "best sell price is now 10"))
    assert "Gold" in channel.sent[0][0]
    db.deactivate_alert.assert_awaited_once_with(3)


def test_the_confirmation_says_where_a_dm_alert_goes_if_dms_are_closed():
    assert delivery.delivery_note("personal") == "I'll DM you (or post here if your DMs are closed)"
    assert delivery.delivery_note("global") == "I'll post here and ping you"


# -- UX-2 ------------------------------------------------------------------------------------

def _listing(listing_id):
    return {"id": listing_id, "title": "Laranite", "price": "100", "in_stock": 1}


def _poll(tmp_path, polls):
    """Run one new alert through `polls` (a list of listing lists, one per poll); returns the
    DMs sent after each poll and the alert's row at the end."""
    async def run():
        db = Database(tmp_path / "alerts.sqlite3", Fernet(Fernet.generate_key()))
        await db.init()
        await db.add_marketplace_alert(user_id=10, keyword="laranite", operation="sell")
        user = _target()
        uex = NS(get_marketplace_listings=AsyncMock())
        cog = MarketplaceAlerts.__new__(MarketplaceAlerts)
        cog.bot = _bot(user=user, db=db, uex=uex)
        sent_after = []
        for listings in polls:
            uex.get_marketplace_listings.return_value = listings
            alerts = await db.list_active_marketplace_alerts()
            await cog._poll_alert_group("laranite", "sell", alerts, [])
            sent_after.append(len(user.sent))
        return sent_after, (await db.list_active_marketplace_alerts())[0]

    return asyncio.run(run())


def test_a_new_marketplace_alert_skips_what_is_already_listed(tmp_path):
    existing = [_listing(i) for i in range(101, 113)]
    sent_after, alert = _poll(tmp_path, [existing, existing, [*existing, _listing(200)]])
    assert sent_after == [0, 0, 1], "12 old listings are not 'new'; the one posted later is"
    assert alert["baseline_done"] == 1


def test_a_new_alert_with_nothing_listed_yet_still_announces_the_first_listing(tmp_path):
    sent_after, _ = _poll(tmp_path, [[], [_listing(300)]])
    assert sent_after == [0, 1]


def test_alerts_from_before_the_change_keep_announcing_as_they_did(tmp_path):
    """They had already been polling, so their unseen listings are genuinely new: the migration
    that adds the column marks them done."""
    import sqlite3

    path = tmp_path / "old.sqlite3"
    with sqlite3.connect(path) as old:
        old.execute("""CREATE TABLE marketplace_alerts (
            id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL, keyword TEXT NOT NULL,
            operation TEXT NOT NULL CHECK (operation IN ('buy', 'sell')), target_price REAL,
            min_quality REAL, max_quality REAL, created_at TEXT NOT NULL DEFAULT (datetime('now')),
            active INTEGER NOT NULL DEFAULT 1,
            scope TEXT NOT NULL DEFAULT 'personal' CHECK (scope IN ('personal', 'global')),
            guild_id INTEGER, channel_id INTEGER)""")
        old.execute("INSERT INTO marketplace_alerts (user_id, keyword, operation) VALUES (10, 'x', 'sell')")

    async def run():
        db = Database(path, Fernet(Fernet.generate_key()))
        await db.init()
        return (await db.list_active_marketplace_alerts())[0]["baseline_done"]

    assert asyncio.run(run()) == 1
