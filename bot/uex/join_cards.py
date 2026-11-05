"""Join and leave cards for staff (bot/cogs/server_analytics.py posts them).

When someone joins, a small card in #staff (or the channel `/join-cards` sets) says where they
came from - the invite matched for the join, never a guess - how old their account is and the
member count, to sit next to Sapphire's join alert. When someone leaves, one line: how long they
stayed and where they'd come from, so it shows which sources bring people who leave quickly.
Names, counts and dates only, never what anyone wrote.
"""
from __future__ import annotations

from datetime import datetime

from bot.uex.new_members import _md
from bot.uex.server_analytics import MEMBER_REFERRAL, NO_INVITE, UNKNOWN_INVITE

STAFF_CHANNEL_ID = 1564673977597567087  # #staff: where the cards go unless /join-cards says otherwise
NEW_ACCOUNT_DAYS = 7                    # ⚠️ an account younger than this
SOURCE_UNKNOWN = "Source unknown"
BEFORE_TRACKING = "joined before join tracking began"


def _plural(n: int, word: str) -> str:
    return f"{n} {word}{'s' if n != 1 else ''}"


def name_link(user_id: int, name: str, username: str = "") -> str:
    """A name linked to the profile (a mention reads "@unknown-user" to anyone whose Discord hasn't
    loaded the member), with the username when it's different."""
    name = name or username or str(user_id)
    text = f"**[{_md(name)}](https://discord.com/users/{user_id})**"
    if username and username != name:
        text += f" (@{_md(username)})"
    return text


def account_age(created: datetime, now: datetime) -> str:
    days = max(0, (now - created).days)
    if days < NEW_ACCOUNT_DAYS:
        return "⚠️ " + (f"{_plural(days, 'day')} old" if days else "less than a day old")
    if days >= 365:
        return f"{_plural(days // 365, 'year')} old"
    if days >= 60:
        return f"{_plural(days // 30, 'month')} old"
    return f"{_plural(days, 'day')} old"


def stay_length(joined: datetime, left: datetime) -> str:
    """How long someone stayed: minutes or hours on their first day, then days."""
    seconds = max(0, int((left - joined).total_seconds()))
    if seconds < 3600:
        return _plural(max(1, seconds // 60), "minute")
    if seconds < 86400:
        return _plural(seconds // 3600, "hour")
    return _plural(seconds // 86400, "day")


def source_text(code: str | None, source: str, inviter_name: str | None) -> str:
    """Where a join came from: the invite's label and code, a member referral and who made it, or
    unknown when no single invite matched (`source` is source_of's answer)."""
    if code is None or source == NO_INVITE:
        return SOURCE_UNKNOWN
    if source == MEMBER_REFERRAL:
        return f"{MEMBER_REFERRAL} (invited by {_md(inviter_name)})" if inviter_name else MEMBER_REFERRAL
    if source == UNKNOWN_INVITE:
        return f"Unnamed invite (`{code}`)"
    return f"{_md(source)} (`{code}`)"


def join_card(user_id: int, name: str, username: str, source: str, created: datetime, now: datetime,
              member_count: int | None) -> str:
    lines = [f"📥 {name_link(user_id, name, username)} joined · `{user_id}`",
             f"**Source:** {source}",
             f"**Account:** {account_age(created, now)}"
             + (f" · **Members:** {member_count:,}" if member_count else "")]
    return "\n".join(lines)


def leave_card(user_id: int, name: str, username: str, joined: datetime | None, now: datetime,
               source: str | None) -> str:
    """One line. `source` None: the bot has no record of their join."""
    stayed = f" after {stay_length(joined, now)}" if joined is not None else ""
    if source is None:
        where = BEFORE_TRACKING
    else:
        where = "source unknown" if source == SOURCE_UNKNOWN else f"from {source}"
    return f"📤 {name_link(user_id, name, username)} left{stayed} · {where}"
