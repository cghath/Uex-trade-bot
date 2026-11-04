"""New members (rules and wording in bot/uex/new_members.py): a temporary "🌱 New Member" role,
/new-members for the owner and moderators, and a weekly note to the owner.

The role: given on join and taken off once a member has been here NEW_MEMBER_DAYS, by Discord's
own join date - so nothing is lost on a restart, and a check every RECONCILE_MINUTES puts it
right (that check also gave it, on the first start, to everyone who joined in the last
NEW_MEMBER_DAYS). It's the bot's own role (bot/managed_roles.py): no permissions, not mentionable,
and shown separately in the member list (hoisted) when it's made - staff can change that in
Server Settings, and the bot never changes it back. A member list groups each member under their
highest hoisted role, so the role sits wherever staff drag it.

/new-members: private, for the owner and anyone with Moderate Members (the moderators). Newest
first, each with their join date, where they came from (server analytics' join source), their
Onboarding picks, whether they've posted or been in voice, and who has reached out. The menu
under it marks "reached out", so the owner and moderators don't all DM the same person.

The weekly note: Mondays from 10 AM Eastern, a DM to the owner naming new members still waiting
for a hello - only when there are any.
"""
from __future__ import annotations

import io
import logging
from datetime import date, datetime, timedelta

import discord
from discord import app_commands
from discord.ext import commands, tasks

from bot.cogs.anniversaries import ANNIVERSARY_CHANNEL_ID
from bot.cogs.server_analytics import _inviter
from bot.delivery import Delivery, home_channel, send_dm
from bot.discord_ui import LAYOUT_TEXT_LIMIT, BotLayoutView
from bot.managed_roles import ManagedRoles
from bot.uex.new_members import (
    DRIFTING, EMPTY, FLAGS, MAX_DAYS, NEEDS_HELLO, NEW_MEMBER_DAYS, NEW_MEMBER_ROLE, PAGE_SIZE, SETTLING, NewMember,
    breakdown_text, flag, is_new, member_picks, onboarding_status, member_text, members_csv, summary_text, week_key, weekly_text,
)
from bot.uex.server_analytics import EASTERN, MEMBER_REFERRAL, TIME_FORMAT, UTC, source_of

logger = logging.getLogger("uexbot.new_members")

RECONCILE_MINUTES = 30
WEEKLY_DAY, WEEKLY_HOUR = 0, 10  # Monday, 10 AM Eastern
NO_MENTIONS = discord.AllowedMentions.none()


def _parse(text: str) -> datetime:
    return datetime.strptime(text, TIME_FORMAT).replace(tzinfo=UTC)


def _onboarding(member) -> str:
    flags = getattr(member, "flags", None)
    return onboarding_status(member.joined_at, bool(getattr(flags, "started_onboarding", False)),
                             bool(getattr(flags, "completed_onboarding", False)))


def latest(last_active: datetime | None, last_day: date | None) -> datetime | None:
    """Their last activity: the exact last post or voice join, or the start of the last day the
    activity log has them (voice ranks credit minutes all session long), whichever is later."""
    day = datetime.combine(last_day, datetime.min.time(), tzinfo=UTC) if last_day else None
    return max((t for t in (last_active, day) if t is not None), default=None)


