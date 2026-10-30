"""Event cards (bot/cogs/event_cards.py): the rules and the wording, no Discord or I/O.

The owner's design (2026-10-30, the "Event Card Mockup" artifact): each Discord scheduled event
gets a card in #events with a roster. An organizer gives an event roles with spots
(/event-roles: "Miner 6, Hauler 2, Escort 2"); with none, everyone just joins one Going list.
Picking a role puts you in it, or on its wait list once it's full - the first one waiting
moves up when a spot opens. Maybe is its own status: no spot taken. Regulars of a repeating
event are signed up on each new card by themselves ("every week"). Members can say which ship
they're bringing.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

GOING, MAYBE, WAITING = "going", "maybe", "waiting"
OPEN, STARTED, ENDED, CANCELLED = "open", "started", "ended", "cancelled"
MAX_ROLES = 10
MAX_CAPACITY = 99
MAX_ROLE_NAME = 40
READY_BEFORE = 15 * 60   # seconds: "Ready by" this long before the start
REMIND_BEFORE = 60 * 60  # seconds: the roster is reminded this long before the start
ROSTER_BUDGET = 2400     # characters of roster text; Discord allows 4000 across one card
DESCRIPTION_LIMIT = 800  # the rest of the card's text, at most ~1,200, fits beside them
REGULAR_MARK = "🔁"


@dataclass(frozen=True)
class EventRole:
    name: str
    capacity: int


@dataclass(frozen=True)
class Signup:
    user_id: int
    status: str
    role: str | None = None
    ship: str | None = None


_ROLE_ITEM = re.compile(r"^(?P<name>.+?)\s+(?P<spots>\d+)$")


def parse_roles(text: str) -> tuple[list[EventRole], str | None]:
    """"Miner 6, Hauler 2, Escort 2" -> the roles, or ([], why) when it can't be read. Blank or
    "none" means no roles: everyone just joins."""
    text = text.strip()
    if not text or text.casefold() == "none":
        return [], None
    roles: list[EventRole] = []
    seen: set[str] = set()
    for item in re.split(r"[,\n]", text):
        item = " ".join(item.split())
        if not item:
            continue
        match = _ROLE_ITEM.match(item)
        if not match:
            return [], f"Each role needs a name and its number of spots, like `Miner 6` - I couldn't read `{item}`."
        name, spots = match["name"].strip(), int(match["spots"])
        if len(name) > MAX_ROLE_NAME:
            return [], f"Role names can be up to {MAX_ROLE_NAME} characters - `{name}` is longer."
        if not 1 <= spots <= MAX_CAPACITY:
            return [], f"**{name}** needs 1 to {MAX_CAPACITY} spots."
        if name.casefold() in seen:
            return [], f"**{name}** is listed twice."
        seen.add(name.casefold())
        roles.append(EventRole(name, spots))
    if len(roles) > MAX_ROLES:
        return [], f"Up to {MAX_ROLES} roles."
    return roles, None


def _capacity(roles: list[EventRole], role: str | None) -> int | None:
    return next((r.capacity for r in roles if r.name == role), None)


def going_in(signups: list[Signup], role: str | None) -> int:
    return sum(1 for s in signups if s.status == GOING and s.role == role)


def placed_status(roles: list[EventRole], signups: list[Signup], user_id: int, role: str | None) -> str:
    """GOING when `role` has a free spot (this member's own place not counted), WAITING when
    it's full. A role without a limit (no roles at all) is always GOING."""
    capacity = _capacity(roles, role)
    if capacity is None:
        return GOING
    taken = sum(1 for s in signups if s.status == GOING and s.role == role and s.user_id != user_id)
    return GOING if taken < capacity else WAITING


def next_in_line(roles: list[EventRole], signups: list[Signup], role: str | None) -> int | None:
    """Who moves up now that `role` may have a spot: the first on its wait list (signups in
    the order they joined it), if there's room."""
    capacity = _capacity(roles, role)
    if capacity is None or going_in(signups, role) >= capacity:
        return None
    return next((s.user_id for s in signups if s.status == WAITING and s.role == role), None)


def wait_position(signups: list[Signup], user_id: int, role: str | None) -> int:
    waiting = [s.user_id for s in signups if s.status == WAITING and s.role == role]
    return waiting.index(user_id) + 1 if user_id in waiting else 0


def repeats_text(rule: dict | None) -> str | None:
    """A Discord recurrence rule in words. The weekday is left out on purpose: Discord keeps
    it in UTC (Mining Mondays, 7:30 PM Eastern, is a UTC Tuesday) and the start time already
    shows each reader their own day."""
    if not rule:
        return None
    frequency, interval = rule.get("frequency"), rule.get("interval") or 1
    unit = {0: "year", 1: "month", 2: "week", 3: "day"}.get(frequency)
    if unit is None:
        return "Repeats"
    if interval == 1:
        return f"Every {unit}"
    if interval == 2:
        return f"Every other {unit}"
    return f"Every {interval} {unit}s"


def ship_matches(names: list[str], query: str) -> list[str]:
    """Ships a typed name could mean: an exact name (any case) alone, else every name that
    contains it. More than one means asking which."""
    query = " ".join(query.split()).casefold()
    if not query:
        return []
    exact = [n for n in names if n.casefold() == query]
    if exact:
        return exact[:1]
    return sorted({n for n in names if query in n.casefold()})


def _member(signup: Signup, regulars: set[int]) -> str:
    text = f"<@{signup.user_id}>"
    if signup.ship:
        text += f" · {signup.ship}"
    if signup.user_id in regulars:
        text += f" {REGULAR_MARK}"
    return text


def roster_text(roles: list[EventRole], signups: list[Signup], regulars: set[int]) -> str:
    """The roster part of a card: each role's [taken/spots] and who, the wait list, Maybe."""
    going = [s for s in signups if s.status == GOING]
    waiting = [s for s in signups if s.status == WAITING]
    maybe = [s for s in signups if s.status == MAYBE]
    if not signups:
        return "**Roster** - nobody yet. Be the first!"
    counts = [f"{len(going)} going"] + ([f"{len(waiting)} waiting"] if waiting else []) \
        + ([f"{len(maybe)} maybe"] if maybe else [])
    blocks = [[f"**Roster** - {', '.join(counts)}"]]
    names = {r.name for r in roles}
    for role in roles:
        members = [s for s in going if s.role == role.name]
        title = f"**{role.name} [{len(members)}/{role.capacity}]**" + (" full" if len(members) >= role.capacity else "")
        blocks.append([title] + [f"✓ {_member(s, regulars)}" for s in members])
    loose = [s for s in going if s.role not in names]
    if loose:
        title = "**Going - no role yet**" if roles else f"**Going ({len(loose)})**"
        blocks.append([title] + [f"✓ {_member(s, regulars)}" for s in loose])
    if waiting:
        blocks.append([f"**Wait list ({len(waiting)})**"]
                      + [f"{i}. <@{s.user_id}> - {s.role}" for i, s in enumerate(waiting, 1)])
    if maybe:
        blocks.append([f"**Maybe ({len(maybe)})**"]
                      + [f"? <@{s.user_id}>" + (f" - {s.role or 'no role yet'}" if roles else "") for s in maybe])
    lines = [line for block in blocks for line in (*block, "")][:-1]
    kept: list[str] = []
    for i, line in enumerate(lines):
        rest = sum(1 for later in lines[i:] if later.startswith(("✓", "?")) or later[:1].isdigit())
        tail = f"…and {rest} more on the roster." if rest else ""
        if len("\n".join([*kept, line])) + len(tail) + 1 > ROSTER_BUDGET:
            return "\n".join([*kept, tail]).rstrip()
        kept.append(line)
    return "\n".join(kept)


def details_text(*, creator_id: int | None, description: str | None, starts_at: int, voice_channel_id: int | None,
                 repeats: str | None, regulars: int, state: str) -> str:
    """Who, what, when and where, under the card's title and picture."""
    lines = []
    if creator_id:
        lines.append(f"-# Organized by <@{creator_id}>")
    if description:
        lines.append(description.strip()[:DESCRIPTION_LIMIT])
    lines.append(f"**Starts** <t:{starts_at}:F> (<t:{starts_at}:R>)")
    lines.append(f"**Ready by** <t:{starts_at - READY_BEFORE}:t>")
    if voice_channel_id:
        lines.append(f"**Where** <#{voice_channel_id}>")
    if repeats:
        lines.append(f"**Repeats** {repeats}" + (f" - {plural(regulars, 'regular')} signed up every time" if regulars else ""))
    status = {STARTED: "**Live now** - see you in voice!", ENDED: "**This op has ended.**",
              CANCELLED: "**This op was cancelled.**"}.get(state)
    if status:
        lines.append(status)
    return "\n".join(lines)


def plural(n: int, word: str) -> str:
    return f"{n} {word}" + ("" if n == 1 else "s")


def mentions(user_ids: list[int]) -> str:
    return " ".join(f"<@{uid}>" for uid in user_ids)


def announce_text(org_mention: str | None) -> str:
    return f"{org_mention} a new op is on the schedule." if org_mention else "A new op is on the schedule."


def reminder_text(name: str, starts_at: int, voice_channel_id: int | None) -> str:
    where = f", in <#{voice_channel_id}>" if voice_channel_id else ""
    return f"**{name}** starts in 1 hour, at <t:{starts_at}:t>{where}."


def start_text(name: str, voice_channel_id: int | None) -> str:
    return f"**{name} is starting now**" + (f" - jump in <#{voice_channel_id}>." if voice_channel_id else "!")


def time_change_text(name: str, starts_at: int, was: int) -> str:
    return f"**Time changed:** {name} now starts <t:{starts_at}:F> (was <t:{was}:f>). The card is updated."


def cancelled_text(name: str) -> str:
    return f"**{name}** was cancelled."


def ended_text(name: str) -> str:
    return f"**{name}** has ended. Thanks for flying!"


def joined_text(name: str, status: str, role: str | None, roles: list[EventRole], signups: list[Signup],
                user_id: int) -> str:
    """The private reply after joining."""
    if status == WAITING:
        return (f"**{role}** is full - you're **#{wait_position(signups, user_id, role)} on the wait list**. If a spot "
                "opens, you get it and I'll DM you.")
    if status == MAYBE:
        return f"You're on **Maybe** for **{name}**." + (" Pick a role when you're sure." if roles else "")
    capacity = _capacity(roles, role)
    if capacity is None:
        return f"You're in for **{name}**."
    return f"You're in for **{name}** as **{role}** ({going_in(signups, role)}/{capacity})."


def promoted_text(name: str, role: str, starts_at: int) -> str:
    return (f"A spot opened up - you're now **{role}** for **{name}**, <t:{starts_at}:F>. Can't make it? "
            "Press Leave on the card.")
