"""Voice ranks (bot/uex/voice_ranks.py, bot/cogs/voice_ranks.py).

Guarantees under test: a minute counts only with 2+ members who aren't bots or deafened, never in
the AFK channel, double in the Ops category, up to the daily cap; a rank-up is announced once and
gives the one rank role; an op is 3+ people for 30+ minutes, attended by those there for half of
it, and survives a restart; roles are put right by the check; /rank and the leaderboard read
right, escape names and say audio is never read.
"""
from __future__ import annotations

import asyncio
import sqlite3
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace as NS

import discord
from cryptography.fernet import Fernet

from bot.cogs.voice_ranks import (
    ANNOUNCE_CHANNEL_ID, OPS_CATEGORY_ID, PRIVACY_NOTE, VoiceRanks, leaderboard_text, rank_text,
)
from bot.db.database import Database
from bot.uex.voice_ranks import (
    OP_GRACE_MINUTES, OpSession, counted_members, op_attendees, op_step, progress, rank_index, season_name,
    season_start, week_start,
)

GUILD_ID = 1132378045232197694
AFK_ID = 1161450821750378516
T0 = datetime(2026, 10, 26, 18, 0, tzinfo=timezone.utc)


# -- the rules ------------------------------------------------------------------

def _m(user_id, *, bot=False, self_deaf=False, deaf=False, self_mute=False, name=None):
    return NS(id=user_id, bot=bot, voice=NS(self_deaf=self_deaf, deaf=deaf, self_mute=self_mute),
              display_name=name or f"Member{user_id}", mention=f"<@{user_id}>", roles=[])


def _vc(channel_id, members, category_id=None):
    return NS(id=channel_id, members=members, category_id=category_id)


def test_a_minute_counts_only_with_two_people_who_can_hear_and_never_in_afk():
    pair = [_m(1), _m(2, self_mute=True)]
    assert [m.id for m in counted_members(_vc(10, pair), AFK_ID)] == [1, 2], "muted still counts"
    assert counted_members(_vc(AFK_ID, pair), AFK_ID) == []
    assert counted_members(_vc(10, [_m(1)]), AFK_ID) == [], "alone"
    assert counted_members(_vc(10, [_m(1), _m(2, bot=True)]), AFK_ID) == [], "a bot isn't company"
    assert counted_members(_vc(10, [_m(1), _m(2, self_deaf=True)]), AFK_ID) == [], "nor is someone deafened"
    assert counted_members(_vc(10, [_m(1), _m(2, deaf=True)]), AFK_ID) == []
    assert [m.id for m in counted_members(_vc(10, [_m(1), _m(2), _m(3, self_deaf=True)]), AFK_ID)] == [1, 2]


def test_the_ladder_and_progress():
    assert [rank_index(x) for x in (0, 59, 60, 299, 300, 60_000, 10**7)] == [-1, -1, 0, 0, 1, 8, 8]
    p = progress(600)
    assert (p.rank, p.next_rank, p.needed, round(p.fraction, 2)) == ("Crewman", "Specialist", 300, 0.5)
    top = progress(70_000)
    assert (top.rank, top.next_rank, top.needed) == ("Admiral", None, 0)
    ops = progress(14, (("Operator", 5), ("Veteran Operator", 15)))
    assert (ops.rank, ops.next_rank, ops.needed) == ("Operator", "Veteran Operator", 1)


def test_weeks_start_monday_and_seasons_are_quarters():
    assert week_start(date(2026, 10, 26)) == date(2026, 10, 26)
    assert week_start(date(2026, 11, 1)) == date(2026, 10, 26)
    assert season_start(date(2026, 12, 31)) == date(2026, 10, 1) and season_name(date(2026, 12, 31)) == "Q4 2026"
    assert season_start(date(2027, 1, 1)) == date(2027, 1, 1)


def test_an_op_needs_three_people_for_thirty_minutes_and_half_of_it_to_attend():
    session, ended = None, None
    crew, late = {1, 2, 3}, {1, 2, 3, 4}
    for minute in range(40):
        people = late if minute >= 25 else crew  # 4 joins for the last 15 of 40 minutes
        session, ended = op_step(session, people, T0 + timedelta(minutes=minute))
        assert ended is None
    for minute in range(OP_GRACE_MINUTES - 1):
        session, ended = op_step(session, {1}, T0)
        assert ended is None, "a short gap doesn't end it"
    session, ended = op_step(session, {1}, T0)
    assert session is None and ended.minutes == 40
    assert op_attendees(ended) == [1, 2, 3], "15 of 40 minutes isn't half"
    short = OpSession(started_at=T0, minutes=29, present={1: 29, 2: 29, 3: 29})
    assert op_attendees(short) == []
    assert op_step(None, {1, 2}, T0) == (None, None), "two people aren't an op"


