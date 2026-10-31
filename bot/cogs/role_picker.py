"""The self-serve role picker: panels of buttons and a dropdown where members pick their own
roles (bot/uex/role_picker.py has the groups and the rules).

- /rolesetup (admins): makes sure every picker role exists - a role saved earlier is kept by
  id (so renaming it is fine), else one with the same name is used, else it's created plain (no
  permissions, not mentionable, no color but a Colors role's own). Never a duplicate. Saves the
  ids, and moves the Colors roles right under the voice rank roles.
- /rolepanel post (admins): posts the five panels in a channel.

The panels' buttons and dropdown are DynamicItems registered at startup, each carrying its
role's id (the dropdown's options carry theirs; Everything needs none), so they work through restarts for as long as
the message stays up. A pick is only acted on if that id is still one of the picker's saved
roles, so an old panel can't hand out anything else. Each pick gets a private "Added/Removed"
reply, and is logged in #role-log; picking New Pilot also pings Mentor in #👋welcome - once per
member ever (`new_pilot_pings`, shared with Onboarding), so toggling it can't ping Mentor again.
The dropdown answers a pick by re-sending the panel, which clears its ticks: Discord otherwise
keeps the last picks ticked, and picking again would toggle them off.

Roles picked in Discord's Onboarding (the same roles) never pass through the panels, so
on_member_update catches them: picker roles a member gained that a panel didn't just give
(PANEL_GRACE) are held for BATCH_SECONDS, so one onboarding is one #role-log line, and New Pilot
pings Mentor in #👋welcome with the member's timezone. Every other role - voice ranks, the veteran roles,
Mentor - is ignored. Staff giving a picker role by hand reads as Onboarding too.

Each member's first post in #👋welcome also gets a reply there pinging Mentor, so every intro
gets an answer. Who has posted there is kept in `welcome_intros`; the first start reads the
channel's history once, so only members new to it count.
"""
from __future__ import annotations

import asyncio
import logging
import time
from collections import defaultdict
from typing import Callable

import discord
from discord import app_commands
from discord.ext import commands, tasks

from bot.delivery import Delivery, fit_lines, send_to_channel
from bot.discord_ui import BotDynamicItem
from bot.managed_roles import harmless
from bot.uex.welcome import WELCOME_CHANNEL_ID
from bot.uex.role_picker import (
    CREATE, GROUPS, GROUPS_BY_KEY, MATCHED, MENTOR_ROLE, MULTI, NEW_PILOT_ROLE, ROLE_COLOURS, ROLE_EMOJI, RoleGroup,
    change_text, color_positions, everything_changes, intro_text, log_text, mentor_text, new_pilot_text,
    onboarding_log_text, picker_order, role_changes, setup_plan,
)
from bot.uex.voice_ranks import OP_RANKS, RANKS

logger = logging.getLogger("uexbot.role_picker")

# #role-log on Star Tech Dynamics: every pick is logged here (not #staff, which the owner keeps for
# moderation).
ROLE_LOG_CHANNEL_ID = 1565653911472439356
# The voice rank roles (bot/cogs/voice_ranks.py) the Colors roles go right under: found by the ids
# it remembers (managed_roles), by these names only for one it hasn't remembered yet.
RANK_ROLE_NAMES = {name for name, _ in RANKS + OP_RANKS}
# #👋welcome (WELCOME_CHANNEL_ID): Mentor is pinged there for a New Pilot and each member's first post.
BUTTON_TEMPLATE = r"rolepick:btn:(?P<group>[a-z]+):(?P<role>\d+)"
MENU_TEMPLATE = r"rolepick:menu:(?P<group>[a-z]+)"
EVERYTHING_TEMPLATE = r"rolepick:all:(?P<group>[a-z]+)"
OUT_OF_DATE = "This panel is out of date - ask staff to post a new one."
REASON = "Role picker"
PANEL_GRACE = 15.0   # seconds: a role the panels just gave isn't an Onboarding pick
BATCH_SECONDS = 5.0  # Onboarding's roles for one member, gathered into one #role-log line
INTRO_TYPES = (discord.MessageType.default, discord.MessageType.reply)

