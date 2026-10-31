"""The welcome DM (bot/uex/welcome.py has the wording): once, to each member who joins Star Tech
Dynamics, the first time they join. Uses the Server Members intent, like anniversaries.

Sent when a member joins - or, if the server ever turns on membership screening, once they've
accepted its rules (Discord's `pending` flag), so it arrives when they can use the channel it
names. Each welcome is claimed in `member_welcomes` before it's sent, so a rejoin or a restart
never sends it twice; a send that fails only for now (bot/delivery.py) is retried by a short loop
for a day, and one Discord refuses (DMs closed) isn't. A claim stays 'pending' while its DM is on
its way, so the cog also keeps who it's sending to right now: a retry tick or a second event
landing mid-send skips them rather than sending again (audit COMM-1).
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

import discord
from discord import app_commands
from discord.ext import commands, tasks

from bot.cogs.anniversaries import ANNIVERSARY_CHANNEL_ID, next_event_line
from bot.cogs.welcome_back import welcome_back_view
from bot.delivery import Delivery, home_channel, send_dm
from bot.uex.anniversaries import welcome_back_text
from bot.uex.welcome import welcome_text

logger = logging.getLogger("uexbot.welcome")

RETRY_MINUTES = 10
_STATUS = {Delivery.DELIVERED: "sent", Delivery.UNDELIVERABLE: "skipped"}  # RETRY stays pending


def _name(member: discord.abc.User) -> str:
    return discord.utils.escape_mentions(discord.utils.escape_markdown(member.display_name))


class Welcome(commands.Cog):
    def __init__(self, bot: commands.Bot, *, start_loop: bool = True) -> None:
        self.bot = bot
        self._sending: set[int] = set()  # members whose welcome is being sent right now
        if start_loop:
            self.retry_welcomes.start()

    def cog_unload(self) -> None:
        self.retry_welcomes.cancel()

    def _our_guild(self, guild: discord.Guild) -> bool:
        channel = home_channel(self.bot, ANNIVERSARY_CHANNEL_ID, feature="welcome DMs")
        return channel is not None and channel.guild.id == guild.id

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member) -> None:
        if member.bot or member.pending or not self._our_guild(member.guild):
            return  # a pending member is welcomed once they accept the rules (on_member_update)
        await self.welcome(member)

    @commands.Cog.listener()
    async def on_member_update(self, before: discord.Member, after: discord.Member) -> None:
        if before.pending and not after.pending and not after.bot and self._our_guild(after.guild):
            await self.welcome(after)

    async def welcome(self, member: discord.Member) -> None:
        """Send the welcome unless it has already been sent (or refused) for this member, or is
        being sent right now."""
        if member.id in self._sending:  # checked and taken with no await between: exclusive
            return
        self._sending.add(member.id)
        try:
            if await self.bot.db.claim_welcome(member.guild.id, member.id) != "pending":
                return
            outcome = await send_dm(self.bot, member.id, welcome_text(_name(member), ANNIVERSARY_CHANNEL_ID),
                                    label="welcome")
            if outcome in _STATUS:
                await self.bot.db.set_welcome_status(member.guild.id, member.id, _STATUS[outcome])
        except Exception:  # an event handler: log, never raise into discord.py
            logger.exception("Welcome for member %s failed", member.id)
        finally:
            self._sending.discard(member.id)

    @tasks.loop(minutes=RETRY_MINUTES)
    async def retry_welcomes(self) -> None:
        """Welcomes that failed only for now, claimed within the last day, tried again."""
        try:
            channel = home_channel(self.bot, ANNIVERSARY_CHANNEL_ID, feature="welcome DMs")
            if channel is None:
                return
            for user_id in await self.bot.db.get_pending_welcomes(channel.guild.id):
                member = channel.guild.get_member(user_id)
                if member is not None:
                    await self.welcome(member)
        except Exception:
            logger.exception("Welcome retry failed")

    @retry_welcomes.before_loop
    async def before_retry(self) -> None:
        await self.bot.wait_until_ready()

    @app_commands.command(name="welcome-preview", description="Owner only: send yourself the welcome DM new members get.")
    @app_commands.default_permissions(administrator=True)  # hidden from members (audit COMM-6)
    @app_commands.describe(message="Which DM: for a new member (default) or for someone back after 30+ days")
    @app_commands.choices(message=[app_commands.Choice(name="New member", value="new"),
                                   app_commands.Choice(name="Welcome back", value="back")])
    async def welcome_preview(self, interaction: discord.Interaction,
                              message: app_commands.Choice[str] | None = None) -> None:
        if not await self.bot.is_owner(interaction.user):
            await interaction.response.send_message("This command is for the bot's owner.", ephemeral=True)
            return
        if message is not None and message.value == "back":
            await interaction.response.defer(ephemeral=True)
            channel = self.bot.get_channel(ANNIVERSARY_CHANNEL_ID)
            event = await next_event_line(channel.guild, datetime.now(timezone.utc)) if channel else None
            outcome = await send_dm(self.bot, interaction.user.id, welcome_back_text(_name(interaction.user), event),
                                    label="welcome back preview",
                                    **({"view": welcome_back_view(channel.guild.id)} if channel else {}))
            await interaction.followup.send(
                "I've sent you the welcome-back DM." if outcome is Delivery.DELIVERED
                else "I couldn't DM you - your DMs from server members may be off.", ephemeral=True)
            return
        outcome = await send_dm(self.bot, interaction.user.id,
                                welcome_text(_name(interaction.user), ANNIVERSARY_CHANNEL_ID), label="welcome preview")
        await interaction.response.send_message(
            "I've sent you the welcome DM." if outcome is Delivery.DELIVERED
            else "I couldn't DM you - your DMs from server members may be off.", ephemeral=True)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Welcome(bot))
