"""Server analytics (bot/uex/server_analytics.py, bot/cogs/server_analytics.py).

Guarantees under test: a join is matched to the one invite whose use count went up (or a limited
invite used up and gone), never guessed between two; labels are seeded once and never overwrite
the owner's own; first hello, 30-day retention, weekly active and lapsed count what they say and
say "not known yet" instead of guessing; peak times are in Eastern time; event attendance needs
30 minutes in the event's voice channel or Ops while it runs; the one-time history read keeps
only authors, times and channels and is recorded once; the monthly DM goes once, after 9 AM
Eastern on the 1st, and never for a month before tracking began. Every clock here is pinned.
"""
from __future__ import annotations

import asyncio
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock

from cryptography.fernet import Fernet

from bot.cogs.anniversaries import ANNIVERSARY_CHANNEL_ID
from bot.cogs.server_analytics import ServerAnalytics, invite_code, invite_list_text
from bot.cogs.voice_ranks import OPS_CATEGORY_ID
from bot.db.database import Database
from bot.discord_ui import LAYOUT_TEXT_LIMIT
from bot.uex.server_analytics import (
    DEFAULT_INVITE_LABELS, NO_INVITE, UNKNOWN_INVITE, EventResult, InviteUse, Movement, build_report, event_kinds,
    event_running, first_hello, kept, lapsed_counts, match_invite, peak_times, previous_month, report_blocks,
    retention_cohort, slot_label,
)

UTC = timezone.utc
GUILD_ID = 1132378045232197694
OWNER = 999
T0 = datetime(2026, 11, 4, 15, 0, tzinfo=UTC)  # a Wednesday, 10 AM Eastern
RSI, SPECTRUM = "6eXmvxEXsa", "3tvwGdGGQD"


# -- invites ----------------------------------------------------------------------

def test_a_join_matches_the_one_invite_whose_uses_went_up_never_a_guess_between_two():
    before = {RSI: InviteUse(3), SPECTRUM: InviteUse(0)}
    assert match_invite(before, {RSI: InviteUse(4), SPECTRUM: InviteUse(0)}) == RSI
    assert match_invite(before, {RSI: InviteUse(4), SPECTRUM: InviteUse(1)}) is None, "two joins at once"
    assert match_invite(before, {RSI: InviteUse(3), SPECTRUM: InviteUse(0), "new1": InviteUse(1)}) == "new1"
    assert match_invite(before, dict(before)) is None, "vanity link, Discovery, or an invite we never saw"


def test_a_limited_invite_used_up_and_deleted_is_still_matched_but_a_deleted_unlimited_one_is_not():
    before = {"once": InviteUse(0, 1), RSI: InviteUse(3)}
    assert match_invite(before, {RSI: InviteUse(3)}) == "once"
    assert match_invite({"gone": InviteUse(5), RSI: InviteUse(3)}, {RSI: InviteUse(3)}) is None
    assert match_invite({"a": InviteUse(0, 1), "b": InviteUse(4, 5)}, {}) is None, "two could have been it"


def test_invite_codes_come_from_links_or_bare_codes():
    assert invite_code("https://discord.gg/6eXmvxEXsa") == RSI
    assert invite_code(" discord.com/invite/3tvwGdGGQD/ ") == SPECTRUM
    assert invite_code("FQKcCTdm3C") == "FQKcCTdm3C"


# -- the rules ----------------------------------------------------------------------

def test_first_hello_counts_only_members_who_have_had_their_full_seven_days():
    joins = [Movement(1, "join", T0), Movement(2, "join", T0), Movement(3, "join", T0 + timedelta(days=5))]
    active = {1: {date(2026, 11, 11)}, 2: {date(2026, 11, 12)}, 3: {date(2026, 11, 9)}}
    # 1 said hello on day 7 (counts), 2 on day 8 (too late), 3 hasn't had 7 days yet
    assert first_hello(joins, active, T0 - timedelta(days=1), T0 + timedelta(days=9)) == (1, 2)


