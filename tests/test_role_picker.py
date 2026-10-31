"""The role picker (bot/uex/role_picker.py, bot/cogs/role_picker.py).

Guarantees under test:
  1. Rules - the dropdown and the Pings buttons toggle each pick; Timezone and Experience keep
     one role, picking another swaps it, picking the one you have removes it; picks outside the
     group are ignored. Pings' Everything adds the missing ones, or removes all once you have all.
  2. /rolesetup - creates only what's missing, plain (no permissions, not mentionable, no color but
     a Colors role's own), matches an existing role by name (any case), keeps a saved role by id
     even after a rename, never duplicates on a second run, warns about a role it can't give or
     can't ping; admins only. It puts the Colors roles right under the lowest voice rank role,
     moving nothing above it and nothing at all once they're there, and says so when it can't.
  3. /rolepanel post - five panels whose buttons and dropdown carry the saved role ids, matched
     by the startup-registered templates, the Colors buttons with their emoji; refuses until
     /rolesetup has run.
  4. Picking - changes only the member's roles in that group, replies privately, logs in #role-log
     without pinging the member, pings Mentor (only) in #welcome for New Pilot; an id that isn't a saved
     picker role does nothing; a refused role change says so and logs nothing.
  5. Onboarding - picker roles gained outside the panels are logged in #role-log, all of one
     onboarding in one line; New Pilot pings Mentor in #welcome with the member's timezone; a role a panel
     just gave, Mentor, any other role, or one taken off again before the line is written isn't.
  6. #welcome - a member's first post gets a reply there pinging Mentor, once; not before the history
     read, not for anyone who posted before it, not for staff, a join notice or another channel.
"""
from __future__ import annotations

import asyncio
import re
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock

import discord
import pytest
from cryptography.fernet import Fernet

import bot.cogs.role_picker as role_picker_cog
from bot.cogs.role_picker import (
    BUTTON_TEMPLATE, EVERYTHING_TEMPLATE, MENU_TEMPLATE, OUT_OF_DATE, ROLE_LOG_CHANNEL_ID, WELCOME_CHANNEL_ID,
    EverythingButton, RoleButton, RoleMenu, RolePicker, apply_pick,
)
from bot.db.database import Database
from bot.uex.role_picker import (
    ALL_ROLES, COLOR_ROLES, CREATE, GROUPS, KEPT, MATCHED, MULTI, ONE, ROLE_COLOURS, TOGGLE, change_text,
    color_positions, everything_changes, log_text, role_changes, setup_plan,
)

GUILD_ID = 1132378045232197694


@pytest.fixture(autouse=True)
def _no_panel_picks_from_other_tests():
    role_picker_cog._panel_adds.clear()


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
    assert [g.key for g in GROUPS] == ["activity", "timezone", "experience", "pings", "colors"]
    for group in GROUPS:
        # a dropdown takes 25 options; buttons wrap 5 to a row, 5 rows to a message
        assert len(group.roles) + group.everything <= 25
    assert [g.key for g in GROUPS if g.everything] == ["pings"]
    assert len(ALL_ROLES) == len(set(ALL_ROLES)) == 32
    assert GROUPS[-1].mode == ONE and GROUPS[-1].roles == tuple(name for name, _, _ in COLOR_ROLES)
    assert len(COLOR_ROLES) == 10 and len({colour for _, colour, _ in COLOR_ROLES}) == 10
    assert all(0 < colour <= 0xFFFFFF for _, colour, _ in COLOR_ROLES)


def test_color_positions_put_colors_right_under_the_anchor_and_keep_the_rest_in_order():
    # anchor 10 (the lowest rank role) and one role above it; X and Y are the colors
    roles = [(98, 11), (99, 10), (1, 9), (2, 8), (3, 3), (50, 2), (51, 1)]
    moves = color_positions(roles, 10, [50, 51])
    assert moves == {50: 9, 51: 8, 1: 7, 2: 6, 3: 5}
    after = dict(roles) | moves
    assert color_positions(list(after.items()), 10, [50, 51]) == {}   # already in place: nothing moves
    # a color above the anchor comes down too; nothing at or above the anchor otherwise moves
    assert color_positions([(99, 10), (50, 12), (1, 9)], 10, [50]) == {50: 9, 1: 8}


