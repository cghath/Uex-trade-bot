"""Server analytics (the owner's plan, 2026-11-04): the handful of numbers that say whether Star
Tech Dynamics is growing, active and keeping people - net joins, the first-hello rate, 30-day
retention by where members came from, weekly active members, op attendance and lapsed members -
plus when people are on, which channels are busy or dead, and how each kind of event draws.

Pure rules, no Discord and no I/O: bot/cogs/server_analytics.py records, bot/db/database.py
stores. Only counts and "active that day" are ever kept, never what anyone wrote.

Moderation comes from the audit log (the bot needs View Audit Log): bans, kicks, timeouts,
messages a moderator or bot deleted, and AutoMod's actions, each counted once by its entry id,
and compared with the period before so a sudden rise stands out.

Join sources come from invites: Discord doesn't say which invite a member used, so the bot keeps
each invite's use count and, on a join, looks for the one that went up (match_invite). Each
recruiting place has its own permanent invite, labelled in invite_labels (seeded with
DEFAULT_INVITE_LABELS, changed with /invite-label). Matching is on the code, never the channel:
Discord hands back an existing invite when the channel and settings match, which is why each
source invite points at a different channel. An invite with no label that a member made
(Discord names its inviter) is a member referral: members make short-lived ones, often on a
voice channel, to bring friends - they're one source, with the top referrers named.
"""
from __future__ import annotations

import csv
import io
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from bot.uex.anniversaries import COMMUNITY_NAME

UTC = timezone.utc
EASTERN = ZoneInfo("America/New_York")  # the server's own clock: events are set in Eastern time

TIME_FORMAT = "%Y-%m-%d %H:%M:%S"  # SQLite's datetime('now'), UTC
DAY_FORMAT = "%Y-%m-%d"            # voice ranks' voice_daily uses the same, UTC
HOUR_FORMAT = "%Y-%m-%d %H"

PERIOD_DAYS = 30
FIRST_HELLO_DAYS = 7      # a new member who posts or joins voice within this many days said hello
RETENTION_DAYS = 30       # joined 30-60 days before: still here and active in the last 30?
WEEK_DAYS = 7
ATTENDED_MINUTES = 30     # in the event's voice channel or Ops for this long: came to the event
EVENT_OPEN_HOURS = 4      # a card nobody pressed Start on still counts this long from its start
LAPSED_STEPS = (30, 60, 90)
SHOW_ROWS = 5

UNKNOWN_INVITE = "Unknown / temporary invite"
MEMBER_REFERRAL = "Member referral"
TOP_REFERRERS = 3
NO_INVITE = "Not known (no invite matched)"

# The per-source invites, created 2026-11-04 (the owner's list). Seeded once; /invite-label
# renames these and names new ones.
DEFAULT_INVITE_LABELS = {
    "6eXmvxEXsa": "RSI org page",
    "3tvwGdGGQD": "Spectrum ad",
    "pmpVkfA6dv": "Spectrum LFG replies",
    "FQKcCTdm3C": "Reddit ad",
    "ExQ2ZYV5mQ": "Reddit LFG replies",
    "yNGCqUHXDq": "Members (shared with friends)",
    "daZgyA5mBs": "Disboard",
    "euH5uJHEVe": "Legacy recruiting link",
    "ZvrsTb4EFw": "Legacy",
    "pznqwZjP7q": "Legacy",
}
LABEL_MAX = 60

# Newcomer op times: a new member's post counts like this many minutes in voice when ranking
# 2-hour windows, and their activity picks the times once it covers this many days (before
# that, join times do).
NEWCOMER_POST_WEIGHT = 10
NEWCOMER_MIN_DAYS = 14
WINDOW_HOURS = 2

AUDIT_LOG_DAYS = 45  # how far back Discord keeps the audit log
MOD_SHOWN = ("ban", "kick", "timeout", "deleted", "automod")
MOD_LABELS = {"ban": "bans", "kick": "kicks", "timeout": "timeouts", "deleted": "deleted messages",
              "automod": "AutoMod actions"}
MOD_ONE = {"ban": "ban", "kick": "kick", "timeout": "timeout", "deleted": "deleted message", "automod": "AutoMod action"}
_AUTOMOD_RULE_CHANGES = {"automod_rule_create", "automod_rule_update", "automod_rule_delete"}
_WEEKDAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")


# -- join sources -------------------------------------------------------------

