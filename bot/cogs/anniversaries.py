"""Server anniversaries: a DM and a post in the bot channel on the day a member joined, at the
time they joined (bot/uex/anniversaries.py has the timing and wording). Needs the Server Members
intent, switched on in the Developer Portal, to see who's a member and when they joined.

A check runs every CHECK_MINUTES. A member whose anniversary moment has passed (within the
catch-up window) is claimed in the database first, then their DM and post are sent; each part
that fails only temporarily is tried again on the next check (bot/delivery.py's outcomes), so a
restart or an outage neither repeats one nor loses it. The DM's "Stop anniversary messages"
button works for good, through restarts (a DynamicItem).

Each anniversary also gives the member that year's veteran role ("1-Year Veteran", ...),
replacing the last; the roles are created by name when first needed (Manage Roles, which voice
ranks needs too). Every VETERAN_CHECK_HOURS - and at startup - everyone's veteran role is put
right quietly: that gave the members already past an anniversary theirs when this shipped, and
fixes a role change that failed. A member who stopped the messages still gets the role. The DM
ends with the server's next Discord event (next_event_line), when there is one.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

import discord
from discord import app_commands
from discord.ext import commands, tasks

from bot.delivery import Delivery, owner_only_mentions, send_dm, send_to_channel
from bot.uex.anniversaries import (
    COMMUNITY_NAME, Event, due_anniversary, dm_text, event_line, is_veteran_role, post_text, upcoming,
    veteran_role_name, years_completed,
)

logger = logging.getLogger("uexbot.anniversaries")

# #stdynamics-bot on Star Tech Dynamics: where the posts go, and whose members are celebrated.
ANNIVERSARY_CHANNEL_ID = 1539831863965913198
CHECK_MINUTES = 5
VETERAN_CHECK_HOURS = 6
STOP_TEMPLATE = r"anniversary:stop:(?P<guild>\d+)"
RESUME_TEMPLATE = r"anniversary:resume:(?P<guild>\d+)"
_STATUS = {Delivery.DELIVERED: "sent", Delivery.UNDELIVERABLE: "skipped"}  # RETRY stays pending


def _name(member: discord.abc.User) -> str:
    return discord.utils.escape_mentions(discord.utils.escape_markdown(member.display_name))


class StopAnniversariesButton(discord.ui.DynamicItem[discord.ui.Button], template=STOP_TEMPLATE):
    """Under every anniversary DM: no more DMs or posts for this member."""

    def __init__(self, guild_id: int) -> None:
        super().__init__(discord.ui.Button(label="Stop anniversary messages", style=discord.ButtonStyle.secondary,
                                           custom_id=f"anniversary:stop:{guild_id}"))
        self.guild_id = guild_id

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item: discord.ui.Button, match, /):
        return cls(int(match["guild"]))

    async def callback(self, interaction: discord.Interaction) -> None:
        await interaction.client.db.set_anniversary_opt_out(self.guild_id, interaction.user.id, True)
        view = discord.ui.View(timeout=None)
        view.add_item(ResumeAnniversariesButton(self.guild_id))
        await interaction.response.send_message(
            f"You won't receive anniversary messages from {COMMUNITY_NAME} again.", view=view, ephemeral=True)


class ResumeAnniversariesButton(discord.ui.DynamicItem[discord.ui.Button], template=RESUME_TEMPLATE):
    """Undoes a stop."""

    def __init__(self, guild_id: int) -> None:
        super().__init__(discord.ui.Button(label="Turn them back on", style=discord.ButtonStyle.secondary,
                                           custom_id=f"anniversary:resume:{guild_id}"))
        self.guild_id = guild_id

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item: discord.ui.Button, match, /):
        return cls(int(match["guild"]))

    async def callback(self, interaction: discord.Interaction) -> None:
        await interaction.client.db.set_anniversary_opt_out(self.guild_id, interaction.user.id, False)
        await interaction.response.send_message("Anniversary messages are back on.", ephemeral=True)


async def next_event_line(guild: discord.Guild, now: datetime) -> str | None:
    """The DM line for the server's next Discord event (bot/uex/anniversaries.py: event_line),
    from a fresh fetch, else what discord.py has cached."""
    try:
        events = await guild.fetch_scheduled_events()
    except discord.HTTPException as exc:
        logger.warning("Couldn't fetch scheduled events: %s", exc)
        events = list(getattr(guild, "scheduled_events", []))
    return event_line([Event(e.id, e.name, e.start_time, int(e.status.value)) for e in events], guild.id, now)


def dm_view(guild_id: int) -> discord.ui.View:
    view = discord.ui.View(timeout=None)
    view.add_item(StopAnniversariesButton(guild_id))
    return view


class Anniversaries(commands.Cog):
    def __init__(self, bot: commands.Bot, *, start_loop: bool = True) -> None:
        self.bot = bot
        self._roles_warned = False
        if start_loop:
            self.check_anniversaries.start()
            self.sync_veteran_roles.start()

    async def cog_load(self) -> None:
        self.bot.add_dynamic_items(StopAnniversariesButton, ResumeAnniversariesButton)

    def cog_unload(self) -> None:
        self.check_anniversaries.cancel()
        self.sync_veteran_roles.cancel()

    @tasks.loop(minutes=CHECK_MINUTES)
    async def check_anniversaries(self) -> None:
        try:
            await self.send_due(datetime.now(timezone.utc))
        except Exception:
            logger.exception("Anniversary check failed")

    @check_anniversaries.before_loop
    async def before_check(self) -> None:
        await self.bot.wait_until_ready()

    async def send_due(self, now: datetime) -> int:
        """Send every anniversary due at `now`; how many members were handled."""
        channel = self.bot.get_channel(ANNIVERSARY_CHANNEL_ID)
        if channel is None:
            logger.warning("Anniversary channel %s not found; nothing sent", ANNIVERSARY_CHANNEL_ID)
            return 0
        guild = channel.guild
        opted_out = await self.bot.db.get_anniversary_opt_outs(guild.id)
        handled = 0
        event, looked = None, False
        for member in guild.members:
            if member.bot or member.joined_at is None or member.id in opted_out:
                continue
            due = due_anniversary(member.joined_at, now)
            if due is None:
                continue
            if not looked:  # once a check, and only when someone is due
                event, looked = await next_event_line(guild, now), True
            try:
                await self._celebrate(guild, member, *due, event=event)
                handled += 1
            except Exception:  # one member's failure never stops the rest
                logger.exception("Anniversary for member %s failed", member.id)
        return handled

    async def _celebrate(self, guild: discord.Guild, member: discord.Member, years: int, moment: datetime, *,
                         event: str | None = None) -> None:
        guild_id = guild.id
        status = await self.bot.db.claim_anniversary(guild_id, member.id, moment.year, years)
        label = f"{years}-year anniversary"
        if status["dm"] == "pending" or status["post"] == "pending":
            await self.set_veteran_role(guild, member, years)
        if status["dm"] == "pending":
            outcome = await send_dm(self.bot, member.id, dm_text(_name(member), years, member.joined_at, event),
                                    label=label, view=dm_view(guild_id))
            if outcome in _STATUS:
                await self.bot.db.set_anniversary_status(guild_id, member.id, moment.year, part="dm",
                                                         status=_STATUS[outcome])
        if status["post"] == "pending":
            outcome = await send_to_channel(self.bot, ANNIVERSARY_CHANNEL_ID, post_text(member.mention, years, member.joined_at),
                                            label=label, allowed_mentions=owner_only_mentions(member.id))
            if outcome in _STATUS:
                await self.bot.db.set_anniversary_status(guild_id, member.id, moment.year, part="post",
                                                         status=_STATUS[outcome])

    async def set_veteran_role(self, guild: discord.Guild, member: discord.Member, years: int) -> None:
        """Give `member` the veteran role for `years` and take away any other; nothing before a
        first anniversary. A failure is logged, never raised: the message still goes out, and
        the next check tries again."""
        if years < 1:
            return
        name = veteran_role_name(years)
        try:
            role = discord.utils.get(guild.roles, name=name) or await guild.create_role(name=name, reason="Veteran role")
            stale = [r for r in member.roles if is_veteran_role(r.name) and r != role]
            if stale:
                await member.remove_roles(*stale, reason="Veteran role")
            if role not in member.roles:
                await member.add_roles(role, reason="Veteran role")
        except discord.HTTPException as exc:
            if not self._roles_warned:
                logger.warning("Couldn't set veteran roles (needs Manage Roles): %s", exc)
                self._roles_warned = True

    @tasks.loop(hours=VETERAN_CHECK_HOURS)
    async def sync_veteran_roles(self) -> None:
        """Everyone's veteran role, put right quietly (no message)."""
        try:
            channel = self.bot.get_channel(ANNIVERSARY_CHANNEL_ID)
            if channel is None:
                return
            now = datetime.now(timezone.utc)
            for member in list(channel.guild.members):
                if not member.bot and member.joined_at is not None:
                    await self.set_veteran_role(channel.guild, member, years_completed(member.joined_at, now))
        except Exception:
            logger.exception("Veteran role check failed")

    @sync_veteran_roles.before_loop
    async def before_veteran_roles(self) -> None:
        await self.bot.wait_until_ready()

    @app_commands.command(name="anniversary-preview",
                          description="Owner only: send yourself the anniversary DM and see the channel post.")
    async def anniversary_preview(self, interaction: discord.Interaction) -> None:
        if not await self.bot.is_owner(interaction.user):
            await interaction.response.send_message("This command is for the bot's owner.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        channel = self.bot.get_channel(ANNIVERSARY_CHANNEL_ID)
        member = channel.guild.get_member(interaction.user.id) if channel is not None else None
        if member is None or member.joined_at is None:
            await interaction.followup.send(f"You need to be a member of {COMMUNITY_NAME} (and the bot able to see "
                                            "its members) to preview this.", ephemeral=True)
            return
        now = datetime.now(timezone.utc)
        years = max(1, now.year - member.joined_at.year)
        outcome = await send_dm(self.bot, member.id,
                                dm_text(_name(member), years, member.joined_at,
                                        await next_event_line(channel.guild, now)),
                                label="anniversary preview", view=dm_view(channel.guild.id))
        sent = ("I've sent you the DM." if outcome is Delivery.DELIVERED
                else "I couldn't DM you - your DMs from server members may be off.")
        nxt = upcoming([(m.display_name, m.joined_at) for m in channel.guild.members if not m.bot and m.joined_at],
                       now)
        lines = [f"<t:{int(moment.timestamp())}:f> · {discord.utils.escape_markdown(name)} · {years} year{'s' if years != 1 else ''}"
                 for moment, name, years in nxt]
        await interaction.followup.send(
            f"{sent} This is the post <#{ANNIVERSARY_CHANNEL_ID}> would show:\n\n"
            f"{post_text(member.mention, years, member.joined_at)}\n\n"
            + ("**Next anniversaries**\n" + "\n".join(lines) if lines else "No anniversaries coming up."),
            ephemeral=True, allowed_mentions=discord.AllowedMentions.none())


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Anniversaries(bot))
