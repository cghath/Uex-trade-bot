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
import re
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock

import discord
from cryptography.fernet import Fernet

from bot.cogs.anniversaries import ANNIVERSARY_CHANNEL_ID
from bot.cogs.new_members import NewMembers, NewMembersView
from bot.db.database import Database
from bot.uex.new_members import (
    CSV_COLUMNS, DRIFTING, EMPTY, LEFT, member_label, NEEDS_HELLO, NEW_MEMBER_ROLE, SETTLING, NewMember, breakdown_text, days_since, flag, is_new,
    member_picks, member_text, members_csv, onboarding_status, summary_text, timezone_counts, week_key, weekly_text,
    zone_bucket,
)

UTC = timezone.utc
GUILD_ID = 1132378045232197694
OWNER = 999
NOW = datetime(2026, 11, 9, 16, 0, tzinfo=UTC)  # a Monday, 11 AM Eastern
RSI = "6eXmvxEXsa"


def _ids(text: str) -> list[int]:
    return [int(uid) for uid in re.findall(r"discord\.com/users/(\d+)\)\*\* ", text)]


def test_a_member_shows_by_name_linked_to_their_profile_never_as_unknown_user():
    named = NewMember(5, NOW - timedelta(days=1), username="kara_sc", display_name="Kara*")
    assert member_label(named) == r"**[Kara\*](https://discord.com/users/5)** (@kara\_sc)"
    assert member_label(NewMember(6, NOW, username="vex", display_name="vex")) == "**[vex](https://discord.com/users/6)**"


# -- the rules ----------------------------------------------------------------------

def test_a_member_is_new_for_their_first_21_days_by_join_date():
    assert is_new(NOW - timedelta(days=20, hours=23), NOW)
    assert not is_new(NOW - timedelta(days=21), NOW)
    assert not is_new(None, NOW)
    assert is_new(NOW - timedelta(days=40), NOW, days=45)


def test_days_are_eastern_calendar_days_so_joining_yesterday_is_one_day_whatever_the_hour():
    morning = datetime(2026, 11, 4, 14, tzinfo=UTC)  # Nov 4, 9 AM Eastern
    assert days_since(datetime(2026, 11, 1, 3, tzinfo=UTC), morning) == 4, "Oct 31, 11 PM Eastern"
    assert days_since(datetime(2026, 11, 3, 6, tzinfo=UTC), morning) == 1, "Nov 3, 1 AM Eastern"
    assert days_since(datetime(2026, 11, 4, 4, tzinfo=UTC), morning) == 1, "Nov 3, 11 PM Eastern: a UTC Nov 4"
    assert days_since(datetime(2026, 11, 4, 13, tzinfo=UTC), morning) == 0


def test_the_flags_needs_a_hello_drifting_and_settling_in():
    def member(days_ago, *, active=0, voice=0, quiet=None, reached=None):
        last = NOW - timedelta(days=quiet) if quiet is not None else None
        return NewMember(1, NOW - timedelta(days=days_ago), first_active=date(2026, 11, 1) if active else None,
                         active_days=active, voice_minutes=voice, last_active=last,
                         reached_by="Cheeno" if reached is not None else None,
                         reached_at=NOW - timedelta(days=reached) if reached is not None else None)

    assert flag(member(3), NOW) == NEEDS_HELLO
    assert flag(member(2), NOW) is None, "give them a couple of days"
    assert flag(member(5, reached=1), NOW) is None
    assert flag(member(10, active=2, quiet=5), NOW) == DRIFTING
    assert flag(member(10, active=2, quiet=6, reached=7), NOW) == DRIFTING, "reached out before they went quiet"
    assert flag(member(10, active=2, quiet=6, reached=2), NOW) is None
    assert flag(member(10, active=6, voice=200, quiet=6), NOW) == DRIFTING, "quiet now beats a good start"
    assert flag(member(3, active=2, quiet=0), NOW) == SETTLING, "2 of 4 days"
    assert flag(member(3, active=1, voice=60, quiet=0), NOW) == SETTLING
    assert flag(member(3, active=1, voice=59, quiet=0), NOW) is None
    assert flag(member(0, active=1, quiet=0), NOW) is None, "posting on day 0 is too soon to call"
    assert flag(member(0, active=1, voice=75, quiet=0), NOW) == SETTLING