def test_retention_is_still_here_and_active_in_the_last_thirty_days_for_joins_thirty_to_sixty_days_back():
    end = T0 + timedelta(days=45)
    stays = Movement(1, "join", T0)
    quiet = Movement(2, "join", T0)
    gone = Movement(3, "join", T0)
    too_new = Movement(4, "join", end - timedelta(days=10))
    moves = [stays, quiet, gone, too_new, Movement(3, "leave", T0 + timedelta(days=40))]
    active = {1: {(end - timedelta(days=3)).date()}, 2: {T0.date()}, 3: {(end - timedelta(days=6)).date()}}
    cohort = retention_cohort([m for m in moves if m.kind == "join"], end)
    assert cohort == [stays, quiet, gone]
    assert [kept(j, moves, active, end) for j in cohort] == [True, False, False]


def test_lapsed_counts_wait_until_history_reaches_back_far_enough():
    now = T0
    members = {1: T0 - timedelta(days=400), 2: T0 - timedelta(days=400), 3: T0 - timedelta(days=10)}
    last = {1: T0 - timedelta(days=45)}  # 2 never seen, 3 too new to be lapsed
    counts, known = lapsed_counts(members, last, T0 - timedelta(days=65), now)
    assert counts == {30: 2, 60: 1, 90: None}
    assert known == {90: T0 + timedelta(days=25)}
    assert lapsed_counts(members, last, None, now) == ({30: None, 60: None, 90: None}, {})


def test_peak_times_are_in_eastern_time_and_averaged_over_how_often_the_hour_came_round():
    assert slot_label(T0) == "Wed 10 AM"
    assert slot_label(datetime(2026, 7, 1, 0, 30, tzinfo=UTC)) == "Tue 8 PM", "summer time"
    hours = [(T0, 4, 120), (T0 + timedelta(days=7), 2, 0), (T0 + timedelta(hours=10), 9, 30)]
    voice, chat = peak_times(hours, T0, T0 + timedelta(days=14))
    assert voice == [("Wed 10 AM", 1.0), ("Wed 8 PM", 0.25)]  # 120 min over 2 Wednesdays; 30 over 2
    assert chat == [("Wed 8 PM", 9), ("Wed 10 AM", 6)]


def test_an_event_counts_from_start_until_its_over_or_four_hours_if_nobody_starts_it():
    start = int(T0.timestamp())
    assert event_running("started", start + 99999, start)
    assert not event_running("open", start, start - 60)
    assert event_running("open", start, start + 3 * 3600)
    assert not event_running("open", start, start + 4 * 3600)
    assert not event_running("ended", start, start + 60)


def test_event_kinds_rank_by_how_many_came():
    rows = event_kinds([EventResult("Mining Mondays", T0, 8, 12, 6, 5), EventResult("Mining Mondays", T0, 6, None, 4, 3),
                        EventResult("Contested Tuesdays", T0, 9, 9, 7, 7)])
    assert rows == [("Contested Tuesdays", 1, 7.0, 9.0, 9.0, 7.0), ("Mining Mondays", 2, 5.0, 7.0, 12.0, 4.0)]


def test_previous_month_is_the_eastern_calendar_month_across_a_year_and_summer_time():
    start, end, title, key = previous_month(datetime(2026, 12, 1, 15, tzinfo=UTC))
    assert (start, end, title, key) == (datetime(2026, 11, 1, 4, tzinfo=UTC), datetime(2026, 12, 1, 5, tzinfo=UTC),
                                        "November 2026", "2026-11")
    assert previous_month(datetime(2027, 1, 1, 15, tzinfo=UTC))[2:] == ("December 2026", "2026-12")


def test_a_report_with_little_data_says_so_rather_than_showing_zeros_as_results():
    report = build_report(title="last 30 days", start=T0 - timedelta(days=30), end=T0 + timedelta(days=1),
                          tracking_since=T0, chat_from=T0 - timedelta(days=30), movements=[], active={},
                          members={1: T0 - timedelta(days=100)}, last_active={}, seen_from=None, hours=[],
                          channel_counts={}, channel_names={5: "general"}, ops=[], events=[], labels={})
    text = "\n".join(report_blocks(report))
    assert "**Net joins:** +0 (0 joined, 0 left) (tracked since Nov 4)" in text
    assert "**First hello:** no new members to judge yet" in text
    assert "first numbers from Dec 4" in text
    assert "**Op attendance:** no ops counted" in text
    assert "**Lapsed:** not known yet" in text
    assert "No posts in this time: #general" in text
    assert "never what anyone wrote" in text