# (member id, role id) -> when a panel gave it (time.monotonic()), so on_member_update skips it.
_panel_adds: dict[tuple[int, int], float] = {}
# (guild id, member id, group key) -> the lock one member's picks in one group take turns on.
_pick_locks: defaultdict[tuple[int, int, str], asyncio.Lock] = defaultdict(asyncio.Lock)


def _note_panel_adds(member_id: int, role_ids: set[int]) -> None:
    now = time.monotonic()
    for key, at in list(_panel_adds.items()):
        if now - at > PANEL_GRACE:
            del _panel_adds[key]
    for role_id in role_ids:
        _panel_adds[(member_id, role_id)] = now


def _from_panel(member_id: int, role_id: int) -> bool:
    at = _panel_adds.pop((member_id, role_id), None)
    return at is not None and time.monotonic() - at <= PANEL_GRACE


async def ping_mentor(client, guild: discord.Guild, saved: dict[str, int], text: Callable[[str], str], *,
                      label: str, **send_kwargs) -> bool:
    """Post `text(mentor mention)` in #welcome, pinging Mentor and nobody else. True if it arrived."""
    mentor = guild.get_role(saved.get(MENTOR_ROLE, 0))
    if mentor is None:
        logger.warning("No Mentor role to ping - run /rolesetup")
        return False
    return await send_to_channel(client, WELCOME_CHANNEL_ID, text(mentor.mention), label=label,
                          allowed_mentions=discord.AllowedMentions(everyone=False, users=False, roles=[mentor],
                                                                   replied_user=False),
                          **send_kwargs) == Delivery.DELIVERED


def _is_admin(interaction: discord.Interaction) -> bool:
    perms = getattr(interaction.user, "guild_permissions", None)
    return interaction.guild is not None and perms is not None and perms.administrator


async def apply_pick(interaction: discord.Interaction, group_key: str, picked: set[int] | None, *,
                     reset: discord.ui.View | None = None) -> None:
    """Change the member's roles for a pick in one group, reply privately, log it in #role-log.
    `picked` None is the group's Everything button. `reset`: the panel's view to answer with (the
    dropdown's, so its ticks clear), the reply then following privately."""
    guild, member = interaction.guild, interaction.user
    group = GROUPS_BY_KEY.get(group_key)
    if guild is None or group is None:
        await interaction.response.send_message(OUT_OF_DATE, ephemeral=True)
        return
    if reset is not None:
        await interaction.response.edit_message(view=reset)
    else:
        await interaction.response.defer(ephemeral=True, thinking=True)
    saved = await interaction.client.db.get_role_picker_roles(guild.id)
    order = [saved[name] for name in group.roles if name in saved]
    everything = picked is None and group.everything
    if everything:
        picked = set(order)
    if not picked or not picked <= set(order) or any(guild.get_role(role_id) is None for role_id in picked):
        await interaction.followup.send(OUT_OF_DATE, ephemeral=True)
        return
    # A click carries the member's roles as they were when it was made, so two quick clicks on a
    # one-at-a-time panel each kept the other's new role (two Colors). One pick per member and
    # group at a time, worked out from the roles Discord has now.
    async with _pick_locks[(guild.id, member.id, group.key)]:
        try:
            member = await guild.fetch_member(member.id)
        except discord.HTTPException as exc:
            logger.info("Role picker couldn't re-read %s's roles, using the click's: %s", member.id, exc)
        held = {role.id for role in member.roles}
        add, remove = (everything_changes(set(order), held) if everything
                       else role_changes(group.mode, set(order), held, picked))
        to_add = [guild.get_role(role_id) for role_id in order if role_id in add]
        to_remove = [role for role in (guild.get_role(role_id) for role_id in order if role_id in remove) if role]
        _note_panel_adds(member.id, add)
        try:
            if to_remove:
                await member.remove_roles(*to_remove, reason=REASON)
            if to_add:
                await member.add_roles(*to_add, reason=REASON)
        except discord.HTTPException as exc:
            logger.warning("Role picker couldn't change %s's roles: %s", member.id, exc)
            await interaction.followup.send("I couldn't change your roles - staff will need to check my permissions.",
                                            ephemeral=True)
            return
    added, removed = [role.name for role in to_add], [role.name for role in to_remove]
    await interaction.followup.send(change_text(added, removed), ephemeral=True)
    if not added and not removed:
        return
    await send_to_channel(interaction.client, ROLE_LOG_CHANNEL_ID, log_text(member.mention, added, removed),
                          label="role picker log", allowed_mentions=discord.AllowedMentions.none())
    db = interaction.client.db
    if saved.get(NEW_PILOT_ROLE) in add and await db.claim_new_pilot_ping(guild.id, member.id):
        if not await ping_mentor(interaction.client, guild, saved, lambda mentor: mentor_text(mentor, member.mention),
                                 label="role picker mentor ping"):
            await db.release_new_pilot_ping(guild.id, member.id)  # didn't arrive: the next pick tries again


