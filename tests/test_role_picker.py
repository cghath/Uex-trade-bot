"""The role picker (bot/uex/role_picker.py, bot/cogs/role_picker.py).

Guarantees under test:
  1. Rules - the dropdown and the Pings buttons toggle each pick; Timezone and Experience keep
     one role, picking another swaps it, picking the one you have removes it; picks outside the
     group are ignored. Pings' Everything adds the missing ones, or removes all once you have all.
  2. /rolesetup - creates only what's missing, plain (no permissions, no color, not
     mentionable), matches an existing role by name (any case), keeps a saved role by id even
     after a rename, never duplicates on a second run, warns about a role it can't give or
     can't ping; admins only.
  3. /rolepanel post - four panels whose buttons and dropdown carry the saved role ids, matched
     by the startup-registered templates; refuses until /rolesetup has run.
  4. Picking - changes only the member's roles in that group, replies privately, logs in #staff
     without pinging the member, pings Mentor (only) for New Pilot; an id that isn't a saved
     picker role does nothing; a refused role change says so and logs nothing.
"""
from __future__ import annotations

import asyncio
import re
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock

import discord
from cryptography.fernet import Fernet

from bot.cogs.role_picker import (
    BUTTON_TEMPLATE, EVERYTHING_TEMPLATE, MENU_TEMPLATE, OUT_OF_DATE, STAFF_CHANNEL_ID, EverythingButton, RoleButton,
    RoleMenu, RolePicker, apply_pick,
)
from bot.db.database import Database
from bot.uex.role_picker import (
    ALL_ROLES, CREATE, GROUPS, KEPT, MATCHED, MULTI, ONE, TOGGLE, change_text, everything_changes, log_text,
    role_changes, setup_plan,
)

GUILD_ID = 1132378045232197694


# -- 1. rules -------------------------------------------------------------------------------------

def test_toggle_groups_flip_each_pick():
    group = {1, 2, 3}
    for mode in (MULTI, TOGGLE):
        assert role_changes(mode, group, held={1, 99}, picked={1, 2}) == ({2}, {1})
        assert role_changes(mode, group, held=set(), picked={3, 42}) == ({3}, set())


def test_one_role_groups_swap_and_untoggle():
    group = {1, 2, 3}
    assert role_changes(ONE, group, held={1, 99}, picked={2}) == ({2}, {1})
    assert role_changes(ONE, group, held={2}, picked={2}) == (set(), {2})
    assert role_changes(ONE, group, held=set(), picked={3}) == ({3}, set())
    assert role_changes(ONE, group, held={1}, picked={42}) == (set(), set())


def test_everything_adds_the_missing_ones_or_clears_a_full_set():
    group = {1, 2, 3, 4}
    assert everything_changes(group, held={2, 99}) == ({1, 3, 4}, set())
    assert everything_changes(group, held=set()) == (group, set())
    assert everything_changes(group, held={1, 2, 3, 4, 99}) == (set(), group)


def test_reply_and_log_wording():
    assert change_text(["Mining", "FPS"], ["Pacific"]) == "Added **Mining**, **FPS**. Removed **Pacific**."
    assert change_text([], []) == "No change."
    assert log_text("<@5>", ["Eastern"], ["Pacific"]) == "🎭 <@5> added **Eastern** and removed **Pacific**."


def test_every_group_fits_a_discord_panel():
    assert [g.key for g in GROUPS] == ["activity", "timezone", "experience", "pings"]
    for group in GROUPS:
        assert len(group.roles) + group.everything <= (25 if group.mode == MULTI else 5)
    assert [g.key for g in GROUPS if g.everything] == ["pings"]
    assert len(ALL_ROLES) == len(set(ALL_ROLES)) == 21


# -- 2. setup plan --------------------------------------------------------------------------------

def test_setup_plan_keeps_by_id_matches_by_name_creates_the_rest():
    existing = [(10, "mining"), (11, "Renamed Salvage"), (12, "Veteran Operator"), (13, "1-Year Veteran")]
    plan = setup_plan({"Salvage": 11, "FPS": 777}, existing)
    assert plan["Mining"] == (MATCHED, 10)
    assert plan["Salvage"] == (KEPT, 11)
    assert plan["FPS"] == (CREATE, None)       # saved id gone
    assert plan["Veteran"] == (CREATE, None)   # "Veteran Operator"/"1-Year Veteran" aren't it
    assert list(plan) == list(ALL_ROLES)


# -- fakes ----------------------------------------------------------------------------------------

