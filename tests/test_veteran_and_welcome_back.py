"""Veteran roles, the next-event line (bot/cogs/anniversaries.py) and welcome backs
(bot/cogs/welcome_back.py).

Guarantees under test: each anniversary gives that year's veteran role, replacing the last, and
the DM names it and the next event; the role check gives everyone theirs quietly; a role failure
never stops the message. A member back after 30+ days gets one welcome-back DM; a newer member,
someone around recently, and anyone before the one-time history read never do; the read records
only authors and times, skips channels it can't read, and takes voice time.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock

from cryptography.fernet import Fernet

from bot.cogs.anniversaries import ANNIVERSARY_CHANNEL_ID
from bot.cogs.welcome_back import WelcomeBack
from bot.db.database import Database
from bot.uex.anniversaries import Event, event_line, is_veteran_role, years_completed
from tests.test_anniversaries import JOINED, NOW, _make, _member

UTC = timezone.utc
GUILD_ID = 1132378045232197694
MINING = NS(id=55, name="Mining *Mondays*", start_time=NOW + timedelta(days=2), status=NS(value=1))


# -- the rules ------------------------------------------------------------------

def test_years_completed_and_veteran_role_names():
    joined = datetime(2023, 11, 12, 3, 30, tzinfo=UTC)
    assert [years_completed(joined, joined + d) for d in (timedelta(0), timedelta(days=365))] == [0, 0], "2024 is a leap year"
    assert years_completed(joined, joined.replace(year=2024)) == 1
    assert years_completed(joined, datetime(2026, 11, 12, 3, 29, tzinfo=UTC)) == 2
    assert years_completed(joined, datetime(2026, 11, 12, 3, 30, tzinfo=UTC)) == 3
    assert is_veteran_role("2-Year Veteran") and not is_veteran_role("Veteran Operator")
    assert not is_veteran_role("X-Year Veteran") and not is_veteran_role("2-Year Veteran Plus")


def test_the_event_line_points_to_what_is_on_now_or_else_the_soonest_next():
    now = datetime(2026, 10, 29, tzinfo=UTC)
    soon = Event(1, "Mining *Mondays*", now + timedelta(days=4), 1)
    later = Event(2, "Contested Tuesdays", now + timedelta(days=5), 1)
    past = Event(3, "Old", now - timedelta(days=1), 1)
    stamp = int(soon.start.timestamp())
    assert event_line([later, past, soon], GUILD_ID, now) == (
        f"🚀 Come fly with us at our next event: [**Mining \\*Mondays\\***](https://discord.com/events/{GUILD_ID}/1), "
        f"<t:{stamp}:F>.")
    assert event_line([soon, Event(4, "Live op", now - timedelta(hours=1), 2)], GUILD_ID, now) == (
        f"🚀 Come fly with us: [**Live op**](https://discord.com/events/{GUILD_ID}/4) is happening now.")
    assert event_line([past, Event(5, "Cancelled", now + timedelta(days=1), 4)], GUILD_ID, now) is None


# -- veteran roles --------------------------------------------------------------

def test_an_anniversary_gives_that_years_role_replacing_last_years_and_names_it_and_the_next_event(tmp_path):
    async def run():
        member = _member(1, JOINED, name="Shikashi")  # JOINED is 2023: a third anniversary at NOW
        cog, _bot, channel, users = await _make(tmp_path, [member], events=[MINING])
        two = await channel.guild.create_role("2-Year Veteran")
        member.roles = [two]
        await cog.send_due(NOW)
        return [r.name for r in member.roles], users[1].sent[0][0], channel.sent[0][0]

    roles, dm, post = asyncio.run(run())
    assert roles == ["3-Year Veteran"]
    assert "🎖️ You've earned the **3-Year Veteran** role." in dm
    assert dm.endswith(f"[**Mining \\*Mondays\\***](https://discord.com/events/{GUILD_ID}/55), "
                       f"<t:{int(MINING.start_time.timestamp())}:F>.")
    assert "is now a **3-Year Veteran**" in post and "🚀" not in post


def test_without_manage_roles_the_anniversary_still_goes_out(tmp_path):
    async def run():
        member = _member(1, JOINED)
        cog, _bot, channel, users = await _make(tmp_path, [member], can_manage_roles=False)
        await cog.send_due(NOW)
        return member.roles, len(users[1].sent), len(channel.sent)

    assert asyncio.run(run()) == ([], 1, 1)


def test_the_role_check_gives_everyone_theirs_quietly(tmp_path):
    async def run():
        now = datetime.now(UTC)
        members = [_member(1, now - timedelta(days=400)), _member(2, now - timedelta(days=3 * 365 + 5)),
                   _member(3, now - timedelta(days=30)), _member(4, now - timedelta(days=900), bot=True)]
        cog, _bot, channel, users = await _make(tmp_path, members)
        await cog.sync_veteran_roles.coro(cog)
        await cog.sync_veteran_roles.coro(cog)  # twice: nothing doubles up
        return ([[r.name for r in m.roles] for m in members], sum(len(u.sent) for u in users.values()),
                channel.sent, sorted(r.name for r in channel.guild.roles))

    roles, dms, posts, created = asyncio.run(run())
    assert roles == [["1-Year Veteran"], ["3-Year Veteran"], [], []]
    assert dms == 0 and posts == []
    assert created == ["1-Year Veteran", "3-Year Veteran"]


# -- welcome back ---------------------------------------------------------------

class FakeUser:
    def __init__(self):
        self.sent = []

    async def send(self, content=None, **kwargs):
        self.sent.append(content)


def _person(user_id, joined, name=None):
    return NS(id=user_id, bot=False, joined_at=joined, display_name=name or f"P{user_id}", guild=None)


async def _wb(tmp_path, people, *, events=()):
    db = Database(tmp_path / "wb.sqlite3", Fernet(Fernet.generate_key()))
    await db.init()
    users = {p.id: FakeUser() for p in people}
    guild = NS(id=GUILD_ID, afk_channel=NS(id=999), members=people, me=NS(),
               get_member=lambda uid: next((p for p in people if p.id == uid), None),
               fetch_scheduled_events=AsyncMock(return_value=list(events)),
               text_channels=[], voice_channels=[], threads=[])
    for p in people:
        p.guild = guild
    channel = NS(id=ANNIVERSARY_CHANNEL_ID, guild=guild)
    bot = NS(db=db, get_channel=lambda cid: channel if cid == ANNIVERSARY_CHANNEL_ID else None,
             get_user=lambda uid: users.get(uid), fetch_user=AsyncMock())
    return WelcomeBack(bot, start_loop=False), db, guild, users


T = datetime(2026, 11, 1, 20, 0, tzinfo=UTC)


def test_someone_back_after_thirty_days_gets_one_welcome_back_and_the_next_event(tmp_path):
    async def run():
        nova = _person(1, T - timedelta(days=400), "Nova")
        cog, db, guild, users = await _wb(tmp_path, [nova], events=[MINING])
        await db.seed_member_activity(GUILD_ID, {1: (T - timedelta(days=31)).strftime("%Y-%m-%d %H:%M:%S")})
        await cog.active(nova, T)
        await cog.active(nova, T + timedelta(minutes=5))   # still chatting
        await cog.active(nova, T + timedelta(hours=2))     # later the same day
        return users[1].sent

    (dm,) = asyncio.run(run())
    assert dm.startswith("👋 **Welcome back, Nova!**\nIt's good to see you around **Star Tech Dynamics** again.\n🚀")


def test_no_welcome_back_for_someone_around_recently_a_newer_member_or_before_the_read(tmp_path):
    async def run():
        recent, new, unknown = _person(1, T - timedelta(days=400)), _person(2, T - timedelta(days=5)), \
            _person(3, T - timedelta(days=400))
        cog, db, guild, users = await _wb(tmp_path, [recent, new, unknown])
        await cog.active(unknown, T)                       # before the read: not known to be away
        await db.seed_member_activity(GUILD_ID, {1: (T - timedelta(days=29)).strftime("%Y-%m-%d %H:%M:%S")})
        await cog.active(recent, T)
        await cog.active(new, T)
        return {uid: len(u.sent) for uid, u in users.items()}

    assert asyncio.run(run()) == {1: 0, 2: 0, 3: 0}


def test_after_the_read_someone_not_seen_in_thirty_days_is_welcomed_back(tmp_path):
    async def run():
        away = _person(1, T - timedelta(days=400))
        cog, db, guild, users = await _wb(tmp_path, [away])
        await db.seed_member_activity(GUILD_ID, {})
        await cog.active(away, T)
        return len(users[1].sent)

    assert asyncio.run(run()) == 1


def test_voice_counts_but_not_the_afk_channel_or_switching_channels(tmp_path):
    async def run():
        p = _person(1, T - timedelta(days=400))
        cog, db, guild, users = await _wb(tmp_path, [p])
        cog.active = AsyncMock()
        voice, other, afk = NS(id=10), NS(id=11), NS(id=999)
        await cog.on_voice_state_update(p, NS(channel=None), NS(channel=afk))
        await cog.on_voice_state_update(p, NS(channel=voice), NS(channel=voice))  # mute toggled
        await cog.on_voice_state_update(p, NS(channel=voice), NS(channel=None))   # left
        await cog.on_voice_state_update(p, NS(channel=None), NS(channel=voice))
        await cog.on_voice_state_update(p, NS(channel=voice), NS(channel=other))
        return cog.active.await_count

    assert asyncio.run(run()) == 2


class FakeHistoryChannel:
    def __init__(self, messages, *, readable=True):
        self.messages, self.readable = messages, readable

    def permissions_for(self, me):
        return NS(view_channel=True, read_message_history=self.readable)

    def history(self, after=None, limit=None):
        async def gen():
            for m in self.messages:
                if m.created_at > after:
                    yield m
        return gen()


def test_the_one_time_read_takes_authors_and_times_skips_unreadable_channels_and_adds_voice(tmp_path):
    async def run():
        cog, db, guild, users = await _wb(tmp_path, [])
        msg = lambda uid, days, bot=False: NS(author=NS(id=uid, bot=bot), created_at=T - timedelta(days=days))
        guild.text_channels = [FakeHistoryChannel([msg(1, 10), msg(1, 3), msg(2, 40), msg(9, 1, bot=True)]),
                               FakeHistoryChannel([msg(3, 2)], readable=False)]
        await db.credit_voice_minute(GUILD_ID, (T - timedelta(days=2)).strftime("%Y-%m-%d"), [(4, 1)], 300)
        await cog.seed_from_history(guild, T)
        async with db.connect() as conn:
            rows = await (await conn.execute("SELECT user_id, last_active FROM member_activity ORDER BY user_id")).fetchall()
        return [(r["user_id"], r["last_active"]) for r in rows], await db.member_activity_seeded(GUILD_ID)

    rows, seeded = asyncio.run(run())
    assert rows == [(1, (T - timedelta(days=3)).strftime("%Y-%m-%d %H:%M:%S")),
                    (4, (T - timedelta(days=2)).strftime("%Y-%m-%d") + " 23:59:59")]
    assert seeded


def test_a_busy_member_is_written_at_most_hourly_and_the_read_never_rewinds_a_newer_time(tmp_path):
    async def run():
        p = _person(1, T - timedelta(days=400))
        cog, db, guild, users = await _wb(tmp_path, [p])
        real = db.touch_member_activity
        calls = []

        async def counted(*args):
            calls.append(args[2])
            return await real(*args)

        db.touch_member_activity = counted
        for minutes in (0, 5, 30, 59, 61, 90):
            await cog.active(p, T + timedelta(minutes=minutes))
        await db.seed_member_activity(GUILD_ID, {1: (T - timedelta(days=10)).strftime("%Y-%m-%d %H:%M:%S")})
        async with db.connect() as conn:
            row = await (await conn.execute("SELECT last_active FROM member_activity")).fetchone()
        return calls, row["last_active"]

    calls, last = asyncio.run(run())
    assert calls == [T.strftime("%Y-%m-%d %H:%M:%S"), (T + timedelta(minutes=61)).strftime("%Y-%m-%d %H:%M:%S")]
    assert last == calls[-1], "an older time from the read doesn't replace a newer one"