# -- fakes --------------------------------------------------------------------------

class FakeChannel:
    def __init__(self, cid, name, *, members=(), category_id=None, history=(), readable=True):
        self.id, self.name, self.members, self.category_id = cid, name, list(members), category_id
        self._history, self._readable = list(history), readable

    def permissions_for(self, _me):
        return NS(view_channel=self._readable, read_message_history=self._readable)

    async def history(self, *, after=None, before=None, limit=None):
        for message in self._history:
            if (after is None or message.created_at > after) and (before is None or message.created_at < before):
                yield message

    async def archived_threads(self, *, limit=None):
        return
        yield


class FakeGuild:
    def __init__(self, *, text=(), voice=(), members=(), invites=()):
        self.id = GUILD_ID
        self.text_channels, self.voice_channels = list(text), list(voice)
        self.stage_channels, self.forums, self.threads = [], [], []
        self.afk_channel, self.me = None, NS()
        self.members = list(members)
        self.invite_list = list(invites)
        self.user_count = 7

    async def invites(self):
        return list(self.invite_list)

    async def fetch_scheduled_event(self, event_id, *, with_counts=True):
        return NS(id=event_id, user_count=self.user_count)


def _member(uid, *, joined=T0 - timedelta(days=300), bot=False):
    return NS(id=uid, bot=bot, joined_at=joined, voice=NS(self_deaf=False, deaf=False))


def _invite(code, uses, max_uses=0, channel="welcome"):
    return NS(code=code, uses=uses, max_uses=max_uses, channel=NS(name=channel))


class FakeUser:
    def __init__(self):
        self.sent = []

    async def send(self, content=None, **kwargs):
        self.sent.append((content, kwargs))


async def _make(tmp_path, guild):
    db = Database(tmp_path / "db.sqlite3", Fernet(Fernet.generate_key()))
    await db.init()
    home = NS(id=ANNIVERSARY_CHANNEL_ID, guild=guild)
    owner = FakeUser()
    bot = NS(db=db, get_channel=lambda cid: home if cid == ANNIVERSARY_CHANNEL_ID else None, is_ready=lambda: True,
             is_owner=AsyncMock(return_value=True), get_user=lambda uid: owner if uid == OWNER else None,
             application_info=AsyncMock(return_value=NS(team=None, owner=NS(id=OWNER))))
    cog = ServerAnalytics(bot, start_loop=False)
    clock = [T0]
    cog.now = lambda: clock[0]
    return cog, db, clock, owner


def _message(author, channel, at):
    return NS(guild=NS(id=GUILD_ID), author=author, channel=channel, created_at=at)


# -- the database -----------------------------------------------------------------

def test_invite_labels_are_seeded_once_and_never_overwrite_the_owners_names(tmp_path):
    async def run():
        db = Database(tmp_path / "db.sqlite3", Fernet(Fernet.generate_key()))
        await db.init()
        first = await db.analytics_start(GUILD_ID, "2026-11-04 15:00:00")
        await db.set_invite_label(RSI, "RSI org page (new)")
        again = await db.analytics_start(GUILD_ID, "2026-12-01 00:00:00")
        return first, again, await db.get_invite_labels(), [await db.claim_analytics_report(GUILD_ID, m)
                                                             for m in ("2026-11", "2026-11", "2026-10", "2026-12")]

    first, again, labels, claims = asyncio.run(run())
    assert first["tracking_since"] == again["tracking_since"] == "2026-11-04 15:00:00"
    assert labels[RSI] == "RSI org page (new)" and labels[SPECTRUM] == "Spectrum ad"
    assert set(labels) == set(DEFAULT_INVITE_LABELS)
    assert claims == [True, False, False, True]


# -- the cog ------------------------------------------------------------------------

