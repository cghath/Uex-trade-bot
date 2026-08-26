"""Marketplace listing alerts: DM a user when a new UEX Marketplace listing matches a
keyword they're watching, optionally at or better than a target price.

Unlike bot/cogs/alerts.py's price alerts (one-shot: fire once, deactivate), these are
persistent watches - new listings keep appearing, so each alert stays active indefinitely
and instead dedups per-listing-id (a listing only ever notifies once) via
marketplace_alert_seen_listings. Delivery is always a DM, not a channel post, since a
listing match is personal to whoever set the watch.
"""
from __future__ import annotations

import logging

import discord
from discord import app_commands
from discord.ext import commands, tasks

from bot.cogs.marketplace import OPERATION_CHOICES, traded_item_autocomplete
from bot.discord_ui import send_alert_remove_picker
from bot.uex.exceptions import UexApiError
from bot.uex.marketplace import (
    exclude_sold_out,
    filter_listings_by_keyword,
    filter_listings_by_quality,
    find_item_id_by_name,
    parse_listing_quality,
    parse_uex_number,
)

logger = logging.getLogger("uexbot.marketplace_alerts")

POLL_INTERVAL_MINUTES = 15
MAX_NOTIFY_PER_ALERT_PER_POLL = 5  # cap DM spam if a broad keyword suddenly matches a lot at once


