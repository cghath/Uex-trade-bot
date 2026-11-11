"""Server analytics (the owner's plan, 2026-11-04; rules and wording in bot/uex/server_analytics.py):
whether Star Tech Dynamics is growing, active and keeping people. Owner-only /server-stats shows
the last 30 days, a DM to the owner after 9 AM Eastern on the 1st shows the month before, and
/invite-label names the invite links joins are matched to.

What's kept is counts and "active that day", never what anyone wrote - from events the bot gets
anyway (Guild Messages and Guild Voice States, in Intents.default(); Message Content stays off):
- a member's day as active when they post or join voice (member_active_days; voice ranks'
  voice_daily counts too),
- server totals of posts per hour and per channel per day, and of people in voice per hour,
- joins, with the invite whose use count went up (the bot needs Manage Server to read invites),
  and leaves,
- each event card's attendance: minutes each member is in the event's voice channel or Ops while
  the op runs, and the most "Interested" Discord showed,
- moderation from the audit log (the bot needs View Audit Log): bans, kicks, timeouts, messages
  a moderator or bot deleted, AutoMod's actions - each as it happens, and read back every
  AUDIT_EVERY_HOURS (the first time, as far as Discord keeps it) so nothing is missed while the
  bot is down. Who was acted on is kept with each entry, but only counts are ever shown.
The first start reads the 30 days of chat before tracking began (authors, times and channels
only, like welcome-back's read) so the activity numbers don't start from nothing. Joins, leaves,
sources, voice-by-hour and attendance can't be read back, so they start when this shipped.
"""
from __future__ import annotations

import asyncio
import io
import logging

import aiohttp
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta

import discord
from discord import app_commands
from discord.ext import commands, tasks

from bot.cogs.anniversaries import ANNIVERSARY_CHANNEL_ID
from bot.cogs.role_picker import INTRO_TYPES
from bot.cogs.voice_ranks import OPS_CATEGORY_ID
from bot.cogs.welcome_back import recent_threads
from bot.delivery import Delivery, home_channel, send_dm
from bot.discord_ui import LAYOUT_TEXT_LIMIT, BotLayoutView
from bot.uex.anniversaries import COMMUNITY_NAME, INACTIVE_DAYS
from bot.uex.server_analytics import (
    MEMBER_REFERRAL, ATTENDED_MINUTES, AUDIT_LOG_DAYS, DAY_FORMAT, EASTERN, HOUR_FORMAT, PERIOD_DAYS, RETENTION_DAYS, TIME_FORMAT, UTC,
    EventResult, InviteUse, ModAction, Movement, build_report, clean_label, event_running, invite_changes,
    match_invite, ops_card,
    Report, moderation_kind, previous_month, report_blocks, report_csv, source_of,
)
from bot.uex.join_cards import STAFF_CHANNEL_ID, join_card, leave_card, source_text
from bot.uex.new_members import is_new, member_picks, onboarding_status, timezone_counts, zone_bucket
from bot.uex.voice_ranks import counts

logger = logging.getLogger("uexbot.server_analytics")

HISTORY_DAYS = 30
INTEREST_EVERY = 10   # minutes between reads of a running event's Interested count
INVITE_RETRY = 10     # minutes between tries to read invites while the bot can't
REPORT_HOUR = 9       # the monthly DM goes from 9 AM Eastern on the 1st
AUDIT_EVERY_HOURS = 6  # how often the audit log is read back
# The audit log actions that can be moderation, each read on its own: reading every entry would
# mostly be the bot's own role changes.
AUDIT_ACTIONS = ("ban", "unban", "kick", "member_update", "message_delete", "message_bulk_delete",
                 "automod_block_message", "automod_flag_message", "automod_timeout_member", "automod_quarantine_user")


def entry_action(entry) -> tuple | None:
    """An audit log entry as a moderation_actions row, or None when it isn't moderation."""
    kind = moderation_kind(getattr(entry.action, "name", ""), getattr(entry.before, "timed_out_until", None),
                           getattr(entry.after, "timed_out_until", None))
    if kind is None:
        return None
    count = getattr(entry.extra, "count", None) if kind == "deleted" else None
    user = entry.user
    by_bot = kind != "automod" and user is not None and user.bot
    return (entry.id, kind, _time(entry.created_at), count or 1, getattr(entry, "user_id", None),
            by_bot, getattr(entry.target, "id", None))


def _time(moment: datetime) -> str:
    return moment.astimezone(UTC).strftime(TIME_FORMAT)


def _parse(text: str) -> datetime:
    return datetime.strptime(text, TIME_FORMAT).replace(tzinfo=UTC)


def _channel_id(channel) -> int:
    """Where a post counts: a thread's or forum post's parent channel, otherwise the channel."""
    return channel.parent_id if isinstance(channel, discord.Thread) and channel.parent_id else channel.id


def _readable(channel, me) -> bool:
    perms = channel.permissions_for(me)
    return perms.view_channel and perms.read_message_history