def test_color_positions_keep_discords_order_at_a_shared_position_and_never_go_below_one():
    # 7 and 8 share position 1: the newer (8) is lower, and stays lower
    assert color_positions([(7, 1), (8, 1), (50, 2)], 5, [50]) == {50: 4, 7: 3, 8: 2}
    # more roles than places under the anchor: the bottom ones share position 1
    moves = color_positions([(1, 2), (2, 1), (50, 1)], 3, [50])
    assert moves == {50: 2, 1: 1} and min(moves.values()) == 1


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
    def __init__(self, role_id, name, *, permissions=0, assignable=True, position=1):
        self.id, self.name, self.mentionable, self.position = role_id, name, False, position
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
        self.moves, self.move_error = [], None

    async def fetch_roles(self):
        return [FakeRole(self.id, "@everyone", position=0)] + list(self.roles)

    async def edit_role_positions(self, positions, *, reason=None):
        if self.move_error:
            raise self.move_error
        moves = {role.id: position for role, position in positions.items()}
        self.moves.append(moves)
        for role in self.roles:
            role.position = moves.get(role.id, role.position)

    def get_role(self, role_id):
        return next((r for r in self.roles if r.id == role_id), None)

    async def create_role(self, **kwargs):
        self.created.append(kwargs)
        for other in self.roles:   # like Discord: a new role goes in at the bottom, the rest move up
            other.position += 1
        role = FakeRole(5000 + len(self.roles), kwargs["name"])
        self.roles.append(role)
        return role


def _interaction(db, guild, user, staff, welcome=None):
    return NS(guild=guild, user=user,
              client=NS(db=db, get_channel={ROLE_LOG_CHANNEL_ID: staff, WELCOME_CHANNEL_ID: welcome}.get),
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
            assert kwargs["hoist"] is False
            assert kwargs["colour"] == discord.Colour(ROLE_COLOURS.get(kwargs["name"], 0))
        colours = {c["name"]: c["colour"] for c in guild.created}
        assert colours["Teal"].value == 0x2DD4BF and colours["Eastern"] == discord.Colour.default()
        saved = await db.get_role_picker_roles(GUILD_ID)
        assert saved["Mining"] == 10 and len(saved) == 32
        assert "Using existing: Mining" in reply and "32 of 32 ready" in reply
        guild.get_role(saved["Salvage"]).name = "Salvagers"   # renamed later: still the same role
        _, again = await _set_up(db, guild)
        assert len(guild.created) == 31 and await db.get_role_picker_roles(GUILD_ID) == saved
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


def _ranked_guild():
    return FakeGuild([FakeRole(900, "Admiral", position=6), FakeRole(901, "Operator", position=5),
                      FakeRole(902, "Star Citizen", position=4), FakeRole(903, "Mining", position=3)])


def test_rolesetup_puts_the_colors_right_under_the_rank_roles_once(tmp_path):
    async def body(db):
        guild = _ranked_guild()
        _, reply = await _set_up(db, guild)
        assert "Moved the color roles right under **Operator**" in reply
        saved = await db.get_role_picker_roles(GUILD_ID)
        by_id = {role.id: role for role in guild.roles}
        operator, admiral = by_id[901].position, by_id[900].position
        colours = [by_id[saved[name]].position for name, _, _ in COLOR_ROLES]
        assert colours == list(range(operator - 1, operator - 11, -1))   # panel order, top first
        assert admiral == operator + 1                                     # nothing above moved
        others = [role.position for role in guild.roles if role.id not in {900, 901, *map(saved.get, ROLE_COLOURS)}]
        assert max(others) < min(colours) and min(others) >= 1
        assert by_id[902].position > by_id[903].position                  # the rest keep their order
        _, again = await _set_up(db, guild)
        assert len(guild.moves) == 1 and "Moved" not in again and GUILD_ID not in guild.moves[0]
    _run(tmp_path, body)


def test_rolesetup_says_when_it_cant_move_the_colors(tmp_path):
    async def body(db):
        guild = _ranked_guild()
        guild.move_error = discord.Forbidden(NS(status=403, reason="Forbidden"), "Missing Permissions")
        _, reply = await _set_up(db, guild)
        assert "couldn't move the color roles" in reply and "right under **Operator**" in reply
        _, no_ranks = await _set_up(db, FakeGuild())
        assert "no voice rank roles yet" in no_ranks
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
        assert channel.send.await_count == 5
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
        assert [str(b.item.emoji) for b in views[4].children] == [emoji for _, _, emoji in COLOR_ROLES]
        assert views[4].children[0].item.label == "Orange" and views[1].children[0].item.emoji is None
        assert "Posted the five role panels" in inter.followup.send.await_args.args[0]
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
                                                  "Free Fly & Events", "Joint Ops", "Streams & Videos"}
        assert inter.followup.send.await_args.args[0] == (
            "Added **Org Events**, **Patch & Server Status**, **Free Fly & Events**, **Streams & Videos**.")
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
        staff, welcome = _staff(), _staff()
        member = FakeMember(7, [guild.get_role(saved["Veteran"])])
        await RoleButton("experience", saved["New Pilot"]).callback(_interaction(db, guild, member, staff, welcome))
        assert [r.name for r in member.roles] == ["New Pilot"]
        assert staff.send.await_count == 1 and welcome.send.await_count == 1   # the log, then the ping in #welcome
        ping = welcome.send.await_args
        mentor = guild.get_role(saved["Mentor"])
        assert ping.args[0].startswith(f"👋 {mentor.mention} - <@7> just picked **New Pilot**")
        assert ping.kwargs["allowed_mentions"].roles == [mentor]
        assert ping.kwargs["allowed_mentions"].users is False and ping.kwargs["allowed_mentions"].everyone is False
        staff.send.reset_mock()
        await RoleButton("experience", saved["New Pilot"]).callback(_interaction(db, guild, member, staff, welcome))
        assert member.roles == [] and staff.send.await_count == 1   # removing it pings nobody
        assert welcome.send.await_count == 1
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