class MarketplaceAlerts(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.poll_marketplace_alerts.start()

    def cog_unload(self) -> None:
        self.poll_marketplace_alerts.cancel()

    @app_commands.command(
        name="marketplace-alert-add",
        description="DM me when a new Marketplace listing matches a keyword.",
    )
    @app_commands.describe(
        keyword="Item name or keyword to watch for, e.g. 'Cutlass Black' or 'Laranite'",
        operation="Watch sell listings (so you can buy) or buy listings (so you can sell into them)",
        target_price="Optional: only notify at or better than this price",
        min_quality="Optional: only notify for listings with quality at least this (seller-set, UEX's 0-100 scale)",
        max_quality="Optional: only notify for listings with quality at most this (seller-set, UEX's 0-100 scale)",
    )
    @app_commands.choices(operation=OPERATION_CHOICES)
    @app_commands.autocomplete(keyword=traded_item_autocomplete)
    async def marketplace_alert_add(
        self,
        interaction: discord.Interaction,
        keyword: str,
        operation: app_commands.Choice[str],
        target_price: float | None = None,
        min_quality: float | None = None,
        max_quality: float | None = None,
    ) -> None:
        alert_id = await self.bot.db.add_marketplace_alert(
            user_id=interaction.user.id,
            keyword=keyword,
            operation=operation.value,
            target_price=target_price,
            min_quality=min_quality,
            max_quality=max_quality,
        )
        side_note = "sell listings (so you can buy)" if operation.value == "sell" else "buy listings (so you can sell into them)"
        price_note = f" at or better than **{target_price:,.0f}**" if target_price is not None else ""
        quality_note = ""
        if min_quality is not None or max_quality is not None:
            lo = f"{min_quality:.0f}" if min_quality is not None else "0"
            hi = f"{max_quality:.0f}" if max_quality is not None else "100"
            quality_note = f" and quality {lo}-{hi}"
        quality_caveat = (
            " (note: most listings today don't have a quality value set at all, so this may match very little for now)"
            if quality_note
            else ""
        )
        await interaction.response.send_message(
            f"Marketplace alert #{alert_id} set: I'll DM you when a new {side_note} matching "
            f"'{keyword}'{price_note}{quality_note} appears (checked every {POLL_INTERVAL_MINUTES} min)."
            f"{quality_caveat} This keeps watching - it won't turn off after the first match.",
            ephemeral=True,
        )

    @app_commands.command(name="marketplace-alert-list", description="List your active marketplace listing alerts.")
    async def marketplace_alert_list(self, interaction: discord.Interaction) -> None:
        alerts = await self.bot.db.list_user_marketplace_alerts(interaction.user.id)
        if not alerts:
            await interaction.response.send_message("You have no active marketplace alerts.", ephemeral=True)
            return
        lines = []
        for a in alerts:
            price_note = f" @ target {a['target_price']:,.0f}" if a["target_price"] is not None else ""
            min_q, max_q = a.get("min_quality"), a.get("max_quality")
            quality_note = ""
            if min_q is not None or max_q is not None:
                lo = f"{min_q:.0f}" if min_q is not None else "0"
                hi = f"{max_q:.0f}" if max_q is not None else "100"
                quality_note = f" · quality {lo}-{hi}"
            lines.append(f"#{a['id']} — {a['operation']} listings matching '{a['keyword']}'{price_note}{quality_note}")
        await interaction.response.send_message("\n".join(lines), ephemeral=True)

    @app_commands.command(name="marketplace-alert-remove", description="Remove one of your marketplace alerts (pick from a menu).")
    async def marketplace_alert_remove(self, interaction: discord.Interaction) -> None:
        alerts = await self.bot.db.list_user_marketplace_alerts(interaction.user.id)
        picker_items = []
        for a in alerts:
            price_note = f" @ {a['target_price']:,.0f}" if a["target_price"] is not None else ""
            min_q, max_q = a.get("min_quality"), a.get("max_quality")
            quality_note = ""
            if min_q is not None or max_q is not None:
                lo = f"{min_q:.0f}" if min_q is not None else "0"
                hi = f"{max_q:.0f}" if max_q is not None else "100"
                quality_note = f" · quality {lo}-{hi}"
            picker_items.append(
                {
                    "id": a["id"],
                    "label": f"#{a['id']} {a['keyword']}",
                    "description": f"{a['operation']} listings{price_note}{quality_note}",
                }
            )

        async def _remove(picker_interaction: discord.Interaction, alert_id: int) -> str:
            removed = await self.bot.db.remove_marketplace_alert(alert_id, picker_interaction.user.id)
            return f"Marketplace alert #{alert_id} removed." if removed else f"Marketplace alert #{alert_id} was already removed."

        await send_alert_remove_picker(
            interaction,
            alerts=picker_items,
            remove_callback=_remove,
            empty_message="You have no active marketplace alerts.",
            placeholder_noun="marketplace alert",
        )

    @tasks.loop(minutes=POLL_INTERVAL_MINUTES)
    async def poll_marketplace_alerts(self) -> None:
        alerts = await self.bot.db.list_active_marketplace_alerts()
        if not alerts:
            return

        # Alert names normally come from the Marketplace activity autocomplete, which
        # already persists id_item. UEX's /items endpoint requires a category and an
        # unfiltered call returns no rows, so reuse this local index instead of making a
        # 66-category catalog sweep on every background poll.
        activity = await self.bot.db.list_marketplace_item_activity()
        items = [
            {"id": row.get("id_item"), "name": row.get("item_name")}
            for row in activity
        ]

        # Group alerts by (keyword, operation) so identical watches from different users
        # share one API call instead of one per alert.
        groups: dict[tuple[str, str], list[dict]] = {}
        for alert in alerts:
            key = (alert["keyword"].strip().lower(), alert["operation"])
            groups.setdefault(key, []).append(alert)

        for (keyword, operation), group_alerts in groups.items():
            id_item = find_item_id_by_name(items, keyword)
            try:
                if id_item is not None:
                    listings = await self.bot.uex.get_marketplace_listings(id_item=id_item, operation=operation)
                else:
                    listings = await self.bot.uex.get_marketplace_listings(operation=operation)
                    listings = filter_listings_by_keyword(listings, keyword)
            except UexApiError as exc:
                logger.warning("Failed to poll marketplace listings for '%s': %s", keyword, exc)
                continue

            listings = exclude_sold_out(listings)
            if not listings:
                continue

            for alert in group_alerts:
                seen_ids = await self.bot.db.get_seen_marketplace_listing_ids(alert["id"])
                # Quality bounds are per-alert (two alerts can share a keyword/operation group
                # but want different quality ranges), so this filter is applied here, not
                # against the shared `listings` fetched above.
                candidate_listings = filter_listings_by_quality(
                    listings, alert.get("min_quality"), alert.get("max_quality")
                )
                notified = 0
                for listing in candidate_listings:
                    if notified >= MAX_NOTIFY_PER_ALERT_PER_POLL:
                        break
                    listing_id = listing.get("id")
                    if listing_id is None or listing_id in seen_ids:
                        continue

                    price = parse_uex_number(listing.get("price"))
                    target = alert["target_price"]
                    if target is not None and price is not None:
                        # sell listing = you'd be buying -> want price at or below target.
                        # buy listing = you'd be selling -> want price at or above target.
                        if alert["operation"] == "sell" and price > target:
                            await self.bot.db.mark_marketplace_listing_seen(alert["id"], listing_id)
                            continue
                        if alert["operation"] == "buy" and price < target:
                            await self.bot.db.mark_marketplace_listing_seen(alert["id"], listing_id)
                            continue

                    await self._notify_marketplace_alert(alert, listing)
                    await self.bot.db.mark_marketplace_listing_seen(alert["id"], listing_id)
                    notified += 1

    async def _notify_marketplace_alert(self, alert: dict, listing: dict) -> None:
        title = listing.get("title", "Untitled listing")
        price = parse_uex_number(listing.get("price"))
        currency = listing.get("currency", "UEC")
        seller = listing.get("user_username") or listing.get("user_name") or "unknown seller"
        price_text = f"{price:,.0f} {currency}" if price is not None else "price n/a"
        quality = parse_listing_quality(listing.get("quality"))
        quality_text = f" · quality {quality:.0f}" if quality is not None else ""
        message = (
            f"Marketplace alert #{alert['id']} ('{alert['keyword']}'): new **{alert['operation']}** listing — "
            f"**{title}** · {price_text}{quality_text} · by {seller}"
        )
        try:
            user = await self.bot.fetch_user(alert["user_id"])
            await user.send(message)
        except discord.HTTPException as exc:
            logger.warning("Failed to DM marketplace alert #%s: %s", alert["id"], exc)

    @poll_marketplace_alerts.before_loop
    async def before_poll_marketplace_alerts(self) -> None:
        await self.bot.wait_until_ready()


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(MarketplaceAlerts(bot))