def test_a_members_lines_say_where_they_came_from_what_they_picked_how_active_and_the_flag():
    joined = NOW - timedelta(days=4)
    text = member_text(NewMember(5, joined, "RSI org page", ["Mining", "Eastern", "New Pilot"]), NOW)
    assert text.splitlines() == [
        f"**[5](https://discord.com/users/5)** · joined <t:{int(joined.timestamp())}:d> (4 days) · RSI org page",
        "-# Mining · Eastern · New Pilot",
        "🎙️ 0 min voice · 📅 active 0 of 5 days",
        "🔴 **Needs a hello**: no posts or voice yet, 4 days in"]
    active = member_text(NewMember(6, joined, None, [], date(2026, 11, 6), 2, 95, NOW - timedelta(days=1),
                                   "Cheeno", NOW - timedelta(days=1)), NOW)
    assert "before join sources were tracked" in active and "No Onboarding picks" in active
    assert active.splitlines()[2:] == ["🎙️ 95 min voice · 📅 active 2 of 5 days",
                                       "🟢 **Settling in** · ✅ Reached out: Cheeno, Nov 8"]


def _flagged():
    return [NewMember(5, NOW - timedelta(days=6), "Reddit ad"),
            NewMember(7, NOW - timedelta(days=12), None, [], date(2026, 10, 30), 2, 10, NOW - timedelta(days=6)),
            NewMember(8, NOW - timedelta(days=12), None, [], date(2026, 10, 30), 1, 0, NOW - timedelta(days=9)),
            NewMember(6, NOW - timedelta(days=6), first_active=date(2026, 11, 4), active_days=5,
                      last_active=NOW)]


def test_the_header_counts_every_flag_whatever_is_shown():
    assert summary_text(_flagged(), NOW, 21).splitlines()[1] == (
        "4 joined · **1 needs a hello** · **2 drifting** · 1 settling in")
    assert "Showing 🟡 Drifting only" in summary_text(_flagged(), NOW, 21, DRIFTING)


def test_the_weekly_note_lists_who_needs_a_hello_or_is_drifting_with_why_and_is_skipped_when_nobody_is():
    text = weekly_text(_flagged(), NOW)
    assert text.splitlines()[:4] == ["🌱 **New members this week: 1 needs a hello · 2 drifting**",
                                     "🔴 **[5](https://discord.com/users/5)**: no posts or voice yet, 6 days in · Reddit ad",
                                     "🟡 **[7](https://discord.com/users/7)**: quiet 6 days",
                                     "🟡 **[8](https://discord.com/users/8)**: quiet 9 days"]
    assert "users/6)" not in text
    assert weekly_text(_flagged()[3:], NOW) is None
    assert week_key(NOW) == "2026-W46"


# -- fakes --------------------------------------------------------------------------