@dataclass(frozen=True)
class InviteUse:
    uses: int
    max_uses: int = 0  # 0 = no limit
    inviter_id: int | None = None  # the member who made it; None for a bot's or an unknown maker


def match_invite(before: dict[str, InviteUse], after: dict[str, InviteUse]) -> str | None:
    """The invite a member just joined with: the one whose use count went up since `before`.
    When none did, an invite that's gone and was one use from its limit (Discord deletes a
    limited invite on its last use). Only ever one answer: two candidates - two joins at once,
    say - is no answer, never a guess."""
    grew = [code for code, now in after.items() if now.uses > before.get(code, InviteUse(0)).uses]
    if grew:
        return grew[0] if len(grew) == 1 else None
    used_up = [code for code, was in before.items()
               if code not in after and was.max_uses and was.uses + 1 >= was.max_uses]
    return used_up[0] if len(used_up) == 1 else None


def event_running(state: str, starts_at: int, now: int) -> bool:
    """Whether an event card's op is on now, for attendance: from Start (on the card or in
    Discord) until it's ended - or, for one nobody started, from its start for EVENT_OPEN_HOURS."""
    return state == "started" or (state == "open" and starts_at <= now < starts_at + EVENT_OPEN_HOURS * 3600)


def source_of(code: str | None, labels: dict[str, str], inviter_id: int | None = None) -> str:
    """Where a join came from: its invite's label, else a member referral when a member made the
    invite, else unknown. A label always wins, so naming a member's invite moves its joins there."""
    if code is None:
        return NO_INVITE
    if code in labels:
        return labels[code]
    return MEMBER_REFERRAL if inviter_id is not None else UNKNOWN_INVITE


def clean_label(text: str) -> str:
    return " ".join(text.split())[:LABEL_MAX]


# -- moderation ---------------------------------------------------------------

@dataclass(frozen=True)
class ModAction:
    kind: str  # ban, unban, kick, timeout, timeout_removed, deleted, automod
    at: datetime
    count: int = 1
    moderator_id: int | None = None
    by_bot: bool = False


def moderation_kind(action: str, timeout_before: datetime | None, timeout_after: datetime | None) -> str | None:
    """What an audit log entry counts as, from its action's name (discord.py's AuditLogAction) and,
    for a member update, the timeout before and after it. None: not moderation (a nickname or
    role change, an AutoMod rule edited)."""
    if action in ("ban", "unban", "kick"):
        return action
    if action in ("message_delete", "message_bulk_delete"):
        return "deleted"
    if action.startswith("automod_") and action not in _AUTOMOD_RULE_CHANGES:
        return "automod"
    if action == "member_update":
        if timeout_after is not None:
            return "timeout"
        if timeout_before is not None:
            return "timeout_removed"
    return None


def moderation_counts(actions: list[ModAction], start: datetime, end: datetime) -> Counter:
    counts: Counter = Counter()
    for action in actions:
        if start <= action.at < end and action.kind in MOD_SHOWN:
            counts[action.kind] += action.count
    return counts


def sharp_rises(now: Counter, before: Counter) -> list[str]:
    """Kinds that at least doubled and went up by 3 or more: worth a look, not noise."""
    return [kind for kind in MOD_SHOWN if now[kind] >= before[kind] + 3 and now[kind] >= 2 * before[kind]]


def by_bots(actions: list[ModAction], start: datetime, end: datetime, names: dict[int, str]) -> list[tuple[str, Counter]]:
    """What each bot (Sapphire's Join Guard and spam filter, say) did, busiest first."""
    groups: dict[str, Counter] = defaultdict(Counter)
    for action in actions:
        if action.by_bot and start <= action.at < end and action.kind in MOD_SHOWN and action.kind != "automod":
            groups[names.get(action.moderator_id, "A bot")][action.kind] += action.count
    return sorted(groups.items(), key=lambda kv: (-sum(kv[1].values()), kv[0]))


# -- the report ---------------------------------------------------------------

@dataclass(frozen=True)
class Movement:
    user_id: int
    kind: str  # "join" or "leave"
    at: datetime
    code: str | None = None
    inviter_id: int | None = None


@dataclass(frozen=True)
class EventResult:
    name: str
    starts_at: datetime
    going: int
    interested: int | None
    attended: int
    going_came: int


