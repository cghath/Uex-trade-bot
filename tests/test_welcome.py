"""The welcome DM (bot/uex/welcome.py, bot/cogs/welcome.py).

Guarantees under test: the wording (Star Tech Dynamics in full, the six commands, the channel,
no /intro); a new member gets it once - never again on a rejoin or a second event - while bots,
other servers and members still accepting the rules don't (until they accept); a temporary
failure is retried by the loop and a refusal isn't; the preview is owner-only.
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock

import discord
from cryptography.fernet import Fernet

from bot.cogs.anniversaries import ANNIVERSARY_CHANNEL_ID
from bot.cogs.welcome import Welcome
from bot.db.database import Database
from bot.uex.welcome import HIGHLIGHTS, welcome_text

GUILD_ID = 1132378045232197694


def test_the_welcome_names_the_server_six_commands_and_the_channel_and_nothing_more():
    text = welcome_text("Nova", ANNIVERSARY_CHANNEL_ID)
    assert text.startswith("👋 **Welcome to Star Tech Dynamics, Nova!**")
    assert [c for c, _ in HIGHLIGHTS] == ["multi-stop-route", "ingame-item-finder", "ship-loadout", "ship-parts-finder",
                                         "where-to-mine", "blueprint-search"]
    assert all(f"`/{c}`" in text for c, _ in HIGHLIGHTS)
    assert text.endswith(f"You can use it in <#{ANNIVERSARY_CHANNEL_ID}>.")
    assert "/intro" not in text and "STDynamics" not in text and len(text) < 600


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
