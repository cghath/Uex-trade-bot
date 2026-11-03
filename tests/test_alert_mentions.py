"""Audit REL-2: a "Post in this channel" marketplace alert posts a listing title and seller name
any UEX player wrote, so an @everyone, role or user mention in them would have pinged. Alerts may
only ping their owner, and the bot never pings @everyone, @here or a role at all."""
import asyncio
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, MagicMock

import discord

from bot import delivery
from bot.cogs.marketplace_alerts import MarketplaceAlerts
from bot.config import Config
from bot.main import UexBot

TITLE = "Laranite @everyone <@&999> <@7> cheap"


def _target():
    target = NS(sent=[])

    async def send(content=None, **kwargs):
        target.sent.append((content, kwargs))

    target.send = send
    return target


def _bot(*, channel=None, user=None):
    return NS(get_channel=MagicMock(return_value=channel), get_user=MagicMock(return_value=user),
              fetch_user=AsyncMock(return_value=user))


def _owner_only(mentions: discord.AllowedMentions, owner: int) -> bool:
    return (mentions.everyone is False and mentions.roles is False
            and [user.id for user in mentions.users] == [owner])


def test_a_channel_marketplace_alert_can_only_ping_its_owner():
    channel = _target()
    cog = MarketplaceAlerts.__new__(MarketplaceAlerts)
    cog.bot = _bot(channel=channel)
    alert = {"id": 3, "user_id": 42, "scope": "global", "channel_id": 5, "keyword": "laranite", "operation": "sell"}
    listing = {"title": TITLE, "price": "100", "currency": "UEC", "user_username": "@here", "id_item": None}

    outcome = asyncio.run(cog._notify_marketplace_alert(alert, listing))

    assert outcome is delivery.Delivery.DELIVERED
    (content, kwargs), = channel.sent
    assert TITLE in content and content.startswith("<@42>")
    assert _owner_only(kwargs["allowed_mentions"], 42)


def test_a_dm_alert_and_the_dm_fallback_can_only_ping_the_owner():
    user = _target()
    outcome = asyncio.run(delivery.send_alert(_bot(user=user), {"user_id": 42, "scope": "personal"}, TITLE,
                                              label="test"))
    assert outcome is delivery.Delivery.DELIVERED
    assert _owner_only(user.sent[0][1]["allowed_mentions"], 42)
    # A channel alert whose channel is gone falls back to a DM, still owner-only.
    user = _target()
    asyncio.run(delivery.send_to_channel_or_dm(_bot(channel=None, user=user), 5, 42, TITLE, label="test"))
    assert _owner_only(user.sent[0][1]["allowed_mentions"], 42)


def test_a_callers_own_allowed_mentions_is_kept():
    channel = _target()
    none = discord.AllowedMentions.none()
    asyncio.run(delivery.send_to_channel_or_dm(_bot(channel=channel), 5, 42, "hi", label="test",
                                               allowed_mentions=none))
    assert channel.sent[0][1]["allowed_mentions"] is none


def test_the_bot_never_pings_everyone_or_a_role(tmp_path):
    """Replies echo what players type (a keyword, a ship name), so the bot-wide default covers
    every send that doesn't set its own."""
    config = Config(discord_bot_token="x", discord_dev_guild_id=None, uex_app_token="t", uex_secret_key=None,
                    database_path=tmp_path / "bot.sqlite3", scanner_steal_threshold=0.5)
    bot = UexBot(config)
    try:
        assert bot.allowed_mentions.everyone is False and bot.allowed_mentions.roles is False
        assert bool(bot.allowed_mentions.users), "a player can still be pinged by id"
    finally:
        asyncio.run(bot.uex.aclose())
