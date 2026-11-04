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

import csv
import io
import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

from bot.uex.role_picker import GROUPS
from bot.uex.server_analytics import EASTERN, csv_cell

NEW_MEMBER_ROLE = "🌱 New Member"
NEW_MEMBER_DAYS = 21       # the role comes off after this many days
MAX_DAYS = 90              # /new-members days: at most this far back
PAGE_SIZE = 8
PICK_GROUPS = ("activity", "timezone", "experience")  # the Onboarding picks shown for each member
WEEKLY_TEXT_LIMIT = 1900    # under a DM's 2,000 characters
EMPTY = {"needs-hello": "Nobody needs a hello right now.", "drifting": "Nobody is drifting right now.",
         "settling": "Nobody is settling in right now."}

# The flags' thresholds (the owner's, 2026-11-04): change them here.
HELLO_AFTER_DAYS = 3        # 🔴 no posts or voice after this many days
DRIFT_AFTER_DAYS = 5        # 🟡 active at least once, then quiet this many days
SETTLED_SHARE = 0.5         # 🟢 active on at least this share of their days...
SETTLED_VOICE_MINUTES = 60  # ...or this much voice since joining
SETTLED_MIN_DAYS = 3        # the share rule waits this many days: 1 post on day 0 is "1 of 1 day"

NEEDS_HELLO, DRIFTING, SETTLING = "needs-hello", "drifting", "settling"
FLAGS = {NEEDS_HELLO: ("🔴", "Needs a hello"), DRIFTING: ("🟡", "Drifting"), SETTLING: ("🟢", "Settling in")}

_GROUPS = {group.key: group for group in GROUPS}
TIMEZONES = _GROUPS["timezone"].roles       # the Onboarding timezone roles, in panel order
ACTIVITIES = _GROUPS["activity"].roles
EXPERIENCES = _GROUPS["experience"].roles
NOT_PICKED = "Not picked"

# Onboarding (Timezone and Experience required) went live 2026-10-30; Discord flags each member
# who started it and who completed it (member.flags).
ONBOARDING_LIVE = datetime(2026, 10, 30, tzinfo=EASTERN)
COMPLETED, DIDNT_FINISH, NOT_STARTED, BEFORE_ONBOARDING = "completed", "didnt_finish", "not_started", "before_onboarding"
JOINED_BEFORE_LABEL = "Joined before Onboarding"
DIDNT_FINISH_LABEL = "Didn't finish Onboarding"
UNFINISHED_HELLO_DAYS = 1   # 🔴 for a member who didn't finish Onboarding and hasn't posted: after a day


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
    timezone: str | None = None
    activities: list[str] = field(default_factory=list)
    experience: str | None = None
    inviter: str | None = None            # who made the invite, for a member referral
    username: str = ""
    display_name: str = ""
    still_here: bool = True               # False: joined in the window and has left (the CSV only)
    onboarding: str | None = None         # COMPLETED, DIDNT_FINISH, NOT_STARTED, BEFORE_ONBOARDING; None unknown


def onboarding_status(joined_at: datetime | None, started: bool, completed: bool) -> str:
    """Where a member is with Onboarding, from Discord's member flags."""
    if completed:
        return COMPLETED
    if joined_at is not None and joined_at < ONBOARDING_LIVE:
        return BEFORE_ONBOARDING
    return DIDNT_FINISH if started else NOT_STARTED


def unfinished(member: NewMember) -> bool:
    """Joined since Onboarding went live and hasn't completed it (started or not)."""
    return member.onboarding in (DIDNT_FINISH, NOT_STARTED)


def zone_bucket(timezone: str | None, onboarding: str | None) -> str:
    """Where a member counts in a timezone breakdown: their timezone role, else why they have none."""
    if timezone:
        return timezone
    if onboarding == BEFORE_ONBOARDING:
        return JOINED_BEFORE_LABEL
    if onboarding in (DIDNT_FINISH, NOT_STARTED):
        return DIDNT_FINISH_LABEL
    return NOT_PICKED


def member_picks(held: set[int], picker: dict[str, int]) -> tuple[str | None, list[str], str | None]:
    """A member's Onboarding picks from the role ids they hold: (timezone, activities, experience)."""
    def picked(names):
        return [name for name in names if picker.get(name) in held]

    zones, levels = picked(TIMEZONES), picked(EXPERIENCES)
    return (zones[0] if zones else None), picked(ACTIVITIES), (levels[0] if levels else None)


ZONE_ORDER = (*TIMEZONES, JOINED_BEFORE_LABEL, DIDNT_FINISH_LABEL, NOT_PICKED)


def timezone_counts(zones: list[str | None]) -> list[tuple[str, int]]:
    """How many in each zone_bucket, in panel order, then those with none and why."""
    counts = Counter(zone or NOT_PICKED for zone in zones)
    return [(name, counts[name]) for name in ZONE_ORDER if counts[name]]