class RoleButton(BotDynamicItem, discord.ui.DynamicItem[discord.ui.Button], template=BUTTON_TEMPLATE):
    """A Timezone, Experience or Pings button; its custom id carries the role's id."""

    def __init__(self, group_key: str, role_id: int, label: str = "Role", emoji: str | None = None) -> None:
        super().__init__(discord.ui.Button(label=label, style=discord.ButtonStyle.secondary, emoji=emoji,
                                           custom_id=f"rolepick:btn:{group_key}:{role_id}"))
        self.group_key, self.role_id = group_key, role_id

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item: discord.ui.Button, match, /):
        return cls(match["group"], int(match["role"]))

    async def callback(self, interaction: discord.Interaction) -> None:
        await apply_pick(interaction, self.group_key, {self.role_id})


class EverythingButton(BotDynamicItem, discord.ui.DynamicItem[discord.ui.Button], template=EVERYTHING_TEMPLATE):
    """Every role in a group at once (Pings), or none once the member has them all."""

    def __init__(self, group_key: str) -> None:
        super().__init__(discord.ui.Button(label="Everything", style=discord.ButtonStyle.primary,
                                           custom_id=f"rolepick:all:{group_key}"))
        self.group_key = group_key

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item: discord.ui.Button, match, /):
        return cls(match["group"])

    async def callback(self, interaction: discord.Interaction) -> None:
        await apply_pick(interaction, self.group_key, None)


class RoleMenu(BotDynamicItem, discord.ui.DynamicItem[discord.ui.Select], template=MENU_TEMPLATE):
    """The Activities dropdown; each option's value is its role's id."""

    def __init__(self, group_key: str, options: list[discord.SelectOption] | None = None) -> None:
        options = options or [discord.SelectOption(label="-")]
        super().__init__(discord.ui.Select(custom_id=f"rolepick:menu:{group_key}", min_values=1,
                                           max_values=len(options), options=options,
                                           placeholder="Pick activities to add or remove"))
        self.group_key = group_key

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item: discord.ui.Select, match, /):
        return cls(match["group"], list(item.options) if item is not None else None)

    async def callback(self, interaction: discord.Interaction) -> None:
        picked = {int(value) for value in self.item.values if value.isdigit()}
        fresh = discord.ui.View(timeout=None)  # the same dropdown, nothing ticked
        fresh.add_item(RoleMenu(self.group_key, [
            discord.SelectOption(label=o.label, value=o.value, description=o.description, emoji=o.emoji)
            for o in self.item.options]))
        await apply_pick(interaction, self.group_key, picked, reset=fresh)