class FakeMember:
    def __init__(self, uid, joined_at, *, name=None, roles=(), bot=False, moderator=False, onboarded=True):
        self.id, self.joined_at, self.bot = uid, joined_at, bot
        self.flags = NS(started_onboarding=True, completed_onboarding=onboarded)
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
             fetch_user=AsyncMock(side_effect=lambda uid: NS(name=f"gone{uid}", display_name=f"Gone {uid}")),
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
        await db.touch_member_activity(GUILD_ID, 11, "2026-11-08 20:00:00")
        await db.credit_voice_minute(GUILD_ID, "2026-11-08", [(11, 1)], 300)
        await db.credit_voice_minute(GUILD_ID, "2026-11-08", [(11, 1)], 300)
        await db.credit_voice_minute(GUILD_ID, "2026-11-01", [(11, 1)], 300)  # before they joined
        await db.mark_active_day(GUILD_ID, 10, "2026-11-01")  # before they joined: doesn't count
        await db.mark_new_member_contacted(GUILD_ID, 12, OWNER, "2026-11-09 12:00:00")
        sent = []
        interaction = NS(guild_id=GUILD_ID, user=FakeMember(77, NOW - timedelta(days=500), name="Mod", moderator=True),
                         response=NS(defer=AsyncMock(), send_message=AsyncMock()),
                         followup=NS(send=AsyncMock(side_effect=lambda **kw: sent.append(kw["view"]))))
        await cog.new_members.callback(cog, interaction, 21, None)
        view = sent[0]
        await cog.new_members.callback(cog, interaction, 21, "needs-hello", None)
        shown_text = "\n".join(c.content for c in sent[1].walk_children() if hasattr(c, "content"))
        only = _ids(shown_text)
        text = "\n".join(c.content for c in view.walk_children() if hasattr(c, "content"))
        # mark one from the menu
        view.menu._values = ["10"]  # what discord.py fills in from the click
        click = NS(guild_id=GUILD_ID, user=interaction.user, response=NS(edit_message=AsyncMock()))
        await view._mark(click)
        after = "\n".join(c.content for c in view.walk_children() if hasattr(c, "content"))
        refused = NS(guild_id=GUILD_ID, user=FakeMember(78, NOW, moderator=False), response=NS(send_message=AsyncMock(), defer=AsyncMock()))
        await cog.new_members.callback(cog, refused, 21, None)
        return text, after, refused.response.send_message.await_args.args[0], only, shown_text

    text, after, refused, only, shown_text = asyncio.run(run())
    lines = text.splitlines()
    assert lines[1] == "3 joined · **1 needs a hello**"
    assert _ids(text) == [12, 11, 10], "newest first; the owner and Kara aren't new"
    assert "**[member12](https://discord.com/users/12)** · joined" in text and "<@12>" not in text
    assert "Member referral (Kara)" in text
    assert "✅ Reached out: Cheeno, Nov 9" in text
    assert "🎙️ 2 min voice · 📅 active 1 of 3 days" in text
    assert "RSI org page" in text and "-# Mining · Eastern" in text
    assert "🔴 **Needs a hello**: no posts or voice yet, 5 days in" in text
    assert only == [10] and "Showing 🔴 Needs a hello only" in shown_text
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
    assert len(dms) == 1 and "🔴 **[member10](https://discord.com/users/10)**: no posts or voice yet, 6 days in" in dms[0]


# -- breakdowns and the CSV -----------------------------------------------------------

def test_onboarding_picks_by_role_id_and_timezone_counts_in_panel_order():
    picker = {"Eastern": 1, "Pacific": 2, "Mining": 3, "Salvage": 4, "New Pilot": 5}
    assert member_picks({1, 3, 5}, picker) == ("Eastern", ["Mining"], "New Pilot")
    assert member_picks({99}, picker) == (None, [], None)
    assert timezone_counts(["Pacific", None, "Eastern", "Pacific", None]) == [
        ("Eastern", 1), ("Pacific", 2), ("Not picked", 2)]


def test_the_breakdown_shows_each_timezones_flags_and_activity_counts():
    def member(zone, activities, *, days_ago, active=0, quiet=None):
        return NewMember(1, NOW - timedelta(days=days_ago), first_active=date(2026, 11, 1) if active else None,
                         active_days=active, last_active=NOW - timedelta(days=quiet) if quiet is not None else None,
                         timezone=zone, activities=activities)

    members = [member("Eastern", ["Mining", "Salvage"], days_ago=10, active=6, quiet=0),  # settling
               member("Eastern", ["Mining"], days_ago=10, active=1, quiet=8),             # drifting
               member("Eastern", [], days_ago=4),                                         # needs a hello
               member("Pacific", ["FPS"], days_ago=1),                                    # nothing yet
               member(None, ["Salvage"], days_ago=10, active=1, quiet=7)]                 # drifting
    assert breakdown_text(members, NOW).splitlines() == [
        "**By timezone:** Eastern 3 (🟢1 🟡1 🔴1) · Pacific 1 · Not picked 1 (🟡1)",
        "**By activity:** Mining 2 · Salvage 2 · FPS 1 · Not picked 1"]


