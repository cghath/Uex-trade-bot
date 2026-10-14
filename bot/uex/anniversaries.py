"""Server anniversaries: on the day a member joined Star Tech Dynamics, at the same time of day,
the bot DMs them a short formal note and posts one in the bot channel (the owner's design,
2026-10-14). Pure logic - when one is due and what the messages say; the cog
(bot/cogs/anniversaries.py) sends them.

The owner's decisions: sent at the member's own join time (members are all over the world), a
DM plus a post that @mentions them, formal, no push to use the bot, "Star Tech Dynamics" in full
(never "STDynamics", the bot's own name). A button under the DM stops them.
"""
from __future__ import annotations

import calendar
from datetime import datetime, timedelta

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


def _years(years: int) -> str:
    return f"{years} year{'s' if years != 1 else ''}"


def _stamp(joined: datetime, style: str) -> str:
    """A Discord timestamp: shown in each reader's own time zone and language."""
    return f"<t:{int(joined.timestamp())}:{style}>"


def dm_text(name: str, years: int, joined: datetime) -> str:
    return (f"🎉 **Happy anniversary, {name}!**\n"
            f"Today marks {_years(years)} since you joined **{COMMUNITY_NAME}**, on {_stamp(joined, 'D')} at "
            f"{_stamp(joined, 't')}. Thank you for being part of the community. We're glad to have you with us. 🥳")


def post_text(mention: str, years: int, joined: datetime) -> str:
    return (f"🎊 **Happy server anniversary!** 🎊\n"
            f"{mention} joined **{COMMUNITY_NAME}** {_years(years)} ago today, on {_stamp(joined, 'D')}. "
            "Thank you for being part of the community! 🎉")


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
