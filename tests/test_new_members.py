"""New members (bot/uex/new_members.py, bot/cogs/new_members.py).

Guarantees under test: the 🌱 New Member role goes on in a member's first 21 days by Discord's
join date and comes off after - made hoisted and not mentionable, and a check puts it right
after a restart (which is also the first deploy's backfill); /new-members lists the newest first
with source, Onboarding picks, whether they've posted or been in voice and who reached out, flags
"needs a hello" after 3 quiet days, and is for the owner and moderators only; reaching out is
remembered; the weekly note goes once on a Monday and only when someone's waiting. Clocks pinned.
"""
from __future__ import annotations

import asyncio
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock

from cryptography.fernet import Fernet

from bot.cogs.anniversaries import ANNIVERSARY_CHANNEL_ID
from bot.cogs.new_members import NewMembers
from bot.db.database import Database
from bot.uex.new_members import (
    NEW_MEMBER_ROLE, NewMember, is_new, member_text, needs_hello, voice_time, week_key, weekly_text,
)

UTC = timezone.utc
GUILD_ID = 1132378045232197694
OWNER = 999
NOW = datetime(2026, 11, 9, 16, 0, tzinfo=UTC)  # a Monday, 11 AM Eastern
RSI = "6eXmvxEXsa"


# -- the rules ----------------------------------------------------------------------

def test_a_member_is_new_for_their_first_21_days_by_join_date():
    assert is_new(NOW - timedelta(days=20, hours=23), NOW)
    assert not is_new(NOW - timedelta(days=21), NOW)
    assert not is_new(None, NOW)
    assert is_new(NOW - timedelta(days=40), NOW, days=45)


def test_needs_a_hello_after_three_quiet_days_unless_someone_reached_out():
    quiet = NewMember(1, NOW - timedelta(days=3))
    assert needs_hello(quiet, NOW)
    assert not needs_hello(NewMember(1, NOW - timedelta(days=2)), NOW), "give them a couple of days"
    assert not needs_hello(NewMember(1, NOW - timedelta(days=5), first_active=date(2026, 11, 5)), NOW)
    assert not needs_hello(NewMember(1, NOW - timedelta(days=5), reached_by="Cheeno", reached_at=NOW), NOW)


def test_a_members_lines_say_where_they_came_from_what_they_picked_and_whether_theyre_around():
    joined = NOW - timedelta(days=4)
    text = member_text(NewMember(5, joined, "RSI org page", ["Mining", "Eastern", "New Pilot"]), NOW)
    assert text.splitlines() == [
        f"<@5> · joined <t:{int(joined.timestamp())}:d> (4 days) · RSI org page",
        "-# Mining · Eastern · New Pilot",
        "⚠️ **Needs a hello**: no posts or voice in 4 days"]
    active = member_text(NewMember(6, joined, None, [], date(2026, 11, 6), 2, reached_by="Cheeno",
                                   reached_at=NOW - timedelta(days=1)), NOW)
    assert "before join sources were tracked" in active and "No Onboarding picks" in active
    assert "💬 Active 2 of 5 days · ✅ Reached out: Cheeno, Nov 8" in active, "joined Nov 5, today Nov 9"
    voice = member_text(NewMember(6, joined, "Reddit ad", [], date(2026, 11, 6), 3, voice_minutes=130), NOW)
    assert "💬 Active 3 of 5 days · 2h 10m in voice" in voice
    assert [voice_time(m) for m in (45, 60, 130)] == ["45m", "1h", "2h 10m"]


def test_the_weekly_note_lists_only_members_still_waiting_and_is_skipped_when_nobody_is():
    waiting = NewMember(5, NOW - timedelta(days=6), "Reddit ad")
    fine = NewMember(6, NOW - timedelta(days=6), first_active=date(2026, 11, 4))
    text = weekly_text([waiting, fine], NOW)
    assert text.startswith("🌱 **1 new member still needs a hello**")
    assert "- <@5>, joined 6 days ago · Reddit ad" in text and "<@6>" not in text
    assert weekly_text([fine], NOW) is None
    assert week_key(NOW) == "2026-W46"


