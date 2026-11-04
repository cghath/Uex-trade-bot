"""New members (the owner's ask, 2026-11-04): a temporary "🌱 New Member" role for each member's
first NEW_MEMBER_DAYS in Star Tech Dynamics, and /new-members - a private list of them for the
owner and moderators, so someone reaches out before they drift off. Pure rules and wording; the
cog is bot/cogs/new_members.py.

How long someone has been here comes from Discord's own join date (member.joined_at), so a
restart never loses it and a member who leaves and rejoins starts again. This is about time in
the server, not account age: Sapphire's new-account role is a different thing, and a member can
have both.

Nothing anyone wrote or said is read: activity is which days they posted or joined voice (server
analytics' daily activity log), their last post or voice join (welcome-back's member_activity),
and voice ranks' voice minutes - the same counting as ranks: 2+ people in the channel, not
deafened, never the AFK channel, up to 5 hours a day.

Days are calendar days in Eastern time, the server's clock: joined yesterday is 1 day, whatever
the hour. The activity log keeps UTC days, so "active X of Y days" counts its days from the UTC
day of the join, never more than Y.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

from bot.uex.server_analytics import EASTERN

NEW_MEMBER_ROLE = "🌱 New Member"
NEW_MEMBER_DAYS = 21       # the role comes off after this many days
MAX_DAYS = 90              # /new-members days: at most this far back
PAGE_SIZE = 8
PICK_GROUPS = ("activity", "timezone", "experience")  # the Onboarding picks shown for each member

# The flags' thresholds (the owner's, 2026-11-04): change them here.
HELLO_AFTER_DAYS = 3        # 🔴 no posts or voice after this many days
DRIFT_AFTER_DAYS = 5        # 🟡 active at least once, then quiet this many days
SETTLED_SHARE = 0.5         # 🟢 active on at least this share of their days...
SETTLED_VOICE_MINUTES = 60  # ...or this much voice since joining
SETTLED_MIN_DAYS = 3        # the share rule waits this many days: 1 post on day 0 is "1 of 1 day"

NEEDS_HELLO, DRIFTING, SETTLING = "needs-hello", "drifting", "settling"
FLAGS = {NEEDS_HELLO: ("🔴", "Needs a hello"), DRIFTING: ("🟡", "Drifting"), SETTLING: ("🟢", "Settling in")}


def is_new(joined_at: datetime | None, now: datetime, days: int = NEW_MEMBER_DAYS) -> bool:
    return joined_at is not None and now - joined_at < timedelta(days=days)


def days_since(moment: datetime, now: datetime) -> int:
    """Calendar days in Eastern time from `moment` to `now`: yesterday is 1, today 0."""
    return max(0, (now.astimezone(EASTERN).date() - moment.astimezone(EASTERN).date()).days)


@dataclass
class NewMember:
    user_id: int
    joined_at: datetime
    source: str | None = None            # None: joined before join sources were tracked
    picks: list[str] = field(default_factory=list)
    first_active: date | None = None      # first day they posted or joined voice since joining
    active_days: int = 0                  # how many days since joining they posted or joined voice
    voice_minutes: int = 0                # voice ranks' minutes since joining
    last_active: datetime | None = None   # their last post or voice join
    reached_by: str | None = None
    reached_at: datetime | None = None


def days_here(member: NewMember, now: datetime) -> int:
    """The days "active X of Y days" counts: the join day through today."""
    return days_since(member.joined_at, now) + 1


def quiet_days(member: NewMember, now: datetime) -> int | None:
    """Days since their last post or voice join; None if they've never been active."""
    if member.first_active is None:
        return None
    last = member.last_active
    if last is None:  # only the day is known
        last = datetime.combine(member.first_active, datetime.min.time(), tzinfo=EASTERN)
    return days_since(last, now)


def flag(member: NewMember, now: datetime) -> str | None:
    """🔴 needs a hello, 🟡 drifting, 🟢 settling in, or nothing. The first two only while nobody
    has reached out - for drifting, nobody since they went quiet."""
    if member.first_active is None:
        if member.reached_by is None and days_since(member.joined_at, now) >= HELLO_AFTER_DAYS:
            return NEEDS_HELLO
        return None
    quiet = quiet_days(member, now)
    reached_since = (member.reached_at is not None and member.last_active is not None
                     and member.reached_at >= member.last_active)
    if quiet is not None and quiet >= DRIFT_AFTER_DAYS and not reached_since:
        return DRIFTING
    here = days_here(member, now)
    share = days_since(member.joined_at, now) >= SETTLED_MIN_DAYS and min(member.active_days, here) >= SETTLED_SHARE * here
    if share or member.voice_minutes >= SETTLED_VOICE_MINUTES:
        return SETTLING
    return None