# -- the cog ------------------------------------------------------------------

class FakeMember:
    def __init__(self, user_id, *, bot=False, self_deaf=False, name=None):
        self.id, self.bot = user_id, bot
        self.voice = NS(self_deaf=self_deaf, deaf=False, self_mute=False)
        self.display_name = name or f"Member{user_id}"
        self.mention = f"<@{user_id}>"
        self.roles = []

    async def add_roles(self, *roles, reason=None):
        self.roles += [r for r in roles if r not in self.roles]

    async def remove_roles(self, *roles, reason=None):
        self.roles = [r for r in self.roles if r not in roles]


class FakeGuild:
    def __init__(self, *, can_manage_roles=True):
        self.id = GUILD_ID
        self.afk_channel = NS(id=AFK_ID)
        self.voice_channels, self.stage_channels, self.roles, self.members = [], [], [], {}
        self.can_manage_roles = can_manage_roles

    def get_member(self, user_id):
        return self.members.get(user_id)

    async def create_role(self, name, reason=None):
        if not self.can_manage_roles:
            raise discord.Forbidden(NS(status=403, reason="Forbidden"), "Missing Permissions")
        role = NS(id=9000 + len(self.roles), name=name)
        self.roles.append(role)
        return role

    def put(self, channel_id, members, category_id=None):
        """Seat these members in a voice channel (replacing whoever was there)."""
        for m in members:
            self.members[m.id] = m
        self.voice_channels = [c for c in self.voice_channels if c.id != channel_id]
        self.voice_channels.append(NS(id=channel_id, members=list(members), category_id=category_id))


async def _make(tmp_path, guild, db=None):
    if db is None:
        db = Database(tmp_path / "voice.sqlite3", Fernet(Fernet.generate_key()))
        await db.init()
    posts = []

    async def send(content=None, **kwargs):
        posts.append((content, kwargs.get("allowed_mentions")))

    channel = NS(id=ANNOUNCE_CHANNEL_ID, guild=guild, send=send)
    bot = NS(db=db, get_channel=lambda cid: channel if cid == ANNOUNCE_CHANNEL_ID else None)
    return VoiceRanks(bot, start_loop=False), db, posts


def _sql(tmp_path, statement, params=()):
    conn = sqlite3.connect(tmp_path / "voice.sqlite3")
    rows = conn.execute(statement, params).fetchall()
    conn.commit()
    conn.close()
    return rows


def test_a_minute_credits_xp_double_in_ops_and_nothing_in_afk(tmp_path):
    async def run():
        guild = FakeGuild()
        a, b, c, d, e, f = (FakeMember(i) for i in range(1, 7))
        guild.put(10, [a, b, FakeMember(99, bot=True)])
        guild.put(20, [c, d], category_id=OPS_CATEGORY_ID)
        guild.put(AFK_ID, [e, f])
        cog, db, _posts = await _make(tmp_path, guild)
        await cog.count_minute(T0)
        await cog.count_minute(T0 + timedelta(minutes=1))
        return {r["user_id"]: (r["xp"], r["minutes"]) for r in await db.get_voice_members(GUILD_ID)}

    assert asyncio.run(run()) == {1: (2, 2), 2: (2, 2), 3: (4, 2), 4: (4, 2)}


def test_the_daily_cap_stops_counting_until_the_next_utc_day(tmp_path):
    async def run():
        db = Database(tmp_path / "voice.sqlite3", Fernet(Fernet.generate_key()))
        await db.init()
        got = [await db.credit_voice_minute(GUILD_ID, "2026-10-26", [(1, 2)], 3) for _ in range(5)]
        next_day = await db.credit_voice_minute(GUILD_ID, "2026-10-27", [(1, 2)], 3)
        return got, next_day, await db.get_voice_member(GUILD_ID, 1)

    got, next_day, member = asyncio.run(run())
    assert [len(g) for g in got] == [1, 1, 1, 0, 0] and got[2] == [(1, 4, 6)]
    assert next_day == [(1, 6, 8)] and (member["xp"], member["minutes"]) == (8, 4)


