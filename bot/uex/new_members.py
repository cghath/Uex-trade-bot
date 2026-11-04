"""New members (the owner's ask, 2026-11-04): a temporary "🌱 New Member" role for each member's
first NEW_MEMBER_DAYS in Star Tech Dynamics, and /new-members - a private list of them for the
owner and moderators, so someone reaches out before they drift off. Pure rules and wording; the
cog is bot/cogs/new_members.py.

How long someone has been here comes from Discord's own join date (member.joined_at), so a
restart never loses it and a member who leaves and rejoins starts again. This is about time in
the server, not account age: Sapphire's new-account role is a different thing, and a member can
have both.

Nothing anyone wrote is read: "said hello" is whether they've posted or joined voice since
joining (server analytics' daily activity log, bot/uex/server_analytics.py).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

from bot.uex.server_analytics import EASTERN

NEW_MEMBER_ROLE = "🌱 New Member"
NEW_MEMBER_DAYS = 21     # the role comes off after this many days
HELLO_AFTER_DAYS = 3     # nothing posted and no voice after this long: needs a hello
MAX_DAYS = 90            # /new-members days: at most this far back
PAGE_SIZE = 8
PICK_GROUPS = ("activity", "timezone", "experience")  # the Onboarding picks shown for each member


def is_new(joined_at: datetime | None, now: datetime, days: int = NEW_MEMBER_DAYS) -> bool:
    return joined_at is not None and now - joined_at < timedelta(days=days)


def days_in(joined_at: datetime, now: datetime) -> int:
    return max(0, (now - joined_at).days)


@dataclass
class NewMember:
    user_id: int
    joined_at: datetime
    source: str | None = None            # None: joined before join sources were tracked
    picks: list[str] = field(default_factory=list)
    first_active: date | None = None      # first day they posted or joined voice since joining
    reached_by: str | None = None
    reached_at: datetime | None = None


def needs_hello(member: NewMember, now: datetime) -> bool:
    """Nothing posted and no voice after HELLO_AFTER_DAYS, and nobody's reached out yet."""
    return (member.first_active is None and member.reached_by is None
            and now - member.joined_at >= timedelta(days=HELLO_AFTER_DAYS))


def _day(moment: datetime) -> str:
    local = moment.astimezone(EASTERN)
    return f"{local:%b} {local.day}"


def member_text(member: NewMember, now: datetime) -> str:
    """One member's lines in /new-members."""
    days = days_in(member.joined_at, now)
    head = f"<@{member.user_id}> · joined <t:{int(member.joined_at.timestamp())}:d> ({days} day{'s' if days != 1 else ''})"
    head += f" · {member.source}" if member.source else " · before join sources were tracked"
    lines = [head, "-# " + (" · ".join(member.picks) if member.picks else "No Onboarding picks")]
    if member.first_active is not None:
        status = f"💬 Posted or in voice since {member.first_active:%b} {member.first_active.day}"
    elif needs_hello(member, now):
        status = f"⚠️ **Needs a hello**: no posts or voice in {days} days"
    else:
        status = "No posts or voice yet"
    if member.reached_by:
        status += f" · ✅ Reached out: {member.reached_by}, {_day(member.reached_at)}"
    lines.append(status)
    return "\n".join(lines)


def summary_text(members: list[NewMember], now: datetime, days: int) -> str:
    waiting = sum(1 for m in members if needs_hello(m, now))
    text = f"## 🌱 New members: last {days} days\n{len(members)} joined"
    if waiting:
        text += f" · **{waiting} need{'s' if waiting == 1 else ''} a hello**"
    return text + "\n-# Newest first. Join dates, sources, Onboarding picks and whether they've posted or been in voice; never what anyone wrote."


def weekly_text(members: list[NewMember], now: datetime) -> str | None:
    """The owner's weekly note: new members still waiting for a hello, or None when there are none."""
    waiting = [m for m in members if needs_hello(m, now)]
    if not waiting:
        return None
    lines = [f"🌱 **{len(waiting)} new member{'s' if len(waiting) != 1 else ''} still need{'s' if len(waiting) == 1 else ''} "
             "a hello** (no posts or voice yet, and nobody's reached out):"]
    lines += [f"- <@{m.user_id}>, joined {days_in(m.joined_at, now)} days ago" + (f" · {m.source}" if m.source else "")
              for m in waiting[:20]]
    if len(waiting) > 20:
        lines.append(f"- and {len(waiting) - 20} more")
    lines.append("-# `/new-members` has the full list and marks who's reached out.")
    return "\n".join(lines)


def week_key(now: datetime) -> str:
    year, week, _ = now.astimezone(EASTERN).isocalendar()
    return f"{year}-W{week:02d}"