def stats_views(blocks: list[str]) -> list[BotLayoutView]:
    """The report as layout messages, sections split by lines: as many messages as Discord's text
    limit needs, so no section is ever left out."""
    views: list[BotLayoutView] = []
    container, used = None, 0
    for block in blocks:
        block = block[:LAYOUT_TEXT_LIMIT]
        if container is None or used + len(block) > LAYOUT_TEXT_LIMIT:
            view = BotLayoutView(timeout=None)
            container = discord.ui.Container(accent_colour=discord.Colour.blurple())
            view.add_item(container)
            views.append(view)
            used = 0
        elif used:
            container.add_item(discord.ui.Separator())
        container.add_item(discord.ui.TextDisplay(block))
        used += len(block)
    return views


def stats_view(blocks: list[str]) -> BotLayoutView:
    """The first message of the report (tests and short reports)."""
    return stats_views(blocks)[0]


def _inviter(invite) -> int | None:
    """The member who made an invite; None when a bot made it or Discord doesn't say."""
    who = getattr(invite, "inviter", None)
    return who.id if who is not None and not who.bot else None


def _uses(invite) -> InviteUse:
    return InviteUse(invite.uses or 0, invite.max_uses or 0, _inviter(invite), getattr(invite, "expires_at", None))


# Reading invites can fail at the network too, not only with an HTTP error: either way the
# join's source is unknown, never a lost join.
_INVITE_ERRORS = (discord.HTTPException, OSError, asyncio.TimeoutError, aiohttp.ClientError)


def _who(user) -> str:
    return discord.utils.escape_markdown(getattr(user, "display_name", None) or user.name)


def invite_code(text: str) -> str:
    """The code from a pasted invite link or a bare code (an event link's ?event=... dropped)."""
    return text.strip().split("?", 1)[0].split("#", 1)[0].rstrip("/").rsplit("/", 1)[-1]