def test_a_rank_up_is_announced_once_and_swaps_the_role(tmp_path):
    async def run():
        guild = FakeGuild()
        a, b = FakeMember(1, name="Nova"), FakeMember(2)
        guild.put(10, [a, b])
        cog, db, posts = await _make(tmp_path, guild)
        await cog.count_minute(T0)
        _sql(tmp_path, "UPDATE voice_members SET xp = 59 WHERE user_id = 1")
        await cog.count_minute(T0 + timedelta(minutes=1))   # 59 -> 60: Cadet
        await cog.count_minute(T0 + timedelta(minutes=2))
        cadet_roles = [r.name for r in a.roles]
        _sql(tmp_path, "UPDATE voice_members SET xp = 299 WHERE user_id = 1")
        await cog.count_minute(T0 + timedelta(minutes=3))   # Crewman
        return posts, cadet_roles, [r.name for r in a.roles], [r.name for r in guild.roles]

    posts, cadet_roles, roles, created = asyncio.run(run())
    assert [p[0] for p in posts] == ["🎖️ <@1> has been promoted to **Cadet**! (1h 0m of voice time)",
                                     "🎖️ <@1> has been promoted to **Crewman**! (5h 0m of voice time)"]
    assert posts[0][1].users[0].id == 1 and not posts[0][1].everyone
    assert cadet_roles == ["Cadet"] and roles == ["Crewman"], "only the highest rank is held"
    assert len(created) == 13 and "Wing Commander" in created


def test_without_manage_roles_the_rank_up_is_still_announced(tmp_path):
    async def run():
        guild = FakeGuild(can_manage_roles=False)
        a = FakeMember(1)
        guild.put(10, [a, FakeMember(2)])
        cog, _db, posts = await _make(tmp_path, guild)
        await cog.count_minute(T0)
        _sql(tmp_path, "UPDATE voice_members SET xp = 59 WHERE user_id = 1")
        await cog.count_minute(T0 + timedelta(minutes=1))
        return posts, a.roles

    posts, roles = asyncio.run(run())
    assert len(posts) == 1 and roles == []


def _op_minutes(cog, guild, start, count, people):
    async def run():
        for minute in range(count):
            guild.put(20, people, category_id=OPS_CATEGORY_ID)
            await cog.count_minute(start + timedelta(minutes=minute))
    return run()


def test_an_op_is_counted_for_who_attended_and_a_fifth_op_earns_operator(tmp_path):
    async def run():
        guild = FakeGuild()
        crew = [FakeMember(i) for i in (1, 2, 3)]
        cog, db, posts = await _make(tmp_path, guild)
        await cog.count_minute(T0)  # creates nobody's row; seed person 1 at 4 ops
        _sql(tmp_path, "INSERT INTO voice_members (guild_id, user_id, ops, op_rank_index) VALUES (?, 1, 4, -1)",
             (GUILD_ID,))
        await _op_minutes(cog, guild, T0, 31, crew)
        await _op_minutes(cog, guild, T0 + timedelta(minutes=31), OP_GRACE_MINUTES, crew[:1])
        members = {r["user_id"]: r["ops"] for r in await db.get_voice_members(GUILD_ID)}
        ops = _sql(tmp_path, "SELECT minutes FROM voice_ops")
        attended = _sql(tmp_path, "SELECT user_id FROM voice_op_attendance ORDER BY user_id")
        return members, ops, attended, posts, [r.name for r in crew[0].roles], await db.load_op_session(GUILD_ID)

    members, ops, attended, posts, roles, saved = asyncio.run(run())
    assert ops == [(31,)] and attended == [(1,), (2,), (3,)]
    assert (members[1], members[2], members[3]) == (5, 1, 1)
    assert [p[0] for p in posts if p[0].startswith("🎯")] == ["🎯 <@1> has earned **Operator**: 5 ops attended."]
    assert sum("**Cadet**" in p[0] for p in posts) == 3, "31 minutes at double XP is 62: Cadet too"
    assert sorted(roles) == ["Cadet", "Operator"], "one role from each ladder"
    assert saved is None


def test_an_op_carries_on_through_a_restart_and_one_that_ended_meanwhile_is_counted(tmp_path):
    async def run():
        guild = FakeGuild()
        crew = [FakeMember(i) for i in (1, 2, 3)]
        cog, db, _ = await _make(tmp_path, guild)
        await _op_minutes(cog, guild, T0, 20, crew)
        restarted, _, _ = await _make(tmp_path, guild, db)     # a restart: memory gone, DB kept
        await _op_minutes(restarted, guild, T0 + timedelta(minutes=20), 15, crew)
        await _op_minutes(restarted, guild, T0 + timedelta(minutes=35), OP_GRACE_MINUTES, [])
        one_op = _sql(tmp_path, "SELECT minutes FROM voice_ops")
        # Another op that the bot was down for the end of: counted on the first minute back.
        await _op_minutes(restarted, guild, T0 + timedelta(hours=2), 40, crew)
        later, _, _ = await _make(tmp_path, guild, db)
        await _op_minutes(later, guild, T0 + timedelta(hours=5), 1, [])
        return one_op, _sql(tmp_path, "SELECT minutes FROM voice_ops ORDER BY id")

    one_op, both = asyncio.run(run())
    assert one_op == [(35,)], "one op of 35 minutes, not two short ones"
    assert both == [(35,), (40,)]