@dataclass
class Report:
    title: str
    start: datetime
    end: datetime
    tracking_since: datetime
    chat_from: datetime | None
    joined: int = 0
    left: int = 0
    hello: tuple[int, int] = (0, 0)
    new_this_week: int = 0
    retention: tuple[int, int] = (0, 0)
    weekly_active: int = 0
    member_count: int = 0
    ops: list[int] = field(default_factory=list)
    lapsed: dict[int, int | None] = field(default_factory=dict)
    lapsed_known_from: dict[int, datetime] = field(default_factory=dict)
    sources: list[tuple[str, int, int, int]] = field(default_factory=list)  # label, joined, kept, cohort
    referrers: list[tuple[str, int]] = field(default_factory=list)
    peak_voice: list[tuple[str, float]] = field(default_factory=list)
    peak_chat: list[tuple[str, int]] = field(default_factory=list)
    busiest: list[tuple[str, int]] = field(default_factory=list)
    quiet: list[str] = field(default_factory=list)
    events: list[tuple[str, int, float, float, float | None, float]] = field(default_factory=list)
    moderation_from: datetime | None = None
    mod_now: Counter = field(default_factory=Counter)
    mod_before: Counter = field(default_factory=Counter)
    mod_rises: list[str] = field(default_factory=list)
    mod_bots: list[tuple[str, Counter]] = field(default_factory=list)
    timezones: list[tuple[str, int]] = field(default_factory=list)  # every member, by Onboarding timezone
    joins_all: tuple[list[int], list[int]] = ([0] * 7, [0] * 24)   # every member's join, by day and hour
    joins_recent: tuple[list[int], list[int]] = ([0] * 7, [0] * 24)  # the period's joins (still here)
    newcomer_posts: tuple[list[int], list[int]] = ([0] * 7, [0] * 24)
    newcomer_voice: tuple[list[int], list[int]] = ([0] * 7, [0] * 24)
    best_times: list[str] = field(default_factory=list)
    best_from_joins: bool = True
    newcomer_since: datetime | None = None


def _here_at(movements: list[Movement], user_id: int, when: datetime) -> bool:
    """Whether the member's last recorded move by `when` was a join."""
    last = None
    for m in movements:
        if m.user_id == user_id and m.at <= when and (last is None or m.at >= last.at):
            last = m
    return last is not None and last.kind == "join"


def _last_day(end: datetime) -> date:
    """The last whole or part day before `end` (a midnight end belongs to the day before)."""
    return (end - timedelta(seconds=1)).date()


def first_hello(joins: list[Movement], active: dict[int, set[date]], start: datetime,
                end: datetime) -> tuple[int, int]:
    """Of members who joined from `start` until 7 days before `end` (so each had the full 7
    days), how many posted or joined voice by 7 days after joining: (said hello, joined)."""
    eligible = [j for j in joins if start <= j.at < end - timedelta(days=FIRST_HELLO_DAYS)]
    hello = sum(1 for j in eligible
                if any(j.at.date() <= d <= (j.at + timedelta(days=FIRST_HELLO_DAYS)).date()
                       for d in active.get(j.user_id, ())))
    return hello, len(eligible)


def retention_cohort(joins: list[Movement], end: datetime) -> list[Movement]:
    """Members who joined 30-60 days before `end`: each has had a full 30 days."""
    return [j for j in joins
            if end - timedelta(days=2 * RETENTION_DAYS) <= j.at < end - timedelta(days=RETENTION_DAYS)]


def kept(join: Movement, movements: list[Movement], active: dict[int, set[date]], end: datetime) -> bool:
    """Still here at `end` and posted or joined voice in the 30 days before it."""
    last = _last_day(end)
    first = last - timedelta(days=RETENTION_DAYS - 1)
    return _here_at(movements, join.user_id, end) and any(first <= d <= last for d in active.get(join.user_id, ()))


def lapsed_counts(members: dict[int, datetime], last_active: dict[int, datetime], seen_from: datetime | None,
                  now: datetime) -> tuple[dict[int, int | None], dict[int, datetime]]:
    """Members silent N+ days, for each N in LAPSED_STEPS: last seen N+ days ago, or never seen
    and joined N+ days ago. An N that history doesn't reach back to yet is None, with the day it
    will be known: a member not seen since `seen_from` may have been active just before it."""
    counts: dict[int, int | None] = {}
    known_from: dict[int, datetime] = {}
    for n in LAPSED_STEPS:
        cutoff = now - timedelta(days=n)
        if seen_from is None or seen_from > cutoff:
            counts[n] = None
            if seen_from is not None:
                known_from[n] = seen_from + timedelta(days=n)
            continue
        counts[n] = sum(1 for uid, joined in members.items()
                        if joined <= cutoff and (uid not in last_active or last_active[uid] < cutoff))
    return counts, known_from