# -- 5. Onboarding --------------------------------------------------------------------------------

def _listener(db, guild, staff, welcome):
    channels = {ROLE_LOG_CHANNEL_ID: staff, WELCOME_CHANNEL_ID: welcome}
    return RolePicker(NS(db=db, get_channel=channels.get), start_loop=False)


def _member(guild, user_id, roles=()):
    member = FakeMember(user_id, roles)
    member.guild, member.bot = guild, False
    member.guild_permissions = discord.Permissions.none()
    return member


async def _gain(cog, member, *roles):
    before = NS(roles=list(member.roles))
    member.roles += list(roles)
    await cog.on_member_update(before, member)


def test_onboarding_roles_are_logged_in_one_line_and_new_pilot_pings_mentor(tmp_path, monkeypatch):
    monkeypatch.setattr(role_picker_cog, "BATCH_SECONDS", 0.05)

    async def body(db):
        guild = FakeGuild()
        await _set_up(db, guild)
        saved = await db.get_role_picker_roles(GUILD_ID)
        role = lambda name: guild.get_role(saved[name])  # noqa: E731
        staff, welcome = _staff(), _staff()
        cog, member = _listener(db, guild, staff, welcome), _member(guild, 7)
        # Onboarding's roles can arrive as more than one update; one line either way.
        await asyncio.gather(_gain(cog, member, role("New Pilot"), role("Mining")),
                             _gain(cog, member, role("Eastern")))
        log = staff.send.await_args
        assert staff.send.await_count == 1
        assert log.args[0] == "🎭 <@7> picked **Mining**, **Eastern**, **New Pilot** via Onboarding."
        assert log.kwargs["allowed_mentions"].roles is False and log.kwargs["allowed_mentions"].users is False
        mentor = role("Mentor")
        ping = welcome.send.await_args
        assert welcome.send.await_count == 1
        assert ping.args[0] == f"👋 {mentor.mention} New pilot joined: <@7> (Eastern). Say hi and invite them to the next op."
        assert ping.kwargs["allowed_mentions"].roles == [mentor] and ping.kwargs["allowed_mentions"].users is False
        # Without a timezone, and without New Pilot: no brackets, no ping.
        other = _member(guild, 8)
        await _gain(cog, other, role("New Pilot"))
        assert welcome.send.await_args.args[0].endswith("New pilot joined: <@8>. Say hi and invite them to the next op.")
        await _gain(cog, other, role("Salvage"))
        assert staff.send.await_count == 3 and welcome.send.await_count == 2
        assert staff.send.await_args.args[0] == "🎭 <@8> picked **Salvage** via Onboarding."
    _run(tmp_path, body)


def test_panel_picks_and_other_roles_are_not_logged_as_onboarding(tmp_path, monkeypatch):
    monkeypatch.setattr(role_picker_cog, "BATCH_SECONDS", 0)

    async def body(db):
        guild = FakeGuild([FakeRole(1, "Cadet"), FakeRole(2, "1-Year Veteran")])
        await _set_up(db, guild)
        saved = await db.get_role_picker_roles(GUILD_ID)
        staff = _staff()
        cog, member = _listener(db, guild, staff, _staff()), _member(guild, 7)
        before = NS(roles=list(member.roles))
        await apply_pick(_interaction(db, guild, member, staff), "timezone", {saved["Eastern"]})
        await cog.on_member_update(before, member)   # the gateway event for the panel's own change
        assert staff.send.await_count == 1             # just the panel's line
        await _gain(cog, member, guild.get_role(1), guild.get_role(2), guild.get_role(saved["Mentor"]))
        assert staff.send.await_count == 1             # a voice rank, a veteran role, Mentor
        # A panel's role is only its own once: the same role given later is Onboarding.
        await member.remove_roles(guild.get_role(saved["Eastern"]))
        await _gain(cog, member, guild.get_role(saved["Eastern"]))
        assert staff.send.await_args.args[0] == "🎭 <@7> picked **Eastern** via Onboarding."
    _run(tmp_path, body)