class FakeRole:
    def __init__(self, role_id, name, *, permissions=0, assignable=True):
        self.id, self.name, self.mentionable = role_id, name, False
        self.permissions, self._assignable = discord.Permissions(permissions), assignable
        self.mention = f"<@&{role_id}>"

    def is_assignable(self):
        return self._assignable


class FakeMember:
    def __init__(self, user_id, roles=()):
        self.id, self.mention, self.roles = user_id, f"<@{user_id}>", list(roles)
        self.guild_permissions = discord.Permissions(administrator=True)
        self.fail = False

    async def add_roles(self, *roles, reason=None):
        if self.fail:
            raise discord.Forbidden(NS(status=403, reason="Forbidden"), "Missing Permissions")
        self.roles += [r for r in roles if r not in self.roles]

    async def remove_roles(self, *roles, reason=None):
        self.roles = [r for r in self.roles if r not in roles]


class FakeGuild:
    def __init__(self, roles=(), *, mention_everyone=True):
        self.id, self.roles, self.created = GUILD_ID, list(roles), []
        self.me = NS(guild_permissions=discord.Permissions(mention_everyone=mention_everyone))

    def get_role(self, role_id):
        return next((r for r in self.roles if r.id == role_id), None)

    async def create_role(self, **kwargs):
        self.created.append(kwargs)
        role = FakeRole(5000 + len(self.roles), kwargs["name"])
        self.roles.append(role)
        return role


def _interaction(db, guild, user, staff):
    return NS(guild=guild, user=user,
              client=NS(db=db, get_channel=lambda cid: staff if cid == STAFF_CHANNEL_ID else None),
              response=NS(defer=AsyncMock(), send_message=AsyncMock()), followup=NS(send=AsyncMock()))


def _run(tmp_path, body):
    async def go():
        db = Database(tmp_path / "rp.sqlite3", Fernet(Fernet.generate_key()))
        await db.init()
        await body(db)
    asyncio.run(go())


async def _set_up(db, guild):
    cog = RolePicker.__new__(RolePicker)
    cog.bot = NS(db=db)
    admin = FakeMember(1)
    inter = _interaction(db, guild, admin, None)
    await RolePicker.rolesetup.callback(cog, inter)
    return cog, inter.followup.send.await_args.args[0]


# -- 2. /rolesetup --------------------------------------------------------------------------------

def test_rolesetup_creates_plain_roles_matches_existing_and_never_duplicates(tmp_path):
    async def body(db):
        guild = FakeGuild([FakeRole(10, "MINING"), FakeRole(11, "miner")])
        _, reply = await _set_up(db, guild)
        assert [c["name"] for c in guild.created] == [n for n in ALL_ROLES if n != "Mining"]
        for kwargs in guild.created:
            assert kwargs["permissions"].value == 0 and kwargs["mentionable"] is False
            assert kwargs["hoist"] is False and kwargs["colour"] == discord.Colour.default()
        saved = await db.get_role_picker_roles(GUILD_ID)
        assert saved["Mining"] == 10 and len(saved) == 21
        assert "Using existing: Mining" in reply and "21 of 21 ready" in reply
        guild.get_role(saved["Salvage"]).name = "Salvagers"   # renamed later: still the same role
        _, again = await _set_up(db, guild)
        assert len(guild.created) == 20 and await db.get_role_picker_roles(GUILD_ID) == saved
        assert "Created" not in again
    _run(tmp_path, body)


def test_rolesetup_warns_about_roles_it_cannot_give_or_ping(tmp_path):
    async def body(db):
        guild = FakeGuild([FakeRole(10, "Mining", permissions=8, assignable=False)], mention_everyone=False)
        _, reply = await _set_up(db, guild)
        assert "**Mining** already existed with permissions" in reply
        assert "**Mining** is above my role" in reply
        assert "can't ping **Mentor**" in reply
    _run(tmp_path, body)


def test_rolesetup_and_panel_post_are_admin_only(tmp_path):
    async def body(db):
        cog = RolePicker.__new__(RolePicker)
        cog.bot = NS(db=db)
        member = FakeMember(2)
        member.guild_permissions = discord.Permissions.none()
        inter = _interaction(db, FakeGuild(), member, None)
        await RolePicker.rolesetup.callback(cog, inter)
        await RolePicker.rolepanel_post.callback(cog, inter, NS())
        assert all(c.args[0] == "This command is for server admins." for c in inter.response.send_message.await_args_list)
        assert inter.response.send_message.await_count == 2 and await db.get_role_picker_roles(GUILD_ID) == {}
    _run(tmp_path, body)


