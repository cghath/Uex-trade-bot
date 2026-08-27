"""Opt-in DM notification when someone sends a new message in one of the user's UEX
Marketplace negotiations - any listing they're party to, not just ones this bot posted.

Enabling seeds a baseline (every current negotiation's date_modified, and every message
that already exists in it) without notifying, so existing history never floods in as if
it were new - only messages that arrive after opting in ever DM.
"""
from __future__ import annotations

import logging
from typing import Any

import discord
from discord import app_commands
from discord.ext import commands, tasks

from bot.uex.exceptions import UexApiError
from bot.uex.marketplace import parse_uex_number

logger = logging.getLogger("uexbot.negotiation_alerts")

POLL_INTERVAL_MINUTES = 5


def _as_int(value: Any) -> int | None:
    parsed = parse_uex_number(value)
    return int(parsed) if parsed is not None else None


class NegotiationAlerts(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.poll_negotiation_messages.start()

    def cog_unload(self) -> None:
        self.poll_negotiation_messages.cancel()

    @app_commands.command(
        name="negotiation-alerts",
        description="DM me when someone sends a new message in one of my UEX negotiations.",
    )
    @app_commands.describe(enabled="Turn negotiation-message DMs on or off")
    async def negotiation_alerts(self, interaction: discord.Interaction, enabled: bool) -> None:
        if not enabled:
            await self.bot.db.set_negotiation_alerts_enabled(interaction.user.id, False)
            await interaction.response.send_message("Negotiation-message DMs are now **off**.", ephemeral=True)
            return

        secret_key = await self.bot.db.get_user_secret_key(interaction.user.id)
        if not secret_key:
            await interaction.response.send_message(
                "Link your UEX account first with `/link-uex-account`, then enable this.", ephemeral=True
            )
            return

        await interaction.response.defer(ephemeral=True)
        # Enabling and successfully baselining are not the same thing - if the seed can't
        # even fetch a negotiation list (e.g. an invalid secret key), the feature must not
        # turn on with an empty baseline, or the first successful poll later floods every
        # negotiation's entire prior history as if it just happened.
        try:
            seeded = await self._seed_baseline(interaction.user.id, secret_key)
        except UexApiError as exc:
            await interaction.followup.send(
                f"Couldn't enable negotiation alerts - UEX rejected the check: {exc}\n"
                "This usually means your linked secret key isn't valid. Check `/uex-account-status`, "
                "re-link with `/link-uex-account` if needed, then try again.",
                ephemeral=True,
            )
            return

        await self.bot.db.set_negotiation_alerts_enabled(interaction.user.id, True)
        await interaction.followup.send(
            f"Negotiation-message DMs are now **on**. Checked {seeded} existing negotiation(s) as a starting "
            f"point - only messages from here on will DM you, checked every {POLL_INTERVAL_MINUTES} min.",
            ephemeral=True,
        )

    async def _seed_baseline(self, user_id: int, secret_key: str) -> int:
        """Mark everything that already exists as seen, without notifying, so enabling this
        doesn't dump a negotiation's entire prior history as if it just happened.

        Raises UexApiError if the initial fetch fails - the caller must not enable the
        feature on a failed seed. A per-negotiation messages fetch failing is tolerated
        (that negotiation just won't be fully baselined), since it's a narrower miss than
        blocking the whole enable over one flaky call.
        """
        negotiations = await self.bot.uex.get_marketplace_negotiations(secret_key=secret_key)
        for negotiation in negotiations:
            id_negotiation = _as_int(negotiation.get("id"))
            if id_negotiation is None:
                continue
            try:
                messages = await self.bot.uex.get_marketplace_negotiations_messages(
                    secret_key=secret_key, id_negotiation=id_negotiation
                )
            except UexApiError as exc:
                logger.warning("Failed to seed messages for negotiation %s: %s", id_negotiation, exc)
                messages = []
            for row in messages:
                message_id = _as_int(row.get("id"))
                if message_id is not None:
                    await self.bot.db.mark_negotiation_message_seen(message_id)
            await self.bot.db.set_negotiation_last_modified(
                user_id, id_negotiation, _as_int(negotiation.get("date_modified")) or 0
            )
        return len(negotiations)

    @tasks.loop(minutes=POLL_INTERVAL_MINUTES)
    async def poll_negotiation_messages(self) -> None:
        user_ids = await self.bot.db.list_negotiation_alert_user_ids()
        if not user_ids:
            return
        for user_id in user_ids:
            secret_key = await self.bot.db.get_user_secret_key(user_id)
            if not secret_key:
                continue  # unlinked since enabling; nothing to poll until relinked
            try:
                negotiations = await self.bot.uex.get_marketplace_negotiations(secret_key=secret_key)
            except UexApiError as exc:
                logger.warning("Failed to poll negotiations for user %s: %s", user_id, exc)
                continue
            last_modified = await self.bot.db.get_negotiation_last_modified(user_id)
            for negotiation in negotiations:
                id_negotiation = _as_int(negotiation.get("id"))
                date_modified = _as_int(negotiation.get("date_modified"))
                if id_negotiation is None or date_modified is None:
                    continue
                if date_modified <= last_modified.get(id_negotiation, 0):
                    continue
                await self._check_negotiation(user_id, secret_key, negotiation, id_negotiation)
                await self.bot.db.set_negotiation_last_modified(user_id, id_negotiation, date_modified)

    async def _check_negotiation(
        self, user_id: int, secret_key: str, negotiation: dict[str, Any], id_negotiation: int
    ) -> None:
        is_advertiser = bool(negotiation.get("is_listing_advertiser"))
        own_username = negotiation.get("advertiser_username") if is_advertiser else negotiation.get("client_username")
        try:
            messages = await self.bot.uex.get_marketplace_negotiations_messages(
                secret_key=secret_key, id_negotiation=id_negotiation
            )
        except UexApiError as exc:
            logger.warning("Failed to fetch messages for negotiation %s: %s", id_negotiation, exc)
            return
        for row in sorted(messages, key=lambda r: _as_int(r.get("date_added")) or 0):
            message_id = _as_int(row.get("id"))
            text = row.get("message")
            sender = row.get("user_username") or row.get("user_name")
            # event-only rows (message is null) and the watching user's own outgoing
            # messages are never worth a DM - only the other party's real chat text is.
            if message_id is None or not text or sender == own_username:
                continue
            if await self.bot.db.is_negotiation_message_seen(message_id):
                continue
            await self._notify_user(
                user_id,
                f"New negotiation message on **{negotiation.get('listing_title') or 'a listing'}** "
                f"from **{sender}**: {text}",
            )
            await self.bot.db.mark_negotiation_message_seen(message_id)

    async def _notify_user(self, user_id: int, message: str) -> None:
        try:
            user = self.bot.get_user(user_id) or await self.bot.fetch_user(user_id)
            await user.send(message)
        except (discord.HTTPException, AttributeError):
            logger.warning("Could not DM negotiation alert to user %s", user_id)

    @poll_negotiation_messages.before_loop
    async def before_poll_negotiation_messages(self) -> None:
        await self.bot.wait_until_ready()


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(NegotiationAlerts(bot))
