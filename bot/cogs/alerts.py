"""Price alert commands + background poller.

Alerts are stored in SQLite. A background task polls UEX every few minutes for each
distinct commodity being watched and fires when a target price is crossed.
"""
from __future__ import annotations

import logging

import discord
from discord import app_commands
from discord.ext import commands, tasks

from bot.cogs.prices import commodity_name_autocomplete
from bot.delivery import Delivery, send_to_channel_or_dm
from bot.discord_ui import send_alert_remove_picker
from bot.uex.exceptions import UexApiError
from bot.uex.marketplace import format_quality_range

logger = logging.getLogger("uexbot.alerts")

POLL_INTERVAL_MINUTES = 10

# Discord's plain-message content cap is 2000 chars; leave headroom for the truncation
# note itself so appending it can never push the final message back over the real limit.
ALERT_LIST_MAX_CHARS = 1900

DIRECTION_CHOICES = [
    app_commands.Choice(name="Sell price reaches at least...", value="sell_at_least"),
    app_commands.Choice(name="Buy price drops to at most...", value="buy_at_most"),
]


class Alerts(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.poll_alerts.start()

    def cog_unload(self) -> None:
        self.poll_alerts.cancel()

    @app_commands.command(name="alert-add", description="Get notified when a commodity's price crosses a target.")
    @app_commands.describe(
        commodity="Commodity name, e.g. 'Gold'",
        direction="Whether to watch the sell price or the buy price",
        target_price="Target price in aUEC/unit",
    )
    @app_commands.choices(direction=DIRECTION_CHOICES)
    @app_commands.autocomplete(commodity=commodity_name_autocomplete)
    async def alert_add(
        self,
        interaction: discord.Interaction,
        commodity: str,
        direction: app_commands.Choice[str],
        target_price: float,
    ) -> None:
        alert_id = await self.bot.db.add_price_alert(
            guild_id=interaction.guild_id,
            channel_id=interaction.channel_id,
            user_id=interaction.user.id,
            commodity_name=commodity,
            direction=direction.value,
            target_price=target_price,
        )
        readable = "sells for at least" if direction.value == "sell_at_least" else "can be bought for at most"
        await interaction.response.send_message(
            f"Alert #{alert_id} set: I'll ping you here when **{commodity}** {readable} "
            f"**{target_price:.2f} aUEC/unit**. (checked every {POLL_INTERVAL_MINUTES} min)"
        )

    @app_commands.command(name="alert-list", description="List all your active alerts (price, restock, and marketplace).")
    async def alert_list(self, interaction: discord.Interaction) -> None:
        price_alerts = await self.bot.db.list_user_alerts(interaction.user.id)
        stock_alerts = await self.bot.db.list_user_stock_alerts(interaction.user.id)
        marketplace_alerts = await self.bot.db.list_user_marketplace_alerts(interaction.user.id)

        if not price_alerts and not stock_alerts and not marketplace_alerts:
            await interaction.response.send_message("You have no active alerts.", ephemeral=True)
            return

        sections: list[str] = []
        if price_alerts:
            lines = []
            for a in price_alerts:
                readable = "sell >=" if a["direction"] == "sell_at_least" else "buy <="
                lines.append(f"#{a['id']} — {a['commodity_name']} {readable} {a['target_price']:.2f}")
            sections.append("**Price alerts**\n" + "\n".join(lines))
        if stock_alerts:
            lines = []
            for a in stock_alerts:
                ship_note = f" · ship: {a['ship_query']}" if a.get("ship_query") else ""
                scope_note = " · personal (DM)" if a.get("scope") == "personal" else " · global (channel)"
                lines.append(f"#{a['id']} — {a['commodity_name']}{ship_note}{scope_note}")
            sections.append("**Stock (restock) alerts**\n" + "\n".join(lines))
        if marketplace_alerts:
            lines = []
            for a in marketplace_alerts:
                price_note = f" @ target {a['target_price']:,.0f}" if a["target_price"] is not None else ""
                min_q, max_q = a.get("min_quality"), a.get("max_quality")
                quality_note = ""
                if min_q is not None or max_q is not None:
                    quality_note = f" · quality {format_quality_range(min_q, max_q)}"
                lines.append(f"#{a['id']} — {a['operation']} listings matching '{a['keyword']}'{price_note}{quality_note}")
            sections.append("**Marketplace alerts**\n" + "\n".join(lines))

        message = "\n\n".join(sections)
        if len(message) > ALERT_LIST_MAX_CHARS:
            total_alerts = len(price_alerts) + len(stock_alerts) + len(marketplace_alerts)
            truncated = message[:ALERT_LIST_MAX_CHARS]
            cutoff = truncated.rfind("\n")  # cut at a full line, never mid-entry
            message = (
                (truncated[:cutoff] if cutoff > 0 else truncated)
                + f"\n\n… truncated — {total_alerts} alerts total. Remove some with "
                "`/alert-remove` to see the rest."
            )
        await interaction.response.send_message(message, ephemeral=True)

    @app_commands.command(
        name="alert-remove",
        description="Remove one of your active alerts — price, restock, or marketplace (pick from a menu).",
    )
    async def alert_remove(self, interaction: discord.Interaction) -> None:
        price_alerts = await self.bot.db.list_user_alerts(interaction.user.id)
        stock_alerts = await self.bot.db.list_user_stock_alerts(interaction.user.id)
        marketplace_alerts = await self.bot.db.list_user_marketplace_alerts(interaction.user.id)

        picker_items: list[dict] = []
        for a in price_alerts:
            readable = "sell >= " if a["direction"] == "sell_at_least" else "buy <= "
            picker_items.append({
                "id": f"price:{a['id']}",
                "label": f"#{a['id']} {a['commodity_name']} (price)",
                "description": readable + f"{a['target_price']:.2f}",
            })
        for a in stock_alerts:
            picker_items.append({
                "id": f"stock:{a['id']}",
                "label": f"#{a['id']} {a['commodity_name']} (restock)",
                "description": (
                    ("personal · " if a.get("scope") == "personal" else "global · ")
                    + (f"ship: {a['ship_query']}" if a.get("ship_query") else "no ship set")
                ),
            })
        for a in marketplace_alerts:
            price_note = f" @ {a['target_price']:,.0f}" if a["target_price"] is not None else ""
            min_q, max_q = a.get("min_quality"), a.get("max_quality")
            quality_note = ""
            if min_q is not None or max_q is not None:
                quality_note = f" · quality {format_quality_range(min_q, max_q)}"
            picker_items.append({
                "id": f"marketplace:{a['id']}",
                "label": f"#{a['id']} {a['keyword']} (marketplace)",
                "description": f"{a['operation']} listings{price_note}{quality_note}",
            })

        async def _remove(picker_interaction: discord.Interaction, composite_id: str) -> str:
            kind, _, raw_id = composite_id.partition(":")
            alert_id = int(raw_id)
            if kind == "price":
                removed = await self.bot.db.remove_alert(alert_id, picker_interaction.user.id)
                noun = "Price alert"
            elif kind == "stock":
                removed = await self.bot.db.remove_stock_alert(alert_id, picker_interaction.user.id)
                noun = "Stock alert"
            else:
                removed = await self.bot.db.remove_marketplace_alert(alert_id, picker_interaction.user.id)
                noun = "Marketplace alert"
            return f"{noun} #{alert_id} removed." if removed else f"{noun} #{alert_id} was already removed."

        await send_alert_remove_picker(
            interaction,
            alerts=picker_items,
            remove_callback=_remove,
            empty_message="You have no active alerts.",
            placeholder_noun="alert",
        )

    @tasks.loop(minutes=POLL_INTERVAL_MINUTES)
    async def poll_alerts(self) -> None:
        # Nothing may escape a tasks.loop body: it only restarts itself after a narrow set
        # of network errors, so anything else (a locked database, an odd UEX row) would
        # stop this poller until the bot restarts.
        try:
            await self._poll_alerts_once()
        except Exception:
            logger.exception("Price alert poll failed; retrying next cycle")

    async def _poll_alerts_once(self) -> None:
        alerts = await self.bot.db.list_active_alerts()
        if not alerts:
            return

        by_commodity: dict[str, list[dict]] = {}
        for alert in alerts:
            by_commodity.setdefault(alert["commodity_name"], []).append(alert)

        for commodity_name, commodity_alerts in by_commodity.items():
            try:
                rows = await self.bot.uex.get_commodities_prices(commodity_name=commodity_name)
            except UexApiError as exc:
                logger.warning("Failed to poll prices for %s: %s", commodity_name, exc)
                continue
            if not rows:
                continue

            try:
                best_sell = max((r.get("price_sell") or 0 for r in rows), default=0)
                best_buy_candidates = [r.get("price_buy") or 0 for r in rows if (r.get("price_buy") or 0) > 0]
                best_buy = min(best_buy_candidates) if best_buy_candidates else None
            except (AttributeError, TypeError) as exc:
                logger.warning("Skipping price alerts for %s: unexpected price rows (%s)", commodity_name, exc)
                continue

            for alert in commodity_alerts:
                triggered = False
                detail = ""
                if alert["direction"] == "sell_at_least" and best_sell >= alert["target_price"]:
                    triggered = True
                    detail = f"best sell price is now **{best_sell:.2f} aUEC/unit**"
                elif alert["direction"] == "buy_at_most" and best_buy is not None and best_buy <= alert["target_price"]:
                    triggered = True
                    detail = f"best buy price is now **{best_buy:.2f} aUEC/unit**"

                if triggered:
                    try:
                        await self._fire_alert(alert, detail)
                    except Exception:
                        logger.exception("Failed to fire price alert #%s", alert["id"])

    async def _fire_alert(self, alert: dict, detail: str) -> None:
        """One-shot: deactivated once the alert is settled - delivered, or refused outright
        by Discord (see bot/delivery.py). A temporary failure leaves it active, so the next
        poll tries again instead of the alert being used up without ever arriving."""
        message = f"<@{alert['user_id']}> price alert #{alert['id']} triggered for **{alert['commodity_name']}**: {detail}"
        outcome = await send_to_channel_or_dm(
            self.bot, alert["channel_id"], alert["user_id"], label=f"price alert #{alert['id']}", content=message,
        )
        if outcome.settled:
            await self.bot.db.deactivate_alert(alert["id"])
        if outcome is Delivery.UNDELIVERABLE:
            logger.warning("Price alert #%s couldn't reach its owner by channel or DM; deactivated", alert["id"])

    @poll_alerts.before_loop
    async def before_poll_alerts(self) -> None:
        await self.bot.wait_until_ready()


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Alerts(bot))
