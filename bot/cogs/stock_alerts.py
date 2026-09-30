"""Commodity restock alerts: notify when a watched commodity flips from out-of-stock to
in-stock at any terminal, so you don't have to keep re-running /best-route hoping the usual
"Out of Stock" has cleared.

Persistent watches, like Marketplace alerts (not one-shot like /alert-add) - a background
poller remembers each watch's last-known per-terminal availability
(stock_alert_terminal_state) and only notifies on a genuine empty->available transition, not
on every poll while a terminal just stays in stock.

Delivery is per-alert, via the `delivery` option every alert command shares (stored as
`scope`): 'personal' (the default since audit UX-12) DMs only the creator; 'global' posts in the
channel the alert was created in and @-mentions the creator there, visible to everyone else in
that channel too. Two people independently watching the same commodity in the same channel on
'global' stay fully separate alerts - no merging, so both would post on the same restock.
"""
from __future__ import annotations

import logging

import discord
from discord import app_commands
from discord.ext import commands, tasks

from bot.cogs.prices import commodity_name_autocomplete
from bot.cogs.ships import ship_name_autocomplete
from bot.delivery import DELIVERY_CHOICES, DELIVERY_DESCRIPTION, Delivery, delivery_note, delivery_scope, send_alert
from bot.uex.exceptions import UexApiError, describe_uex_api_error
from bot.uex.ships import resolve_ship
from bot.uex.stock_alerts import compute_terminal_availability, detect_restocks, format_restock_message
from bot.uex.trading import resolve_tradeable_commodity, unknown_commodity_message

logger = logging.getLogger("uexbot.stock_alerts")

# /commodities_prices is cached 30 min client-side (bot/uex/client.py) - polling faster
# wouldn't see fresher data, just repeat the same cached response.
POLL_INTERVAL_MINUTES = 30

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
        ship="Optional: which ship to report the cargo fit against (defaults to your saved ship)",
        delivery=DELIVERY_DESCRIPTION,
    )
    @app_commands.autocomplete(ship=ship_name_autocomplete, commodity=commodity_name_autocomplete)
    @app_commands.choices(delivery=DELIVERY_CHOICES)
    async def stock_alert_add(
        self,
        interaction: discord.Interaction,
        commodity: str,
        ship: str | None = None,
        delivery: app_commands.Choice[str] | None = None,
    ) -> None:
        scope_value = delivery_scope(delivery)
        # Deferred before the UEX lookup and the DB write (audit REL-8), at the reply's own
        # visibility: a personal alert's replies stay private.
        private = scope_value == "personal"
        await interaction.response.defer(ephemeral=private)
        try:
            commodities = await self.bot.uex.get_commodities()
        except UexApiError as exc:
            await interaction.followup.send(
                f"Couldn't check the commodity name against UEX right now, so no alert was set. "
                f"Try again in a minute. ({describe_uex_api_error(exc)})",
                ephemeral=private,
            )
            return
        resolved = resolve_tradeable_commodity(commodities, commodity)
        if resolved is None:
            await interaction.followup.send(unknown_commodity_message(commodities, commodity), ephemeral=private)
            return
        commodity = resolved["name"]
        alert_id = await self.bot.db.add_stock_alert(
            guild_id=interaction.guild_id,
            channel_id=interaction.channel_id,
            user_id=interaction.user.id,
            commodity_name=commodity,
            ship_query=ship,
            scope=scope_value,
        )
        ship_note = f" (cargo fit checked against **{ship}**)" if ship else " (set a ship with /set-trading-preferences for a cargo-fit estimate)"
        await interaction.followup.send(
            f"Stock alert #{alert_id} set: {delivery_note(scope_value)} when **{commodity}** has real stock "
            f"at any terminal{ship_note} (checked every {POLL_INTERVAL_MINUTES} min). This keeps "
            "watching - it fires again on every future restock, not just the first one.",
            ephemeral=private,
        )

    @tasks.loop(minutes=POLL_INTERVAL_MINUTES)
    async def poll_stock_alerts(self) -> None:
        # Nothing may escape a tasks.loop body: it only restarts itself after a narrow set
        # of network errors, so anything else would stop this poller until a restart.
        try:
            await self._poll_stock_alerts_once()
        except Exception:
            logger.exception("Stock alert poll failed; retrying next cycle")

    async def _poll_stock_alerts_once(self) -> None:
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

            try:
                current = compute_terminal_availability(rows)
            except (AttributeError, KeyError, TypeError, ValueError) as exc:
                logger.warning("Skipping stock alerts for %s: unexpected price rows (%s)", commodity_name, exc)
                continue

            for alert in commodity_alerts:
                # Per alert, so one alert's failure can't block every alert queued after it.
                try:
                    previous_state = await self.bot.db.get_stock_alert_terminal_state(alert["id"])
                    to_notify, new_state = detect_restocks(current, previous_state)

                    # Notify before saving the new state: a restock whose notification hit a
                    # temporary failure keeps its old "not in stock" state, so the next poll
                    # sees it as a restock again and retries, instead of it being lost.
                    unsent: set[int] = set()
                    if to_notify:
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

                        # One message for every terminal this check found (audit UX-1). If it
                        # isn't settled, none of them are recorded, so all are retried.
                        outcome = await self._notify_stock_alert(alert, commodity_name, to_notify, ship_cargo_scu)
                        if not outcome.settled:
                            unsent.update(terminal["id_terminal"] for terminal in to_notify)

                    for id_terminal, state in new_state.items():
                        if id_terminal in unsent:
                            continue
                        await self.bot.db.upsert_stock_alert_terminal_state(
                            alert["id"], id_terminal, state["was_available"], state["last_seen_scu"]
                        )
                except Exception:
                    logger.exception("Stock alert #%s failed this cycle", alert["id"])

    async def _notify_stock_alert(
        self, alert: dict, commodity_name: str, terminals: list[dict], ship_cargo_scu: float | None
    ) -> Delivery:
        body = format_restock_message(alert["id"], commodity_name, terminals, ship_cargo_scu)
        return await send_alert(self.bot, alert, body, label=f"stock alert #{alert['id']}")

    @poll_stock_alerts.before_loop
    async def before_poll_stock_alerts(self) -> None:
        await self.bot.wait_until_ready()


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(StockAlerts(bot))
