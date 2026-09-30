"""Audit UX-12: the three alert types arrived in different places with no choice - price
alerts only in the channel they were set in, marketplace alerts only by DM, restock alerts
either way but defaulting to the channel. All three now share one `delivery` option (DM by
default, or post in this channel and ping me), existing alerts keep arriving where they did,
and the confirmations and /alert-list say how often each type fires and where it arrives."""
import asyncio
import sqlite3
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, MagicMock

import pytest
from cryptography.fernet import Fernet

from bot.cogs.alerts import Alerts
from bot.cogs.marketplace_alerts import MarketplaceAlerts
from bot.cogs.stock_alerts import StockAlerts
from bot.db.database import Database
from bot.delivery import DELIVERY_CHOICES, send_alert
from tests.test_alert_add_and_restock import _bot, _interaction

GLOBAL = NS(value="global")


def _choices(command, name="delivery"):
    param = next(p for p in command.parameters if p.name == name)
    return [(c.name, c.value) for c in param.choices], param.required


def test_all_three_alert_commands_offer_the_same_delivery_choice():
    expected = [(c.name, c.value) for c in DELIVERY_CHOICES]
    for command in (Alerts.alert_add, StockAlerts.stock_alert_add, MarketplaceAlerts.marketplace_alert_add):
        assert _choices(command) == (expected, False), command.name
    assert expected[0] == ("DM me (default)", "personal")


# ---- adding: DM unless asked, and the reply says where and how often ------------------------

def _price_add(delivery=None):
    order = []
    cog = Alerts.__new__(Alerts)
    cog.bot = _bot(order)
    asyncio.run(Alerts.alert_add.callback(cog, _interaction(order), "gold", NS(value="sell_at_least"), 5000.0,
                                          delivery))
    return order


def _stock_add(delivery=None):
    order = []
    cog = StockAlerts.__new__(StockAlerts)
    cog.bot = _bot(order)
    asyncio.run(StockAlerts.stock_alert_add.callback(cog, _interaction(order), "gold", None, delivery))
    return order


@pytest.mark.parametrize("add, saved_as", [(_price_add, "add_price_alert"), (_stock_add, "add_stock_alert")])
def test_a_new_alert_is_a_private_dm_unless_the_channel_is_picked(add, saved_as):
    default = dict(add())
    assert default["defer"] == {"ephemeral": True} and default[saved_as]["scope"] == "personal"
    assert "I'll DM you" in default["send"]
    channel = dict(add(GLOBAL))
    assert channel["defer"] == {"ephemeral": False} and channel[saved_as]["scope"] == "global"
    assert "I'll post here and ping you" in channel["send"]


def test_confirmations_say_how_often_each_type_fires():
    assert "It fires once, then switches off." in dict(_price_add())["send"]
    assert "it fires again on every future restock" in dict(_stock_add())["send"]


def test_a_marketplace_alert_can_post_in_its_channel():
    async def run(delivery):
        db = NS(add_marketplace_alert=AsyncMock(return_value=9))
        cog = MarketplaceAlerts.__new__(MarketplaceAlerts)
        cog.bot = NS(db=db)
        interaction = NS(guild_id=1, channel_id=2, user=NS(id=3), response=NS(defer=AsyncMock()),
                         followup=NS(send=AsyncMock()))
        await MarketplaceAlerts.marketplace_alert_add.callback(
            cog, interaction, "Laranite", NS(value="sell"), None, None, None, delivery)
        return db.add_marketplace_alert.await_args.kwargs, interaction

    saved, interaction = asyncio.run(run(None))
    assert saved["scope"] == "personal"
    interaction.response.defer.assert_awaited_once_with(ephemeral=True)
    assert "fires on every new matching listing" in interaction.followup.send.await_args.args[0]
    saved, interaction = asyncio.run(run(GLOBAL))
    assert (saved["scope"], saved["guild_id"], saved["channel_id"]) == ("global", 1, 2)
    interaction.response.defer.assert_awaited_once_with(ephemeral=False)
    assert "I'll post here and ping you" in interaction.followup.send.await_args.args[0]


# ---- firing: where each alert asked ---------------------------------------------------------

def _targets():
    channel, user = NS(send=AsyncMock()), NS(send=AsyncMock())
    bot = NS(get_channel=MagicMock(return_value=channel), get_user=MagicMock(return_value=user),
             fetch_user=AsyncMock(return_value=user))
    return bot, channel, user


def test_send_alert_dms_or_posts_with_a_ping():
    async def run(alert):
        bot, channel, user = _targets()
        await send_alert(bot, alert, "price alert #3 triggered", label="x")
        return channel, user

    channel, user = asyncio.run(run({"user_id": 10, "channel_id": 5, "scope": "global"}))
    channel.send.assert_awaited_once_with("<@10> price alert #3 triggered")
    user.send.assert_not_awaited()
    for alert in ({"user_id": 10, "channel_id": 5, "scope": "personal"},
                  {"user_id": 10, "channel_id": None, "scope": "global"}):
        channel, user = asyncio.run(run(alert))
        user.send.assert_awaited_once_with("Your price alert #3 triggered")
        channel.send.assert_not_awaited()