class ServerAnalytics(commands.Cog):
    def __init__(self, bot: commands.Bot, *, start_loop: bool = True) -> None:
        self.bot = bot
        self.invites: dict[str, InviteUse] | None = None  # use counts as last read
        self._deleted: dict[str, datetime] = {}           # invite code -> when its delete was seen
        self._started = datetime.now(UTC)                  # posts before this were counted by the old rules
        self._invite_lock = asyncio.Lock()
        self._invites_warned = False
        self._audit_warned = False
        self._cards_warned = False
        self._state: dict | None = None
        self._seen: dict[str, set[int]] = {}               # day -> members already marked active
        self._messages: Counter = Counter()                # hour -> posts not yet saved
        self._voice: Counter = Counter()                   # hour -> voice minutes not yet saved
        self._new_messages: Counter = Counter()            # the same two, for members in their first weeks
        self._new_voice: Counter = Counter()
        self._channels: Counter = Counter()                # (channel_id, day) -> posts not yet saved
        self._minute = 0
        self.now = lambda: datetime.now(UTC)  # the clock; tests set their own
        if start_loop:
            self.tick.start()
            self.monthly.start()
            self.read_history.start()
            self.read_audit_log.start()

    def cog_unload(self) -> None:
        self.tick.cancel()
        self.monthly.cancel()
        self.read_history.cancel()
        self.read_audit_log.cancel()

    def _guild(self) -> discord.Guild | None:
        channel = home_channel(self.bot, ANNIVERSARY_CHANNEL_ID, feature="server analytics")
        return channel.guild if channel is not None else None

    def _ours(self, guild_id: int | None) -> discord.Guild | None:
        guild = self._guild()
        return guild if guild is not None and guild_id == guild.id else None

    async def _start(self, guild: discord.Guild) -> dict:
        if self._state is None:
            self._state = await self.bot.db.analytics_start(guild.id, _time(self.now()))
        return self._state

    # -- what happens ---------------------------------------------------------

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        # Only real posts: Discord's own "X joined" message is authored by the member who joined,
        # and boosts, pins and the like are system messages too (audit 2026-11-04).
        if (message.guild is None or message.author.bot or message.type not in INTRO_TYPES
                or self._ours(message.guild.id) is None):
            return
        moment = message.created_at
        if self._state is not None:  # tracking has begun: the history read covers what came before
            self._messages[moment.strftime(HOUR_FORMAT)] += 1
            self._channels[(_channel_id(message.channel), moment.strftime(DAY_FORMAT))] += 1
            if is_new(getattr(message.author, "joined_at", None), moment):
                self._new_messages[moment.strftime(HOUR_FORMAT)] += 1
        await self._active(message.guild.id, message.author.id, moment)

    @commands.Cog.listener()
    async def on_voice_state_update(self, member: discord.Member, before, after) -> None:
        guild = self._ours(member.guild.id)
        if guild is None or member.bot or after.channel is None or after.channel == before.channel:
            return
        if guild.afk_channel is None or after.channel.id != guild.afk_channel.id:
            await self._active(guild.id, member.id, self.now())

    async def _active(self, guild_id: int, user_id: int, moment: datetime) -> None:
        """Mark the member active that day: one write per member per day, not one per post."""
        day = moment.strftime(DAY_FORMAT)
        seen = self._seen.setdefault(day, set())
        if user_id in seen:
            return
        seen.add(user_id)
        for old in sorted(self._seen)[:-2]:
            del self._seen[old]
        try:
            await self.bot.db.mark_active_day(guild_id, user_id, day)
        except Exception:  # an event handler: log, never raise into discord.py
            seen.discard(user_id)
            logger.exception("Marking member %s active failed", user_id)

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member) -> None:
        guild = self._ours(member.guild.id)
        if guild is None or member.bot:
            return
        try:
            code, inviter = await self.joined_with(guild)
        except Exception:  # the join still counts, its source unknown
            logger.exception("Matching member %s's invite failed", member.id)
            code = inviter = None
        try:
            await self.bot.db.record_member_movement(guild.id, member.id, "join", _time(self.now()), code, inviter)
            logger.info("Member %s joined (invite %s%s)", member.id, code or "not known",
                        f", made by {inviter}" if inviter else "")
        except Exception:
            logger.exception("Recording member %s's join failed", member.id)
        try:
            source = await self._source(guild, code, inviter)
            await self._post_card(guild, join_card(member.id, getattr(member, "display_name", ""), member.name, source,
                                                   member.created_at, self.now(), getattr(guild, "member_count", None)),
                                  discord.Colour.green())
        except Exception:
            logger.exception("Posting member %s's join card failed", member.id)

    @commands.Cog.listener()
    async def on_raw_member_remove(self, payload: discord.RawMemberRemoveEvent) -> None:
        guild = self._ours(payload.guild_id)
        if guild is None or payload.user.bot:
            return
        user, now = payload.user, self.now()
        try:
            joined = await self.bot.db.last_join(guild.id, user.id)
        except Exception:
            logger.exception("Reading member %s's join failed", user.id)
            joined = None
        try:
            await self.bot.db.record_member_movement(payload.guild_id, user.id, "leave", _time(now))
        except Exception:
            logger.exception("Recording member %s leaving failed", user.id)
        try:
            source = None
            joined_at = getattr(user, "joined_at", None)  # a Member when the cache still had them
            if joined is not None:
                source = await self._source(guild, joined["invite_code"], joined["inviter_id"])
                joined_at = joined_at or _parse(joined["at"])
            await self._post_card(guild, leave_card(user.id, getattr(user, "display_name", ""), getattr(user, "name", ""),
                                                    joined_at, now, source), discord.Colour.dark_grey())
        except Exception:
            logger.exception("Posting member %s's leave card failed", user.id)

    async def _source(self, guild: discord.Guild, code: str | None, inviter: int | None) -> str:
        """A join's source for a card: the matched invite's label, who made it, or unknown."""
        labels = await self.bot.db.get_invite_labels()
        name = None
        if inviter is not None:
            who = guild.get_member(inviter) or self.bot.get_user(inviter)
            name = (getattr(who, "display_name", None) or who.name) if who is not None else str(inviter)
        return source_text(code, source_of(code, labels, inviter), name)

    async def _post_card(self, guild: discord.Guild, text: str, colour: discord.Colour) -> None:
        """A small card in the join cards channel (#staff unless `/join-cards` set another, or off)."""
        setting = await self.bot.db.get_join_cards_channel(guild.id)
        if setting == 0:
            return
        channel = guild.get_channel(setting or STAFF_CHANNEL_ID)
        if channel is None:
            if not self._cards_warned:
                logger.warning("The join cards channel %s isn't there: set one with /join-cards",
                               setting or STAFF_CHANNEL_ID)
            self._cards_warned = True
            return
        try:
            await channel.send(embed=discord.Embed(description=text, colour=colour),
                               allowed_mentions=discord.AllowedMentions.none())
        except discord.HTTPException as exc:
            if not self._cards_warned:
                logger.warning("Couldn't post a join card in #%s (the bot needs Send Messages and Embed Links "
                               "there): %s", getattr(channel, "name", channel.id), exc)
            self._cards_warned = True

    @commands.Cog.listener()
    async def on_invite_create(self, invite: discord.Invite) -> None:
        if self.invites is not None and invite.guild is not None and self._ours(invite.guild.id) is not None:
            self.invites[invite.code] = _uses(invite)

    @commands.Cog.listener()
    async def on_invite_delete(self, invite: discord.Invite) -> None:
        """Note when: a limited invite deleted well before a join was revoked, not used up by it."""
        if invite.guild is not None and self._ours(invite.guild.id) is not None:
            self._deleted[invite.code] = self.now()

    async def _read_invites(self, guild: discord.Guild) -> list[discord.Invite] | None:
        try:
            invites = await guild.invites()
        except _INVITE_ERRORS as exc:
            if not self._invites_warned:
                logger.warning("Can't read invites (the bot needs Manage Server), so join sources aren't known: %s", exc)
            self._invites_warned = True
            return None
        self._invites_warned = False
        return invites

    async def _snapshot(self, guild: discord.Guild) -> dict[str, InviteUse] | None:
        invites = await self._read_invites(guild)
        return None if invites is None else {i.code: _uses(i) for i in invites}

    async def joined_with(self, guild: discord.Guild) -> tuple[str | None, int | None]:
        """The invite a member who just joined used, by which use count went up, and the member
        who made it. One join at a time, so two close together each compare against the count the
        other left."""
        async with self._invite_lock:
            after = await self._snapshot(guild)
            if after is None:
                return None, None
            before, self.invites = self.invites, after
            if before is None:
                logger.info("A join's invite isn't known: the invites hadn't been read yet")
                return None, None
            code = match_invite(before, after, now=self.now(), deleted=self._deleted)
            if code is None:  # what the bot saw, so a "not known" source can be explained later
                logger.info("A join matched no single invite: %s",
                            invite_changes(before, after, now=self.now(), deleted=self._deleted))
                return None, None
            used = after.get(code) or before.get(code)
            return code, used.inviter_id if used else None

    # -- every minute ---------------------------------------------------------

    @tasks.loop(minutes=1)
    async def tick(self) -> None:
        await self.minute(self.now())

    @tick.before_loop
    async def before_tick(self) -> None:
        await self.bot.wait_until_ready()

    async def minute(self, now: datetime) -> None:
        """Each step on its own: one failing never stops the others, or the loop (CLAUDE.md)."""
        guild = self._guild()
        if guild is None:
            return
        self._minute += 1
        try:
            await self._start(guild)
        except Exception:
            logger.exception("Starting server analytics failed")
            return
        if self.invites is None and self._minute % INVITE_RETRY == 1:
            try:
                async with self._invite_lock:
                    if self.invites is None:
                        self.invites = await self._snapshot(guild)
            except Exception:
                logger.exception("Reading invites failed")
        try:
            afk = guild.afk_channel.id if guild.afk_channel else None
            people = [m for channel in list(guild.voice_channels) + list(guild.stage_channels)
                      if channel.id != afk for m in channel.members if not m.bot]
            if people:
                self._voice[now.strftime(HOUR_FORMAT)] += len(people)
                newcomers = sum(1 for m in people if is_new(getattr(m, "joined_at", None), now))
                if newcomers:
                    self._new_voice[now.strftime(HOUR_FORMAT)] += newcomers
        except Exception:
            logger.exception("Counting who's in voice failed")
        await self._flush(guild.id)
        try:
            await self.count_attendance(guild, now)
        except Exception:
            logger.exception("Counting event attendance failed")

    async def _flush(self, guild_id: int) -> None:
        messages, voice, channels = self._messages, self._voice, self._channels
        new_messages, new_voice = self._new_messages, self._new_voice
        if not (messages or voice or channels):
            return
        self._messages, self._voice, self._channels = Counter(), Counter(), Counter()
        self._new_messages, self._new_voice = Counter(), Counter()
        hours = {hour: (messages[hour], voice[hour], new_messages[hour], new_voice[hour])
                 for hour in set(messages) | set(voice)}
        try:
            await self.bot.db.add_activity_counts(guild_id, hours, dict(channels))
        except Exception:
            self._messages.update(messages)
            self._voice.update(voice)
            self._channels.update(channels)
            self._new_messages.update(new_messages)
            self._new_voice.update(new_voice)
            logger.exception("Saving activity counts failed; they're kept for the next minute")

    async def count_attendance(self, guild: discord.Guild, now: datetime) -> None:
        """A minute for everyone in each running event's voice channel or Ops (not deafened)."""
        stamp = int(now.timestamp())
        cards = [c for c in await self.bot.db.open_event_cards(guild.id) if event_running(c["state"], c["starts_at"], stamp)]
        if not cards:
            return
        channels = list(guild.voice_channels) + list(guild.stage_channels)
        ops = {c.id for c in channels if c.category_id == OPS_CATEGORY_ID}
        ops_for = ops_card([(c["card_id"], c["starts_at"]) for c in cards], stamp)  # one op holds Ops at a time
        for card in cards:
            where = (ops if card["card_id"] == ops_for else set()) | (
                {card["voice_channel_id"]} if card["voice_channel_id"] else set())
            present = sorted({m.id for c in channels if c.id in where for m in c.members if counts(m)})
            if present:
                await self.bot.db.credit_event_minute(card["card_id"], present)
            if self._minute % INTEREST_EVERY == 1:
                await self._interest(guild, card)

    async def _interest(self, guild: discord.Guild, card: dict) -> None:
        try:
            event = await guild.fetch_scheduled_event(card["event_id"], with_counts=True)
        except discord.HTTPException as exc:
            logger.info("Couldn't read event %s's Interested count: %s", card["event_id"], exc)
            return
        if event.user_count is not None:
            await self.bot.db.record_event_interest(card["card_id"], event.user_count)

    # -- moderation -----------------------------------------------------------

    @commands.Cog.listener()
    async def on_audit_log_entry_create(self, entry: discord.AuditLogEntry) -> None:
        if self._ours(entry.guild.id) is None:
            return
        row = entry_action(entry)
        if row is None:
            return
        try:
            await self.bot.db.record_moderation_actions(entry.guild.id, [row])
        except Exception:
            logger.exception("Recording audit log entry %s failed", entry.id)

    @tasks.loop(hours=AUDIT_EVERY_HOURS)
    async def read_audit_log(self) -> None:
        try:
            guild = self._guild()
            if guild is not None:
                await self.catch_up_audit_log(guild, self.now())
        except Exception:
            logger.exception("Reading the audit log failed; it's tried again in %d hours", AUDIT_EVERY_HOURS)

    @read_audit_log.before_loop
    async def before_read_audit_log(self) -> None:
        await self.bot.wait_until_ready()

    async def catch_up_audit_log(self, guild: discord.Guild, now: datetime) -> int:
        """Read the audit log back from a day before the last completed read (the first time, as far
        as Discord keeps it), so anything missed while the bot was down is counted - once, by id.
        From the last read, not the newest entry kept: a live entry recorded just after a restart
        would otherwise hide the downtime before it."""
        state = await self._start(guild)
        oldest = now - timedelta(days=AUDIT_LOG_DAYS)
        checked = state.get("audit_checked_at")
        latest = None if checked else await self.bot.db.latest_moderation_action(guild.id)
        mark = checked or latest
        after = max(_parse(mark) - timedelta(days=1), oldest) if mark else oldest
        rows = []
        for name in AUDIT_ACTIONS:
            try:
                async for entry in guild.audit_logs(limit=None, after=after, action=getattr(discord.AuditLogAction, name)):
                    row = entry_action(entry)
                    if row is not None:
                        rows.append(row)
            except discord.Forbidden as exc:
                if not self._audit_warned:
                    logger.warning("Can't read the audit log (the bot needs View Audit Log), so moderation isn't "
                                   "counted: %s", exc)
                self._audit_warned = True
                return 0
        self._audit_warned = False
        first = _time(after) if state.get("audit_read_from") is None else None
        await self.bot.db.record_moderation_actions(guild.id, rows, read_from=first, checked_at=_time(now))
        if first is not None:
            state["audit_read_from"] = first
        state["audit_checked_at"] = _time(now)
        return len(rows)

    # -- the one-time read ----------------------------------------------------

    @tasks.loop(count=1)
    async def read_history(self) -> None:
        try:
            guild = self._guild()
            if guild is not None:
                state = await self._start(guild)
                if state["history_read_at"] is None:
                    await self.read_chat_history(guild, _parse(state["tracking_since"]))
                await self.clean_system_posts(guild, self._started)
        except Exception:
            logger.exception("Reading chat history for server analytics failed; it's tried again on the next start")

    @read_history.before_loop
    async def before_read_history(self) -> None:
        await self.bot.wait_until_ready()

    async def read_chat_history(self, guild: discord.Guild, until: datetime) -> None:
        """Who posted on which day, and how many posts per hour and per channel, over the
        HISTORY_DAYS before `until` (when live counting began) - authors, times and channels
        only, from every channel the bot can read."""
        since = until - timedelta(days=HISTORY_DAYS)
        days: set[tuple[int, str]] = set()
        hours: Counter = Counter()
        per_channel: Counter = Counter()
        read = 0
        for channel in list(guild.text_channels) + list(guild.voice_channels) + await recent_threads(guild, since):
            if not _readable(channel, guild.me):
                continue
            try:
                async for message in channel.history(after=since, before=until, limit=None):
                    if message.author.bot or message.type not in INTRO_TYPES:
                        continue
                    day = message.created_at.strftime(DAY_FORMAT)
                    days.add((message.author.id, day))
                    hours[message.created_at.strftime(HOUR_FORMAT)] += 1
                    per_channel[(_channel_id(channel), day)] += 1
                read += 1
            except discord.HTTPException as exc:
                logger.warning("Couldn't read history in channel %s: %s", channel.id, exc)
        if await self.bot.db.seed_analytics_history(guild.id, days, dict(hours), dict(per_channel)):
            if self._state is not None:
                self._state["history_read_at"] = _time(self.now())
            logger.info("Server analytics read %d days of chat: %d posts across %d channels", HISTORY_DAYS,
                        sum(hours.values()), read)

    async def clean_system_posts(self, guild: discord.Guild, until: datetime) -> int:
        """Once (audit 2026-11-04): until then Discord's system messages - the member's own "X
        joined" one, boosts, pins - were counted as posts. Re-read the chat those counts cover and
        take them back out: from the hour and channel totals, and a member's day where that was
        all there was (no real post, no voice). Recorded in one transaction with the done mark."""
        state = await self._start(guild)
        if state.get("system_cleaned_at") is not None or state["history_read_at"] is None:
            return 0
        tracking = _parse(state["tracking_since"])
        since = tracking - timedelta(days=HISTORY_DAYS)
        real: set[tuple[int, str]] = set()
        system: set[tuple[int, str]] = set()
        hours: Counter = Counter()
        new_hours: Counter = Counter()
        per_channel: Counter = Counter()
        for channel in list(guild.text_channels) + list(guild.voice_channels) + await recent_threads(guild, since):
            if not _readable(channel, guild.me):
                continue
            try:
                async for message in channel.history(after=since, before=until, limit=None):
                    if message.author.bot:
                        continue
                    moment = message.created_at
                    key = (message.author.id, moment.strftime(DAY_FORMAT))
                    if message.type in INTRO_TYPES:
                        real.add(key)
                        continue
                    system.add(key)
                    hours[moment.strftime(HOUR_FORMAT)] += 1
                    per_channel[(_channel_id(channel), key[1])] += 1
                    if moment >= tracking and is_new(getattr(message.author, "joined_at", None), moment):
                        new_hours[moment.strftime(HOUR_FORMAT)] += 1  # only live counting had a newcomer series
            except discord.HTTPException as exc:
                logger.warning("Couldn't read history in channel %s: %s", channel.id, exc)
        voice = await self.bot.db.voice_day_pairs(guild.id, since.strftime(DAY_FORMAT))
        drop = {key for key in system if key not in real and key not in voice}
        if await self.bot.db.remove_system_posts(guild.id, drop, dict(hours), dict(new_hours), dict(per_channel)):
            state["system_cleaned_at"] = _time(self.now())
            logger.info("Server analytics took out %d system messages (%d member-days that were only that)",
                        sum(hours.values()), len(drop))
        return len(drop)

    # -- the report -----------------------------------------------------------

    async def report(self, guild: discord.Guild, start: datetime, end: datetime, title: str) -> list[str]:
        return report_blocks(await self.build(guild, start, end, title))

    async def build(self, guild: discord.Guild, start: datetime, end: datetime, title: str) -> Report:
        since = min(start, end - timedelta(days=2 * RETENTION_DAYS))
        data = await self.bot.db.analytics_data(guild.id, _time(start), _time(end), _time(since),
                                                int(start.timestamp()), int(end.timestamp()), ATTENDED_MINUTES,
                                                mod_since=_time(start - (end - start)),
                                                first_day=start.astimezone(EASTERN).date().isoformat(),
                                                last_day=(end - timedelta(seconds=1)).astimezone(EASTERN).date().isoformat())
        state = data["state"] or await self._start(guild)
        tracking = _parse(state["tracking_since"])
        chat_from = tracking - timedelta(days=HISTORY_DAYS) if state["history_read_at"] else tracking
        seeded = data["activity_seeded_at"]
        active: dict[int, set[date]] = defaultdict(set)
        for row in data["active"]:
            active[row["user_id"]].add(date.fromisoformat(row["day"]))
        names = {c.id: c.name for c in list(guild.text_channels) + list(guild.forums) if _readable(c, guild.me)}
        moderation = [ModAction(r["kind"], _parse(r["at"]), r["count"], r["moderator_id"], bool(r["by_bot"]))
                      for r in data["moderation"]]
        # Joins recorded before inviters were kept: who made their invite, if it's still up.
        makers = {i.code: _inviter(i) for i in await self._read_invites(guild) or []}
        movements = [Movement(r["user_id"], r["kind"], _parse(r["at"]), r["invite_code"],
                              r["inviter_id"] if r["inviter_id"] is not None else makers.get(r["invite_code"]))
                     for r in data["movements"]]
        built = build_report(
            title=title, start=start, end=end, tracking_since=tracking, chat_from=chat_from,
            movements=movements,
            active=active,
            members={m.id: m.joined_at for m in guild.members if not m.bot and m.joined_at is not None},
            last_active={r["user_id"]: _parse(r["last_active"]) for r in data["last_active"]},
            seen_from=self._seen_from(seeded, data["voice_history_start"]),
            hours=[(datetime.strptime(r["hour"], HOUR_FORMAT).replace(tzinfo=UTC), r["messages"], r["voice_minutes"],
                    r["new_messages"], r["new_voice_minutes"]) for r in data["hours"]],
            channel_counts={r["channel_id"]: r["messages"] for r in data["channels"]},
            channel_names=names, ops=data["ops"],
            events=[EventResult(r["name"], datetime.fromtimestamp(r["starts_at"], UTC), r["going"], r["interested"],
                                r["attended"], r["going_came"])
                    for r in data["events"] if r["starts_at"] >= tracking.timestamp()],
            labels=data["labels"],
            moderation=moderation, bot_names=self._names({a.moderator_id for a in moderation if a.by_bot}, guild),
            moderation_from=_parse(state["audit_read_from"]) if state.get("audit_read_from") else None,
            referrer_names=self._names({m.inviter_id for m in movements if m.inviter_id}, guild),
            timezones=await self._timezones(guild),
            joined_all=[m.joined_at for m in guild.members if not m.bot and m.joined_at is not None],
            newcomer_since=(datetime.strptime(data["newcomer_since"], HOUR_FORMAT).replace(tzinfo=UTC)
                            if data["newcomer_since"] else None),
            # A report that ends now also knows who's here now: a leave missed while the bot was down
            # doesn't count as kept. Lapsed is always as of now - the activity times are today's.
            present={m.id for m in guild.members if not m.bot} if self.now() - end < timedelta(hours=1) else None,
            lapsed_at=self.now())
        return built

    async def _timezones(self, guild: discord.Guild) -> list[tuple[str, int]]:
        """Every member (not bots) by their Onboarding timezone role."""
        picker = await self.bot.db.get_role_picker_roles(guild.id)
        zones = []
        for m in guild.members:
            if m.bot:
                continue
            flags = getattr(m, "flags", None)
            onboarding = onboarding_status(m.joined_at, bool(getattr(flags, "started_onboarding", False)),
                                           bool(getattr(flags, "completed_onboarding", False)))
            zones.append(zone_bucket(member_picks({r.id for r in getattr(m, "roles", ())}, picker)[0], onboarding))
        return timezone_counts(zones)

    @staticmethod
    def _seen_from(seeded: str | None, voice_start: str | None) -> datetime | None:
        """How far back "no activity seen" means silent: chat goes back INACTIVE_DAYS before
        welcome-back's seed, voice only to when voice ranks began recording - the later of the two,
        or a voice-only member would read as silent before then (welcome-back's own COMM-2 guard)."""
        if not seeded or not voice_start:
            return None
        chat = _parse(seeded) - timedelta(days=INACTIVE_DAYS)
        return max(chat, datetime.strptime(voice_start, DAY_FORMAT).replace(tzinfo=UTC))

    def _names(self, user_ids: set, guild: discord.Guild) -> dict[int, str]:
        names = {}
        for uid in user_ids:
            who = guild.get_member(uid) or self.bot.get_user(uid)
            if who is not None:
                names[uid] = _who(who)
        return names

    @app_commands.command(name="server-stats",
                          description="Owner only: joins, activity, retention, ops and lapsed members, last 30 days.")
    @app_commands.describe(export="Get the scorecard and breakdowns as a CSV file instead")
    @app_commands.default_permissions(administrator=True)  # hidden from members
    async def server_stats(self, interaction: discord.Interaction, export: bool = False) -> None:
        if not await self.bot.is_owner(interaction.user):
            await interaction.response.send_message("This command is for the bot's owner.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        guild = self._guild()
        if guild is None:
            await interaction.followup.send(f"I can't find {COMMUNITY_NAME} right now.", ephemeral=True)
            return
        end = self.now()
        built = await self.build(guild, end - timedelta(days=PERIOD_DAYS), end, f"last {PERIOD_DAYS} days")
        if export:
            stamp = end.astimezone(EASTERN).date().isoformat()
            file = discord.File(io.BytesIO(report_csv(built).encode("utf-8-sig")), filename=f"server-stats-{stamp}.csv")
            await interaction.followup.send(f"Server stats, last {PERIOD_DAYS} days, as CSV.", file=file, ephemeral=True)
            return
        for view in stats_views(report_blocks(built)):
            await interaction.followup.send(view=view, ephemeral=True)

    # -- the monthly DM -------------------------------------------------------

    @tasks.loop(hours=1)
    async def monthly(self) -> None:
        try:
            await self.send_monthly(self.now())
        except Exception:
            logger.exception("The monthly server stats DM failed")

    @monthly.before_loop
    async def before_monthly(self) -> None:
        await self.bot.wait_until_ready()

    async def send_monthly(self, now: datetime) -> bool:
        """Last month's report to the owner, once, from REPORT_HOUR Eastern on the 1st. Claimed
        before sending, so a restart can't send it twice; a month before tracking is skipped."""
        guild = self._guild()
        if guild is None or now.astimezone(EASTERN).hour < REPORT_HOUR:
            return False
        state = await self._start(guild)
        start, end, title, month = previous_month(now)
        if (state.get("last_report_month") or "") >= month:
            return False
        # Built before it's claimed: a report that fails to build is tried again next hour, not lost.
        blocks = await self.report(guild, start, end, title) if _parse(state["tracking_since"]) < end else []
        if not await self.bot.db.claim_analytics_report(guild.id, month):
            return False
        state["last_report_month"] = month
        if _parse(state["tracking_since"]) >= end:
            return False
        owner_id = await self._owner_id()
        outcomes = [await send_dm(self.bot, owner_id, label="monthly server stats", view=view)
                    for view in stats_views(blocks)]
        return all(outcome is Delivery.DELIVERED for outcome in outcomes)

    async def _owner_id(self) -> int:
        info = await self.bot.application_info()
        return info.team.owner_id if info.team and info.team.owner_id else info.owner.id

    # -- naming invites -------------------------------------------------------

    @app_commands.command(name="invite-label",
                          description="Owner only: name an invite link, so members who join through it show where they came from.")
    @app_commands.describe(invite="The invite link or code; leave out to list every invite",
                           label="What to call it, e.g. Reddit ad; leave out to see its name")
    @app_commands.default_permissions(administrator=True)
    async def invite_label(self, interaction: discord.Interaction, invite: str | None = None,
                           label: str | None = None) -> None:
        if not await self.bot.is_owner(interaction.user):
            await interaction.response.send_message("This command is for the bot's owner.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        guild = self._guild()
        labels = await self.bot.db.get_invite_labels()
        live = await self._read_invites(guild) if guild is not None else None
        if invite is None:
            await interaction.followup.send(invite_list_text(live, labels), ephemeral=True,
                                            allowed_mentions=discord.AllowedMentions.none())
            return
        code = invite_code(invite)
        name = clean_label(label or "")
        known = live is None or any(i.code == code for i in live)
        if not name:
            maker = next((_inviter(i) for i in live or [] if i.code == code), None)
            await interaction.followup.send(f"`{code}` shows as **{source_of(code, labels, maker)}**.", ephemeral=True)
            return
        await self.bot.db.set_invite_label(code, name)
        note = "" if known else " No invite on the server has that code right now, so check it's right."
        await interaction.followup.send(f"Members who join through `{code}` now show as **{name}**.{note}",
                                        ephemeral=True, allowed_mentions=discord.AllowedMentions.none())

    @app_commands.command(name="join-cards",
                          description="Owner only: where the join and leave cards for staff go (default #staff).")
    @app_commands.describe(channel="Post the cards here instead", off="Stop posting them")
    @app_commands.default_permissions(administrator=True)
    async def join_cards(self, interaction: discord.Interaction, channel: discord.TextChannel | None = None,
                         off: bool = False) -> None:
        if not await self.bot.is_owner(interaction.user):
            await interaction.response.send_message("This command is for the bot's owner.", ephemeral=True)
            return
        guild = self._guild()
        if guild is None:
            await interaction.response.send_message("I can't find the server right now.", ephemeral=True)
            return
        await self._start(guild)
        if off:
            await self.bot.db.set_join_cards_channel(guild.id, 0)
            text = "Join and leave cards are off. Turn them back on with `/join-cards channel:`."
        elif channel is not None:
            if channel.guild.id != guild.id:
                await interaction.response.send_message("Pick a channel in Star Tech Dynamics.", ephemeral=True)
                return
            await self.bot.db.set_join_cards_channel(guild.id, channel.id)
            self._cards_warned = False
            perms = channel.permissions_for(channel.guild.me)
            note = "" if perms.send_messages and perms.embed_links else (
                " I can't post there yet: the bot needs Send Messages and Embed Links in that channel.")
            text = f"Join and leave cards now go to {channel.mention}.{note}"
        else:
            setting = await self.bot.db.get_join_cards_channel(guild.id)
            text = ("Join and leave cards are off." if setting == 0
                    else f"Join and leave cards go to <#{setting or STAFF_CHANNEL_ID}>"
                    + (" (the default)." if setting is None else "."))
        await interaction.response.send_message(text, ephemeral=True)

    @invite_label.autocomplete("invite")
    async def invite_autocomplete(self, interaction: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
        labels = await self.bot.db.get_invite_labels()
        uses = self.invites or {}
        codes = sorted(set(uses) | set(labels), key=lambda c: (-(uses[c].uses if c in uses else -1), c))
        text = current.strip().lower()
        choices = []
        for code in codes:
            name = f"{code} · {labels.get(code, 'no name')}" + (f" · {uses[code].uses} uses" if code in uses else "")
            if text in name.lower():
                choices.append(app_commands.Choice(name=name[:100], value=code))
        return choices[:25]


def invite_list_text(live: list | None, labels: dict[str, str]) -> str:
    lines = ["**Invites and what joins through them show as**"]
    if live is None:
        lines.append("-# I can't read the server's invites: the bot needs Manage Server. Names saved so far:")
        lines += [f"`{code}` · {label}" for code, label in sorted(labels.items(), key=lambda kv: kv[1])]
        return "\n".join(lines)[:2000]
    def line(inv, name: str) -> str:
        where = f" · #{inv.channel.name}" if getattr(inv, "channel", None) is not None else ""
        limit = f"/{inv.max_uses}" if inv.max_uses else ""
        return f"`{inv.code}` · {name} · {inv.uses or 0}{limit} uses{where}"

    by_uses = sorted(live, key=lambda i: (-(i.uses or 0), i.code))
    lines += [line(inv, f"**{labels[inv.code]}**") for inv in by_uses if inv.code in labels]
    members = [inv for inv in by_uses if inv.code not in labels and _inviter(inv) is not None]
    if members:
        used = [inv for inv in members if inv.uses]
        unused = len(members) - len(used)
        lines.append(f"**{MEMBER_REFERRAL}**: {len(members)} invite{'s' if len(members) != 1 else ''} made by members, "
                     f"{sum(inv.uses or 0 for inv in members)} uses" + (f" ({unused} unused)" if unused else ""))
        lines += ["-# " + line(inv, _who(inv.inviter)) for inv in used[:5]]
        if len(used) > 5:
            lines.append(f"-# and {len(used) - 5} more")
    others = [inv for inv in by_uses if inv.code not in labels and _inviter(inv) is None]
    lines += [line(inv, "**no name**") for inv in others if inv.uses]
    idle = sum(1 for inv in others if not inv.uses)
    if idle:
        lines.append(f"-# {idle} unused invite{'s' if idle != 1 else ''} with no name not shown")
    gone = sorted(set(labels) - {i.code for i in live})
    if gone:
        lines.append("-# Named but no longer on the server: " + ", ".join(f"`{c}` ({labels[c]})" for c in gone))
    lines.append("-# Name one with `/invite-label invite:<code> label:<name>`.")
    text = "\n".join(lines)
    return text if len(text) <= 2000 else text[:1990].rsplit("\n", 1)[0] + "\n…"


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(ServerAnalytics(bot))