def test_onboarding_status_from_discords_flags_and_where_a_member_without_a_timezone_counts():
    before, after = datetime(2026, 10, 20, tzinfo=UTC), datetime(2026, 11, 2, tzinfo=UTC)
    assert onboarding_status(after, True, True) == "completed"
    assert onboarding_status(before, False, False) == "before_onboarding"
    assert onboarding_status(after, True, False) == "didnt_finish"
    assert onboarding_status(after, False, False) == "not_started"
    assert zone_bucket("Pacific", "didnt_finish") == "Pacific"
    assert zone_bucket(None, "before_onboarding") == "Joined before Onboarding"
    assert zone_bucket(None, "not_started") == zone_bucket(None, "didnt_finish") == "Didn't finish Onboarding"
    assert zone_bucket(None, "completed") == zone_bucket(None, None) == "Not picked"


def test_a_member_who_didnt_finish_onboarding_needs_a_hello_after_a_day_and_is_marked():
    stuck = NewMember(5, NOW - timedelta(days=1), "Reddit ad", onboarding="didnt_finish")
    assert flag(stuck, NOW) == NEEDS_HELLO, "a day, not three: Onboarding may be what's keeping them out"
    assert member_text(stuck, NOW).splitlines()[-1] == "🔴 **Needs a hello**: didn't finish Onboarding, 1 day in"
    assert flag(NewMember(5, NOW - timedelta(hours=2), onboarding="didnt_finish"), NOW) is None
    posting = NewMember(6, NOW - timedelta(days=4), "Reddit ad", first_active=date(2026, 11, 6), active_days=3,
                        last_active=NOW, onboarding="not_started")
    assert member_text(posting, NOW).splitlines()[-1] == "🟢 **Settling in** · ⚠️ Didn't finish Onboarding"
    header = summary_text([stuck, posting, NewMember(7, NOW, onboarding="completed")], NOW, 21).splitlines()[1]
    assert header == "3 joined · **1 needs a hello** · 1 settling in · ⚠️ 2 didn't finish Onboarding"
    zones = breakdown_text([stuck, posting, NewMember(8, NOW, onboarding="before_onboarding")], NOW).splitlines()[0]
    assert zones == "**By timezone:** Joined before Onboarding 1 · Didn't finish Onboarding 2 (🟢1 🔴1)"


def test_the_csv_has_every_column_including_members_who_left_and_defuses_formulas():
    stay = NewMember(10, NOW - timedelta(days=5), "Reddit ad", timezone="Eastern", activities=["Mining", "FPS"],
                     experience="New Pilot", username="=cmd|calc", display_name="Kara", reached_by="Cheeno",
                     reached_at=NOW)
    gone = NewMember(13, NOW - timedelta(days=3), None, still_here=False)
    lines = members_csv([stay, gone], NOW).splitlines()
    assert lines[0] == ",".join(CSV_COLUMNS)
    assert lines[1] == ("'=cmd|calc,Kara,10,2026-11-04,5,Reddit ad,,Eastern,Mining;FPS,New Pilot,,,0,0,"
                        "none,Cheeno,2026-11-09,yes,"), "reached out, so no flag"
    assert lines[2] == ",,13,2026-11-06,3,before tracking,,,,,,,0,0,left,,,no,"
    banned = NewMember(14, NOW - timedelta(days=3), None, still_here=False, left_at=NOW - timedelta(days=1), removed="banned")
    assert members_csv([banned], NOW).splitlines()[1].endswith(",banned,,,no,2026-11-08")


