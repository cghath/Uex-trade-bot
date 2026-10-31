"""The welcome DM (bot/uex/welcome.py, bot/cogs/welcome.py).

Guarantees under test: the wording (Star Tech Dynamics in full, the server's four places
first, then the six commands and the bot's channel, no /intro); a new member gets it once - never again on a rejoin or a second event - while bots,
other servers and members still accepting the rules don't (until they accept); a temporary
failure is retried by the loop and a refusal isn't; the preview is owner-only.
"""
from __future__ import annotations

import asyncio
import logging
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock

import discord
from cryptography.fernet import Fernet

from bot.cogs.anniversaries import ANNIVERSARY_CHANNEL_ID
from bot.cogs.welcome import Welcome
from bot.db.database import Database
from bot.uex.welcome import (
    EVENTS_CHANNEL_ID, HIGHLIGHTS, ROLES_CHANNEL_ID, RULES_CHANNEL_ID, WELCOME_CHANNEL_ID, welcome_text,
)

GUILD_ID = 1132378045232197694


def test_the_welcome_names_the_server_its_places_six_commands_and_the_channel_and_nothing_more():
    text = welcome_text("Nova", ANNIVERSARY_CHANNEL_ID)
    lines = text.split("\n")
    assert lines[:7] == [
        "👋 **Welcome to Star Tech Dynamics, Nova!**",
        f"• Say hi in <#{WELCOME_CHANNEL_ID}> and tell us what you fly",
        f"• Upcoming ops are in <#{EVENTS_CHANNEL_ID}>",
        f"• Pick your roles and pings any time in Channels & Roles or <#{ROLES_CHANNEL_ID}>",
        f"• Server rules: <#{RULES_CHANNEL_ID}>",
        "",
        f"Our bot can help too (use it in <#{ANNIVERSARY_CHANNEL_ID}>):",
    ]
    assert [c for c, _ in HIGHLIGHTS] == ["multi-stop-route", "ingame-item-finder", "ship-loadout", "ship-parts-finder",
                                         "where-to-mine", "blueprint-search"]
    assert lines[7:] == [f"• `/{c}`: {what}" for c, what in HIGHLIGHTS]
    assert "/intro" not in text and "STDynamics" not in text and len(text) < 1000


class FakeUser:
    def __init__(self, fail=None):
        self.sent, self.fail = [], list(fail or [])

    async def send(self, content=None, **kwargs):
        if self.fail:
            raise self.fail.pop(0)
        self.sent.append(content)


def _http(status):
    error = discord.Forbidden if status == 403 else discord.HTTPException
    return error(NS(status=status, reason="x"), "refused" if status == 403 else "server error")


def _member(user_id, guild, *, bot=False, pending=False, name=None):
    return NS(id=user_id, bot=bot, pending=pending, guild=guild, display_name=name or f"Member{user_id}")


async def _make(tmp_path, users):
    db = Database(tmp_path / "welcome.sqlite3", Fernet(Fernet.generate_key()))
    await db.init()
    members = {}
    guild = NS(id=GUILD_ID, get_member=lambda uid: members.get(uid))
    channel = NS(id=ANNIVERSARY_CHANNEL_ID, guild=guild)
    bot = NS(db=db, get_channel=lambda cid: channel if cid == ANNIVERSARY_CHANNEL_ID else None,
             get_user=lambda uid: users.get(uid), fetch_user=AsyncMock(), is_owner=AsyncMock(return_value=True),
             wait_until_ready=AsyncMock())
    return Welcome(bot, start_loop=False), bot, guild, members


def test_a_new_member_is_welcomed_once_even_if_they_leave_and_rejoin(tmp_path):
    async def run():
        users = {1: FakeUser()}
        cog, _bot, guild, members = await _make(tmp_path, users)
        member = _member(1, guild, name="Nova")
        members[1] = member
        await cog.on_member_join(member)
        await cog.on_member_join(member)  # a rejoin
        await cog.welcome(member)         # or a second event for the same join
        return users[1].sent

    (dm,) = asyncio.run(run())
    assert dm.startswith("👋 **Welcome to Star Tech Dynamics, Nova!**")


def test_bots_other_servers_and_members_still_accepting_the_rules_wait(tmp_path):
    async def run():
        users = {1: FakeUser(), 2: FakeUser(), 3: FakeUser()}
        cog, _bot, guild, members = await _make(tmp_path, users)
        other = NS(id=999)
        await cog.on_member_join(_member(1, guild, bot=True))
        await cog.on_member_join(_member(2, other))
        screening = _member(3, guild, pending=True)
        await cog.on_member_join(screening)
        before = list(users[3].sent)
        await cog.on_member_update(screening, _member(3, guild, pending=False))
        return users, before

    users, before = asyncio.run(run())
    assert users[1].sent == [] and users[2].sent == []
    assert before == [] and len(users[3].sent) == 1, "welcomed once they accepted the rules"