# -- fakes --------------------------------------------------------------------------

class FakeMember:
    def __init__(self, uid, joined_at, *, name=None, roles=(), bot=False, moderator=False):
        self.id, self.joined_at, self.bot = uid, joined_at, bot
        self.name = self.display_name = name or f"member{uid}"
        self.roles = list(roles)
        self.guild_permissions = NS(moderate_members=moderator)

    async def add_roles(self, *roles, reason=None):
        self.roles += [r for r in roles if r not in self.roles]

    async def remove_roles(self, *roles, reason=None):
        self.roles = [r for r in self.roles if r not in roles]


class FakeGuild:
    def __init__(self, members):
        self.id = GUILD_ID
        self.members = members
        self.roles, self.created = [], []

    def get_member(self, uid):
        return next((m for m in self.members if m.id == uid), None)

    def get_role(self, rid):
        return next((r for r in self.roles if r.id == rid), None)

    async def create_role(self, *, name, reason=None, **settings):
        role = NS(id=7000 + len(self.roles), name=name, permissions=NS(value=0), managed=False, members=[], **settings)
        self.roles.append(role)
        self.created.append((name, settings))
        return role

    async def invites(self):
        return []


async def _make(tmp_path, members):
    db = Database(tmp_path / "db.sqlite3", Fernet(Fernet.generate_key()))
    await db.init()
    guild = FakeGuild(members)
    home = NS(id=ANNIVERSARY_CHANNEL_ID, guild=guild)
    owner = NS(sent=[])

    async def send(content=None, **kwargs):
        owner.sent.append(content)

    owner.send = send
    bot = NS(db=db, get_channel=lambda cid: home if cid == ANNIVERSARY_CHANNEL_ID else None, is_ready=lambda: True,
             is_owner=AsyncMock(side_effect=lambda user: user.id == OWNER), get_user=lambda uid: owner if uid == OWNER else None,
             application_info=AsyncMock(return_value=NS(team=None, owner=NS(id=OWNER))))
    cog = NewMembers(bot, start_loop=False)
    cog.now = lambda: NOW
    return cog, db, guild, owner


# -- the role -----------------------------------------------------------------------

def test_the_role_goes_on_new_members_and_comes_off_after_21_days_and_a_restart_puts_it_right(tmp_path):
    async def run():
        fresh = FakeMember(1, NOW - timedelta(days=2))
        old = FakeMember(2, NOW - timedelta(days=300))
        due = FakeMember(3, NOW - timedelta(days=22))
        bot_member = FakeMember(4, NOW - timedelta(days=1), bot=True)
        cog, _db, guild, _owner = await _make(tmp_path, [fresh, old, due, bot_member])
        first = await cog.put_roles_right(guild, NOW)  # the first deploy: everyone under 21 days
        role = guild.roles[0]
        due.roles.append(role)  # given before; now over 21 days
        joiner = FakeMember(5, NOW)
        joiner.guild = guild
        guild.members.append(joiner)
        await cog.on_member_join(joiner)
        second = await cog.put_roles_right(guild, NOW)
        again = await cog.put_roles_right(guild, NOW)
        return first, second, again, guild.created, [role in m.roles for m in (fresh, old, due, bot_member, joiner)]

    first, second, again, created, holds = asyncio.run(run())
    assert created == [(NEW_MEMBER_ROLE, {"hoist": True, "mentionable": False})], "made once, hoisted, no pings"
    assert first == (1, 0) and second == (0, 1) and again == (0, 0)
    assert holds == [True, False, False, False, True]


# -- the list -----------------------------------------------------------------------