def _md(text: str) -> str:
    """A name as plain text in Discord markdown."""
    return re.sub(r"([\\*_~|`>\[\]()<])", r"\\\1", text)  # [..](..) would be a link, <@..> a mention


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
        wait = UNFINISHED_HELLO_DAYS if unfinished(member) else HELLO_AFTER_DAYS
        if member.reached_by is None and days_since(member.joined_at, now) >= wait:
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
        if unfinished(member):
            return f"didn't finish Onboarding, {_plural(days_since(member.joined_at, now), 'day')} in"
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
    if member.source:
        head += f" · {member.source}" + (f" ({_md(member.inviter)})" if member.inviter else "")
    else:
        head += " · before join sources were tracked"
    lines = [head, "-# " + (" · ".join(member.picks) if member.picks else "No Onboarding picks"),
             activity_line(member, now)]
    status = []
    kind = flag(member, now)
    if kind is not None:
        emoji, name = FLAGS[kind]
        why = reason(member, now)
        status.append(f"{emoji} **{name}**" + (f": {why}" if why else ""))
    if unfinished(member) and kind != NEEDS_HELLO:
        status.append(f"⚠️ {DIDNT_FINISH_LABEL}")
    if member.reached_by:
        status.append(f"✅ Reached out: {_md(member.reached_by)}, {_day(member.reached_at)}")
    if status:
        lines.append(" · ".join(status))
    return "\n".join(lines)


def breakdown_text(members: list[NewMember], now: datetime) -> str:
    """New members by timezone, with each zone's flags, and by activity: counts only."""
    totals: Counter = Counter()
    flags: dict[str, Counter] = defaultdict(Counter)
    for member in members:
        zone = zone_bucket(member.timezone, member.onboarding)
        totals[zone] += 1
        kind = flag(member, now)
        if kind is not None:
            flags[zone][kind] += 1
    zones = []
    for name in ZONE_ORDER:
        if totals[name]:
            marks = " ".join(f"{FLAGS[k][0]}{flags[name][k]}" for k in (SETTLING, DRIFTING, NEEDS_HELLO) if flags[name][k])
            zones.append(f"{name} {totals[name]}" + (f" ({marks})" if marks else ""))
    picked = Counter(activity for member in members for activity in member.activities)
    activities = [f"{name} {n}" for name, n in sorted(picked.items(), key=lambda kv: (-kv[1], ACTIVITIES.index(kv[0])))]
    unpicked = sum(1 for member in members if not member.activities)
    if unpicked:
        activities.append(f"{NOT_PICKED} {unpicked}")
    lines = []
    if zones:
        lines.append("**By timezone:** " + " · ".join(zones))
    if activities:
        lines.append("**By activity:** " + " · ".join(activities))
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
    unfinished_count = sum(1 for member in members if unfinished(member))
    if unfinished_count:
        parts.append(f"⚠️ {unfinished_count} didn't finish Onboarding")
    text = f"## 🌱 New members: last {_plural(days, 'day')}\n" + " · ".join(parts)
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
    head = f"🌱 **New members this week: {' · '.join(_flag_parts(flag_counts(members, now), bold=False))}**"
    foot = "-# `/new-members` has the full list and marks who's reached out."
    lines, used = [head], len(head) + len(foot) + 40  # room for the "and N more" line
    for shown, member in enumerate(flagged):
        emoji, _name = FLAGS[flag(member, now)]
        line = f"{emoji} <@{member.user_id}>: {reason(member, now)}" + (f" · {member.source}" if member.source else "")
        if used + len(line) + 1 > WEEKLY_TEXT_LIMIT:  # a DM's 2,000-character limit, by length not a row count
            lines.append(f"- and {len(flagged) - shown} more")
            break
        lines.append(line)
        used += len(line) + 1
    lines.append(foot)
    return "\n".join(lines)


CSV_COLUMNS = ("username", "display_name", "user_id", "joined_date", "days_in_server", "source", "inviter",
               "timezone", "activities", "experience", "onboarding_status", "first_active_date", "active_days",
               "voice_minutes",
               "status", "reached_out_by", "reached_out_date", "still_in_server")
_CSV_STATUS = {NEEDS_HELLO: "needs_hello", DRIFTING: "drifting", SETTLING: "settling"}


def csv_row(member: NewMember, now: datetime) -> list[str]:
    here = days_here(member, now)
    status = _CSV_STATUS.get(flag(member, now), "none") if member.still_here else "left"
    reached = member.reached_at.astimezone(EASTERN).date().isoformat() if member.reached_at else ""
    return [csv_cell(v) for v in (
        member.username, member.display_name, member.user_id, member.joined_at.astimezone(EASTERN).date().isoformat(),
        days_since(member.joined_at, now), member.source or "before tracking", member.inviter, member.timezone,
        ";".join(member.activities), member.experience, member.onboarding,
        member.first_active.isoformat() if member.first_active else "", min(member.active_days, here),
        member.voice_minutes, status, member.reached_by, reached, "yes" if member.still_here else "no")]


def members_csv(members: list[NewMember], now: datetime) -> str:
    out = io.StringIO()
    writer = csv.writer(out, lineterminator="\n")
    writer.writerow(CSV_COLUMNS)
    writer.writerows(csv_row(member, now) for member in members)
    return out.getvalue()


def week_key(now: datetime) -> str:
    year, week, _ = now.astimezone(EASTERN).isocalendar()
    return f"{year}-W{week:02d}"
