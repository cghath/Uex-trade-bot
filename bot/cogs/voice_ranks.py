"""Voice-activity ranks (rules in bot/uex/voice_ranks.py): ranks earned from time in voice with
other people, and a second ladder for ops attended. Replaces Arcane's levels; nothing carried over.

Once a minute the bot reads who is in which voice channel - Discord keeps that current through
voice state events (the Guild Voice States intent, on by default) - and credits each counted
member. Nothing is read but channel membership and the mute/deafen flags: never audio. A restart
needs no catching up: the next minute simply sees whoever is in voice then, and an op in progress
is saved every minute and carried on.

Rank roles are created by name the first time they're needed (the bot needs Manage Roles, with its
own role above the rank roles). Each person holds only their highest rank, and only their highest
op rank. A rank-up is claimed in voice_members before it's announced in #stdynamics-bot, so it's
never announced twice; roles are also put right every RECONCILE_MINUTES, so a failed role change
fixes itself.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone

import discord
from discord import app_commands
from discord.ext import commands, tasks

from bot.cogs.anniversaries import ANNIVERSARY_CHANNEL_ID
from bot.delivery import owner_only_mentions, send_to_channel
from bot.uex.voice_ranks import (
    DAILY_CAP_MINUTES, OP_GRACE_MINUTES, OP_RANKS, OPS_XP_PER_MINUTE, RANKS, OpSession, bar, counted_members,
    counts, hours, op_attendees, op_promotion_text, op_step, progress, promotion_text, rank_index, rank_name,
    season_name, season_start, week_start,
)

logger = logging.getLogger("uexbot.voice_ranks")

# The "Ops" category on Star Tech Dynamics: double XP, and where ops are counted. A new channel in
# it counts without a code change.
OPS_CATEGORY_ID = 1562819455451533362
# Rank-ups post where anniversaries do, #stdynamics-bot (the owner's pick: "the bot channel or
# general").
ANNOUNCE_CHANNEL_ID = ANNIVERSARY_CHANNEL_ID
RECONCILE_MINUTES = 30
LEADERBOARD_SIZE = 10
PRIVACY_NOTE = ("-# Counts minutes in voice with 2+ people (double in Ops), up to 5h a day. "
                "Only who is in which channel is read, never audio.")


def _name(member: discord.abc.User) -> str:
    return discord.utils.escape_mentions(discord.utils.escape_markdown(member.display_name))


_TIME = "%Y-%m-%d %H:%M:%S"


def _session_to_row(session: OpSession, now: datetime) -> dict:
    return {"started_at": session.started_at.strftime(_TIME), "minutes": session.minutes, "quiet": session.quiet,
            "present": json.dumps({str(k): v for k, v in session.present.items()}), "updated_at": now.strftime(_TIME)}


def _session_from_row(row: dict) -> OpSession:
    started = datetime.strptime(row["started_at"], _TIME).replace(tzinfo=timezone.utc)
    return OpSession(started_at=started, minutes=row["minutes"], quiet=row["quiet"],
                     present={int(k): v for k, v in json.loads(row["present"]).items()})


class VoiceRanks(commands.Cog):
    def __init__(self, bot: commands.Bot, *, start_loop: bool = True) -> None:
        self.bot = bot
        self.op_session: OpSession | None = None
        self._session_loaded = False
        self._roles_warned = False
        if start_loop:
            self.tick.start()
            self.reconcile.start()

    def cog_unload(self) -> None:
        self.tick.cancel()
        self.reconcile.cancel()

    def _guild(self) -> discord.Guild | None:
        channel = self.bot.get_channel(ANNOUNCE_CHANNEL_ID)
        return channel.guild if channel is not None else None

    # -- the minute -----------------------------------------------------------

    @tasks.loop(minutes=1)
    async def tick(self) -> None:
        try:
            await self.count_minute(datetime.now(timezone.utc))
        except Exception:
            logger.exception("Voice rank minute failed")

    @tick.before_loop
    async def before_tick(self) -> None:
        await self.bot.wait_until_ready()

    async def count_minute(self, now: datetime) -> None:
        guild = self._guild()
        if guild is None:
            return
        afk_id = guild.afk_channel.id if guild.afk_channel else None
        credits, op_people = [], set()
        for channel in list(guild.voice_channels) + list(guild.stage_channels):
            ops = channel.category_id == OPS_CATEGORY_ID
            for member in counted_members(channel, afk_id):
                credits.append((member.id, OPS_XP_PER_MINUTE if ops else 1))
            if ops:
                op_people.update(m.id for m in channel.members if counts(m))
        credited = await self.bot.db.credit_voice_minute(guild.id, now.strftime("%Y-%m-%d"), credits,
                                                         DAILY_CAP_MINUTES)
        for user_id, before, after in credited:
            if rank_index(after) > rank_index(before):
                await self._promote(guild, user_id, after)
        await self._step_op(guild, op_people, now)

    async def _step_op(self, guild: discord.Guild, people: set[int], now: datetime) -> None:
        if not self._session_loaded:
            self._session_loaded = True
            row = await self.bot.db.load_op_session(guild.id)
            if row is not None:
                saved = _session_from_row(row)
                updated = datetime.strptime(row["updated_at"], _TIME).replace(tzinfo=timezone.utc)
                if now - updated >= timedelta(minutes=OP_GRACE_MINUTES):
                    # The bot was down longer than an op's grace: it ended while we were away.
                    await self._finish_op(guild, saved, updated)
                else:
                    self.op_session = saved
        self.op_session, ended = op_step(self.op_session, people, now)
        if ended is not None:
            await self._finish_op(guild, ended, now - timedelta(minutes=ended.quiet))
        elif self.op_session is not None:
            await self.bot.db.save_op_session(guild.id, _session_to_row(self.op_session, now))

    async def _finish_op(self, guild: discord.Guild, session: OpSession, ended_at: datetime) -> None:
        attendees = op_attendees(session)
        if not attendees:
            await self.bot.db.save_op_session(guild.id, None)
            return
        counted = await self.bot.db.record_op(
            guild.id, session.started_at.strftime(_TIME), ended_at.strftime(_TIME),
            session.minutes, {uid: session.present[uid] for uid in attendees})
        logger.info("Op over: %d minutes, %d attended", session.minutes, len(attendees))
        for user_id, ops in counted:
            if rank_index(ops, OP_RANKS) >= 0:
                await self._promote(guild, user_id, ops, ops_ladder=True)

    # -- rank-ups and roles ---------------------------------------------------

    async def _promote(self, guild: discord.Guild, user_id: int, value: int, *, ops_ladder: bool = False) -> None:
        ladder = OP_RANKS if ops_ladder else RANKS
        index = rank_index(value, ladder)
        if not await self.bot.db.claim_voice_rank(guild.id, user_id, index, ops=ops_ladder):
            return
        member = guild.get_member(user_id)
        if member is None:
            return
        await self._set_rank_role(guild, member, ladder, index)
        mention = member.mention
        text = (op_promotion_text(mention, rank_name(index, ladder), value) if ops_ladder
                else promotion_text(mention, rank_name(index, ladder), value))
        await send_to_channel(self.bot, ANNOUNCE_CHANNEL_ID, text, label=f"rank-up for {user_id}",
                              allowed_mentions=owner_only_mentions(user_id))

    async def _rank_roles(self, guild: discord.Guild) -> dict[str, discord.Role]:
        """Every rank and op-rank role by name, created when missing. Empty when the bot can't
        manage roles (warned once in the log)."""
        found = {}
        for name, _ in RANKS + OP_RANKS:
            role = discord.utils.get(guild.roles, name=name)
            if role is None:
                try:
                    role = await guild.create_role(name=name, reason="Voice rank")
                except discord.HTTPException as exc:
                    if not self._roles_warned:
                        logger.warning("Can't create voice rank roles (needs Manage Roles): %s", exc)
                        self._roles_warned = True
                    return {}
            found[name] = role
        return found

    async def _set_rank_role(self, guild: discord.Guild, member: discord.Member,
                             ladder: tuple[tuple[str, int], ...], index: int) -> None:
        """Give `member` this ladder's role for `index` and take away its others."""
        roles = await self._rank_roles(guild)
        if not roles:
            return
        wanted = roles.get(rank_name(index, ladder)) if index >= 0 else None
        ladder_roles = {roles[name].id for name, _ in ladder}
        remove = [r for r in member.roles if r.id in ladder_roles and r != wanted]
        try:
            if remove:
                await member.remove_roles(*remove, reason="Voice rank")
            if wanted is not None and wanted not in member.roles:
                await member.add_roles(wanted, reason="Voice rank")
        except discord.HTTPException as exc:
            logger.warning("Couldn't set the voice rank role for member %s: %s", member.id, exc)

    @tasks.loop(minutes=RECONCILE_MINUTES)
    async def reconcile(self) -> None:
        """Put everyone's rank roles right: fixes a role change that failed, or roles edited by
        hand."""
        try:
            guild = self._guild()
            if guild is None:
                return
            for row in await self.bot.db.get_voice_members(guild.id):
                member = guild.get_member(row["user_id"])
                if member is not None:
                    await self._set_rank_role(guild, member, RANKS, rank_index(row["xp"]))
                    await self._set_rank_role(guild, member, OP_RANKS, rank_index(row["ops"], OP_RANKS))
        except Exception:
            logger.exception("Voice rank role check failed")

    @reconcile.before_loop
    async def before_reconcile(self) -> None:
        await self.bot.wait_until_ready()

    # -- commands -------------------------------------------------------------

    @app_commands.command(name="rank", description="Your voice rank, or another member's: time in voice and ops attended.")
    @app_commands.describe(member="Whose rank to show (yours if left empty)")
    async def rank(self, interaction: discord.Interaction, member: discord.Member | None = None) -> None:
        member = member or interaction.user
        guild = self._guild()
        row = await self.bot.db.get_voice_member(guild.id, member.id) if guild else None
        today = datetime.now(timezone.utc).date()
        week = await self.bot.db.voice_totals_since(guild.id, week_start(today).isoformat()) if guild else []
        await interaction.response.send_message(
            rank_text(_name(member), row, next((r["minutes"] for r in week if r["user_id"] == member.id), 0)),
            allowed_mentions=discord.AllowedMentions.none())

    @app_commands.command(name="rank-leaderboard", description="Who has spent the most time in voice: this week, this season or all time.")
    @app_commands.describe(period="Which standings to show")
    @app_commands.choices(period=[
        app_commands.Choice(name="This week", value="week"),
        app_commands.Choice(name="This season", value="season"),
        app_commands.Choice(name="All time", value="all"),
        app_commands.Choice(name="Ops attended", value="ops"),
    ])
    async def rank_leaderboard(self, interaction: discord.Interaction,
                               period: app_commands.Choice[str] | None = None) -> None:
        guild = self._guild()
        if guild is None:
            await interaction.response.send_message("Voice ranks aren't available right now.", ephemeral=True)
            return
        key = period.value if period else "week"
        today = datetime.now(timezone.utc).date()
        if key == "ops":
            rows = sorted((r for r in await self.bot.db.get_voice_members(guild.id) if r["ops"] > 0),
                          key=lambda r: (-r["ops"], r["user_id"]))
        else:
            since = {"week": week_start(today), "season": season_start(today), "all": None}[key]
            rows = await self.bot.db.voice_totals_since(guild.id, since.isoformat() if since else None)
        title = {"week": f"This week (since {week_start(today):%a %d %b})", "season": f"Season {season_name(today)}",
                 "all": "All time", "ops": "Ops attended (all time)"}[key]
        await interaction.response.send_message(
            leaderboard_text(title, rows, key, guild.get_member, interaction.user.id),
            allowed_mentions=discord.AllowedMentions.none())