# -- 3. /rolepanel post ---------------------------------------------------------------------------

def test_panels_carry_the_saved_role_ids(tmp_path):
    async def body(db):
        guild = FakeGuild()
        cog, _ = await _set_up(db, guild)
        saved = await db.get_role_picker_roles(GUILD_ID)
        channel = NS(mention="#pick-your-roles", send=AsyncMock())
        inter = _interaction(db, guild, FakeMember(1), None)
        await RolePicker.rolepanel_post.callback(cog, inter, channel)
        assert channel.send.await_count == 4
        views = [c.kwargs["view"] for c in channel.send.await_args_list]
        menu = views[0].children[0].item
        assert re.fullmatch(MENU_TEMPLATE, menu.custom_id)
        assert [o.value for o in menu.options] == [str(saved[n]) for n in GROUPS[0].roles]
        assert menu.max_values == 8 and menu.min_values == 1
        for view, group in zip(views[1:], GROUPS[1:]):
            ids = [b.custom_id for b in view.children]
            expected = [f"rolepick:btn:{group.key}:{saved[n]}" for n in group.roles]
            assert ids == expected + (["rolepick:all:pings"] if group.key == "pings" else [])
            assert all(re.fullmatch(BUTTON_TEMPLATE, i) for i in expected)
        assert re.fullmatch(EVERYTHING_TEMPLATE, views[3].children[-1].custom_id)
        assert views[3].children[-1].item.label == "Everything"
        assert "Posted the four role panels" in inter.followup.send.await_args.args[0]
    _run(tmp_path, body)


def test_panel_post_waits_for_rolesetup(tmp_path):
    async def body(db):
        cog = RolePicker.__new__(RolePicker)
        cog.bot = NS(db=db)
        channel = NS(mention="#x", send=AsyncMock())
        inter = _interaction(db, FakeGuild(), FakeMember(1), None)
        await RolePicker.rolepanel_post.callback(cog, inter, channel)
        assert channel.send.await_count == 0 and "Run `/rolesetup` first" in inter.followup.send.await_args.args[0]
    _run(tmp_path, body)


def test_items_rebuild_from_their_custom_ids():
    button = asyncio.run(RoleButton.from_custom_id(None, None, re.fullmatch(BUTTON_TEMPLATE, "rolepick:btn:pings:42")))
    assert (button.group_key, button.role_id) == ("pings", 42)
    menu = asyncio.run(RoleMenu.from_custom_id(None, None, re.fullmatch(MENU_TEMPLATE, "rolepick:menu:activity")))
    assert menu.group_key == "activity"
    assert re.fullmatch(BUTTON_TEMPLATE, "rolepick:menu:activity") is None
    every = asyncio.run(EverythingButton.from_custom_id(None, None, re.fullmatch(EVERYTHING_TEMPLATE, "rolepick:all:pings")))
    assert every.group_key == "pings"


# -- 4. picking -----------------------------------------------------------------------------------

def _staff():
    return NS(send=AsyncMock())


def test_picking_a_timezone_swaps_it_and_logs_without_pinging(tmp_path):
    async def body(db):
        guild = FakeGuild()
        await _set_up(db, guild)
        saved = await db.get_role_picker_roles(GUILD_ID)
        other = FakeRole(1, "Some Other Role")
        member = FakeMember(7, [guild.get_role(saved["Pacific"]), guild.get_role(saved["Mining"]), other])
        staff = _staff()
        inter = _interaction(db, guild, member, staff)
        await apply_pick(inter, "timezone", {saved["Eastern"]})
        assert {r.name for r in member.roles} == {"Eastern", "Mining", "Some Other Role"}
        assert inter.followup.send.await_args.args[0] == "Added **Eastern**. Removed **Pacific**."
        assert inter.followup.send.await_args.kwargs["ephemeral"] is True
        log = staff.send.await_args
        assert log.args[0] == "🎭 <@7> added **Eastern** and removed **Pacific**."
        assert log.kwargs["allowed_mentions"].users is False and log.kwargs["allowed_mentions"].roles is False
        assert staff.send.await_count == 1
    _run(tmp_path, body)


def test_the_dropdown_toggles_each_activity(tmp_path):
    async def body(db):
        guild = FakeGuild()
        await _set_up(db, guild)
        saved = await db.get_role_picker_roles(GUILD_ID)
        member = FakeMember(7, [guild.get_role(saved["Mining"])])
        inter = _interaction(db, guild, member, _staff())
        menu = RoleMenu("activity")
        menu.item._values = [str(saved["Mining"]), str(saved["FPS"])]
        await menu.callback(inter)
        assert [r.name for r in member.roles] == ["FPS"]
        assert inter.followup.send.await_args.args[0] == "Added **FPS**. Removed **Mining**."
    _run(tmp_path, body)