def test_the_role_check_puts_wrong_roles_right(tmp_path):
    async def run():
        guild = FakeGuild()
        a = FakeMember(1)
        guild.members[1] = a
        cog, db, posts = await _make(tmp_path, guild)
        roles = await cog._rank_roles(guild)
        a.roles = [roles["Admiral"], roles["Wing Commander"]]
        _sql(tmp_path, "INSERT INTO voice_members (guild_id, user_id, xp, minutes, ops) VALUES (?, 1, 400, 400, 0)",
             (GUILD_ID,))
        await VoiceRanks.reconcile.coro(cog)
        return [r.name for r in a.roles], posts

    roles, posts = asyncio.run(run())
    assert roles == ["Crewman"] and posts == [], "fixed quietly, nothing announced"


def test_rank_text():
    assert rank_text("Nova", None, 0) == f"🎖️ **Nova** has no voice time yet.\n{PRIVACY_NOTE}"
    text = rank_text("Nova", {"xp": 600, "minutes": 480, "ops": 7}, 95)
    assert text.splitlines()[:4] == [
        "🎖️ **Nova** · **Crewman**",
        "`▰▰▰▰▰▱▱▱▱▱` 300 XP to **Specialist**",
        "600 XP · 8h 0m in voice · this week 1h 35m",
        "🎯 7 ops attended · **Operator** (8 more to Veteran Operator)",
    ]
    assert "never audio" in text


def test_the_leaderboard_skips_who_left_escapes_names_and_shows_your_place():
    members = {i: NS(display_name=f"P{i}") for i in range(1, 14)}
    members[1] = NS(display_name="@everyone *x*")
    rows = [{"user_id": i, "xp": 1000 - i, "minutes": 600} for i in range(1, 15)]  # 14 has left
    text = leaderboard_text("This week", rows, "week", members.get, viewer_id=13)
    lines = text.splitlines()
    assert lines[0] == "🏆 **Voice leaderboard · This week**"
    assert lines[1] == "1. @​everyone \\*x\\* · 999 XP · 10h 0m"
    assert len(lines) == 12 and lines[-1] == "-# You: 13. P13 · 987 XP · 10h 0m"
    assert leaderboard_text("All time", [], "all", members.get, 1).endswith("Nobody yet - hop in voice with someone.")


def test_the_weekly_board_counts_only_this_week(tmp_path):
    async def run():
        db = Database(tmp_path / "voice.sqlite3", Fernet(Fernet.generate_key()))
        await db.init()
        await db.credit_voice_minute(GUILD_ID, "2026-10-25", [(1, 1)], 300)   # last Sunday
        await db.credit_voice_minute(GUILD_ID, "2026-10-26", [(2, 2)], 300)   # this Monday
        return (await db.voice_totals_since(GUILD_ID, "2026-10-26"), await db.voice_totals_since(GUILD_ID, None))

    week, all_time = asyncio.run(run())
    assert week == [{"user_id": 2, "xp": 2, "minutes": 1}]
    assert [r["user_id"] for r in all_time] == [2, 1]


def test_a_rank_already_announced_is_never_announced_again(tmp_path):
    """Each op after the fifth leaves you an Operator: only the fifth announces it."""
    async def run():
        guild = FakeGuild()
        crew = [FakeMember(i) for i in (1, 2, 3)]
        cog, db, posts = await _make(tmp_path, guild)
        _sql(tmp_path, "INSERT INTO voice_members (guild_id, user_id, ops) VALUES (?, 1, 4)", (GUILD_ID,))
        for op in range(2):
            start = T0 + timedelta(hours=op)
            await _op_minutes(cog, guild, start, 30, crew)
            await _op_minutes(cog, guild, start + timedelta(minutes=30), OP_GRACE_MINUTES, [])
        claims = [await db.claim_voice_rank(GUILD_ID, 2, i) for i in (0, 0, 1, 0)]
        return [p[0] for p in posts if p[0].startswith("🎯")], claims

    op_posts, claims = asyncio.run(run())
    assert op_posts == ["🎯 <@1> has earned **Operator**: 5 ops attended."]
    assert claims == [False, False, True, False], "person 2 already announced Cadet (index 0)"