def panel(group: RoleGroup, roles: list[discord.Role]) -> tuple[str, discord.ui.View]:
    """One panel's text and its buttons or dropdown."""
    view = discord.ui.View(timeout=None)
    if group.mode == MULTI:
        view.add_item(RoleMenu(group.key, [discord.SelectOption(label=role.name, value=str(role.id))
                                           for role in roles]))
    else:
        for name, role in zip(group.roles, roles):
            # a renamed role's name can run to 100 characters, a button label to 80
            view.add_item(RoleButton(group.key, role.id, role.name[:80], ROLE_EMOJI.get(name)))
        if group.everything:
            view.add_item(EverythingButton(group.key))
    return f"**{group.title}**\n{group.blurb}", view


class RolePicker(commands.Cog):
    rolepanel = app_commands.Group(name="rolepanel", description="Admin: the role picker panels.",
                                   default_permissions=discord.Permissions(administrator=True), guild_only=True)

    def __init__(self, bot: commands.Bot, *, start_loop: bool = True) -> None:
        self.bot = bot
        self._pending: dict[int, set[int]] = {}  # member id -> Onboarding roles waiting for their log line
        if start_loop:
            self.read_welcome_history.start()

    def cog_unload(self) -> None:
        self.read_welcome_history.cancel()

    async def cog_load(self) -> None:
        self.bot.add_dynamic_items(RoleButton, RoleMenu, EverythingButton)

    # -- Onboarding -------------------------------------------------------------

    @commands.Cog.listener()
    async def on_member_update(self, before: discord.Member, after: discord.Member) -> None:
        added = {role.id for role in after.roles} - {role.id for role in before.roles}
        if added and not after.bot:
            await self.picked_outside_panels(after, added)

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member) -> None:
        added = {role.id for role in member.roles} - {member.guild.id}  # @everyone's id is the server's
        if added and not member.bot:
            await self.picked_outside_panels(member, added)

    async def picked_outside_panels(self, member: discord.Member, added: set[int]) -> None:
        """Log picker roles a member gained that no panel gave. The first change waits
        BATCH_SECONDS for the rest of the same onboarding, then logs them all in one line."""
        try:
            saved = await self.bot.db.get_role_picker_roles(member.guild.id)
            new = {role_id for role_id in picker_order(saved, added) if not _from_panel(member.id, role_id)}
            if not new:
                return
            if member.id in self._pending:  # already waiting: this joins that line
                self._pending[member.id] |= new
                return
            self._pending[member.id] = new
            try:
                await asyncio.sleep(BATCH_SECONDS)
            finally:
                gained = self._pending.pop(member.id, set())
            held = {role.id for role in member.roles}
            roles = [member.guild.get_role(role_id) for role_id in picker_order(saved, gained & held)]
            roles = [role for role in roles if role is not None]
            if not roles:
                return
            await send_to_channel(self.bot, ROLE_LOG_CHANNEL_ID,
                                  onboarding_log_text(member.mention, [role.name for role in roles]),
                                  label="onboarding log", allowed_mentions=discord.AllowedMentions.none())
            if (saved.get(NEW_PILOT_ROLE) in {role.id for role in roles}
                    and await self.bot.db.claim_new_pilot_ping(member.guild.id, member.id)):
                timezone = next((member.guild.get_role(saved[name]) for name in GROUPS_BY_KEY["timezone"].roles
                                 if saved.get(name) in held), None)
                if not await ping_mentor(self.bot, member.guild, saved,
                                         lambda mentor: new_pilot_text(mentor, member.mention,
                                                                       timezone.name if timezone else None),
                                         label="onboarding new pilot ping"):
                    await self.bot.db.release_new_pilot_ping(member.guild.id, member.id)
        except Exception:  # an event handler: log, never raise into discord.py
            logger.exception("Logging member %s's Onboarding roles failed", member.id)

    # -- #welcome intros --------------------------------------------------------

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        if (message.channel.id != WELCOME_CHANNEL_ID or message.guild is None or message.author.bot
                or message.type not in INTRO_TYPES):
            return
        try:
            guild, member = message.guild, message.author
            if (not await self.bot.db.welcome_intros_seeded(guild.id)
                    or not await self.bot.db.claim_welcome_intro(guild.id, member.id)):
                return
            saved = await self.bot.db.get_role_picker_roles(guild.id)
            perms = getattr(member, "guild_permissions", None)
            if (perms is not None and perms.manage_messages) or saved.get(MENTOR_ROLE) in {
                    role.id for role in getattr(member, "roles", [])}:
                return  # staff saying hi, not an intro
            await ping_mentor(self.bot, guild, saved, lambda mentor: intro_text(mentor, member.mention),
                              label="welcome intro ping",
                              reference=message.to_reference(fail_if_not_exists=False))
        except Exception:
            logger.exception("Welcome intro ping for member %s failed", message.author.id)

    @tasks.loop(count=1)
    async def read_welcome_history(self) -> None:
        """Once: everyone who has already posted in #welcome, so only new posters ping Mentor."""
        try:
            channel = self.bot.get_channel(WELCOME_CHANNEL_ID)
            if channel is None:
                logger.warning("Can't see #welcome (%s): no intro pings", WELCOME_CHANNEL_ID)
                return
            if await self.bot.db.welcome_intros_seeded(channel.guild.id):
                return
            authors = set()
            async for message in channel.history(limit=None):
                if not message.author.bot and message.type in INTRO_TYPES:
                    authors.add(message.author.id)
            await self.bot.db.seed_welcome_intros(channel.guild.id, authors)
            logger.info("#welcome read: %d members had posted there", len(authors))
        except Exception:
            logger.exception("Reading #welcome failed; it's tried again on the next start")

    @read_welcome_history.before_loop
    async def before_read_welcome_history(self) -> None:
        await self.bot.wait_until_ready()

    async def place_colors(self, guild: discord.Guild, ids: dict[str, int]) -> str:
        """Move the Colors roles right under the lowest voice rank role; what happened, for the
        /rolesetup reply ("" when they were already there)."""
        names = GROUPS_BY_KEY["colors"].roles
        if not all(name in ids for name in names):
            return ""
        roles = [role for role in await guild.fetch_roles() if role.id != guild.id]
        # The rank roles voice ranks remembers by id (a rename keeps them); by name, and only one
        # that grants nothing, for a rank it hasn't remembered yet (audit COMM-8).
        remembered = {name: role_id for name, role_id in (await self.bot.db.get_managed_roles(guild.id)).items()
                      if name in RANK_ROLE_NAMES}
        ranks = [role for role in roles if role.id in remembered.values()
                 or (role.name in RANK_ROLE_NAMES and role.name not in remembered and harmless(role))]
        if not ranks:
            return ("⚠️ There are no voice rank roles yet, so the color roles stay where they are - drag them above "
                    "any colored role you want a picked color to beat.")
        lowest = min(ranks, key=lambda role: role.position)
        moves = color_positions([(role.id, role.position) for role in roles], lowest.position,
                                [ids[name] for name in names])
        if not moves:
            return ""
        try:
            await guild.edit_role_positions({discord.Object(id=role_id): position
                                             for role_id, position in moves.items()},
                                            reason="Role picker setup: colors under the rank roles")
        except discord.HTTPException as exc:
            return (f"⚠️ I couldn't move the color roles ({exc}) - drag them right under **{lowest.name}** in "
                    "Server Settings > Roles.")
        return f"Moved the color roles right under **{lowest.name}**."

    @app_commands.command(name="rolesetup", description="Admin: create any missing role picker roles and save them.")
    @app_commands.default_permissions(administrator=True)
    @app_commands.guild_only()
    async def rolesetup(self, interaction: discord.Interaction) -> None:
        if not _is_admin(interaction):
            await interaction.response.send_message("This command is for server admins.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        guild = interaction.guild
        plan = setup_plan(await self.bot.db.get_role_picker_roles(guild.id),
                          [(role.id, role.name) for role in guild.roles])
        ids: dict[str, int] = {}
        created, matched, problems = [], [], []
        # One line each for a problem many roles can share (no Manage Roles, my role too low), or
        # 32 lines of it pass Discord's 2,000 characters and the reply naming the problem fails
        not_created: list[str] = []
        create_error = None
        with_permissions: list[str] = []
        above_me: list[str] = []
        for name, (action, role_id) in plan.items():
            if action == CREATE:
                try:
                    role = await guild.create_role(name=name, permissions=discord.Permissions.none(),
                                                   colour=discord.Colour(ROLE_COLOURS.get(name, 0)), hoist=False,
                                                   mentionable=False,
                                                   reason="Role picker setup (/rolesetup)")
                except discord.HTTPException as exc:
                    not_created.append(f"**{name}**")
                    create_error = create_error or exc
                    continue
                created.append(name)
            else:
                role = guild.get_role(role_id)
                if action == MATCHED:
                    matched.append(name)
                    if role.permissions.value:
                        with_permissions.append(f"**{role.name}**")
            ids[name] = role.id
            if name != MENTOR_ROLE and not role.is_assignable():
                above_me.append(f"**{role.name}**")
        if not_created:
            problems.append(f"Couldn't create {', '.join(not_created)}: {create_error}")
        if with_permissions:
            one = len(with_permissions) == 1
            problems.append(f"{', '.join(with_permissions)} already existed with permissions of "
                            f"{'its' if one else 'their'} own - picking {'it' if one else 'one'} gives them.")
        if above_me:
            one = len(above_me) == 1
            problems.append(f"{', '.join(above_me)} {'is' if one else 'are'} above my role, so I can't give "
                            f"{'it' if one else 'them'} - move my role above {'it' if one else 'them'}.")
        await self.bot.db.save_role_picker_roles(guild.id, ids)
        mentor = guild.get_role(ids.get(MENTOR_ROLE, 0))
        if mentor is not None and not mentor.mentionable and not guild.me.guild_permissions.mention_everyone:
            problems.append("I can't ping **Mentor** in #👋welcome yet: give my role \"Mention @everyone, @here and "
                            "All Roles\", or make Mentor mentionable.")
        lines = [f"Role picker roles: {len(ids)} of {len(plan)} ready."]
        if created:
            lines.append("Created: " + ", ".join(created))
        if matched:
            lines.append("Using existing: " + ", ".join(matched))
        lines += [f"⚠️ {problem}" for problem in problems]
        placed = await self.place_colors(guild, ids)
        if placed:
            lines.append(placed)
        footer = "Next: `/rolepanel post` in your roles channel." if len(ids) == len(plan) else None
        await interaction.followup.send(fit_lines(lines, footer=footer), ephemeral=True)

    @rolepanel.command(name="post", description="Admin: post the five role picker panels in a channel.")
    @app_commands.describe(channel="Where to post them, e.g. #pick-your-roles")
    async def rolepanel_post(self, interaction: discord.Interaction, channel: discord.TextChannel) -> None:
        if not _is_admin(interaction):
            await interaction.response.send_message("This command is for server admins.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        saved = await self.bot.db.get_role_picker_roles(interaction.guild.id)
        panels, missing = [], []
        for group in GROUPS:
            roles = [interaction.guild.get_role(saved.get(name, 0)) for name in group.roles]
            missing += [name for name, role in zip(group.roles, roles) if role is None]
            panels.append((group, [role for role in roles if role is not None]))
        if missing:
            await interaction.followup.send("Run `/rolesetup` first - these roles aren't set up: "
                                            + ", ".join(missing), ephemeral=True)
            return
        try:
            for group, roles in panels:
                content, view = panel(group, roles)
                await channel.send(content, view=view, allowed_mentions=discord.AllowedMentions.none())
        except discord.HTTPException as exc:
            await interaction.followup.send(f"I couldn't post in {channel.mention}: {exc}", ephemeral=True)
            return
        await interaction.followup.send(f"Posted the five role panels in {channel.mention}.", ephemeral=True)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(RolePicker(bot))