def needs_hello(member: NewMember, now: datetime) -> bool:
    return flag(member, now) == NEEDS_HELLO


def _day(moment: datetime) -> str:
    local = moment.astimezone(EASTERN)
    return f"{local:%b} {local.day}"


def _plural(n: int, word: str) -> str:
    return f"{n} {word}{'s' if n != 1 else ''}"


def reason(member: NewMember, now: datetime) -> str:
    """Why a member is flagged, in a few words ("" for settling in)."""
    kind = flag(member, now)
    if kind == NEEDS_HELLO:
        return f"no posts or voice yet, {_plural(days_since(member.joined_at, now), 'day')} in"
    if kind == DRIFTING:
        return f"quiet {_plural(quiet_days(member, now), 'day')}"
    return ""


def activity_line(member: NewMember, now: datetime) -> str:
    here = days_here(member, now)
    return f"🎙️ {member.voice_minutes:,} min voice · 📅 active {min(member.active_days, here)} of {_plural(here, 'day')}"


def member_text(member: NewMember, now: datetime) -> str:
    """One member's lines in /new-members."""
    days = days_since(member.joined_at, now)
    head = f"<@{member.user_id}> · joined <t:{int(member.joined_at.timestamp())}:d> ({_plural(days, 'day')})"
    head += f" · {member.source}" if member.source else " · before join sources were tracked"
    lines = [head, "-# " + (" · ".join(member.picks) if member.picks else "No Onboarding picks"),
             activity_line(member, now)]
    status = []
    kind = flag(member, now)
    if kind is not None:
        emoji, name = FLAGS[kind]
        why = reason(member, now)
        status.append(f"{emoji} **{name}**" + (f": {why}" if why else ""))
    if member.reached_by:
        status.append(f"✅ Reached out: {member.reached_by}, {_day(member.reached_at)}")
    if status:
        lines.append(" · ".join(status))
    return "\n".join(lines)


def flag_counts(members: list[NewMember], now: datetime) -> dict[str, int]:
    counts = {kind: 0 for kind in FLAGS}
    for member in members:
        kind = flag(member, now)
        if kind is not None:
            counts[kind] += 1
    return counts


def _flag_parts(counts: dict[str, int], *, bold: bool) -> list[str]:
    mark = "**" if bold else ""
    parts = []
    if counts[NEEDS_HELLO]:
        parts.append(f"{mark}{counts[NEEDS_HELLO]} need{'s' if counts[NEEDS_HELLO] == 1 else ''} a hello{mark}")
    if counts[DRIFTING]:
        parts.append(f"{mark}{counts[DRIFTING]} drifting{mark}")
    return parts


def summary_text(members: list[NewMember], now: datetime, days: int, showing: str | None = None) -> str:
    """The header: how many joined and how many have each flag (all of them, whatever's shown)."""
    counts = flag_counts(members, now)
    parts = [f"{len(members)} joined", *_flag_parts(counts, bold=True)]
    if counts[SETTLING]:
        parts.append(f"{counts[SETTLING]} settling in")
    text = f"## 🌱 New members: last {days} days\n" + " · ".join(parts)
    if showing:
        emoji, name = FLAGS[showing]
        text += f"\nShowing {emoji} {name} only"
    return text + ("\n-# Newest first. Join dates, sources, Onboarding picks, active days and voice time; "
                   "never what anyone wrote or said.")


def weekly_text(members: list[NewMember], now: datetime) -> str | None:
    """The owner's weekly note: new members who need a hello or are drifting, each with why, or
    None when there are none."""
    flagged = [m for m in members if flag(m, now) in (NEEDS_HELLO, DRIFTING)]
    if not flagged:
        return None
    lines = [f"🌱 **New members this week: {' · '.join(_flag_parts(flag_counts(members, now), bold=False))}**"]
    for member in flagged[:20]:
        emoji, _name = FLAGS[flag(member, now)]
        lines.append(f"{emoji} <@{member.user_id}>: {reason(member, now)}" + (f" · {member.source}" if member.source else ""))
    if len(flagged) > 20:
        lines.append(f"- and {len(flagged) - 20} more")
    lines.append("-# `/new-members` has the full list and marks who's reached out.")
    return "\n".join(lines)


def week_key(now: datetime) -> str:
    year, week, _ = now.astimezone(EASTERN).isocalendar()
    return f"{year}-W{week:02d}"
