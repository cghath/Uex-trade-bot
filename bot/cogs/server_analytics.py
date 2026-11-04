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
  the op runs, and the most "Interested" Discord showed.
The first start reads the 30 days of chat before tracking began (authors, times and channels
only, like welcome-back's read) so the activity numbers don't start from nothing. Joins, leaves,
sources, voice-by-hour and attendance can't be read back, so they start when this shipped.
"""
from __future__ import annotations

import asyncio
import logging
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta

import discord
from discord import app_commands
from discord.ext import commands, tasks

from bot.cogs.anniversaries import ANNIVERSARY_CHANNEL_ID
from bot.cogs.voice_ranks import OPS_CATEGORY_ID
from bot.cogs.welcome_back import recent_threads
from bot.delivery import Delivery, home_channel, send_dm
from bot.discord_ui import LAYOUT_TEXT_LIMIT, BotLayoutView
from bot.uex.anniversaries import COMMUNITY_NAME, INACTIVE_DAYS
from bot.uex.server_analytics import (
    ATTENDED_MINUTES, DAY_FORMAT, EASTERN, HOUR_FORMAT, PERIOD_DAYS, RETENTION_DAYS, TIME_FORMAT, UTC, EventResult,
    InviteUse, Movement, build_report, clean_label, event_running, match_invite, previous_month, report_blocks,
    source_of,
)
from bot.uex.voice_ranks import counts

logger = logging.getLogger("uexbot.server_analytics")

HISTORY_DAYS = 30
INTEREST_EVERY = 10   # minutes between reads of a running event's Interested count
INVITE_RETRY = 10     # minutes between tries to read invites while the bot can't
REPORT_HOUR = 9       # the monthly DM goes from 9 AM Eastern on the 1st


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


def stats_view(blocks: list[str]) -> BotLayoutView:
    """The report as one layout message, sections split by lines; trailing sections are left out
    rather than going over Discord's text limit."""
    view = BotLayoutView(timeout=None)
    container = discord.ui.Container(accent_colour=discord.Colour.blurple())
    used = 0
    for block in blocks:
        if used + len(block) > LAYOUT_TEXT_LIMIT:
            break
        if used:
            container.add_item(discord.ui.Separator())
        container.add_item(discord.ui.TextDisplay(block))
        used += len(block)
    view.add_item(container)
    return view


def invite_code(text: str) -> str:
    """The code from a pasted invite link or a bare code."""
    return text.strip().rstrip("/").rsplit("/", 1)[-1]


class ServerAnalytics(commands.Cog):
    def __init__(self, bot: commands.Bot, *, start_loop: bool = True) -> None:
        self.bot = bot
        self.invites: dict[str, InviteUse] | None = None  # use counts as last read
        self._invite_lock = asyncio.Lock()
        self._invites_warned = False
        self._state: dict | None = None
        self._seen: dict[str, set[int]] = {}               # day -> members already marked active
        self._messages: Counter = Counter()                # hour -> posts not yet saved
        self._voice: Counter = Counter()                   # hour -> voice minutes not yet saved
        self._channels: Counter = Counter()                # (channel_id, day) -> posts not yet saved
        self._minute = 0
        self.now = lambda: datetime.now(UTC)  # the clock; tests set their own
        if start_loop:
            self.tick.start()
            self.monthly.start()
            self.read_history.start()

    def cog_unload(self) -> None:
        self.tick.cancel()
        self.monthly.cancel()
        self.read_history.cancel()

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
        if message.guild is None or message.author.bot or self._ours(message.guild.id) is None:
            return
        moment = message.created_at
        if self._state is not None:  # tracking has begun: the history read covers what came before
            self._messages[moment.strftime(HOUR_FORMAT)] += 1
            self._channels[(_channel_id(message.channel), moment.strftime(DAY_FORMAT))] += 1
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
            code = await self.joined_with(guild)
            await self.bot.db.record_member_movement(guild.id, member.id, "join", _time(self.now()), code)
            logger.info("Member %s joined (invite %s)", member.id, code or "not known")
        except Exception:
            logger.exception("Recording member %s's join failed", member.id)

    @commands.Cog.listener()
    async def on_raw_member_remove(self, payload: discord.RawMemberRemoveEvent) -> None:
        if self._ours(payload.guild_id) is None or payload.user.bot:
            return
        try:
            await self.bot.db.record_member_movement(payload.guild_id, payload.user.id, "leave",
                                                     _time(self.now()))
        except Exception:
            logger.exception("Recording member %s leaving failed", payload.user.id)

    @commands.Cog.listener()
    async def on_invite_create(self, invite: discord.Invite) -> None:
        if self.invites is not None and invite.guild is not None and self._ours(invite.guild.id) is not None:
            self.invites[invite.code] = InviteUse(invite.uses or 0, invite.max_uses or 0)

    async def _read_invites(self, guild: discord.Guild) -> list[discord.Invite] | None:
        try:
            invites = await guild.invites()
        except discord.HTTPException as exc:
            if not self._invites_warned:
                logger.warning("Can't read invites (the bot needs Manage Server), so join sources aren't known: %s", exc)
            self._invites_warned = True
            return None
        self._invites_warned = False
        return invites

    async def _snapshot(self, guild: discord.Guild) -> dict[str, InviteUse] | None:
        invites = await self._read_invites(guild)
        return None if invites is None else {i.code: InviteUse(i.uses or 0, i.max_uses or 0) for i in invites}

    async def joined_with(self, guild: discord.Guild) -> str | None:
        """The invite a member who just joined used, by which use count went up. One join at a
        time, so two close together each compare against the count the other left."""
        async with self._invite_lock:
            after = await self._snapshot(guild)
            if after is None:
                return None
            before, self.invites = self.invites, after
            return match_invite(before, after) if before is not None else None

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
            in_voice = sum(1 for channel in list(guild.voice_channels) + list(guild.stage_channels)
                           if channel.id != afk for m in channel.members if not m.bot)
            if in_voice:
                self._voice[now.strftime(HOUR_FORMAT)] += in_voice
        except Exception:
            logger.exception("Counting who's in voice failed")
        await self._flush(guild.id)
        try:
            await self.count_attendance(guild, now)
        except Exception:
            logger.exception("Counting event attendance failed")

    async def _flush(self, guild_id: int) -> None:
        messages, voice, channels = self._messages, self._voice, self._channels
        if not (messages or voice or channels):
            return
        self._messages, self._voice, self._channels = Counter(), Counter(), Counter()
        hours = {hour: (messages[hour], voice[hour]) for hour in set(messages) | set(voice)}
        try:
            await self.bot.db.add_activity_counts(guild_id, hours, dict(channels))
        except Exception:
            self._messages.update(messages)
            self._voice.update(voice)
            self._channels.update(channels)
            logger.exception("Saving activity counts failed; they're kept for the next minute")

    async def count_attendance(self, guild: discord.Guild, now: datetime) -> None:
        """A minute for everyone in each running event's voice channel or Ops (not deafened)."""
        stamp = int(now.timestamp())
        cards = [c for c in await self.bot.db.open_event_cards(guild.id) if event_running(c["state"], c["starts_at"], stamp)]
        if not cards:
            return
        channels = list(guild.voice_channels) + list(guild.stage_channels)
        ops = {c.id for c in channels if c.category_id == OPS_CATEGORY_ID}
        for card in cards:
            where = ops | ({card["voice_channel_id"]} if card["voice_channel_id"] else set())
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

    # -- the one-time read ----------------------------------------------------

    @tasks.loop(count=1)
    async def read_history(self) -> None:
        try:
            guild = self._guild()
            if guild is not None:
                state = await self._start(guild)
                if state["history_read_at"] is None:
                    await self.read_chat_history(guild, _parse(state["tracking_since"]))
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
                    if message.author.bot:
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

    # -- the report -----------------------------------------------------------

    async def report(self, guild: discord.Guild, start: datetime, end: datetime, title: str) -> list[str]:
        since = min(start, end - timedelta(days=2 * RETENTION_DAYS))
        data = await self.bot.db.analytics_data(guild.id, _time(start), _time(end), _time(since),
                                                int(start.timestamp()), int(end.timestamp()), ATTENDED_MINUTES)
        state = data["state"] or await self._start(guild)
        tracking = _parse(state["tracking_since"])
        chat_from = tracking - timedelta(days=HISTORY_DAYS) if state["history_read_at"] else tracking
        seeded = data["activity_seeded_at"]
        active: dict[int, set[date]] = defaultdict(set)
        for row in data["active"]:
            active[row["user_id"]].add(date.fromisoformat(row["day"]))
        names = {c.id: c.name for c in list(guild.text_channels) + list(guild.forums) if _readable(c, guild.me)}
        built = build_report(
            title=title, start=start, end=end, tracking_since=tracking, chat_from=chat_from,
            movements=[Movement(r["user_id"], r["kind"], _parse(r["at"]), r["invite_code"]) for r in data["movements"]],
            active=active,
            members={m.id: m.joined_at for m in guild.members if not m.bot and m.joined_at is not None},
            last_active={r["user_id"]: _parse(r["last_active"]) for r in data["last_active"]},
            seen_from=_parse(seeded) - timedelta(days=INACTIVE_DAYS) if seeded else None,
            hours=[(datetime.strptime(r["hour"], HOUR_FORMAT).replace(tzinfo=UTC), r["messages"], r["voice_minutes"])
                   for r in data["hours"]],
            channel_counts={r["channel_id"]: r["messages"] for r in data["channels"]},
            channel_names=names, ops=data["ops"],
            events=[EventResult(r["name"], datetime.fromtimestamp(r["starts_at"], UTC), r["going"], r["interested"],
                                r["attended"], r["going_came"])
                    for r in data["events"] if r["starts_at"] >= tracking.timestamp()],
            labels=data["labels"])
        return report_blocks(built)

    @app_commands.command(name="server-stats",
                          description="Owner only: joins, activity, retention, ops and lapsed members, last 30 days.")
    @app_commands.default_permissions(administrator=True)  # hidden from members
    async def server_stats(self, interaction: discord.Interaction) -> None:
        if not await self.bot.is_owner(interaction.user):
            await interaction.response.send_message("This command is for the bot's owner.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        guild = self._guild()
        if guild is None:
            await interaction.followup.send(f"I can't find {COMMUNITY_NAME} right now.", ephemeral=True)
            return
        end = self.now()
        blocks = await self.report(guild, end - timedelta(days=PERIOD_DAYS), end, f"last {PERIOD_DAYS} days")
        await interaction.followup.send(view=stats_view(blocks), ephemeral=True)

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
        if (state.get("last_report_month") or "") >= month or not await self.bot.db.claim_analytics_report(guild.id, month):
            return False
        state["last_report_month"] = month
        if _parse(state["tracking_since"]) >= end:
            return False
        owner_id = await self._owner_id()
        outcome = await send_dm(self.bot, owner_id, label="monthly server stats",
                                view=stats_view(await self.report(guild, start, end, title)))
        return outcome is Delivery.DELIVERED

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
            await interaction.followup.send(f"`{code}` shows as **{source_of(code, labels)}**.", ephemeral=True)
            return
        await self.bot.db.set_invite_label(code, name)
        note = "" if known else " No invite on the server has that code right now, so check it's right."
        await interaction.followup.send(f"Members who join through `{code}` now show as **{name}**.{note}",
                                        ephemeral=True, allowed_mentions=discord.AllowedMentions.none())

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
    for inv in sorted(live, key=lambda i: (-(i.uses or 0), i.code)):
        where = f" · #{inv.channel.name}" if getattr(inv, "channel", None) is not None else ""
        limit = f"/{inv.max_uses}" if inv.max_uses else ""
        lines.append(f"`{inv.code}` · **{labels.get(inv.code, 'no name')}** · {inv.uses or 0}{limit} uses{where}")
    gone = sorted(set(labels) - {i.code for i in live})
    if gone:
        lines.append("-# Named but no longer on the server: " + ", ".join(f"`{c}` ({labels[c]})" for c in gone))
    lines.append("-# Name one with `/invite-label invite:<code> label:<name>`.")
    text = "\n".join(lines)
    return text if len(text) <= 2000 else text[:1990].rsplit("\n", 1)[0] + "\n…"


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(ServerAnalytics(bot))
