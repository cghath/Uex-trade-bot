"""Marketplace listing alerts: DM a user when a new UEX Marketplace listing matches a
keyword they're watching, optionally at or better than a target price.

Unlike bot/cogs/alerts.py's price alerts (one-shot: fire once, deactivate), these are
persistent watches - new listings keep appearing, so each alert stays active indefinitely
and instead dedups per-listing-id (a listing only ever notifies once) via
marketplace_alert_seen_listings. Delivery is the player's choice, like every alert type: a DM
(the default) or a post in the channel the alert was set in (audit UX-12).
"""
from __future__ import annotations

import logging

import discord
from discord import app_commands
from discord.ext import commands, tasks

from bot.cogs.marketplace import OPERATION_CHOICES, traded_item_autocomplete
from bot.delivery import DELIVERY_CHOICES, DELIVERY_DESCRIPTION, Delivery, delivery_note, delivery_scope, send_alert
from bot.uex.exceptions import UexApiError
from bot.uex.marketplace import (
    QUALITY_MAX,
    exclude_sold_out,
    filter_listings_by_keyword,
    filter_listings_by_quality,
    find_item_id_by_name,
    format_quality_range,
    marketplace_item_link,
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
        min_quality="Optional: only listings with quality at least this, 0-1000 (seller-set)",
        max_quality="Optional: only listings with quality at most this, 0-1000 (seller-set)",
        delivery=DELIVERY_DESCRIPTION,
    )
    @app_commands.choices(operation=OPERATION_CHOICES, delivery=DELIVERY_CHOICES)
    @app_commands.autocomplete(keyword=traded_item_autocomplete)
    async def marketplace_alert_add(
        self,
        interaction: discord.Interaction,
        keyword: str,
        operation: app_commands.Choice[str],
        target_price: float | None = None,
        min_quality: app_commands.Range[float, 0, QUALITY_MAX] | None = None,
        max_quality: app_commands.Range[float, 0, QUALITY_MAX] | None = None,
        delivery: app_commands.Choice[str] | None = None,
    ) -> None:
        scope = delivery_scope(delivery)
        # Deferred before any DB write: a write can wait on a lock past Discord's
        # 3-second window, and a player who sees "did not respond" retries into a
        # duplicate (audit REL-8).
        private = scope == "personal"
        await interaction.response.defer(ephemeral=private)
        alert_id = await self.bot.db.add_marketplace_alert(
            user_id=interaction.user.id,
            keyword=keyword,
            operation=operation.value,
            target_price=target_price,
            min_quality=min_quality,
            max_quality=max_quality,
            scope=scope,
            guild_id=interaction.guild_id,
            channel_id=interaction.channel_id,
        )
        side_note = "sell listings (so you can buy)" if operation.value == "sell" else "buy listings (so you can sell into them)"
        price_note = f" at or better than **{target_price:,.0f}**" if target_price is not None else ""
        quality_note = ""
        if min_quality is not None or max_quality is not None:
            quality_note = f" and quality {format_quality_range(min_quality, max_quality)}"
        quality_caveat = (
            " (note: most listings today don't have a quality value set at all, so this may match very little for now)"
            if quality_note
            else ""
        )
        await interaction.followup.send(
            f"Marketplace alert #{alert_id} set: {delivery_note(scope)} when a new {side_note} matching "
            f"'{keyword}'{price_note}{quality_note} appears (checked every {POLL_INTERVAL_MINUTES} min)."
            f"{quality_caveat} This keeps watching - it fires on every new matching listing, not just the first.",
            ephemeral=private,
        )

    @tasks.loop(minutes=POLL_INTERVAL_MINUTES)
    async def poll_marketplace_alerts(self) -> None:
        # Nothing may escape a tasks.loop body: it only restarts itself after a narrow set
        # of network errors, so anything else would stop this poller until a restart.
        try:
            await self._poll_marketplace_alerts_once()
        except Exception:
            logger.exception("Marketplace alert poll failed; retrying next cycle")

    async def _poll_marketplace_alerts_once(self) -> None:
        alerts = await self.bot.db.list_active_marketplace_alerts()
        if not alerts:
            return

        # Alert names normally come from the Marketplace activity autocomplete, which
        # already persists id_item. That index only covers UEX's top ~100 active-negotiation
        # items though, and an item outside it would otherwise fall through to the unfiltered
        # get_marketplace_listings() branch below, which the API itself caps at 100 rows -
        # silently missing a real item's listings forever. Extend with the full item catalog
        # (already warmed/cached elsewhere, e.g. by autocomplete) so a real id_item is found
        # whenever possible and the higher-limit, server-filtered id_item= query can be used.
        activity = await self.bot.db.list_marketplace_item_activity()
        items = [
            {"id": row.get("id_item"), "name": row.get("item_name")}
            for row in activity
        ]
        known_ids = {item.get("id") for item in items}
        try:
            catalog = await self.bot.uex.get_item_catalog()
        except UexApiError as exc:
            logger.warning("Full item catalog unavailable for alert keyword resolution: %s", exc)
            catalog = []
        items.extend(item for item in catalog if item.get("id") not in known_ids)

        # Group alerts by (keyword, operation) so identical watches from different users
        # share one API call instead of one per alert.
        groups: dict[tuple[str, str], list[dict]] = {}
        for alert in alerts:
            key = (alert["keyword"].strip().lower(), alert["operation"])
            groups.setdefault(key, []).append(alert)

        for (keyword, operation), group_alerts in groups.items():
            # Per group, so one keyword's bad data can't block every group after it.
            try:
                await self._poll_alert_group(keyword, operation, group_alerts, items)
            except Exception:
                logger.exception("Marketplace alerts for '%s' (%s) failed this cycle", keyword, operation)

    async def _poll_alert_group(
        self, keyword: str, operation: str, group_alerts: list[dict], items: list[dict]
    ) -> None:
        id_item = find_item_id_by_name(items, keyword)
        try:
            if id_item is not None:
                listings = await self.bot.uex.get_marketplace_listings(id_item=id_item, operation=operation)
            else:
                listings = await self.bot.uex.get_marketplace_listings(operation=operation)
                listings = filter_listings_by_keyword(listings, keyword)
        except UexApiError as exc:
            logger.warning("Failed to poll marketplace listings for '%s': %s", keyword, exc)
            return

        listings = exclude_sold_out(listings)
        if not listings:
            return

        for alert in group_alerts:
            # Per alert, so one alert's failure can't block the rest of this group.
            try:
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

                    outcome = await self._notify_marketplace_alert(alert, listing)
                    if not outcome.settled:
                        # A temporary failure: leave this listing unseen so the next poll
                        # retries it, and don't try the rest now either.
                        break
                    await self.bot.db.mark_marketplace_listing_seen(alert["id"], listing_id)
                    if outcome is Delivery.UNDELIVERABLE:
                        # Discord refused the DM (closed DMs, unknown user). The others
                        # would be refused the same way, so they wait for a later poll
                        # rather than each failing now.
                        break
                    notified += 1
            except Exception:
                logger.exception("Marketplace alert #%s failed this cycle", alert["id"])

    async def _notify_marketplace_alert(self, alert: dict, listing: dict) -> Delivery:
        title = listing.get("title", "Untitled listing")
        price = parse_uex_number(listing.get("price"))
        currency = listing.get("currency", "UEC")
        seller = listing.get("user_username") or listing.get("user_name") or "unknown seller"
        price_text = f"{price:,.0f} {currency}" if price is not None else "price n/a"
        quality = parse_listing_quality(listing.get("quality"))
        quality_text = f" · quality {quality:.0f}" if quality is not None else ""
        body = (
            f"marketplace alert #{alert['id']} ('{alert['keyword']}'): new **{alert['operation']}** listing — "
            f"**{marketplace_item_link(title, listing.get('id_item'))}** · {price_text}{quality_text} · by {seller}"
        )
        return await send_alert(self.bot, alert, body, label=f"marketplace alert #{alert['id']}")

    @poll_marketplace_alerts.before_loop
    async def before_poll_marketplace_alerts(self) -> None:
        await self.bot.wait_until_ready()


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(MarketplaceAlerts(bot))