def slot_label(moment: datetime) -> str:
    local = moment.astimezone(EASTERN)
    hour = local.hour % 12 or 12
    return f"{_WEEKDAYS[local.weekday()]} {hour} {'AM' if local.hour < 12 else 'PM'}"


def _slot(moment: datetime) -> tuple[int, int]:
    local = moment.astimezone(EASTERN)
    return local.weekday(), local.hour


def hour_label(hour: int) -> str:
    return f"{hour % 12 or 12} {'AM' if hour < 12 else 'PM'}"


def window_label(weekday: int, hour: int, width: int = WINDOW_HOURS) -> str:
    """'Mon 8–10 PM', 'Sat 11 AM–1 PM', 'Fri 11 PM–1 AM'."""
    end = (hour + width) % 24
    same_half = (hour < 12) == (end < 12) and end > hour
    start = f"{hour % 12 or 12}" if same_half else hour_label(hour)
    return f"{_WEEKDAYS[weekday]} {start}–{hour_label(end)}"


def join_histogram(joined: list[datetime]) -> tuple[list[int], list[int]]:
    """Joins by day of the week (Mon first) and by hour of the day, in Eastern time."""
    days, hours = [0] * 7, [0] * 24
    for moment in joined:
        local = moment.astimezone(EASTERN)
        days[local.weekday()] += 1
        hours[local.hour] += 1
    return days, hours


