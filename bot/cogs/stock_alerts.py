"""Commodity restock alerts: notify when a watched commodity flips from out-of-stock to
in-stock at any terminal, so you don't have to keep re-running /best-route hoping the usual
"Out of Stock" has cleared.

Persistent watches, like Marketplace alerts (not one-shot like /alert-add) - a background
poller remembers each watch's last-known per-terminal availability
(stock_alert_terminal_state) and only notifies on a genuine empty->available transition, not
on every poll while a terminal just stays in stock.

Delivery is per-alert, via the `scope` option on /stock-alert-add: 'global' (default) posts in
the channel the alert was created in and @-mentions the creator there, visible to everyone else
in that channel too; 'personal' DMs only the creator, same as Marketplace alerts. Two people
independently watching the same commodity in the same channel on 'global' stay fully separate
alerts - no merging, so both would post on the same restock.
"""
from __future__ import annotations

import logging

import discord
from discord import app_commands
from discord.ext import commands, tasks

from bot.cogs.prices import commodity_name_autocomplete
from bot.cogs.ships import ship_name_autocomplete
from bot.uex.exceptions import UexApiError
from bot.uex.ships import resolve_ship
from bot.uex.stock_alerts import compute_terminal_availability, detect_restocks, format_cargo_fit_note

logger = logging.getLogger("uexbot.stock_alerts")

# /commodities_prices is cached 30 min client-side (bot/uex/client.py) - polling faster
# wouldn't see fresher data, just repeat the same cached response.
POLL_INTERVAL_MINUTES = 30

SCOPE_CHOICES = [
    app_commands.Choice(name="Global - post in this channel, ping me (default)", value="global"),
    app_commands.Choice(name="Personal - DM me only, nothing posted in-channel", value="personal"),
]