@pytest.mark.parametrize("scope, where", [("personal", "dm"), ("global", "channel")])
def test_a_price_alert_fires_where_it_asked(scope, where):
    async def run():
        bot, channel, user = _targets()
        cog = Alerts.__new__(Alerts)
        cog.bot = NS(**vars(bot), db=NS(deactivate_alert=AsyncMock()))
        await cog._fire_alert({"id": 3, "user_id": 10, "channel_id": 5, "commodity_name": "Gold", "scope": scope},
                              "best sell price is now 10")
        return channel, user

    channel, user = asyncio.run(run())
    sent = (user if where == "dm" else channel).send
    other = (channel if where == "dm" else user).send
    sent.assert_awaited_once()
    other.assert_not_awaited()
    assert "price alert #3 triggered for **Gold**" in sent.await_args.args[0]


def test_a_marketplace_alert_set_to_the_channel_posts_there_with_a_ping():
    async def run():
        bot, channel, user = _targets()
        cog = MarketplaceAlerts.__new__(MarketplaceAlerts)
        cog.bot = bot
        await cog._notify_marketplace_alert(
            {"id": 4, "user_id": 10, "channel_id": 5, "scope": "global", "keyword": "Laranite", "operation": "sell"},
            {"title": "Laranite x 32", "price": "450000", "currency": "UEC", "user_username": "Pilot"})
        return channel, user

    channel, user = asyncio.run(run())
    assert channel.send.await_args.args[0].startswith("<@10> marketplace alert #4 ('Laranite')")
    user.send.assert_not_awaited()


# ---- existing alerts keep arriving where they did -------------------------------------------

def test_alerts_from_before_the_option_keep_their_delivery(tmp_path):
    path = tmp_path / "old.sqlite3"
    with sqlite3.connect(path) as con:
        con.executescript("""
            CREATE TABLE price_alerts (id INTEGER PRIMARY KEY AUTOINCREMENT, guild_id INTEGER,
                channel_id INTEGER NOT NULL, user_id INTEGER NOT NULL, commodity_name TEXT NOT NULL,
                direction TEXT NOT NULL, target_price REAL NOT NULL,
                created_at TEXT NOT NULL DEFAULT (datetime('now')), triggered_at TEXT,
                active INTEGER NOT NULL DEFAULT 1);
            CREATE TABLE marketplace_alerts (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL,
                keyword TEXT NOT NULL, operation TEXT NOT NULL, target_price REAL,
                created_at TEXT NOT NULL DEFAULT (datetime('now')), active INTEGER NOT NULL DEFAULT 1);
            INSERT INTO price_alerts (channel_id, user_id, commodity_name, direction, target_price)
                VALUES (5, 10, 'Gold', 'sell_at_least', 100);
            INSERT INTO marketplace_alerts (user_id, keyword, operation) VALUES (10, 'Laranite', 'sell');
        """)

    async def run():
        db = Database(path, Fernet(Fernet.generate_key()))
        await db.init()
        return await db.list_user_alerts(10), await db.list_user_marketplace_alerts(10)

    (price,), (market,) = asyncio.run(run())
    assert price["scope"] == "global", "price alerts always posted in their channel"
    assert market["scope"] == "personal" and market["channel_id"] is None, "marketplace alerts were always DMs"


# ---- /alert-list says how often and where ---------------------------------------------------

def test_alert_list_says_how_often_each_type_fires_and_where_each_alert_arrives(tmp_path):
    async def run():
        db = Database(tmp_path / "list.sqlite3", Fernet(Fernet.generate_key()))
        await db.init()
        await db.add_price_alert(guild_id=1, channel_id=5, user_id=10, commodity_name="Gold",
                                 direction="sell_at_least", target_price=100, scope="global")
        await db.add_stock_alert(guild_id=1, channel_id=5, user_id=10, commodity_name="Laranite", ship_query=None)
        await db.add_marketplace_alert(user_id=10, keyword="Cutlass Black", operation="sell")
        cog = Alerts.__new__(Alerts)
        cog.bot = NS(db=db)
        interaction = NS(user=NS(id=10), response=NS(send_message=AsyncMock()))
        await Alerts.alert_list.callback(cog, interaction)
        return interaction.response.send_message.await_args.args[0]

    text = asyncio.run(run())
    assert "**Price alerts** (each fires once, then switches off)\n#1 — Gold sell >= 100.00 · <#5>" in text
    assert "**Restock alerts** (fire on every restock)\n#1 — Laranite · DM" in text
    assert "**Marketplace alerts** (fire on every new matching listing)" in text
    assert "matching 'Cutlass Black' · DM" in text