def test_a_temporary_failure_is_retried_by_the_loop_and_a_refusal_never_is(tmp_path):
    async def run():
        users = {1: FakeUser(fail=[_http(503)]), 2: FakeUser(fail=[_http(403)] * 3)}
        cog, bot, guild, members = await _make(tmp_path, users)
        for uid in (1, 2):
            members[uid] = _member(uid, guild)
            await cog.on_member_join(members[uid])
        failed_first = list(users[1].sent)
        await cog.retry_welcomes.coro(cog)
        await cog.retry_welcomes.coro(cog)
        return failed_first, users, await bot.db.get_pending_welcomes(GUILD_ID)

    failed_first, users, pending = asyncio.run(run())
    assert failed_first == [] and len(users[1].sent) == 1, "retried once, then sent"
    assert users[2].sent == [] and len(users[2].fail) == 2, "closed DMs: one try, never retried"
    assert pending == []


def test_a_member_name_cannot_ping_or_format(tmp_path):
    async def run():
        users = {1: FakeUser()}
        cog, _bot, guild, members = await _make(tmp_path, users)
        await cog.on_member_join(_member(1, guild, name="@everyone **boss**"))
        return users[1].sent[0]

    dm = asyncio.run(run())
    assert "@​everyone" in dm and "\\*\\*boss\\*\\*" in dm


def test_the_preview_is_owner_only(tmp_path):
    async def run():
        users = {1: FakeUser(), 5: FakeUser()}
        cog, bot, _guild, _members = await _make(tmp_path, users)
        replies = []
        for uid, owner in ((5, False), (1, True)):
            bot.is_owner = AsyncMock(return_value=owner)
            interaction = NS(user=NS(id=uid, display_name=f"User{uid}"), response=NS(send_message=AsyncMock()))
            await Welcome.welcome_preview.callback(cog, interaction)
            replies.append(interaction.response.send_message.await_args)
        return users, replies

    users, (stranger, owner) = asyncio.run(run())
    assert users[5].sent == [] and stranger.kwargs["ephemeral"]
    assert users[1].sent[0].startswith("👋 **Welcome to Star Tech Dynamics, User1!**")
    assert owner.args[0] == "I've sent you the welcome DM." and owner.kwargs["ephemeral"]


def test_a_retry_tick_landing_mid_send_never_sends_the_welcome_twice(tmp_path):  # COMM-1
    """The join's DM is still on its way (its claim is 'pending') when the 10-minute retry tick
    reads the pending claims: the tick must skip that member, not send a second DM."""
    async def run():
        release = asyncio.Event()

        class SlowUser(FakeUser):
            async def send(self, content=None, **kwargs):
                await release.wait()
                self.sent.append(content)

        users = {1: SlowUser()}
        cog, _bot, guild, members = await _make(tmp_path, users)
        members[1] = _member(1, guild)
        join = asyncio.create_task(cog.on_member_join(members[1]))
        await asyncio.sleep(0.2)                 # the join has claimed and is sending
        tick = asyncio.create_task(cog.retry_welcomes.coro(cog))
        await asyncio.sleep(0.2)
        release.set()
        await asyncio.gather(join, tick)
        await cog.retry_welcomes.coro(cog)       # and nothing is left to retry
        return users[1].sent

    assert len(asyncio.run(run())) == 1


def test_owner_tools_are_hidden_from_members_in_the_slash_menu():  # COMM-6
    from bot.cogs.anniversaries import Anniversaries
    from bot.cogs.diagnostics import Diagnostics
    for command in (Welcome.welcome_preview, Anniversaries.anniversary_preview, Diagnostics.command_usage):
        assert command.default_permissions is not None and command.default_permissions.administrator, command.name


def test_the_welcome_back_preview_carries_its_stop_button(tmp_path):  # COMM-3
    from bot.cogs.welcome_back import StopWelcomeBackButton

    async def run():
        users = {1: FakeUser()}
        cog, bot, guild, _members = await _make(tmp_path, users)
        guild.fetch_scheduled_events = AsyncMock(return_value=[])
        sent = []

        async def send(content=None, **kwargs):
            sent.append((content, kwargs))
        users[1].send = send
        interaction = NS(user=NS(id=1, display_name="Owner"), response=NS(defer=AsyncMock()),
                         followup=NS(send=AsyncMock()))
        await Welcome.welcome_preview.callback(cog, interaction, NS(value="back"))
        return sent

    ((dm, kwargs),) = asyncio.run(run())
    (button,) = kwargs["view"].children
    assert dm.startswith("👋 **Welcome back, Owner!**") and isinstance(button, StopWelcomeBackButton)


def test_a_missing_bot_channel_is_logged_once_not_every_tick(tmp_path, caplog):  # COMM-7
    from bot import delivery
    delivery._MISSING_WARNED.clear()

    async def run():
        cog, bot, guild, _members = await _make(tmp_path, {})
        channel = bot.get_channel(ANNIVERSARY_CHANNEL_ID)
        bot.get_channel = lambda cid: None       # #stdynamics-bot deleted
        for _ in range(3):
            await cog.retry_welcomes.coro(cog)
        gone = len(caplog.records)
        bot.get_channel = lambda cid: channel    # back
        await cog.retry_welcomes.coro(cog)
        bot.get_channel = lambda cid: None       # and gone again
        await cog.retry_welcomes.coro(cog)
        return gone

    with caplog.at_level(logging.WARNING, logger="uexbot.delivery"):
        gone = asyncio.run(run())
    assert gone == 1 and len(caplog.records) == 2
    assert f"Channel {ANNIVERSARY_CHANNEL_ID} not found, so welcome DMs are off" in caplog.records[0].message