def test_the_csv_export_is_for_the_owner_only_and_includes_who_left(tmp_path):
    async def run():
        quiet = FakeMember(10, NOW - timedelta(days=5), name="Quiet", roles=[NS(id=501), NS(id=502)])
        cog, db, guild, _owner = await _make(tmp_path, [quiet])
        await db.save_role_picker_roles(GUILD_ID, {"Mining": 501, "Eastern": 502})
        await db.analytics_start(GUILD_ID, "2026-11-01 00:00:00")
        await db.record_member_movement(GUILD_ID, 10, "join", "2026-11-04 16:00:00", RSI)
        await db.record_member_movement(GUILD_ID, 13, "join", "2026-11-06 16:00:00", None)
        await db.record_member_movement(GUILD_ID, 13, "leave", "2026-11-07 16:00:00")
        sent = []

        def interaction(user):
            return NS(guild_id=GUILD_ID, user=user, response=NS(defer=AsyncMock(), send_message=AsyncMock()),
                      followup=NS(send=AsyncMock(side_effect=lambda *a, **kw: sent.append((a, kw)))))

        owner = interaction(FakeMember(OWNER, NOW - timedelta(days=900), name="Cheeno"))
        await cog.new_members.callback(cog, owner, 21, None, None, True)
        hello_only = interaction(FakeMember(OWNER, NOW - timedelta(days=900), name="Cheeno"))
        await cog.new_members.callback(cog, hello_only, 21, NEEDS_HELLO, None, True)
        mod = interaction(FakeMember(77, NOW - timedelta(days=500), name="Mod", moderator=True))
        await cog.new_members.callback(cog, mod, 21, None, None, True)
        files = [kw["file"] for _a, kw in sent]
        return ([(f.filename, f.fp.read().decode("utf-8-sig")) for f in files], [a[0] for a, _kw in sent],
                mod.response.send_message.await_args.args[0])

    files, texts, refused = asyncio.run(run())
    (name, csv_text), (_name2, hello_text) = files
    assert name == "new-members-2026-11-09.csv"
    rows = csv_text.splitlines()
    assert len(rows) == 3, "newest first: the one who left joined Nov 6"
    assert rows[1] == "gone13,Gone 13,13,2026-11-06,1,Not known (no invite matched),,,,,,,0,0,left,,,no,2026-11-07", (
        "a name looked up from Discord, and the days they stayed")
    assert rows[2] == "Quiet,Quiet,10,2026-11-04,5,RSI org page,,Eastern,Mining,,completed,,0,0,needs_hello,,,yes,"
    assert len(hello_text.splitlines()) == 2, "the status filter leaves out who left"
    assert texts[0] == "New members, last 21 days: 2 rows, including any who have left."
    assert refused == "The CSV export is for the bot's owner."


# -- the 2026-11-04 audit fixes ---------------------------------------------------------

def test_the_weekly_note_stays_under_a_dms_limit_however_many_need_a_hello():
    waiting = [NewMember(10_000_000_000_000_000 + i, NOW - timedelta(days=6), "Member referral") for i in range(80)]
    text = weekly_text(waiting, NOW)
    assert len(text) <= 2000 and "more" in text.splitlines()[-2]


def test_a_weekly_note_that_couldnt_be_sent_is_tried_again_the_next_hour(tmp_path):
    async def run():
        quiet = FakeMember(10, NOW - timedelta(days=6))
        cog, _db, _guild, owner = await _make(tmp_path, [quiet])
        sent = owner.send

        async def busy(content=None, **kwargs):
            raise discord.HTTPException(NS(status=503, reason="Service Unavailable"), "busy")

        owner.send = busy
        first = await cog.send_weekly(NOW)
        owner.send = sent
        second = await cog.send_weekly(NOW + timedelta(hours=1))
        third = await cog.send_weekly(NOW + timedelta(hours=2))
        return first, second, third, len(owner.sent)

    assert asyncio.run(run()) == (False, True, False, 1)


def test_reaching_out_before_a_member_left_and_rejoined_doesnt_hide_them(tmp_path):
    async def run():
        back = FakeMember(10, NOW - timedelta(days=4))
        cog, db, guild, _owner = await _make(tmp_path, [back])
        await db.mark_new_member_contacted(GUILD_ID, 10, OWNER, "2026-10-01 12:00:00")  # their last stay
        (member,) = await cog.gather(guild, 21, NOW)
        return member.reached_by, flag(member, NOW)

    assert asyncio.run(run()) == (None, NEEDS_HELLO)


