"""Server anniversaries: on the day a member joined Star Tech Dynamics, at the same time of day,
the bot DMs them a short formal note and posts one in the bot channel (the owner's design,
2026-10-14). Pure logic - when one is due and what the messages say; the cog
(bot/cogs/anniversaries.py) sends them.

The owner's decisions: sent at the member's own join time (members are all over the world), a
DM plus a post that @mentions them, formal, no push to use the bot, "Star Tech Dynamics" in full
(never "STDynamics", the bot's own name). A button under the DM stops them.

Added 2026-10-29 (the owner's ask): a veteran role for each year - "1-Year Veteran", then
"2-Year Veteran", each replacing the last - given as the message goes out and named in it, and a
line in the DM pointing to the server's next Discord event. The same event line goes in the
welcome-back DM (bot/cogs/welcome_back.py).
"""
from __future__ import annotations

import calendar
from dataclasses import dataclass
from datetime import datetime, timedelta

from discord.utils import escape_markdown

COMMUNITY_NAME = "Star Tech Dynamics"
# A bot that was down at someone's moment still sends it when back, up to this long after.
CATCH_UP = timedelta(hours=24)


def anniversary_moment(joined: datetime, year: int) -> datetime:
    """The join moment in `year`: same month, day and time. A 29 February join falls on 28
    February in other years."""
    day = joined.day
    if joined.month == 2 and day == 29 and not calendar.isleap(year):
        day = 28
    return joined.replace(year=year, day=day)


def due_anniversary(joined: datetime, now: datetime) -> tuple[int, datetime] | None:
    """(years, moment) when an anniversary of at least a year has passed within CATCH_UP of
    `now`, else None. Both aware datetimes. Checks last year's moment too, for a 31 December
    join seen just after midnight UTC."""
    for year in (now.year, now.year - 1):
        years = year - joined.year
        if years < 1:
            continue
        moment = anniversary_moment(joined, year)
        if moment <= now < moment + CATCH_UP:
            return years, moment
    return None


def years_completed(joined: datetime, now: datetime) -> int:
    """How many anniversaries have passed by `now` (0 in the first year)."""
    years = now.year - joined.year
    if years > 0 and anniversary_moment(joined, now.year) > now:
        years -= 1
    return max(years, 0)


def veteran_role_name(years: int) -> str:
    return f"{years}-Year Veteran"


def is_veteran_role(name: str) -> bool:
    head, sep, rest = name.partition("-Year Veteran")
    return bool(sep) and rest == "" and head.isdigit()


@dataclass(frozen=True)
class Event:
    """A Discord scheduled event: status 1 is scheduled, 2 is happening now."""
    id: int
    name: str
    start: datetime
    status: int


def event_line(events: list[Event], guild_id: int, now: datetime) -> str | None:
    """'🚀 Come fly with us at our next event: [**Mining Mondays**](link), <t:..:F>.', for the
    event on now or else the soonest still to come; None when there's neither. The time is a
    Discord timestamp, so each reader sees it in their own time zone."""
    live = [e for e in events if e.status == 2]
    coming = sorted((e for e in events if e.status == 1 and e.start > now), key=lambda e: e.start)
    if not live and not coming:
        return None
    event = live[0] if live else coming[0]
    link = f"[**{escape_markdown(event.name)}**](https://discord.com/events/{guild_id}/{event.id})"
    if live:
        return f"🚀 Come fly with us: {link} is happening now."
    return f"🚀 Come fly with us at our next event: {link}, {_stamp(event.start, 'F')}."


def _years(years: int) -> str:
    return f"{years} year{'s' if years != 1 else ''}"


def _stamp(joined: datetime, style: str) -> str:
    """A Discord timestamp: shown in each reader's own time zone and language."""
    return f"<t:{int(joined.timestamp())}:{style}>"


def dm_text(name: str, years: int, joined: datetime, event: str | None = None, *, veteran: bool = True) -> str:
    """`veteran`: the member really holds this year's veteran role - the line naming it is left out
    when giving it failed (audit COMM-4)."""
    lines = [f"🎉 **Happy anniversary, {name}!**",
             f"Today marks {_years(years)} since you joined **{COMMUNITY_NAME}**, on {_stamp(joined, 'D')} at "
             f"{_stamp(joined, 't')}. Thank you for being part of the community. We're glad to have you with us. 🥳"]
    if veteran:
        lines.append(f"🎖️ You've earned the **{veteran_role_name(years)}** role.")
    if event:
        lines.append(event)
    return "\n".join(lines)


def post_text(mention: str, years: int, joined: datetime, *, veteran: bool = True) -> str:
    role = f", and is now a **{veteran_role_name(years)}**" if veteran else ""
    return (f"🎊 **Happy server anniversary!** 🎊\n"
            f"{mention} joined **{COMMUNITY_NAME}** {_years(years)} ago today, on {_stamp(joined, 'D')}{role}. "
            f"Thank you for being part of the community! 🎉")


# The welcome-back DM (bot/cogs/welcome_back.py): for a member back after this long away.
INACTIVE_DAYS = 30


def welcome_back_text(name: str, event: str | None = None) -> str:
    lines = [f"👋 **Welcome back, {name}!**", f"It's good to see you around **{COMMUNITY_NAME}** again."]
    if event:
        lines.append(event)
    return "\n".join(lines)


def upcoming(members: list[tuple[str, datetime]], now: datetime, count: int = 3) -> list[tuple[datetime, str, int]]:
    """The next `count` anniversaries after `now`: (moment, name, years), soonest first."""
    found = []
    for name, joined in members:
        for year in (now.year, now.year + 1):
            moment = anniversary_moment(joined, year)
            if moment > now and year - joined.year >= 1:
                found.append((moment, name, year - joined.year))
                break
    return sorted(found)[:count]