class NewMembersView(BotLayoutView):
    """The list, a page at a time, with a menu to mark who's been reached out to."""

    def __init__(self, cog: "NewMembers", guild: discord.Guild, members: list[NewMember], days: int,
                 now: datetime, showing: str | None = None) -> None:
        super().__init__(timeout=600)  # ephemeral: editable only within the click's 15 minutes
        self.cog, self.guild, self.everyone, self.days, self.now = cog, guild, members, days, now
        self.showing = showing
        self.members = [m for m in members if showing is None or flag(m, now) == showing]
        self.page = 0
        self.pages = self._paginate()
        self.previous_button = discord.ui.Button(label="Newer", style=discord.ButtonStyle.secondary)
        self.previous_button.callback = self._previous
        self.next_button = discord.ui.Button(label="Older", style=discord.ButtonStyle.secondary)
        self.next_button.callback = self._next
        self.render()

    @property
    def page_count(self) -> int:
        return len(self.pages)

    def shown(self) -> list[NewMember]:
        return self.pages[self.page]

    def _head(self, page: int, count: int) -> str:
        head = summary_text(self.everyone, self.now, self.days, self.showing)
        if page == 0 and self.everyone:
            head += "\n" + breakdown_text(self.everyone, self.now)
        if count > 1:
            head += f"\n-# Page {page + 1} of {count}"
        return head

    def _paginate(self) -> list[list[NewMember]]:
        """Pages of up to PAGE_SIZE members that each fit Discord's text limit with their header,
        so a long member never falls off a page (and out of reach of its menu)."""
        pages: list[list[NewMember]] = []
        page: list[NewMember] = []
        used = 0
        for member in self.members:
            block = len(member_text(member, self.now))
            room = LAYOUT_TEXT_LIMIT - len(self._head(len(pages), 99))
            if page and (len(page) >= PAGE_SIZE or used + block > room):
                pages.append(page)
                page, used = [], 0
            page.append(member)
            used += block
        if page:
            pages.append(page)
        return pages or [[]]

    def render(self) -> None:
        self.clear_items()
        container = discord.ui.Container(accent_colour=discord.Colour.green())
        container.add_item(discord.ui.TextDisplay(self._head(self.page, self.page_count)))
        for member in self.shown():
            container.add_item(discord.ui.Separator())
            container.add_item(discord.ui.TextDisplay(member_text(member, self.now)))
        if not self.members:
            nobody = (EMPTY[self.showing] if self.showing and self.everyone
                      else f"Nobody joined in the last {self.days} day{'s' if self.days != 1 else ''}.")
            container.add_item(discord.ui.TextDisplay(nobody))
        self.add_item(container)
        if self.shown():
            menu = discord.ui.Select(placeholder="Mark as reached out…", min_values=1, max_values=1, options=[
                discord.SelectOption(label=self.cog.name(self.guild, m.user_id)[:100], value=str(m.user_id),
                                     description=("Reached out: " + m.reached_by)[:100] if m.reached_by else None)
                for m in self.shown()])
            menu.callback = self._mark
            self.menu = menu
            self.add_item(discord.ui.ActionRow(menu))
        if self.page_count > 1:
            self.previous_button.disabled = self.page == 0
            self.next_button.disabled = self.page >= self.page_count - 1
            self.add_item(discord.ui.ActionRow(self.previous_button, self.next_button))

    async def _turn(self, interaction: discord.Interaction, delta: int) -> None:
        self.page = max(0, min(self.page_count - 1, self.page + delta))
        self.render()
        await interaction.response.edit_message(view=self, allowed_mentions=NO_MENTIONS)

    async def _previous(self, interaction: discord.Interaction) -> None:
        await self._turn(interaction, -1)

    async def _next(self, interaction: discord.Interaction) -> None:
        await self._turn(interaction, 1)

    async def _mark(self, interaction: discord.Interaction) -> None:
        user_id = int(self.menu.values[0])
        now = self.cog.now()
        await self.cog.bot.db.mark_new_member_contacted(self.guild.id, user_id, interaction.user.id,
                                                        now.strftime(TIME_FORMAT))
        for member in self.members:
            if member.user_id == user_id:
                member.reached_by, member.reached_at = interaction.user.display_name, now
        self.pages = self._paginate()
        self.page = min(self.page, self.page_count - 1)
        self.render()
        await interaction.response.edit_message(view=self, allowed_mentions=NO_MENTIONS)


