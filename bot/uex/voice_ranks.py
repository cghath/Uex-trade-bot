"""Voice-activity ranks (the owner's idea, 2026-10-26, replacing Arcane's levels): the rules, with
no Discord or database in them. bot/cogs/voice_ranks.py applies them once a minute.

Only who is in which voice channel is read - the same thing Discord shows everyone - never audio.

The owner's decisions:
- A minute counts in a voice channel with 2+ members who aren't bots and aren't deafened; never
  the AFK channel. Muted members count: plenty listen in on ops with their mic off.
- 1 XP a minute, 2 in the Ops category. At most DAILY_CAP_MINUTES counted a person a day (UTC).
- Ranks are lifetime and never reset; the leaderboards are weekly (from Monday) and seasonal
  (calendar quarters). The ladder: Cadet 1h, Crewman 5h, Specialist 15h, Petty Officer 35h,
  Officer 75h, Lieutenant 150h, Commander 300h, Captain 500h, Admiral 1,000h of ordinary voice.
- A second ladder for ops attended: an op is 3+ people in the Ops channels together for 30+
  minutes, and you attended if you were there for at least half of it.
- Arcane's levels aren't carried over: everyone starts from zero.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta

DAILY_CAP_MINUTES = 5 * 60
OPS_XP_PER_MINUTE = 2
MIN_MEMBERS = 2

RANKS: tuple[tuple[str, int], ...] = (
    ("Cadet", 1 * 60),
    ("Crewman", 5 * 60),
    ("Specialist", 15 * 60),
    ("Petty Officer", 35 * 60),
    ("Officer", 75 * 60),
    ("Lieutenant", 150 * 60),
    ("Commander", 300 * 60),
    ("Captain", 500 * 60),
    ("Admiral", 1000 * 60),
)

OP_RANKS: tuple[tuple[str, int], ...] = (
    ("Operator", 5),
    ("Veteran Operator", 15),
    ("Op Specialist", 40),
    ("Wing Commander", 100),
)

OP_MIN_PEOPLE = 3
OP_MIN_MINUTES = 30
# Minutes below OP_MIN_PEOPLE before an op is over: a dropped connection or someone swapping
# channels shouldn't split one op into two too-short ones.
OP_GRACE_MINUTES = 5


def counts(member) -> bool:
    """A member whose minute can count: not a bot, not deafened (by themselves or the server)."""
    voice = getattr(member, "voice", None)
    if member.bot or voice is None:
        return False
    return not (voice.self_deaf or voice.deaf)


def counted_members(channel, afk_channel_id: int | None) -> list:
    """Who earns this minute in one voice channel: nobody in the AFK channel, and nobody unless at
    least MIN_MEMBERS can count."""
    if afk_channel_id is not None and channel.id == afk_channel_id:
        return []
    members = [m for m in channel.members if counts(m)]
    return members if len(members) >= MIN_MEMBERS else []


def rank_index(value: int, ladder: tuple[tuple[str, int], ...] = RANKS) -> int:
    """The index of the highest rank `value` has reached in `ladder`, -1 for none yet."""
    reached = -1
    for i, (_, needed) in enumerate(ladder):
        if value >= needed:
            reached = i
    return reached


def rank_name(index: int, ladder: tuple[tuple[str, int], ...] = RANKS) -> str | None:
    return ladder[index][0] if index >= 0 else None


@dataclass(frozen=True)
class Progress:
    rank: str | None
    next_rank: str | None
    needed: int          # how much more to the next rank; 0 at the top
    fraction: float      # 0-1 of the way from this rank to the next


def progress(value: int, ladder: tuple[tuple[str, int], ...] = RANKS) -> Progress:
    index = rank_index(value, ladder)
    if index + 1 >= len(ladder):
        return Progress(rank_name(index, ladder), None, 0, 1.0)
    low = ladder[index][1] if index >= 0 else 0
    high = ladder[index + 1][1]
    return Progress(rank_name(index, ladder), ladder[index + 1][0], high - value,
                    max(0.0, min(1.0, (value - low) / (high - low))))


def bar(fraction: float, width: int = 10) -> str:
    filled = round(fraction * width)
    return "▰" * filled + "▱" * (width - filled)


def hours(minutes_or_xp: int) -> str:
    """'12h 5m', '45m'."""
    h, m = divmod(int(minutes_or_xp), 60)
    return f"{h:,}h {m}m" if h else f"{m}m"


def week_start(day: date) -> date:
    return day - timedelta(days=day.weekday())


def season_start(day: date) -> date:
    return date(day.year, 3 * ((day.month - 1) // 3) + 1, 1)


def season_name(day: date) -> str:
    return f"Q{(day.month - 1) // 3 + 1} {day.year}"


def promotion_text(mention: str, rank: str, xp: int) -> str:
    """XP, not hours: a minute in Ops is 2 XP, so XP read as time would overstate it (audit COMM-9)."""
    return f"🎖️ {mention} has been promoted to **{rank}**! ({xp:,} XP)"


def op_promotion_text(mention: str, rank: str, ops: int) -> str:
    return f"🎯 {mention} has earned **{rank}**: {ops} ops attended."


@dataclass
class OpSession:
    """One op in progress. `minutes` counts the minutes it has had OP_MIN_PEOPLE or more;
    `present` each person's minutes in it; `quiet` the minutes in a row below OP_MIN_PEOPLE."""
    started_at: datetime
    minutes: int = 0
    quiet: int = 0
    present: dict[int, int] | None = None

    def __post_init__(self) -> None:
        if self.present is None:
            self.present = {}


def op_step(session: OpSession | None, people: set[int], now: datetime) -> tuple[OpSession | None, OpSession | None]:
    """One minute of the Ops channels. Returns (the session still running, a session that just
    ended). An op starts the first minute OP_MIN_PEOPLE are there together and ends after
    OP_GRACE_MINUTES in a row with fewer."""
    busy = len(people) >= OP_MIN_PEOPLE
    if session is None:
        if not busy:
            return None, None
        session = OpSession(started_at=now)
    if busy:
        session.minutes += 1
        session.quiet = 0
        for user_id in people:
            session.present[user_id] = session.present.get(user_id, 0) + 1
        return session, None
    session.quiet += 1
    if session.quiet >= OP_GRACE_MINUTES:
        return None, session
    return session, None


def op_attendees(session: OpSession) -> list[int]:
    """Who attended an op that has ended: nobody if it was shorter than OP_MIN_MINUTES, else
    everyone there for at least half of it."""
    if session.minutes < OP_MIN_MINUTES:
        return []
    return sorted(uid for uid, minutes in session.present.items() if minutes * 2 >= session.minutes)