def rank_text(name: str, row: dict | None, week_minutes: int) -> str:
    if row is None or (row["xp"] <= 0 and row["ops"] <= 0):
        return f"🎖️ **{name}** has no voice time yet.\n{PRIVACY_NOTE}"
    main = progress(row["xp"])
    lines = [f"🎖️ **{name}** · **{main.rank or 'Unranked'}**"]
    if main.next_rank:
        lines.append(f"`{bar(main.fraction)}` {main.needed:,} XP to **{main.next_rank}**")
    lines.append(f"{row['xp']:,} XP · {hours(row['minutes'])} in voice · this week {hours(week_minutes)}")
    ops = progress(row["ops"], OP_RANKS)
    op_line = f"🎯 {row['ops']:,} op{'s' if row['ops'] != 1 else ''} attended"
    if ops.rank:
        op_line += f" · **{ops.rank}**"
    if ops.next_rank:
        op_line += f" ({ops.needed} more to {ops.next_rank})"
    lines += [op_line, PRIVACY_NOTE]
    return "\n".join(lines)


def leaderboard_text(title: str, rows: list[dict], key: str, get_member, viewer_id: int) -> str:
    """The top LEADERBOARD_SIZE still in the server, and the viewer's own place when they're
    lower down."""
    ranked = [(r, m) for r in rows if (m := get_member(r["user_id"])) is not None]
    lines = [f"🏆 **Voice leaderboard · {title}**"]
    if not ranked:
        lines.append("Nobody yet - hop in voice with someone.")

    def entry(place: int, row: dict, member) -> str:
        figure = (f"{row['ops']:,} op{'s' if row['ops'] != 1 else ''}" if key == "ops"
                  else f"{row['xp']:,} XP · {hours(row['minutes'])}")
        return f"{place}. {_name(member)} · {figure}"

    for place, (row, member) in enumerate(ranked[:LEADERBOARD_SIZE], 1):
        lines.append(entry(place, row, member))
    mine = next((i for i, (r, _) in enumerate(ranked, 1) if r["user_id"] == viewer_id), None)
    if mine and mine > LEADERBOARD_SIZE:
        lines.append(f"-# You: {entry(mine, *ranked[mine - 1])}")
    return "\n".join(lines)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(VoiceRanks(bot))
