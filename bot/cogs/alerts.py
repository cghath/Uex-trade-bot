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
from bot.discord_ui import send_alert_remove_picker
from bot.uex.exceptions import UexApiError

logger = logging.getLogger("uexbot.alerts")

POLL_INTERVAL_MINUTES = 10

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

    @app_commands.command(name="alert-list", description="List your active price alerts.")
    async def alert_list(self, interaction: discord.Interaction) -> None:
        alerts = await self.bot.db.list_user_alerts(interaction.user.id)
        if not alerts:
            await interaction.response.send_message("You have no active alerts.")
            return
        lines = []
        for a in alerts:
            readable = "sell >=" if a["direction"] == "sell_at_least" else "buy <="
            lines.append(f"#{a['id']} — {a['commodity_name']} {readable} {a['target_price']:.2f}")
        await interaction.response.send_message("\n".join(lines))

    @app_commands.command(name="alert-remove", description="Remove one of your price alerts (pick from a menu).")
    async def alert_remove(self, interaction: discord.Interaction) -> None:
        alerts = await self.bot.db.list_user_alerts(interaction.user.id)
        picker_items = [
            {
                "id": a["id"],
                "label": f"#{a['id']} {a['commodity_name']}",
                "description": ("sell >= " if a["direction"] == "sell_at_least" else "buy <= ") + f"{a['target_price']:.2f}",
            }
            for a in alerts
        ]

        async def _remove(picker_interaction: discord.Interaction, alert_id: int) -> str:
            removed = await self.bot.db.remove_alert(alert_id, picker_interaction.user.id)
            return f"Alert #{alert_id} removed." if removed else f"Alert #{alert_id} was already removed."

        await send_alert_remove_picker(
            interaction,
            alerts=picker_items,
            remove_callback=_remove,
            empty_message="You have no active alerts.",
            placeholder_noun="price alert",
        )

    @tasks.loop(minutes=POLL_INTERVAL_MINUTES)
    async def poll_alerts(self) -> None:
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

            best_sell = max((r.get("price_sell") or 0 for r in rows), default=0)
            best_buy_candidates = [r.get("price_buy") or 0 for r in rows if (r.get("price_buy") or 0) > 0]
            best_buy = min(best_buy_candidates) if best_buy_candidates else None

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
                    await self._fire_alert(alert, detail)

    async def _fire_alert(self, alert: dict, detail: str) -> None:
        await self.bot.db.deactivate_alert(alert["id"])
        message = f"<@{alert['user_id']}> price alert #{alert['id']} triggered for **{alert['commodity_name']}**: {detail}"

        channel = self.bot.get_channel(alert["channel_id"])
        if channel is not None:
            try:
                await channel.send(message)
                return
            except discord.HTTPException as exc:
                # Channel resolved fine but the bot can't post there (e.g. 403/50013
                # Missing Permissions) - fall through to the DM fallback below instead
                # of silently dropping the notification.
                logger.warning(
                    "Failed to post alert #%s to channel %s (%s) - falling back to DM.",
                    alert["id"], alert["channel_id"], exc,
                )
        try:
            user = await self.bot.fetch_user(alert["user_id"])
            await user.send(message)
        except discord.HTTPException as exc:
            logger.warning(
                "Failed to deliver alert #%s (DM failed, and channel post either failed "
                "or wasn't attempted): %s", alert["id"], exc,
            )

    @poll_alerts.before_loop
    async def before_poll_alerts(self) -> None:
        await self.bot.wait_until_ready()


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Alerts(bot))