class StockAlerts(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.poll_stock_alerts.start()

    def cog_unload(self) -> None:
        self.poll_stock_alerts.cancel()

    @app_commands.command(
        name="stock-alert-add",
        description="Get notified when a commodity restocks (has real buy-side stock) at any terminal.",
    )
    @app_commands.describe(
        commodity="Commodity name, e.g. 'Gold' or 'Laranite'",
        ship="Optional: which ship to report the cargo fit against (defaults to your /set-default-ship)",
        scope="Global (default): post in this channel and ping me. Personal: DM me only, nothing posted in-channel.",
    )
    @app_commands.autocomplete(ship=ship_name_autocomplete, commodity=commodity_name_autocomplete)
    @app_commands.choices(scope=SCOPE_CHOICES)
    async def stock_alert_add(
        self,
        interaction: discord.Interaction,
        commodity: str,
        ship: str | None = None,
        scope: app_commands.Choice[str] | None = None,
    ) -> None:
        scope_value = scope.value if scope else "global"
        alert_id = await self.bot.db.add_stock_alert(
            guild_id=interaction.guild_id,
            channel_id=interaction.channel_id,
            user_id=interaction.user.id,
            commodity_name=commodity,
            ship_query=ship,
            scope=scope_value,
        )
        ship_note = f" (cargo fit checked against **{ship}**)" if ship else " (set /set-default-ship for a cargo-fit estimate)"
        delivery_note = (
            "I'll post here and ping you" if scope_value == "global" else "I'll DM you (nothing posted in this channel)"
        )
        await interaction.response.send_message(
            f"Stock alert #{alert_id} set: {delivery_note} when **{commodity}** has real stock "
            f"at any terminal{ship_note} (checked every {POLL_INTERVAL_MINUTES} min). This keeps "
            "watching - it fires again on every future restock, not just the first one.",
            ephemeral=(scope_value == "personal"),
        )

    @tasks.loop(minutes=POLL_INTERVAL_MINUTES)
    async def poll_stock_alerts(self) -> None:
        alerts = await self.bot.db.list_active_stock_alerts()
        if not alerts:
            return

        # Group by commodity_name so identical watches from different users/channels share
        # one API call, same pattern as the marketplace-alert poller.
        by_commodity: dict[str, list[dict]] = {}
        for alert in alerts:
            by_commodity.setdefault(alert["commodity_name"].strip().lower(), []).append(alert)

        vehicles_cache: list[dict] | None = None

        for _, commodity_alerts in by_commodity.items():
            commodity_name = commodity_alerts[0]["commodity_name"]
            try:
                rows = await self.bot.uex.get_commodities_prices(commodity_name=commodity_name)
            except UexApiError as exc:
                logger.warning("Failed to poll stock for %s: %s", commodity_name, exc)
                continue
            if not rows:
                continue

            current = compute_terminal_availability(rows)

            for alert in commodity_alerts:
                previous_state = await self.bot.db.get_stock_alert_terminal_state(alert["id"])
                to_notify, new_state = detect_restocks(current, previous_state)

                for id_terminal, state in new_state.items():
                    await self.bot.db.upsert_stock_alert_terminal_state(
                        alert["id"], id_terminal, state["was_available"], state["last_seen_scu"]
                    )

                if not to_notify:
                    continue

                ship_query = alert.get("ship_query") or await self.bot.db.get_default_ship(alert["user_id"])
                ship_cargo_scu = None
                if ship_query:
                    try:
                        if vehicles_cache is None:
                            vehicles_cache = await self.bot.uex.get_vehicles()
                        vehicle = resolve_ship(vehicles_cache, ship_query)
                        ship_cargo_scu = vehicle.get("scu") if vehicle else None
                    except UexApiError as exc:
                        logger.info("Vehicle lookup failed for stock alert #%s: %s", alert["id"], exc)

                for terminal in to_notify:
                    await self._notify_stock_alert(alert, commodity_name, terminal, ship_cargo_scu)

    async def _notify_stock_alert(self, alert: dict, commodity_name: str, terminal: dict, ship_cargo_scu: float | None) -> None:
        fit_note = format_cargo_fit_note(terminal["scu_buy"], ship_cargo_scu)
        body = (
            f"stock alert #{alert['id']}: **{commodity_name}** is back in stock at "
            f"**{terminal['terminal_name']}** — {terminal['price_buy']:.2f} aUEC/unit, "
            f"{terminal['scu_buy']:,.0f} SCU available ({fit_note})"
        )
        is_personal = alert.get("scope") == "personal"

        if is_personal:
            # DM only - nothing posted to the channel, so no @-mention needed (it's
            # already unambiguously addressed to whoever's reading their own DMs).
            await self._send_dm_or_log(alert, f"Your {body}")
            return

        message = f"<@{alert['user_id']}> {body}"
        channel = self.bot.get_channel(alert["channel_id"])
        if channel is not None:
            try:
                await channel.send(message)
                return
            except discord.HTTPException as exc:
                # Covers both "channel resolved fine but the bot can't post there"
                # (e.g. 403/50013 Missing Permissions if the bot's role/overrides
                # changed) and any other one-off delivery hiccup - fall through to the
                # DM fallback below instead of silently dropping a 'global' alert.
                logger.warning(
                    "Failed to post stock alert #%s to channel %s (%s) - falling back to DM.",
                    alert["id"], alert["channel_id"], exc,
                )
        # Either the channel wasn't resolvable at all (e.g. right after a restart before
        # the cache warms, or it was deleted) or the send above failed - try to reach the
        # creator directly instead of dropping the notification.
        await self._send_dm_or_log(alert, message)

    async def _send_dm_or_log(self, alert: dict, message: str) -> None:
        try:
            user = await self.bot.fetch_user(alert["user_id"])
            await user.send(message)
        except discord.HTTPException as exc:
            logger.warning(
                "Failed to deliver stock alert #%s (DM failed, and channel post either "
                "failed or wasn't attempted): %s", alert["id"], exc,
            )

    @poll_stock_alerts.before_loop
    async def before_poll_stock_alerts(self) -> None:
        await self.bot.wait_until_ready()


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(StockAlerts(bot))