def test_joins_posts_voice_and_an_event_add_up_to_the_report(tmp_path):
    async def run():
        old, new, quick, gone = _member(20), _member(10, joined=T0), _member(11, joined=T0), _member(12, joined=T0)
        general = FakeChannel(5, "general")
        mining = FakeChannel(6, "mining")
        ops = FakeChannel(7, "Ops 1", members=[old, new], category_id=OPS_CATEGORY_ID)
        lobby = FakeChannel(8, "Lobby", members=[quick])
        guild = FakeGuild(text=[general, mining], voice=[ops, lobby], members=[old, new, quick],
                          invites=[_invite(RSI, 3), _invite(SPECTRUM, 0), _invite("temp1", 0, 1)])
        for m in (old, new, quick, gone):
            m.guild = guild
        cog, db, clock, _owner = await _make(tmp_path, guild)
        await cog.minute(T0)  # tracking starts, invites read

        guild.invite_list = [_invite(RSI, 4), _invite(SPECTRUM, 0), _invite("temp1", 0, 1)]
        await cog.on_member_join(new)
        guild.invite_list = [_invite(RSI, 4), _invite(SPECTRUM, 0)]
        await cog.on_member_join(quick)
        await cog.on_member_join(gone)  # nothing went up
        await cog.on_raw_member_remove(NS(guild_id=GUILD_ID, user=NS(id=12, bot=False)))

        await cog.on_message(_message(new, general, T0 + timedelta(minutes=5)))
        await cog.on_message(_message(new, general, T0 + timedelta(minutes=6)))
        await cog.on_message(_message(NS(id=77, bot=True), general, T0 + timedelta(minutes=6)))

        card = await db.create_event_card(guild_id=GUILD_ID, event_id=55, starts_at=int(T0.timestamp()) + 60,
                                          channel_id=1, repeats=None, name="Mining Mondays", voice_channel_id=None)
        await db.save_event_signup(card, 10, status="going", role=None, queued=1.0)
        await db.save_event_signup(card, 11, status="going", role=None, queued=2.0)
        for minute in range(1, 31):
            clock[0] = T0 + timedelta(minutes=minute)
            await cog.minute(clock[0])

        async with db.connect() as conn:
            days = await (await conn.execute("SELECT user_id, day FROM member_active_days")).fetchall()
            moves = await (await conn.execute("SELECT user_id, kind, invite_code FROM member_movements ORDER BY id")).fetchall()
            hour = await (await conn.execute("SELECT messages, voice_minutes FROM activity_hours")).fetchall()
        end = T0 + timedelta(days=8)
        blocks = await cog.report(guild, T0 - timedelta(days=1), end, "last 30 days")
        return ([tuple(r) for r in days], [tuple(r) for r in moves], [tuple(r) for r in hour], blocks)

    days, moves, hour, blocks = asyncio.run(run())
    assert days == [(10, "2026-11-04")], "one row a day, not one per post; bots never"
    assert moves == [(10, "join", RSI), (11, "join", "temp1"), (12, "join", None), (12, "leave", None)]
    assert hour == [(2, 3 * 31)], "2 posts; 3 people in voice for 31 minutes"
    text = "\n".join(blocks)
    assert "**Net joins:** +2 (3 joined, 1 left)" in text
    assert "**First hello:** 1 of 3 new members (33%)" in text
    assert "RSI org page: **1** joined" in text
    assert f"{UNKNOWN_INVITE}: **1** joined" in text and f"{NO_INVITE}: **1** joined" in text
    assert "Busiest: #general 2" in text and "No posts in this time: #mining" in text
    assert "In voice: Wed 10 AM" in text
    assert "**Mining Mondays** ×1: 2 came · 2 going (1 of them came) · 7 interested" in text
    assert len("".join(blocks)) < LAYOUT_TEXT_LIMIT