class NewMembers(commands.Cog):
    def __init__(self, bot: commands.Bot, *, start_loop: bool = True) -> None:
        self.bot = bot
        self.roles = ManagedRoles(bot, reason="New member")
        self.now = lambda: datetime.now(UTC)  # the clock; tests set their own
        self._role_warned = False
        if start_loop:
            self.reconcile.start()
            self.weekly.start()

    def cog_unload(self) -> None:
        self.reconcile.cancel()
        self.weekly.cancel()

    def _guild(self) -> discord.Guild | None:
        channel = home_channel(self.bot, ANNIVERSARY_CHANNEL_ID, feature="new member roles")
        return channel.guild if channel is not None else None

    async def _role(self, guild: discord.Guild):
        return await self.roles.get(guild, NEW_MEMBER_ROLE, hoist=True, mentionable=False)

    def _warn(self, exc: Exception) -> None:
        if not self._role_warned:
            logger.warning("Couldn't give or take the %s role (the bot needs Manage Roles, above that role): %s",
                           NEW_MEMBER_ROLE, exc)
        self._role_warned = True

    # -- the role -------------------------------------------------------------

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member) -> None:
        guild = self._guild()
        if guild is None or member.guild.id != guild.id or member.bot:
            return
        if getattr(member, "pending", False):  # still on Rules Screening: given once they pass it
            return
        await self._give(guild, member)

    @commands.Cog.listener()
    async def on_member_update(self, before: discord.Member, after: discord.Member) -> None:
        """Rules Screening passed: the role now, like the welcome DM (welcome.py)."""
        guild = self._guild()
        if (guild is None or after.guild.id != guild.id or after.bot or not getattr(before, "pending", False)
                or getattr(after, "pending", False) or not is_new(after.joined_at, self.now())):
            return
        await self._give(guild, after)

    async def _give(self, guild: discord.Guild, member: discord.Member) -> None:
        try:
            await member.add_roles(await self._role(guild), reason="New member")
        except discord.HTTPException as exc:
            self._warn(exc)
        except Exception:
            logger.exception("Giving member %s the %s role failed", member.id, NEW_MEMBER_ROLE)

    @tasks.loop(minutes=RECONCILE_MINUTES)
    async def reconcile(self) -> None:
        try:
            guild = self._guild()
            if guild is not None:
                await self.put_roles_right(guild, self.now())
        except Exception:
            logger.exception("Checking the %s role failed", NEW_MEMBER_ROLE)

    @reconcile.before_loop
    async def before_reconcile(self) -> None:
        await self.bot.wait_until_ready()

    async def put_roles_right(self, guild: discord.Guild, now: datetime) -> tuple[int, int]:
        """The role on everyone here under NEW_MEMBER_DAYS, and off everyone else: (given, taken)."""
        role = await self._role(guild)
        given = taken = 0
        for member in guild.members:
            if member.bot:
                continue
            new, has = is_new(member.joined_at, now), role in member.roles
            try:
                if new and not has and not getattr(member, "pending", False):
                    await member.add_roles(role, reason="New member")
                    given += 1
                elif has and not new:
                    await member.remove_roles(role, reason=f"Here {NEW_MEMBER_DAYS} days")
                    taken += 1
            except discord.HTTPException as exc:
                self._warn(exc)
                break
        else:
            self._role_warned = False
        if given or taken:
            logger.info("%s role: given to %d, taken from %d", NEW_MEMBER_ROLE, given, taken)
        return given, taken

    # -- the list -------------------------------------------------------------

    def name(self, guild: discord.Guild, user_id: int) -> str:
        who = guild.get_member(user_id) or self.bot.get_user(user_id)
        return (getattr(who, "display_name", None) or who.name) if who is not None else str(user_id)

    async def gather(self, guild: discord.Guild, days: int, now: datetime, *,
                     include_left: bool = False) -> list[NewMember]:
        """Everyone here who joined in the last `days`, newest first - and with `include_left`, those
        who joined in that time and have gone (from the join log; the CSV only)."""
        here = [m for m in guild.members if not m.bot and is_new(m.joined_at, now, days)]
        if not here and not include_left:
            return []
        data = await self.bot.db.new_member_data(guild.id, (now - timedelta(days=days + 1)).strftime(TIME_FORMAT))
        joins = {row["user_id"]: row for row in data["joins"]}  # the latest join wins
        people = [(m.id, m.joined_at, {r.id for r in m.roles}, m.name, m.display_name, True, _onboarding(m))
                  for m in here]
        if include_left:
            for user_id, row in joins.items():
                at = _parse(row["at"])
                if guild.get_member(user_id) is None and is_new(at, now, days):
                    user = self.bot.get_user(user_id)
                    people.append((user_id, at, set(), getattr(user, "name", ""),
                                   getattr(user, "display_name", "") or getattr(user, "name", ""), False, None))
        people.sort(key=lambda p: p[1], reverse=True)
        active: dict[int, list[date]] = {}
        for row in data["active"]:
            active.setdefault(row["user_id"], []).append(date.fromisoformat(row["day"]))
        contacts = {row["user_id"]: row for row in data["contacts"]}
        last_active = {row["user_id"]: _parse(row["last_active"]) for row in data["last_active"]}
        voice: dict[int, list[tuple[date, int]]] = {}
        for row in data["voice"]:
            voice.setdefault(row["user_id"], []).append((date.fromisoformat(row["day"]), row["minutes"]))
        invites = None
        picker = await self.bot.db.get_role_picker_roles(guild.id)
        members = []
        for user_id, joined_at, held, username, display, still_here, onboarding in people:
            source = inviter_name = None
            row = joins.get(user_id)
            if row is not None:
                inviter = row["inviter_id"]
                if inviter is None and row["invite_code"] and row["invite_code"] not in data["labels"]:
                    if invites is None:  # joins from before inviters were kept: the live invite says
                        try:
                            invites = {i.code: _inviter(i) for i in await guild.invites()}
                        except discord.HTTPException:
                            invites = {}
                    inviter = invites.get(row["invite_code"])
                source = source_of(row["invite_code"], data["labels"], inviter)
                if source == MEMBER_REFERRAL and inviter is not None:
                    inviter_name = self.name(guild, inviter)
            zone, activities, experience = member_picks(held, picker)
            seen = sorted(d for d in active.get(user_id, []) if d >= joined_at.date())
            contact = contacts.get(user_id)
            if contact is not None and _parse(contact["at"]) < joined_at:
                contact = None  # from a stay before they left and came back
            members.append(NewMember(
                user_id=user_id, joined_at=joined_at, source=source,
                picks=[*activities, *([zone] if zone else []), *([experience] if experience else [])],
                first_active=seen[0] if seen else None, active_days=len(set(seen)),
                voice_minutes=sum(m for d, m in voice.get(user_id, []) if d >= joined_at.date()),
                last_active=latest(last_active.get(user_id), seen[-1] if seen else None),
                reached_by=self.name(guild, contact["by_id"]) if contact else None,
                reached_at=_parse(contact["at"]) if contact else None,
                timezone=zone, activities=activities, experience=experience, inviter=inviter_name,
                username=username, display_name=display, still_here=still_here, onboarding=onboarding))
        return members

    async def _allowed(self, interaction: discord.Interaction, guild: discord.Guild) -> bool:
        """The owner, or a moderator of Star Tech Dynamics itself - Moderate Members in another
        server the bot is in doesn't count, since the list is always this server's."""
        if await self.bot.is_owner(interaction.user):
            return True
        perms = getattr(interaction.user, "guild_permissions", None)
        return interaction.guild_id == guild.id and bool(perms and perms.moderate_members)

    @app_commands.command(name="new-members",
                          description="Moderators: members who joined lately, whether they've said hello, and who reached out.")
    @app_commands.describe(days=f"How far back, in days (default {NEW_MEMBER_DAYS})",
                           status="Show only members with this flag (default: everyone)",
                           reached_out="Mark that you've reached out to this member",
                           export="Owner only: get the list as a CSV file instead")
    @app_commands.choices(status=[
        app_commands.Choice(name="🔴 Needs a hello", value=NEEDS_HELLO),
        app_commands.Choice(name="🟡 Drifting", value=DRIFTING),
        app_commands.Choice(name="🟢 Settling in", value=SETTLING),
        app_commands.Choice(name="Everyone", value="all")])
    @app_commands.default_permissions(moderate_members=True)  # the owner and moderators
    async def new_members(self, interaction: discord.Interaction,
                          days: app_commands.Range[int, 1, MAX_DAYS] = NEW_MEMBER_DAYS,
                          status: str | None = None, reached_out: discord.Member | None = None,
                          export: bool = False) -> None:
        guild = self._guild()
        if guild is None:
            await interaction.response.send_message("I can't find the server right now.", ephemeral=True)
            return
        if not await self._allowed(interaction, guild):
            await interaction.response.send_message("This command is for the owner and moderators.", ephemeral=True)
            return
        if export and not await self.bot.is_owner(interaction.user):
            await interaction.response.send_message("The CSV export is for the bot's owner.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        now = self.now()
        if reached_out is not None:
            await self.bot.db.mark_new_member_contacted(guild.id, reached_out.id, interaction.user.id,
                                                        now.strftime(TIME_FORMAT))
        members = await self.gather(guild, days, now)
        showing = status if status in FLAGS else None
        if export:
            rows = await self.gather(guild, days, now, include_left=showing is None)
            rows = [m for m in rows if showing is None or (m.still_here and flag(m, now) == showing)]
            stamp = now.astimezone(EASTERN).date().isoformat()
            file = discord.File(io.BytesIO(members_csv(rows, now).encode("utf-8-sig")), filename=f"new-members-{stamp}.csv")
            await interaction.followup.send(
                f"New members, last {days} days: {len(rows)} row{'s' if len(rows) != 1 else ''}"
                + (f", {FLAGS[showing][1].lower()} only" if showing else ", including any who have left") + ".",
                file=file, ephemeral=True)
            return
        view = NewMembersView(self, guild, members, days, now, showing)
        view.origin = interaction  # the first followup after a defer is the original reply: greys out at timeout
        await interaction.followup.send(view=view, ephemeral=True, allowed_mentions=NO_MENTIONS)

    # -- the weekly note ------------------------------------------------------

    @tasks.loop(hours=1)
    async def weekly(self) -> None:
        try:
            await self.send_weekly(self.now())
        except Exception:
            logger.exception("The weekly new member note failed")

    @weekly.before_loop
    async def before_weekly(self) -> None:
        await self.bot.wait_until_ready()

    async def send_weekly(self, now: datetime) -> bool:
        """Mondays from WEEKLY_HOUR Eastern, once a week: the new members who need a hello or are
        drifting, by DM to the owner - nothing when there are none. Built before it's claimed, and
        the claim is let go when the DM fails for a reason that may pass, so a hiccup costs an hour,
        not the week."""
        local = now.astimezone(EASTERN)
        guild = self._guild()
        if guild is None or local.weekday() != WEEKLY_DAY or local.hour < WEEKLY_HOUR:
            return False
        week = week_key(now)
        previous = await self.bot.db.new_member_week(guild.id)
        if previous is not None and previous >= week:
            return False
        text = weekly_text(await self.gather(guild, NEW_MEMBER_DAYS, now), now)
        if not await self.bot.db.claim_new_member_week(guild.id, week):
            return False
        if text is None:
            return False
        info = await self.bot.application_info()
        owner = info.team.owner_id if info.team and info.team.owner_id else info.owner.id
        outcome = await send_dm(self.bot, owner, text, label="weekly new members", allowed_mentions=NO_MENTIONS)
        if outcome is Delivery.RETRY:
            await self.bot.db.release_new_member_week(guild.id, week, previous)
        return outcome is Delivery.DELIVERED


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(NewMembers(bot))