def test_the_role_waits_for_rules_screening_like_the_welcome_dm(tmp_path):
    async def run():
        pending = FakeMember(1, NOW - timedelta(hours=1))
        pending.pending = True
        cog, _db, guild, _owner = await _make(tmp_path, [pending])
        pending.guild = guild
        await cog.on_member_join(pending)
        await cog.put_roles_right(guild, NOW)
        before = len(pending.roles)
        passed = FakeMember(1, pending.joined_at)
        passed.pending, passed.guild, passed.roles = False, guild, pending.roles
        await cog.on_member_update(pending, passed)
        return before, [r.name for r in passed.roles]

    assert asyncio.run(run()) == (0, [NEW_MEMBER_ROLE])


def test_moderate_members_in_another_server_doesnt_open_this_servers_list(tmp_path):
    async def run():
        cog, _db, _guild, _owner = await _make(tmp_path, [])
        elsewhere = NS(guild_id=123, user=FakeMember(77, NOW, moderator=True),
                       response=NS(send_message=AsyncMock(), defer=AsyncMock()))
        await cog.new_members.callback(cog, elsewhere, 21, None, None, False)
        return elsewhere.response.send_message.await_args.args[0]

    assert asyncio.run(run()) == "This command is for the owner and moderators."


def test_long_member_lines_make_more_pages_and_none_drop_off(tmp_path):
    async def run():
        cog, _db, guild, _owner = await _make(tmp_path, [])
        long_picks = ["Mining", "Salvage", "Cargo/Trade", "Combat/Bounty", "FPS", "Exploration", "Medical", "Racing"]
        members = [NewMember(i, NOW - timedelta(days=4), "x" * 400, long_picks * 3) for i in range(8)]
        view = NewMembersView(cog, guild, members, 21, NOW)
        shown = [m.user_id for page in view.pages for m in page]
        sizes = [sum(len(member_text(m, NOW)) for m in page) for page in view.pages]
        return view.page_count, shown, max(sizes)

    pages, shown, biggest = asyncio.run(run())
    assert pages > 1 and shown == list(range(8)) and biggest < 4000


def test_names_cant_become_links_or_mentions_and_empty_lists_read_right():
    text = member_text(NewMember(5, NOW - timedelta(days=1), "Member referral", inviter="[free](https://x.y) <@1>"), NOW)
    assert r"(\[free\]\(https://x.y\) \<@1\>)" in text
    assert EMPTY[NEEDS_HELLO] == "Nobody needs a hello right now."
    assert summary_text([], NOW, 1).startswith("## 🌱 New members: last 1 day\n")


# -- who left ---------------------------------------------------------------------

def test_the_header_counts_who_left_and_their_flags_dont_count():
    quiet = NewMember(1, NOW - timedelta(days=5), "Reddit ad")
    gone = NewMember(2, NOW - timedelta(days=6), "Reddit ad", still_here=False, left_at=NOW - timedelta(days=4))
    header = summary_text([quiet, gone], NOW, 21).splitlines()
    assert header[1] == "2 joined · 👋 1 left · **1 needs a hello**", "the one who left would need a hello too"
    assert summary_text([quiet, gone], NOW, 21, LEFT).splitlines()[2] == "Showing 👋 Who left only"
    assert EMPTY[LEFT] == "Nobody who joined in this time has left."