def test_the_history_read_keeps_authors_times_and_channels_only_and_is_recorded_once(tmp_path):
    async def run():
        a, b = _member(1), _member(2)
        before = T0 - timedelta(days=2)
        general = FakeChannel(5, "general", history=[
            _message(a, None, before), _message(a, None, before + timedelta(minutes=1)),
            _message(NS(id=3, bot=True), None, before), _message(b, None, T0 - timedelta(days=40)),
            _message(b, None, T0 + timedelta(minutes=1))])  # too old, and after tracking began
        secret = FakeChannel(6, "staff", history=[_message(b, None, before)], readable=False)
        guild = FakeGuild(text=[general, secret], members=[a, b])
        cog, db, _clock, _owner = await _make(tmp_path, guild)
        state = await cog._start(guild)
        await cog.read_chat_history(guild, T0)
        await cog.read_chat_history(guild, T0)  # a second read is not counted twice
        async with db.connect() as conn:
            days = await (await conn.execute("SELECT user_id, day FROM member_active_days")).fetchall()
            hours = await (await conn.execute("SELECT hour, messages FROM activity_hours")).fetchall()
            chans = await (await conn.execute("SELECT channel_id, day, messages FROM channel_days")).fetchall()
            read = await (await conn.execute("SELECT history_read_at FROM analytics_state")).fetchone()
        return state, [tuple(r) for r in days], [tuple(r) for r in hours], [tuple(r) for r in chans], read[0]

    state, days, hours, chans, read = asyncio.run(run())
    assert state["tracking_since"] == "2026-11-04 15:00:00"
    assert days == [(1, "2026-11-02")]
    assert hours == [("2026-11-02 15", 2)]
    assert chans == [(5, "2026-11-02", 2)]
    assert read is not None


def test_the_monthly_dm_goes_once_after_nine_eastern_and_never_for_a_month_before_tracking(tmp_path):
    async def run():
        guild = FakeGuild(text=[FakeChannel(5, "general")], members=[_member(1)])
        cog, _db, _clock, owner = await _make(tmp_path, guild)
        await cog._start(guild)  # tracking since 2026-11-04
        october = await cog.send_monthly(datetime(2026, 11, 1, 18, tzinfo=UTC))
        early = await cog.send_monthly(datetime(2026, 12, 1, 12, tzinfo=UTC))  # 7 AM Eastern
        sent = await cog.send_monthly(datetime(2026, 12, 1, 15, tzinfo=UTC))
        again = await cog.send_monthly(datetime(2026, 12, 1, 16, tzinfo=UTC))
        return october, early, sent, again, owner.sent

    october, early, sent, again, dms = asyncio.run(run())
    assert (october, early, sent, again) == (False, False, True, False)
    assert len(dms) == 1
    text = "\n".join(item.content for item in dms[0][1]["view"].walk_children() if hasattr(item, "content"))
    assert "Star Tech Dynamics: November 2026" in text and "Nov 1 to Nov 30" in text


def test_invite_label_names_an_invite_and_lists_them_for_the_owner_only(tmp_path):
    async def run():
        guild = FakeGuild(invites=[_invite(RSI, 4, channel="org-page"), _invite("abc123", 2, 10)])
        cog, db, _clock, _owner = await _make(tmp_path, guild)
        await cog._start(guild)
        sent = []
        interaction = NS(user=NS(id=OWNER), response=NS(defer=AsyncMock(), send_message=AsyncMock()),
                         followup=NS(send=AsyncMock(side_effect=lambda text, **kw: sent.append(text))))
        await cog.invite_label.callback(cog, interaction, "https://discord.gg/abc123", "  Discord   Discovery  ")
        await cog.invite_label.callback(cog, interaction, "zzz999", "Typo")
        await cog.invite_label.callback(cog, interaction, None, None)
        cog.bot.is_owner = AsyncMock(return_value=False)
        await cog.invite_label.callback(cog, interaction, RSI, "Hijack")
        return sent, interaction.response.send_message.await_args, await db.get_invite_labels()

    sent, refused, labels = asyncio.run(run())
    assert sent[0] == "Members who join through `abc123` now show as **Discord Discovery**."
    assert "No invite on the server has that code right now" in sent[1]
    assert "`6eXmvxEXsa` · **RSI org page** · 4 uses · #org-page" in sent[2]
    assert "`abc123` · **Discord Discovery** · 2/10 uses" in sent[2]
    assert "This command is for the bot's owner." in refused.args[0]
    assert labels[RSI] == "RSI org page"


def test_without_manage_server_the_invite_list_says_why():
    text = invite_list_text(None, {RSI: "RSI org page"})
    assert "the bot needs Manage Server" in text and "`6eXmvxEXsa` · RSI org page" in text