def test_an_onboarding_role_taken_off_before_the_line_is_written_is_left_out(tmp_path, monkeypatch):
    monkeypatch.setattr(role_picker_cog, "BATCH_SECONDS", 0.05)

    async def body(db):
        guild = FakeGuild()
        await _set_up(db, guild)
        saved = await db.get_role_picker_roles(GUILD_ID)
        staff = _staff()
        cog, member = _listener(db, guild, staff, _staff()), _member(guild, 7)
        pilot = guild.get_role(saved["New Pilot"])

        async def change_mind():
            await asyncio.sleep(0.01)
            await member.remove_roles(pilot)
        await asyncio.gather(_gain(cog, member, pilot), change_mind())
        assert staff.send.await_count == 0
        joined = _member(guild, 9, [guild.get_role(saved["FPS"])])
        await cog.on_member_join(joined)   # roles already there when the join event comes
        assert staff.send.await_args.args[0] == "🎭 <@9> picked **FPS** via Onboarding."
    _run(tmp_path, body)


# -- 6. #welcome ----------------------------------------------------------------------------------

def _post(guild, author, *, channel_id=WELCOME_CHANNEL_ID, kind=discord.MessageType.default):
    post = NS(channel=NS(id=channel_id), guild=guild, author=author, type=kind)
    post.to_reference = lambda fail_if_not_exists: NS(message=post, fail_if_not_exists=fail_if_not_exists)
    return post


def test_a_first_post_in_welcome_gets_a_reply_pinging_mentor_once(tmp_path):
    async def body(db):
        guild = FakeGuild()
        await _set_up(db, guild)
        saved = await db.get_role_picker_roles(GUILD_ID)
        staff, welcome = _staff(), _staff()
        cog = _listener(db, guild, staff, welcome)
        old, new = _member(guild, 8), _member(guild, 9)
        await cog.on_message(_post(guild, new))
        assert welcome.send.await_count == 0                  # history not read yet
        await db.seed_welcome_intros(GUILD_ID, {old.id})
        await cog.on_message(_post(guild, old))
        await cog.on_message(_post(guild, new, channel_id=123))
        await cog.on_message(_post(guild, new, kind=discord.MessageType.new_member))
        assert welcome.send.await_count == 0
        hello = _post(guild, new)
        await cog.on_message(hello)
        mentor = guild.get_role(saved["Mentor"])
        call = welcome.send.await_args
        assert call.args[0] == f"👋 {mentor.mention} - first hello from <@9>! Give them a reply."
        assert call.kwargs["reference"].message is hello and call.kwargs["reference"].fail_if_not_exists is False
        mentions = call.kwargs["allowed_mentions"]
        assert mentions.roles == [mentor] and mentions.users is False and mentions.replied_user is False
        await cog.on_message(_post(guild, new, kind=discord.MessageType.reply))
        staffer, mentor_member = _member(guild, 10), _member(guild, 11, [mentor])
        staffer.guild_permissions = discord.Permissions(manage_messages=True)
        await cog.on_message(_post(guild, staffer))
        await cog.on_message(_post(guild, mentor_member))
        assert welcome.send.await_count == 1 and staff.send.await_count == 0
    _run(tmp_path, body)


def test_the_history_read_records_who_already_posted(tmp_path):
    async def body(db):
        guild = FakeGuild()
        await _set_up(db, guild)
        staff = _staff()
        posts = [NS(author=NS(id=8, bot=False), type=discord.MessageType.default),
                 NS(author=NS(id=9, bot=False), type=discord.MessageType.new_member),
                 NS(author=NS(id=10, bot=True), type=discord.MessageType.default)]

        async def history(limit):
            for post in posts:
                yield post
        welcome = NS(guild=guild, history=history)
        cog = RolePicker(NS(db=db, get_channel=lambda cid: {ROLE_LOG_CHANNEL_ID: staff, WELCOME_CHANNEL_ID: welcome}.get(cid)),
                         start_loop=False)
        await cog.read_welcome_history.coro(cog)
        assert await db.welcome_intros_seeded(GUILD_ID)
        assert not await db.claim_welcome_intro(GUILD_ID, 8)
        assert await db.claim_welcome_intro(GUILD_ID, 9)    # only joined - their first post still counts
        assert await db.claim_welcome_intro(GUILD_ID, 10)
    _run(tmp_path, body)


def test_command_surface_fits_discords_limits():
    commands = [RolePicker.rolesetup, RolePicker.rolepanel, RolePicker.rolepanel_post]
    for cmd in commands:
        assert len(cmd.name) <= 32 and len(str(cmd.description)) <= 100
    for param in RolePicker.rolepanel_post.parameters:
        assert len(str(param.description)) <= 100
