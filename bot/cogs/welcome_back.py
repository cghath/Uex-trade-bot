"""Welcome back (the owner's idea, 2026-10-29): a DM to a member who posts or joins voice after
INACTIVE_DAYS or more away, with the server's next event (wording in bot/uex/anniversaries.py).

Activity is only when: each member's last post or voice join, from the message and voice state
events the bot gets anyway (Guild Messages and Guild Voice States are in Intents.default();
Message Content isn't needed or asked for - what anyone said is never read or kept). A busy
member's time is written at most every WRITE_EVERY, so chat doesn't mean a write per message.

So that welcome-backs work from the first day, the first start after this shipped reads who
posted in each channel the bot can read over the last INACTIVE_DAYS (authors and times only) -
text and voice channels, active threads, and threads archived in that time, forum posts
included - and takes voice ranks' voice time too. Until that read is done, a member with no
record isn't known to be away, so isn't welcomed back. Voice time only goes back to when voice
ranks began recording it (2026-10-26), so until that covers the whole INACTIVE_DAYS nobody is
known to be away either - a member could have been in voice the whole time (audit COMM-2).
After both, no record means not seen in that time, and a member who joined over INACTIVE_DAYS
ago is. A newer member's first post is never a welcome back. Recording the new time before
sending means one return gets one DM.

The DM has a "Stop welcome-back messages" button (the owner's decision, audit COMM-3), kept per
member and working through restarts (a DynamicItem, like the anniversary one), and its reply
has a button to turn them back on.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

import discord
from discord.ext import commands, tasks

from bot.cogs.anniversaries import ANNIVERSARY_CHANNEL_ID, next_event_line
from bot.delivery import home_channel, send_dm
from bot.discord_ui import BotDynamicItem
from bot.uex.anniversaries import COMMUNITY_NAME, INACTIVE_DAYS, welcome_back_text

logger = logging.getLogger("uexbot.welcome_back")

WRITE_EVERY = timedelta(hours=1)
AWAY = timedelta(days=INACTIVE_DAYS)
_TIME = "%Y-%m-%d %H:%M:%S"
STOP_TEMPLATE = r"welcomeback:stop:(?P<guild>\d+)"
RESUME_TEMPLATE = r"welcomeback:resume:(?P<guild>\d+)"


def _name(member: discord.abc.User) -> str:
    return discord.utils.escape_mentions(discord.utils.escape_markdown(member.display_name))


def _parse(text: str) -> datetime:
    return datetime.strptime(text, _TIME).replace(tzinfo=timezone.utc)


class StopWelcomeBackButton(BotDynamicItem, discord.ui.DynamicItem[discord.ui.Button], template=STOP_TEMPLATE):
    """Under every welcome-back DM: no more of them for this member."""

    def __init__(self, guild_id: int) -> None:
        super().__init__(discord.ui.Button(label="Stop welcome-back messages", style=discord.ButtonStyle.secondary,
                                           custom_id=f"welcomeback:stop:{guild_id}"))
        self.guild_id = guild_id

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item: discord.ui.Button, match, /):
        return cls(int(match["guild"]))

    async def callback(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)  # first: the write can wait on a busy database
        await interaction.client.db.set_welcome_back_opt_out(self.guild_id, interaction.user.id, True)
        view = discord.ui.View(timeout=None)
        view.add_item(ResumeWelcomeBackButton(self.guild_id))
        await interaction.followup.send(
            f"You won't receive welcome-back messages from {COMMUNITY_NAME} again.", view=view, ephemeral=True)


class ResumeWelcomeBackButton(BotDynamicItem, discord.ui.DynamicItem[discord.ui.Button], template=RESUME_TEMPLATE):
    """Undoes a stop."""

    def __init__(self, guild_id: int) -> None:
        super().__init__(discord.ui.Button(label="Turn them back on", style=discord.ButtonStyle.secondary,
                                           custom_id=f"welcomeback:resume:{guild_id}"))
        self.guild_id = guild_id

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item: discord.ui.Button, match, /):
        return cls(int(match["guild"]))

    async def callback(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        await interaction.client.db.set_welcome_back_opt_out(self.guild_id, interaction.user.id, False)
        await interaction.followup.send("Welcome-back messages are back on.", ephemeral=True)


def welcome_back_view(guild_id: int) -> discord.ui.View:
    view = discord.ui.View(timeout=None)
    view.add_item(StopWelcomeBackButton(guild_id))
    return view


class WelcomeBack(commands.Cog):
    def __init__(self, bot: commands.Bot, *, start_loop: bool = True) -> None:
        self.bot = bot
        self._written: dict[int, datetime] = {}
        self._voice_covered = False  # voice time recorded for the whole INACTIVE_DAYS yet
        if start_loop:
            self.seed.start()

    async def cog_load(self) -> None:
        self.bot.add_dynamic_items(StopWelcomeBackButton, ResumeWelcomeBackButton)

    def cog_unload(self) -> None:
        self.seed.cancel()

    def _guild(self) -> discord.Guild | None:
        channel = home_channel(self.bot, ANNIVERSARY_CHANNEL_ID, feature="welcome-backs")
        return channel.guild if channel is not None else None

    async def _known_away(self, guild_id: int, now: datetime) -> bool:
        """Whether a gap of INACTIVE_DAYS can be known yet: voice time must reach back that far."""
        if not self._voice_covered:
            start = await self.bot.db.voice_history_start(guild_id)
            self._voice_covered = (start is not None
                                   and datetime.strptime(start, "%Y-%m-%d").replace(tzinfo=timezone.utc) <= now - AWAY)
        return self._voice_covered

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        guild = self._guild()
        if guild is None or message.guild is None or message.guild.id != guild.id or message.author.bot:
            return
        member = message.author if isinstance(message.author, discord.Member) else guild.get_member(message.author.id)
        if member is not None:
            await self.active(member, datetime.now(timezone.utc))

    @commands.Cog.listener()
    async def on_voice_state_update(self, member: discord.Member, before, after) -> None:
        guild = self._guild()
        if guild is None or member.guild.id != guild.id or member.bot or after.channel is None:
            return
        afk = guild.afk_channel.id if guild.afk_channel else None
        if after.channel != before.channel and after.channel.id != afk:
            await self.active(member, datetime.now(timezone.utc))

    async def active(self, member: discord.Member, now: datetime) -> None:
        """This member did something now: welcome them back if they'd been away long enough."""
        last_written = self._written.get(member.id)
        if last_written is not None and now - last_written < WRITE_EVERY:
            return
        self._written[member.id] = now
        try:
            guild_id = member.guild.id
            previous = await self.bot.db.touch_member_activity(guild_id, member.id, now.strftime(_TIME))
            if previous is not None:
                away = now - _parse(previous) >= AWAY
            else:
                away = (member.joined_at is not None and now - member.joined_at >= AWAY
                        and await self.bot.db.member_activity_seeded(guild_id))
            if (away and await self._known_away(guild_id, now)
                    and not await self.bot.db.welcome_back_opted_out(guild_id, member.id)):
                event = await next_event_line(member.guild, now)
                await send_dm(self.bot, member.id, welcome_back_text(_name(member), event), label="welcome back",
                              view=welcome_back_view(guild_id))
        except Exception:  # an event handler: log, never raise into discord.py
            logger.exception("Welcome back for member %s failed", member.id)

    # -- the one-time read ----------------------------------------------------

    @tasks.loop(count=1)
    async def seed(self) -> None:
        try:
            guild = self._guild()
            if guild is not None and not await self.bot.db.member_activity_seeded(guild.id):
                await self.seed_from_history(guild, datetime.now(timezone.utc))
        except Exception:
            logger.exception("Reading recent activity failed; it's tried again on the next start")

    @seed.before_loop
    async def before_seed(self) -> None:
        await self.bot.wait_until_ready()

    async def seed_from_history(self, guild: discord.Guild, now: datetime) -> None:
        """Who posted in the last INACTIVE_DAYS in each channel the bot can read (authors and
        times only), plus voice time, recorded as their last activity."""
        since = now - AWAY
        seen: dict[int, datetime] = {}
        channels = list(guild.text_channels) + list(guild.voice_channels) + await self._threads(guild, since)
        read = 0
        for channel in channels:
            perms = channel.permissions_for(guild.me)
            if not (perms.view_channel and perms.read_message_history):
                continue
            try:
                async for message in channel.history(after=since, limit=None):
                    if not message.author.bot and (message.author.id not in seen
                                                   or message.created_at > seen[message.author.id]):
                        seen[message.author.id] = message.created_at
                read += 1
            except discord.HTTPException as exc:
                logger.warning("Couldn't read recent history in channel %s: %s", channel.id, exc)
        for user_id, day in (await self.bot.db.voice_last_days(guild.id)).items():
            end = datetime.strptime(day, "%Y-%m-%d").replace(hour=23, minute=59, second=59, tzinfo=timezone.utc)
            if user_id not in seen or end > seen[user_id]:
                seen[user_id] = end
        await self.bot.db.seed_member_activity(guild.id, {uid: t.strftime(_TIME) for uid, t in seen.items()})
        logger.info("Recent activity read: %d members seen in the last %d days across %d channels",
                    len(seen), INACTIVE_DAYS, read)

    async def _threads(self, guild: discord.Guild, since: datetime) -> list:
        """Every thread that can hold a post since `since`: the active ones discord.py has, and
        the public ones archived since then under each text channel and forum (forum posts are
        threads). Archived threads come newest-archived first, and a thread's last post can't
        be after it was archived, so the walk stops at the first one archived before `since`."""
        threads = {thread.id: thread for thread in guild.threads}
        for parent in list(guild.text_channels) + list(guild.forums):
            perms = parent.permissions_for(guild.me)
            if not (perms.view_channel and perms.read_message_history):
                continue
            try:
                async for thread in parent.archived_threads(limit=None):
                    if thread.archive_timestamp < since:
                        break
                    threads.setdefault(thread.id, thread)
            except discord.HTTPException as exc:
                logger.warning("Couldn't list archived threads in channel %s: %s", parent.id, exc)
        return list(threads.values())


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(WelcomeBack(bot))