def best_windows(grid: Counter, n: int = 3, width: int = WINDOW_HOURS) -> list[tuple[int, int]]:
    """The n best (weekday, start hour) windows `width` hours long by `grid`'s (weekday, hour)
    scores, Eastern, never two overlapping on the same day."""
    scored = []
    for weekday in range(7):
        for hour in range(24):
            score = sum(grid[((weekday + (hour + i) // 24) % 7, (hour + i) % 24)] for i in range(width))
            if score:
                scored.append((score, weekday, hour))
    scored.sort(key=lambda s: (-s[0], s[1], s[2]))
    chosen: list[tuple[int, int]] = []
    for _score, weekday, hour in scored:
        if any(weekday == w and abs(hour - h) < width for w, h in chosen):
            continue
        chosen.append((weekday, hour))
        if len(chosen) == n:
            break
    return chosen


def peak_times(hours: list[tuple], voice_from: datetime, end: datetime,
               ) -> tuple[list[tuple[str, float]], list[tuple[str, int]]]:
    """The busiest weekday-hours in Eastern time: by people in voice on average (voice minutes
    in that hour over how many times it came round since `voice_from`), and by posts."""
    voice: Counter = Counter()
    chat: Counter = Counter()
    label: dict[tuple[int, int], str] = {}
    for moment, messages, minutes, *_new in hours:
        slot = _slot(moment)
        label.setdefault(slot, slot_label(moment))
        voice[slot] += minutes
        chat[slot] += messages
    seen: Counter = Counter()
    hour = voice_from.replace(minute=0, second=0, microsecond=0)
    while hour < end:
        seen[_slot(hour)] += 1
        hour += timedelta(hours=1)
    by_voice = sorted(((minutes / 60 / max(seen[slot], 1), slot) for slot, minutes in voice.items() if minutes),
                      key=lambda pair: (-pair[0], pair[1]))
    by_chat = sorted(((count, slot) for slot, count in chat.items() if count), key=lambda pair: (-pair[0], pair[1]))
    return ([(label[slot], avg) for avg, slot in by_voice[:3]], [(label[slot], count) for count, slot in by_chat[:3]])


def event_kinds(events: list[EventResult]) -> list[tuple[str, int, float, float, float | None, float]]:
    """Each event name's draw: (name, times held, average attended, average going, average
    interested or None, average of the going who came), best attended first."""
    groups: dict[str, list[EventResult]] = defaultdict(list)
    for event in events:
        groups[event.name].append(event)
    rows = []
    for name, held in groups.items():
        known = [e.interested for e in held if e.interested is not None]
        rows.append((name, len(held), sum(e.attended for e in held) / len(held), sum(e.going for e in held) / len(held),
                     sum(known) / len(known) if known else None, sum(e.going_came for e in held) / len(held)))
    return sorted(rows, key=lambda row: (-row[2], row[0]))


def build_report(*, title: str, start: datetime, end: datetime, tracking_since: datetime,
                 chat_from: datetime | None, movements: list[Movement], active: dict[int, set[date]],
                 members: dict[int, datetime], last_active: dict[int, datetime], seen_from: datetime | None,
                 hours: list[tuple], channel_counts: dict[int, int],
                 channel_names: dict[int, str], ops: list[int], events: list[EventResult],
                 labels: dict[str, str], moderation: list[ModAction] = (), bot_names: dict[int, str] | None = None,
                 moderation_from: datetime | None = None, referrer_names: dict[int, str] | None = None,
                 timezones: list[tuple[str, int]] | None = None, joined_all: list[datetime] = (),
                 newcomer_since: datetime | None = None) -> Report:
    """Everything /server-stats and the monthly DM show, for `start` to `end`."""
    report = Report(title=title, start=start, end=end, tracking_since=tracking_since, chat_from=chat_from)
    joins = [m for m in movements if m.kind == "join"]
    report.joined = sum(1 for j in joins if start <= j.at < end)
    report.left = sum(1 for m in movements if m.kind == "leave" and start <= m.at < end)
    report.hello = first_hello(joins, active, start, end)
    report.new_this_week = sum(1 for j in joins if end - timedelta(days=FIRST_HELLO_DAYS) <= j.at < end)

    cohort = retention_cohort(joins, end)
    stayed = {id(j): kept(j, movements, active, end) for j in cohort}
    report.retention = (sum(stayed.values()), len(cohort))
    per_source: dict[str, list[int]] = defaultdict(lambda: [0, 0, 0])
    referrals: Counter = Counter()
    for j in joins:
        if start <= j.at < end:
            source = source_of(j.code, labels, j.inviter_id)
            per_source[source][0] += 1
            if source == MEMBER_REFERRAL:
                referrals[j.inviter_id] += 1
    for j in cohort:
        row = per_source[source_of(j.code, labels, j.inviter_id)]
        row[1] += stayed[id(j)]
        row[2] += 1
    report.sources = sorted(((name, *row) for name, row in per_source.items()), key=lambda r: (-r[1], -r[3], r[0]))
    names = referrer_names or {}
    report.referrers = [(names.get(uid, "a member no longer here"), n)
                        for uid, n in sorted(referrals.items(), key=lambda kv: (-kv[1], names.get(kv[0], "")))[:TOP_REFERRERS]]

    last = _last_day(end)
    week = last - timedelta(days=WEEK_DAYS - 1)
    report.weekly_active = sum(1 for days in active.values() if any(week <= d <= last for d in days))
    report.member_count = len(members)
    report.ops = ops
    report.lapsed, report.lapsed_known_from = lapsed_counts(members, last_active, seen_from, end)
    report.peak_voice, report.peak_chat = peak_times(hours, max(start, tracking_since), end)

    named = {cid: count for cid, count in channel_counts.items() if cid in channel_names and count}
    report.busiest = [(channel_names[cid], count)
                      for cid, count in sorted(named.items(), key=lambda kv: (-kv[1], channel_names[kv[0]]))[:SHOW_ROWS]]
    report.quiet = sorted(name for cid, name in channel_names.items() if not channel_counts.get(cid))
    report.events = event_kinds(events)

    report.moderation_from = moderation_from
    before = start - (end - start)
    report.mod_now = moderation_counts(list(moderation), start, end)
    report.mod_before = moderation_counts(list(moderation), before, start)
    report.mod_rises = sharp_rises(report.mod_now, report.mod_before) if moderation_from and moderation_from <= before else []
    report.mod_bots = by_bots(list(moderation), start, end, bot_names or {})
    report.timezones = list(timezones or [])

    report.joins_all = join_histogram(list(joined_all))
    report.joins_recent = join_histogram([t for t in joined_all if start <= t < end])
    posts, voice, grid = ([0] * 7, [0] * 24), ([0] * 7, [0] * 24), Counter()
    for moment, _messages, _minutes, *new in hours:
        new_posts, new_minutes = (new + [0, 0])[:2]
        local = moment.astimezone(EASTERN)
        posts[0][local.weekday()] += new_posts
        posts[1][local.hour] += new_posts
        voice[0][local.weekday()] += new_minutes
        voice[1][local.hour] += new_minutes
        grid[(local.weekday(), local.hour)] += new_minutes + NEWCOMER_POST_WEIGHT * new_posts
    report.newcomer_posts, report.newcomer_voice = posts, voice
    report.newcomer_since = newcomer_since
    report.best_from_joins = newcomer_since is None or end - newcomer_since < timedelta(days=NEWCOMER_MIN_DAYS)
    if report.best_from_joins:
        grid = Counter()
        for moment in joined_all:
            local = moment.astimezone(EASTERN)
            grid[(local.weekday(), local.hour)] += 1
    report.best_times = [window_label(w, h) for w, h in best_windows(grid)]
    return report


# -- the words ----------------------------------------------------------------

def _day(moment: datetime) -> str:
    local = moment.astimezone(EASTERN)
    return f"{local:%b} {local.day}"


def _pct(part: int, whole: int) -> str:
    return f"{round(100 * part / whole)}%"


def _one(value: float) -> str:
    return f"{value:.1f}".rstrip("0").rstrip(".")


def report_blocks(report: Report) -> list[str]:
    """The report as text blocks, one per section (each well under Discord's limits)."""
    partial = report.tracking_since > report.start
    since = f" (tracked since {_day(report.tracking_since)})" if partial else ""
    last_day = report.end - timedelta(seconds=1)
    head = [f"## 📊 {COMMUNITY_NAME}: {report.title}",
            f"-# {_day(report.start)} to {_day(last_day)} · counts only, never what anyone wrote"]

    net = report.joined - report.left
    head.append(f"**Net joins:** {net:+d} ({report.joined} joined, {report.left} left){since}")
    hello, joined = report.hello
    if joined:
        head.append(f"**First hello:** {hello} of {joined} new members ({_pct(hello, joined)}) posted or joined "
                    f"voice within {FIRST_HELLO_DAYS} days")
    else:
        week = f"; {report.new_this_week} still in their first week" if report.new_this_week else ""
        head.append(f"**First hello:** no new members to judge yet{week}")
    stayed, cohort = report.retention
    if cohort:
        head.append(f"**30-day retention:** {stayed} of {cohort} who joined 30-60 days ago are still here and "
                    f"active ({_pct(stayed, cohort)})")
    else:
        ready = report.tracking_since + timedelta(days=RETENTION_DAYS)
        head.append("**30-day retention:** no one has been here 30 days since joins were tracked"
                    + (f"; first numbers from {_day(ready)}" if ready > report.end else ""))
    head.append(f"**Weekly active:** {report.weekly_active} of {report.member_count} members posted or joined "
                f"voice in the last {WEEK_DAYS} days")
    if report.ops:
        head.append(f"**Op attendance:** {_one(sum(report.ops) / len(report.ops))} people on average across "
                    f"{len(report.ops)} op{'s' if len(report.ops) != 1 else ''}")
    else:
        head.append("**Op attendance:** no ops counted")
    steps = []
    for n in LAPSED_STEPS:
        count = report.lapsed.get(n)
        if count is not None:
            steps.append(f"{count} silent {n}+ days")
        elif n in report.lapsed_known_from:
            steps.append(f"{n}+ days known from {_day(report.lapsed_known_from[n])}")
    head.append("**Lapsed:** " + (" · ".join(steps) if steps else "not known yet"))
    blocks = ["\n".join(head)]

    if report.sources:
        lines = [f"### Where new members came from{since}"]
        for name, joins, stayed, cohort in report.sources[:8]:
            line = f"{name}: **{joins}** joined"
            if cohort:
                line += f" · kept {stayed} of {cohort} after 30 days"
            lines.append(line)
        if report.referrers:
            lines.append("Top referrers: " + " · ".join(f"{name} ({n})" for name, n in report.referrers))
        blocks.append("\n".join(lines))

    if report.peak_voice or report.peak_chat:
        lines = ["### When people are on (Eastern)"]
        if report.peak_voice:
            lines.append("In voice: " + " · ".join(f"{slot} ({_one(avg)} on average)" for slot, avg in report.peak_voice))
        if report.peak_chat:
            lines.append("In chat: " + " · ".join(f"{slot} ({count} posts)" for slot, count in report.peak_chat))
        blocks.append("\n".join(lines))

    if sum(report.joins_all[0]):
        blocks.append(joins_block(report))

    if report.timezones:
        total = sum(n for _name, n in report.timezones)
        blocks.append("### Members by timezone\n" + " · ".join(f"{name} {n}" for name, n in report.timezones)
                      + f"\n-# All {total} members, by their Onboarding timezone role")

    lines = ["### Channels"]
    if report.busiest:
        lines.append("Busiest: " + " · ".join(f"#{name} {count:,}" for name, count in report.busiest))
    if report.quiet:
        shown = ", ".join(f"#{name}" for name in report.quiet[:10])
        more = f" (+{len(report.quiet) - 10} more)" if len(report.quiet) > 10 else ""
        lines.append(f"No posts in this time: {shown}{more}")
    if report.chat_from and report.chat_from > report.start:
        lines.append(f"-# Chat counted from {_day(report.chat_from)}")
    if len(lines) > 1:
        blocks.append("\n".join(lines))

    if report.events:
        lines = ["### Events", f"-# Came = {ATTENDED_MINUTES}+ minutes in the event's voice channel or Ops"]
        for name, held, came, going, interested, going_came in report.events[:SHOW_ROWS]:
            extra = f" · {_one(interested)} interested" if interested is not None else ""
            lines.append(f"**{name}** ×{held}: {_one(came)} came · {_one(going)} going "
                         f"({_one(going_came)} of them came){extra}")
        blocks.append("\n".join(lines))
    blocks.append(moderation_block(report))
    return blocks


def _busiest_hours(hours: list[int], n: int = 3) -> str:
    top = sorted((h for h in range(24) if hours[h]), key=lambda h: (-hours[h], h))[:n]
    return " · ".join(f"{hour_label(h)} ({hours[h]})" for h in top)


def _by_day(days: list[int]) -> str:
    return " · ".join(f"{_WEEKDAYS[d]} {days[d]}" for d in range(7))


def joins_block(report: Report) -> str:
    """When members join, and the best times for a newcomer op (Eastern)."""
    lines = ["### Joins and newcomer op times (Eastern)",
             f"All members, by join day: {_by_day(report.joins_all[0])}",
             f"Busiest join hours: {_busiest_hours(report.joins_all[1])}"]
    if sum(report.joins_recent[0]):
        lines.append(f"This period: {_by_day(report.joins_recent[0])} · busiest {_busiest_hours(report.joins_recent[1])}")
    if report.best_times:
        lines.append("**Best times for a newcomer op:** " + " · ".join(report.best_times))
        if report.best_from_joins:
            ready = (f" (from {_day(report.newcomer_since + timedelta(days=NEWCOMER_MIN_DAYS))})"
                     if report.newcomer_since else "")
            lines.append(f"-# From join times, until new members' own posts and voice cover {NEWCOMER_MIN_DAYS} days{ready}")
        else:
            lines.append(f"-# From when members in their first 3 weeks post and are in voice, since {_day(report.newcomer_since)}")
    return "\n".join(lines)


def csv_cell(value) -> str:
    """A cell as text, with a leading = + - @ made harmless: a spreadsheet would run it as a formula."""
    text = "" if value is None else str(value)
    return "'" + text if text[:1] in ("=", "+", "-", "@") else text


def report_csv(report: Report) -> str:
    """The report as rows of (section, item, metric, value), the same keys every time, so monthly
    snapshots line up side by side."""
    rows: list[tuple] = []

    def add(section, item, metric, value):
        rows.append((section, item, metric, "" if value is None else value))

    add("period", "start", "date", report.start.astimezone(EASTERN).date().isoformat())
    add("period", "end", "date", (report.end - timedelta(seconds=1)).astimezone(EASTERN).date().isoformat())
    add("period", "tracking_since", "date", report.tracking_since.astimezone(EASTERN).date().isoformat())
    add("scorecard", "joins", "joined", report.joined)
    add("scorecard", "joins", "left", report.left)
    add("scorecard", "joins", "net", report.joined - report.left)
    add("scorecard", "first_hello", "said_hello", report.hello[0])
    add("scorecard", "first_hello", "joined", report.hello[1])
    add("scorecard", "retention_30d", "kept", report.retention[0])
    add("scorecard", "retention_30d", "cohort", report.retention[1])
    add("scorecard", "weekly_active", "members", report.weekly_active)
    add("scorecard", "members", "total", report.member_count)
    add("scorecard", "ops", "count", len(report.ops))
    add("scorecard", "ops", "average_attendance", round(sum(report.ops) / len(report.ops), 2) if report.ops else None)
    for n in LAPSED_STEPS:
        add("scorecard", "lapsed", f"{n}_days", report.lapsed.get(n))
    for name, joins, stayed, cohort in report.sources:
        add("source", name, "joined", joins)
        add("source", name, "kept_30d", stayed)
        add("source", name, "cohort_30d", cohort)
    for name, n in report.referrers:
        add("referrer", name, "joins", n)
    for name, n in report.timezones:
        add("timezone", name, "members", n)
    for section, (days, hours) in (("joins_all", report.joins_all), ("joins_period", report.joins_recent),
                                   ("newcomer_posts", report.newcomer_posts),
                                   ("newcomer_voice_minutes", report.newcomer_voice)):
        for d in range(7):
            add(section, _WEEKDAYS[d], "by_day", days[d])
        for h in range(24):
            add(section, f"{h:02d}:00", "by_hour", hours[h])
    for rank, label in enumerate(report.best_times, 1):
        add("newcomer_op", label, "best_time", f"{rank} (from {'joins' if report.best_from_joins else 'activity'})")
    for slot, avg in report.peak_voice:
        add("peak_voice", slot, "average_in_voice", round(avg, 2))
    for slot, count in report.peak_chat:
        add("peak_chat", slot, "posts", count)
    for name, count in report.busiest:
        add("channel", name, "posts", count)
    add("channel", "silent", "count", len(report.quiet))
    for name, held, came, going, interested, going_came in report.events:
        add("event", name, "held", held)
        add("event", name, "average_came", round(came, 2))
        add("event", name, "average_going", round(going, 2))
        add("event", name, "average_interested", round(interested, 2) if interested is not None else None)
    if report.moderation_from is not None:
        for kind in MOD_SHOWN:
            add("moderation", kind, "count", report.mod_now[kind])
            add("moderation", kind, "period_before", report.mod_before[kind])
    out = io.StringIO()
    writer = csv.writer(out, lineterminator="\n")
    writer.writerow(("section", "item", "metric", "value"))
    writer.writerows([csv_cell(v) for v in row] for row in rows)
    return out.getvalue()


def _mod_row(counts: Counter, *, bold: bool) -> str:
    mark = "**" if bold else ""
    text = " · ".join(f"{MOD_LABELS[kind]} {mark}{counts[kind]}{mark}" for kind in MOD_SHOWN)
    return text[0].upper() + text[1:] if bold else text


def moderation_block(report: Report) -> str:
    lines = ["### Moderation"]
    if report.moderation_from is None:
        lines.append("-# Not counted yet: the bot needs View Audit Log.")
        return "\n".join(lines)
    lines.append(_mod_row(report.mod_now, bold=True) if sum(report.mod_now.values()) else "None in this time.")
    before = report.start - (report.end - report.start)
    if report.moderation_from < report.start:
        partial = (f" (counted from {_day(report.moderation_from)})" if report.moderation_from > before else "")
        lines.append(f"-# The {(report.end - report.start).days} days before{partial}: "
                     + _mod_row(report.mod_before, bold=False))
    if report.mod_rises:
        lines.append("⚠️ **Up sharply:** " + ", ".join(
            f"{MOD_LABELS[k]} ({report.mod_before[k]} → {report.mod_now[k]})" for k in report.mod_rises))
    if report.mod_bots:
        lines.append("By bots: " + " · ".join(
            f"{name} ({', '.join(f'{n} {MOD_ONE[k] if n == 1 else MOD_LABELS[k]}' for k, n in counts.most_common())})"
            for name, counts in report.mod_bots[:3]))
    if report.mod_now["deleted"]:
        lines.append("-# Deleted messages: removed by a moderator or bot (not by their author); Discord merges "
                     "quick runs of deletes, so it's at least this many.")
    return "\n".join(lines)


def previous_month(now: datetime) -> tuple[datetime, datetime, str, str]:
    """The calendar month before `now`'s, in Eastern time: (start, end, "October 2026", "2026-10")."""
    local = now.astimezone(EASTERN)
    end = local.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    start = (end - timedelta(days=1)).replace(day=1)
    return start.astimezone(UTC), end.astimezone(UTC), f"{start:%B %Y}", f"{start:%Y-%m}"
