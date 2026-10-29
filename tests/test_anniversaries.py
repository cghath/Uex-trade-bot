"""Server anniversaries (bot/uex/anniversaries.py, bot/cogs/anniversaries.py).

Guarantees under test:
  1. Timing - due from the member's own join moment, a year or more on, for CATCH_UP after it;
     a 29 February join on 28 February; a 31 December join just after midnight UTC.
  2. Wording - formal, "Star Tech Dynamics" in full, the join date as a Discord timestamp, a
     display name that can't ping or format anything.
  3. Sending - a DM with the stop button and a post that pings only that member, each sent once
     through restarts; a temporary failure is tried again, a refusal (DMs closed) isn't, and one
     part's failure never stops the other; bots, members under a year and opted-out ones get
     nothing.
  4. The buttons stop and resume; /anniversary-preview is owner-only.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock

import discord
from cryptography.fernet import Fernet

from bot.cogs import anniversaries as cog_module
from bot.cogs.anniversaries import (
    ANNIVERSARY_CHANNEL_ID,
    Anniversaries,
    ResumeAnniversariesButton,
    StopAnniversariesButton,
)
from bot.db.database import Database
from bot.uex.anniversaries import CATCH_UP, anniversary_moment, dm_text, due_anniversary, post_text, upcoming

UTC = timezone.utc
JOINED = datetime(2023, 11, 12, 3, 39, 20, tzinfo=UTC)  # Shikashi, one of the first members
GUILD_ID = 1132378045232197694


# -- 1. timing ----------------------------------------------------------------------------------


def test_due_from_the_join_moment_for_a_day_and_never_before_a_full_year():
    moment = datetime(2026, 11, 12, 3, 39, 20, tzinfo=UTC)
    assert due_anniversary(JOINED, moment - timedelta(seconds=1)) is None
    assert due_anniversary(JOINED, moment) == (3, moment)
    assert due_anniversary(JOINED, moment + CATCH_UP - timedelta(seconds=1)) == (3, moment)
    assert due_anniversary(JOINED, moment + CATCH_UP) is None
    recent = datetime(2026, 1, 5, 12, 0, tzinfo=UTC)
    assert due_anniversary(recent, datetime(2026, 1, 5, 13, 0, tzinfo=UTC)) is None, "the day they joined isn't one"
    assert due_anniversary(recent, datetime(2027, 1, 5, 12, 0, tzinfo=UTC)) == (1, datetime(2027, 1, 5, 12, 0, tzinfo=UTC))


def test_a_leap_day_join_falls_on_28_february_and_new_years_eve_crosses_the_year():
    leap = datetime(2024, 2, 29, 18, 0, tzinfo=UTC)
    assert anniversary_moment(leap, 2025) == datetime(2025, 2, 28, 18, 0, tzinfo=UTC)
    assert anniversary_moment(leap, 2028) == datetime(2028, 2, 29, 18, 0, tzinfo=UTC)
    eve = datetime(2024, 12, 31, 23, 30, tzinfo=UTC)
    assert due_anniversary(eve, datetime(2026, 1, 1, 0, 10, tzinfo=UTC)) == (1, datetime(2025, 12, 31, 23, 30, tzinfo=UTC))


def test_upcoming_lists_the_next_ones_soonest_first_skipping_first_years():
    now = datetime(2026, 10, 14, tzinfo=UTC)
    members = [("Shikashi", JOINED), ("Goose", datetime(2025, 11, 12, 20, 59, tzinfo=UTC)),
               ("New", datetime(2026, 9, 1, tzinfo=UTC)), ("Wolfie", datetime(2025, 11, 20, 19, 24, tzinfo=UTC))]
    assert [(name, years) for _moment, name, years in upcoming(members, now)] == [
        ("Shikashi", 3), ("Goose", 1), ("Wolfie", 1)]


# -- 2. wording ---------------------------------------------------------------------------------


def test_the_messages_are_formal_and_name_star_tech_dynamics_in_full():
    stamp = int(JOINED.timestamp())
    assert dm_text("Shikashi", 3, JOINED) == (
        "🎉 **Happy anniversary, Shikashi!**\n"
        f"Today marks 3 years since you joined **Star Tech Dynamics**, on <t:{stamp}:D> at <t:{stamp}:t>. "
        "Thank you for being part of the community. We're glad to have you with us. 🥳\n"
        "🎖️ You've earned the **3-Year Veteran** role.")
    assert dm_text("Shikashi", 3, JOINED, "🚀 event").endswith("role.\n🚀 event")
    assert post_text("<@1>", 1, JOINED) == (
        "🎊 **Happy server anniversary!** 🎊\n"
        f"<@1> joined **Star Tech Dynamics** 1 year ago today, on <t:{stamp}:D>, and is now a **1-Year Veteran**. "
        "Thank you for being part of the community! 🎉")
    assert "STDynamics" not in dm_text("x", 2, JOINED) + post_text("x", 2, JOINED)


# -- 3. sending ---------------------------------------------------------------------------------


class FakeMember:
    def __init__(self, user_id, joined, *, bot=False, name=None):
        self.id, self.bot, self.joined_at = user_id, bot, joined
        self.display_name, self.mention, self.roles = name or f"Member{user_id}", f"<@{user_id}>", []

    async def add_roles(self, *roles, reason=None):
        self.roles += [r for r in roles if r not in self.roles]

    async def remove_roles(self, *roles, reason=None):
        self.roles = [r for r in self.roles if r not in roles]


def _member(user_id, joined, *, bot=False, name=None):
    return FakeMember(user_id, joined, bot=bot, name=name)


class FakeGuild:
    def __init__(self, members, *, events=(), can_manage_roles=True):
        self.id, self.members, self.roles, self.events = GUILD_ID, members, [], list(events)
        self.can_manage_roles = can_manage_roles

    def get_member(self, uid):
        return next((m for m in self.members if m.id == uid), None)

    async def create_role(self, name, reason=None):
        if not self.can_manage_roles:
            raise discord.Forbidden(NS(status=403, reason="Forbidden"), "Missing Permissions")
        role = NS(id=9000 + len(self.roles), name=name)
        self.roles.append(role)
        return role

    async def fetch_scheduled_events(self):
        return self.events


class FakeUser:
    def __init__(self, fail=None):
        self.sent, self.fail = [], fail

    async def send(self, content=None, **kwargs):
        if self.fail:
            raise self.fail.pop(0) if isinstance(self.fail, list) and self.fail else self.fail
        self.sent.append((content, kwargs))


class FakeChannel:
    def __init__(self, guild):
        self.id, self.guild, self.sent, self.fail = ANNIVERSARY_CHANNEL_ID, guild, [], []

    async def send(self, content=None, **kwargs):
        if self.fail:
            raise self.fail.pop(0)
        self.sent.append((content, kwargs))


def _http(status):
    error = discord.Forbidden if status == 403 else discord.HTTPException
    return error(NS(status=status, reason="x"), "refused" if status == 403 else "server error")


async def _make(tmp_path, members, users=None, **guild_kwargs):
    db = Database(tmp_path / "ann.sqlite3", Fernet(Fernet.generate_key()))
    await db.init()
    guild = FakeGuild(members, **guild_kwargs)
    channel = FakeChannel(guild)
    users = users or {}
    for member in members:
        users.setdefault(member.id, FakeUser())
    bot = NS(db=db, get_channel=lambda cid: channel if cid == ANNIVERSARY_CHANNEL_ID else None,
             get_user=lambda uid: users.get(uid), fetch_user=AsyncMock(), is_owner=AsyncMock(return_value=True),
             wait_until_ready=AsyncMock(), add_dynamic_items=lambda *items: None)
    return Anniversaries(bot, start_loop=False), bot, channel, users


NOW = datetime(2026, 11, 12, 3, 45, tzinfo=UTC)


def test_a_due_member_gets_one_dm_and_one_post_that_pings_only_them_even_after_restarts(tmp_path):
    async def run():
        members = [_member(1, JOINED, name="Shikashi"), _member(2, JOINED, bot=True),
                   _member(3, datetime(2026, 1, 1, tzinfo=UTC)), _member(4, datetime(2025, 3, 3, tzinfo=UTC))]
        cog, bot, channel, users = await _make(tmp_path, members)
        first = await cog.send_due(NOW)
        await cog.send_due(NOW + timedelta(minutes=5))
        restarted = Anniversaries(bot, start_loop=False)  # a restart: nothing in memory, only the database
        await restarted.send_due(NOW + timedelta(hours=2))
        return first, channel, users

    first, channel, users = asyncio.run(run())
    assert first == 1
    ((dm, dm_kwargs),) = users[1].sent
    assert dm.startswith("🎉 **Happy anniversary, Shikashi!**") and "3 years" in dm
    (button,) = dm_kwargs["view"].children
    assert isinstance(button, StopAnniversariesButton) and button.item.label == "Stop anniversary messages"
    assert button.custom_id == f"anniversary:stop:{GUILD_ID}"
    ((post, post_kwargs),) = channel.sent
    assert post.startswith("🎊 **Happy server anniversary!** 🎊\n<@1> joined **Star Tech Dynamics** 3 years ago today")
    mentions = post_kwargs["allowed_mentions"]
    assert [u.id for u in mentions.users] == [1] and not mentions.everyone and not mentions.roles
    assert not users[2].sent and not users[3].sent and not users[4].sent, "a bot, a first-year and a not-due member"


def test_a_temporary_failure_is_retried_a_refusal_is_not_and_one_part_never_blocks_the_other(tmp_path):
    async def run():
        retry_user = FakeUser(fail=[_http(503)])
        closed_user = FakeUser(fail=_http(403))
        members = [_member(1, JOINED), _member(5, JOINED)]
        cog, _bot, channel, users = await _make(tmp_path, members, {1: retry_user, 5: closed_user})
        channel.fail = [_http(503)]
        await cog.send_due(NOW)
        after_first = (len(retry_user.sent), len(channel.sent))
        await cog.send_due(NOW + timedelta(minutes=5))
        await cog.send_due(NOW + timedelta(minutes=10))
        return after_first, retry_user, closed_user, channel

    after_first, retry_user, closed_user, channel = asyncio.run(run())
    assert after_first == (0, 1), "member 1's DM failed for now; one post still went (the other failed for now)"
    assert len(retry_user.sent) == 1, "the temporary DM failure was sent on the next check, once"
    assert closed_user.sent == [], "closed DMs aren't retried"
    assert sorted(post.split(" joined")[0].split("\n")[1] for post, _ in channel.sent) == ["<@1>", "<@5>"], \
        "both posts went out once each, the one that failed on a later check"


def test_a_missed_anniversary_is_caught_up_within_the_day_and_not_after(tmp_path):
    async def run():
        cog, _bot, channel, users = await _make(tmp_path, [_member(1, JOINED)])
        await cog.send_due(NOW + CATCH_UP)  # the bot was down all day
        late = len(channel.sent)
        await cog.send_due(NOW + timedelta(hours=20))
        return late, channel

    late, channel = asyncio.run(run())
    assert late == 0 and len(channel.sent) == 1


def test_stop_and_resume_buttons_and_an_opted_out_member_gets_nothing(tmp_path):
    async def run():
        cog, bot, channel, users = await _make(tmp_path, [_member(1, JOINED)])
        click = NS(user=NS(id=1), client=bot, response=NS(send_message=AsyncMock()))
        await StopAnniversariesButton(GUILD_ID).callback(click)
        stopped = await bot.db.get_anniversary_opt_outs(GUILD_ID)
        await cog.send_due(NOW)
        silent = (list(users[1].sent), list(channel.sent))
        reply = click.response.send_message.await_args
        await ResumeAnniversariesButton(GUILD_ID).callback(click)
        await cog.send_due(NOW + timedelta(minutes=5))
        return stopped, silent, reply, users, channel, await bot.db.get_anniversary_opt_outs(GUILD_ID)

    stopped, silent, reply, users, channel, resumed = asyncio.run(run())
    assert stopped == {1} and silent == ([], [])
    assert reply.kwargs["ephemeral"] and "won't receive anniversary messages from Star Tech Dynamics" in reply.args[0]
    (resume,) = reply.kwargs["view"].children
    assert resume.custom_id == f"anniversary:resume:{GUILD_ID}" and resume.item.label == "Turn them back on"
    assert resumed == set() and len(users[1].sent) == 1 and len(channel.sent) == 1


def test_a_display_name_cannot_ping_or_format(tmp_path):
    async def run():
        cog, _bot, _channel, users = await _make(tmp_path, [_member(1, JOINED, name="@everyone **boss**")])
        await cog.send_due(NOW)
        return users[1].sent[0][0]

    dm = asyncio.run(run())
    assert "@everyone" not in dm.replace("@​everyone", "") and "\\*\\*boss\\*\\*" in dm


def test_the_check_survives_one_members_unexpected_error(tmp_path, monkeypatch):
    async def run():
        cog, bot, channel, users = await _make(tmp_path, [_member(1, JOINED), _member(5, JOINED)])
        real = bot.db.claim_anniversary

        async def flaky(guild_id, user_id, *args):
            if user_id == 1:
                raise RuntimeError("boom")
            return await real(guild_id, user_id, *args)

        bot.db.claim_anniversary = flaky
        await cog.check_anniversaries.coro(cog)  # the real loop body: must not raise
        await cog.send_due(NOW)
        return channel

    monkeypatch.setattr(cog_module, "datetime", NS(now=lambda tz=None: NOW))
    channel = asyncio.run(run())
    assert [post.split(" joined")[0].split("\n")[1] for post, _ in channel.sent] == ["<@5>"]


# -- 4. preview -----------------------------------------------------------------------------------


def _interaction(user_id):
    return NS(user=NS(id=user_id), response=NS(send_message=AsyncMock(), defer=AsyncMock()),
              followup=NS(send=AsyncMock()))


def test_preview_is_owner_only_and_shows_the_owner_both_messages(tmp_path):
    async def run():
        cog, bot, channel, users = await _make(tmp_path, [_member(1, JOINED, name="Owner"),
                                                          _member(5, datetime(2025, 11, 20, 19, 24, tzinfo=UTC))])
        bot.is_owner = AsyncMock(return_value=False)
        stranger = _interaction(5)
        await Anniversaries.anniversary_preview.callback(cog, stranger)
        bot.is_owner = AsyncMock(return_value=True)
        owner = _interaction(1)
        await Anniversaries.anniversary_preview.callback(cog, owner)
        return stranger, owner, users, channel

    stranger, owner, users, channel = asyncio.run(run())
    assert stranger.response.send_message.await_args.kwargs["ephemeral"] and not users[5].sent
    assert users[1].sent[0][0].startswith("🎉 **Happy anniversary, Owner!**")
    reply = owner.followup.send.await_args
    assert reply.kwargs["ephemeral"] and "<@1> joined **Star Tech Dynamics**" in reply.args[0]
    assert "**Next anniversaries**" in reply.args[0]
    assert reply.kwargs["allowed_mentions"].users is False
    assert channel.sent == [], "a preview never posts in the channel"