def test_everything_gives_every_ping_then_takes_them_all_off(tmp_path):
    async def body(db):
        guild = FakeGuild()
        await _set_up(db, guild)
        saved = await db.get_role_picker_roles(GUILD_ID)
        mining = guild.get_role(saved["Mining"])
        member, staff = FakeMember(7, [mining, guild.get_role(saved["Joint Ops"])]), _staff()
        inter = _interaction(db, guild, member, staff)
        await EverythingButton("pings").callback(inter)
        assert {r.name for r in member.roles} == {"Mining", "Org Events", "Patch & Server Status",
                                                  "Free Fly & Events", "Joint Ops"}
        assert inter.followup.send.await_args.args[0] == (
            "Added **Org Events**, **Patch & Server Status**, **Free Fly & Events**.")
        inter = _interaction(db, guild, member, staff)
        await EverythingButton("pings").callback(inter)
        assert member.roles == [mining]
        assert inter.followup.send.await_args.args[0].startswith("Removed **Org Events**, ")
        assert staff.send.await_count == 2
        inter = _interaction(db, guild, member, staff)
        await EverythingButton("timezone").callback(inter)   # no Everything there
        assert inter.followup.send.await_args.args[0] == OUT_OF_DATE and member.roles == [mining]
    _run(tmp_path, body)


def test_new_pilot_pings_mentor_and_only_mentor(tmp_path):
    async def body(db):
        guild = FakeGuild()
        await _set_up(db, guild)
        saved = await db.get_role_picker_roles(GUILD_ID)
        staff = _staff()
        member = FakeMember(7, [guild.get_role(saved["Veteran"])])
        await RoleButton("experience", saved["New Pilot"]).callback(_interaction(db, guild, member, staff))
        assert [r.name for r in member.roles] == ["New Pilot"]
        ping = staff.send.await_args_list[1]
        mentor = guild.get_role(saved["Mentor"])
        assert ping.args[0].startswith(f"👋 {mentor.mention} - <@7> just picked **New Pilot**")
        assert ping.kwargs["allowed_mentions"].roles == [mentor]
        assert ping.kwargs["allowed_mentions"].users is False and ping.kwargs["allowed_mentions"].everyone is False
        staff.send.reset_mock()
        await RoleButton("experience", saved["New Pilot"]).callback(_interaction(db, guild, member, staff))
        assert member.roles == [] and staff.send.await_count == 1   # removing it pings nobody
    _run(tmp_path, body)


def test_a_role_that_is_not_a_saved_picker_role_does_nothing(tmp_path):
    async def body(db):
        mod = FakeRole(1, "Mod")
        guild = FakeGuild([mod])
        await _set_up(db, guild)
        saved = await db.get_role_picker_roles(GUILD_ID)
        member, staff = FakeMember(7), _staff()
        for group, role_id in (("pings", mod.id), ("pings", saved["Eastern"]), ("nope", saved["Eastern"])):
            inter = _interaction(db, guild, member, staff)
            await apply_pick(inter, group, {role_id})
            replies = inter.followup.send.await_args_list + inter.response.send_message.await_args_list
            assert [c.args[0] for c in replies] == [OUT_OF_DATE]
        assert member.roles == [] and staff.send.await_count == 0
    _run(tmp_path, body)


def test_a_refused_role_change_says_so_and_logs_nothing(tmp_path):
    async def body(db):
        guild = FakeGuild()
        await _set_up(db, guild)
        saved = await db.get_role_picker_roles(GUILD_ID)
        member, staff = FakeMember(7), _staff()
        member.fail = True
        inter = _interaction(db, guild, member, staff)
        await apply_pick(inter, "pings", {saved["Joint Ops"]})
        assert "couldn't change your roles" in inter.followup.send.await_args.args[0]
        assert staff.send.await_count == 0
    _run(tmp_path, body)


def test_command_surface_fits_discords_limits():
    commands = [RolePicker.rolesetup, RolePicker.rolepanel, RolePicker.rolepanel_post]
    for cmd in commands:
        assert len(cmd.name) <= 32 and len(str(cmd.description)) <= 100
    for param in RolePicker.rolepanel_post.parameters:
        assert len(str(param.description)) <= 100
