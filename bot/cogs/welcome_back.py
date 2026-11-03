"""Welcome back (the owner's idea, 2026-10-29): a DM to a member who posts or joins voice after
INACTIVE_DAYS or more away, with the server's next event (wording in bot/uex/anniversaries.py).

Activity is only when: each member's last post or voice join, from the message and voice state
events the bot gets anyway (Guild Messages and Guild Voice States are in Intents.default();
Message Content isn't needed or asked for - what anyone said is never read or kept). A busy
member's time is written at most every WRITE_EVERY, so chat doesn't mean a write per message.

So that welcome-backs work from the first day, the first start after this shipped reads who
posted in each channel the bot can read over the last INACTIVE_DAYS (authors and times only),
and takes voice ranks' voice time too. Until that read is done, a member with no record isn't
known to be away, so isn't welcomed back; after it, no record means not seen in that time, and a
member who joined over INACTIVE_DAYS ago is. A newer member's first post is never a welcome
back. Recording the new time before sending means one return gets one DM.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

import discord
from discord.ext import commands, tasks

from bot.cogs.anniversaries import ANNIVERSARY_CHANNEL_ID, next_event_line
from bot.delivery import send_dm
from bot.uex.anniversaries import INACTIVE_DAYS, welcome_back_text

logger = logging.getLogger("uexbot.welcome_back")

WRITE_EVERY = timedelta(hours=1)
AWAY = timedelta(days=INACTIVE_DAYS)
_TIME = "%Y-%m-%d %H:%M:%S"


def _name(member: discord.abc.User) -> str:
    return discord.utils.escape_mentions(discord.utils.escape_markdown(member.display_name))


def _parse(text: str) -> datetime:
    return datetime.strptime(text, _TIME).replace(tzinfo=timezone.utc)


class WelcomeBack(commands.Cog):
    def __init__(self, bot: commands.Bot, *, start_loop: bool = True) -> None:
        self.bot = bot
        self._written: dict[int, datetime] = {}
        if start_loop:
            self.seed.start()

    def cog_unload(self) -> None:
        self.seed.cancel()

    def _guild(self) -> discord.Guild | None:
        channel = self.bot.get_channel(ANNIVERSARY_CHANNEL_ID)
        return channel.guild if channel is not None else None

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
            if away:
                event = await next_event_line(member.guild, now)
                await send_dm(self.bot, member.id, welcome_back_text(_name(member), event), label="welcome back")
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
        channels = list(guild.text_channels) + list(guild.voice_channels) + list(guild.threads)
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


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(WelcomeBack(bot))
