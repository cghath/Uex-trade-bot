"""The self-serve role picker (bot/cogs/role_picker.py): which roles there are, how a pick
changes a member's roles, and how /rolesetup matches the roles a server already has.

Four groups, each a panel of its own:
- Activities: a dropdown; picking a role adds it, picking one you already have removes it.
- Timezone and Experience: buttons, one role per member - picking one removes the others,
  picking the one you have removes it.
- Pings: toggle buttons, plus Everything: all four, or none once you have all four.

Every role is plain - no permissions, no color, not mentionable. Mentor isn't on a panel: it's
the staff role pinged in #staff when someone picks New Pilot.
"""
from __future__ import annotations

from dataclasses import dataclass

MULTI = "multi"    # the dropdown: each pick toggles that role
ONE = "one"        # buttons: one role at a time
TOGGLE = "toggle"  # buttons: each toggles its role

MENTOR_ROLE = "Mentor"
NEW_PILOT_ROLE = "New Pilot"


@dataclass(frozen=True)
class RoleGroup:
    key: str
    title: str
    blurb: str
    mode: str
    roles: tuple[str, ...]
    everything: bool = False  # an Everything button: every role in the group at once


GROUPS: tuple[RoleGroup, ...] = (
    RoleGroup("activity", "🛠️ Activities", "What do you like to do? Pick any to add them; pick one you already "
              "have to remove it.", MULTI,
              ("Mining", "Salvage", "Cargo/Trade", "Combat/Bounty", "FPS", "Exploration", "Medical", "Racing")),
    RoleGroup("timezone", "🕒 Timezone", "Where do you play from? One at a time.", ONE,
              ("Eastern", "Central", "Mountain", "Pacific", "Outside US")),
    RoleGroup("experience", "🧭 Experience", "How long have you been flying? One at a time.", ONE,
              ("New Pilot", "Getting There", "Veteran")),
    RoleGroup("pings", "🔔 Pings", "Which announcements do you want to be pinged for? Press again to turn one "
              "off, or Everything for all of them.", TOGGLE,
              ("Org Events", "Patch & Server Status", "Free Fly & Events", "Joint Ops"), everything=True),
)
GROUPS_BY_KEY = {group.key: group for group in GROUPS}

# Every role /rolesetup makes sure exists, panel roles first.
ALL_ROLES: tuple[str, ...] = tuple(name for group in GROUPS for name in group.roles) + (MENTOR_ROLE,)


def role_changes(mode: str, group_ids: set[int], held: set[int], picked: set[int]) -> tuple[set[int], set[int]]:
    """(to add, to remove) for a member holding `held` who picked `picked` in a group whose
    roles are `group_ids`. Picks outside the group are ignored."""
    picked = picked & group_ids
    if not picked:
        return set(), set()
    if mode == ONE:
        if picked & held:  # the one they have: take it off
            return set(), picked & held
        return picked, (group_ids & held) - picked
    return picked - held, picked & held  # MULTI and TOGGLE: each pick flips that role


def everything_changes(group_ids: set[int], held: set[int]) -> tuple[set[int], set[int]]:
    """(to add, to remove) for the Everything button: the ones missing, or all of them off when
    the member already has every one."""
    if group_ids <= held:
        return set(), set(group_ids)
    return group_ids - held, set()


def change_text(added: list[str], removed: list[str]) -> str:
    """The private reply to a pick, e.g. "Added **Mining**. Removed **Pacific**."."""
    parts = []
    if added:
        parts.append("Added " + ", ".join(f"**{name}**" for name in added) + ".")
    if removed:
        parts.append("Removed " + ", ".join(f"**{name}**" for name in removed) + ".")
    return " ".join(parts) or "No change."


def log_text(member_mention: str, added: list[str], removed: list[str]) -> str:
    """The #staff log line for one pick."""
    parts = []
    if added:
        parts.append("added " + ", ".join(f"**{name}**" for name in added))
    if removed:
        parts.append("removed " + ", ".join(f"**{name}**" for name in removed))
    return f"🎭 {member_mention} " + " and ".join(parts) + "."


def mentor_text(mentor_mention: str, member_mention: str) -> str:
    return f"👋 {mentor_mention} - {member_mention} just picked **{NEW_PILOT_ROLE}**. Say hi and help them get started!"


KEPT = "kept"        # the saved role id still exists (even if the role was renamed)
MATCHED = "matched"  # a role with that name already existed
CREATE = "create"    # nothing to use: make it


def setup_plan(saved: dict[str, int], existing: list[tuple[int, str]]) -> dict[str, tuple[str, int | None]]:
    """For each role in ALL_ROLES: what /rolesetup does and the role id it uses. A saved id is
    used for as long as that role exists; otherwise an existing role with the same name
    (ignoring case) is matched, so nothing is ever duplicated; otherwise it's created."""
    ids = {role_id for role_id, _ in existing}
    by_name: dict[str, int] = {}
    for role_id, name in existing:
        by_name.setdefault(name.casefold(), role_id)
    plan: dict[str, tuple[str, int | None]] = {}
    for name in ALL_ROLES:
        if saved.get(name) in ids:
            plan[name] = (KEPT, saved[name])
        elif name.casefold() in by_name:
            plan[name] = (MATCHED, by_name[name.casefold()])
        else:
            plan[name] = (CREATE, None)
    return plan