def test_new_members_lists_the_newest_first_with_source_picks_activity_and_who_reached_out(tmp_path):
    async def run():
        mining, eu = NS(id=501), NS(id=502)
        cheeno = FakeMember(OWNER, NOW - timedelta(days=900), name="Cheeno")
        kara = FakeMember(31, NOW - timedelta(days=400), name="Kara")
        quiet = FakeMember(10, NOW - timedelta(days=5), roles=[mining, eu])
        chatty = FakeMember(11, NOW - timedelta(days=2))
        referred = FakeMember(12, NOW - timedelta(days=1))
        cog, db, guild, _owner = await _make(tmp_path, [cheeno, kara, quiet, chatty, referred])
        await db.save_role_picker_roles(GUILD_ID, {"Mining": 501, "Eastern": 502})
        await db.analytics_start(GUILD_ID, "2026-11-01 00:00:00")
        await db.record_member_movement(GUILD_ID, 10, "join", "2026-11-04 16:00:00", RSI)
        await db.record_member_movement(GUILD_ID, 12, "join", "2026-11-08 16:00:00", "sc1abc", 31)
        await db.mark_active_day(GUILD_ID, 11, "2026-11-08")
        await db.credit_voice_minute(GUILD_ID, "2026-11-08", [(11, 1)], 300)
        await db.credit_voice_minute(GUILD_ID, "2026-11-08", [(11, 1)], 300)
        await db.credit_voice_minute(GUILD_ID, "2026-11-01", [(11, 1)], 300)  # before they joined
        await db.mark_active_day(GUILD_ID, 10, "2026-11-01")  # before they joined: doesn't count
        await db.mark_new_member_contacted(GUILD_ID, 12, OWNER, "2026-11-09 12:00:00")
        sent = []
        interaction = NS(user=FakeMember(77, NOW - timedelta(days=500), name="Mod", moderator=True),
                         response=NS(defer=AsyncMock(), send_message=AsyncMock()),
                         followup=NS(send=AsyncMock(side_effect=lambda **kw: sent.append(kw["view"]))))
        await cog.new_members.callback(cog, interaction, 21, None)
        view = sent[0]
        text = "\n".join(c.content for c in view.walk_children() if hasattr(c, "content"))
        # mark one from the menu
        view.menu._values = ["10"]  # what discord.py fills in from the click
        click = NS(user=interaction.user, response=NS(edit_message=AsyncMock()))
        await view._mark(click)
        after = "\n".join(c.content for c in view.walk_children() if hasattr(c, "content"))
        refused = NS(user=FakeMember(78, NOW, moderator=False), response=NS(send_message=AsyncMock(), defer=AsyncMock()))
        await cog.new_members.callback(cog, refused, 21, None)
        return text, after, refused.response.send_message.await_args.args[0]

    text, after, refused = asyncio.run(run())
    lines = text.splitlines()
    assert lines[1] == "3 joined · **1 needs a hello**"
    order = [line.split(" ·")[0] for line in lines if line.startswith("<@")]
    assert order == ["<@12>", "<@11>", "<@10>"], "newest first; the owner and Kara aren't new"
    assert "Member referral (Kara)" in text
    assert "✅ Reached out: Cheeno, Nov 9" in text
    assert "💬 Active 1 of 3 days · 2m in voice" in text
    assert "RSI org page" in text and "-# Mining · Eastern" in text
    assert "⚠️ **Needs a hello**: no posts or voice in 5 days" in text
    assert "✅ Reached out: Mod, Nov 9" in after and "Needs a hello" not in after
    assert refused == "This command is for the owner and moderators."


def test_the_weekly_note_goes_once_on_monday_and_only_when_someone_is_waiting(tmp_path):
    async def run():
        quiet = FakeMember(10, NOW - timedelta(days=6))
        cog, _db, _guild, owner = await _make(tmp_path, [quiet])
        sunday = await cog.send_weekly(NOW - timedelta(days=1))
        early = await cog.send_weekly(NOW.replace(hour=13))  # 8 AM Eastern
        sent = await cog.send_weekly(NOW)
        again = await cog.send_weekly(NOW + timedelta(hours=2))
        return (sunday, early, sent, again), owner.sent

    results, dms = asyncio.run(run())
    assert results == (False, False, True, False)
    assert len(dms) == 1 and "<@10>, joined 6 days ago" in dms[0]