def test_a_member_who_left_shows_how_and_when_and_what_they_did_while_here():
    joined = datetime(2026, 11, 2, 16, tzinfo=UTC)
    left = NewMember(5, joined, "Spectrum ad", active_days=1, voice_minutes=12, still_here=False,
                     left_at=datetime(2026, 11, 4, 16, tzinfo=UTC), username="drifter", display_name="Drifter")
    lines = member_text(left, NOW).splitlines()
    assert lines[1] == f"👋 **Left** <t:{int(left.left_at.timestamp())}:d>, after 2 days"
    assert lines[2] == "🎙️ 12 min voice · 📅 active 1 of 3 days", "counted over their stay, not until today"
    assert len(lines) == 3, "no Onboarding picks line (their roles went with them) and no flag"
    left.removed, left.left_at = "kicked", joined + timedelta(hours=2)
    assert member_text(left, NOW).splitlines()[1].endswith(", the day they joined")
    assert member_text(left, NOW).splitlines()[1].startswith("👋 **Kicked**")
    left.left_at = None
    assert member_text(left, NOW).splitlines()[1] == "👋 **Kicked** (the day wasn't recorded)"


def test_who_left_lists_members_who_have_gone_with_their_names_and_kicks_and_bans(tmp_path):
    async def run():
        here = FakeMember(10, NOW - timedelta(days=5), name="Here")
        back = FakeMember(14, NOW - timedelta(days=1), name="Back")  # left and came back: here, not gone
        cog, db, guild, _owner = await _make(tmp_path, [here, back])
        await db.analytics_start(GUILD_ID, "2026-11-01 00:00:00")
        await db.record_member_movement(GUILD_ID, 10, "join", "2026-11-04 16:00:00", RSI)
        await db.record_member_movement(GUILD_ID, 12, "join", "2026-11-05 16:00:00", RSI)
        await db.record_member_movement(GUILD_ID, 12, "leave", "2026-11-07 16:00:00")
        await db.record_member_movement(GUILD_ID, 13, "join", "2026-11-06 16:00:00", None)
        await db.record_member_movement(GUILD_ID, 13, "leave", "2026-11-06 18:00:00")
        await db.record_moderation_actions(GUILD_ID, [(1, "ban", "2026-11-06 18:00:00", 1, OWNER, False, 13)])
        await db.record_member_movement(GUILD_ID, 14, "join", "2026-11-03 16:00:00", None)
        await db.record_member_movement(GUILD_ID, 14, "leave", "2026-11-04 16:00:00")
        await db.record_member_movement(GUILD_ID, 14, "join", "2026-11-08 16:00:00", None)
        await db.mark_active_day(GUILD_ID, 12, "2026-11-06")
        sent = []
        interaction = NS(guild_id=GUILD_ID, user=FakeMember(77, NOW - timedelta(days=500), name="Mod", moderator=True),
                         response=NS(defer=AsyncMock(), send_message=AsyncMock()),
                         followup=NS(send=AsyncMock(side_effect=lambda **kw: sent.append(kw["view"]))))
        await cog.new_members.callback(cog, interaction, 21, None)
        await cog.new_members.callback(cog, interaction, 21, LEFT)
        texts = ["\n".join(c.content for c in view.walk_children() if hasattr(c, "content")) for view in sent]
        return texts, getattr(sent[1], "menu", None), cog.bot.fetch_user.await_count

    (everyone, left), menu, fetched = asyncio.run(run())
    assert everyone.splitlines()[1] == "4 joined · 👋 2 left · **1 needs a hello**", "the header counts them"
    assert _ids(everyone) == [14, 10], "the list is who's still here"
    assert _ids(left) == [13, 12], "newest first"
    assert "**[Gone 13](https://discord.com/users/13)** (@gone13)" in left, "named from Discord"
    assert "👋 **Banned** <t:" in left and ", the day they joined" in left
    assert "👋 **Left** <t:" in left and "after 2 days" in left and "📅 active 1 of 3 days" in left
    assert menu is None, "nobody to mark as reached out"
    assert fetched == 2, "names are looked up only for the Who left list"


def test_the_header_explains_what_each_colour_means():
    header = summary_text([NewMember(1, NOW - timedelta(days=5))], NOW, 21).splitlines()
    assert header[2] == ("-# 🔴 no posts or voice after 3 days (1 if Onboarding wasn't finished) · 🟡 was active, "
                         "then quiet 5+ days · 🟢 active on 50% of their days, or 60+ min voice · reaching out clears 🔴 and 🟡")
