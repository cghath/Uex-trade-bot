"""UEX Marketplace commands: player-to-player listings for items/services/contracts.

This is a separate system from the commodity/terminal trading in prices.py and trends.py -
it's a classifieds board (like a player-run auction house), not hauling cargo between
terminals. Read commands (search/trending/negotiations) are safe. /marketplace-post is the
one command that creates a REAL public listing visible to every UEX user, not just this
Discord server - it always shows a preview and requires an explicit Confirm button click
before anything is actually posted; nothing here posts automatically.
"""
from __future__ import annotations

import logging

import discord
from discord import app_commands
from discord.ext import commands, tasks

from bot.uex.charts import render_price_history_chart
from bot.uex.exceptions import UexApiError
from bot.uex.marketplace import (
    MarketplaceAverageEntry,
    compute_marketplace_movers,
    exclude_sold_out,
    extract_item_activity,
    extract_quality_item_ids,
    filter_listings_by_keyword,
    filter_listings_by_quality,
    find_item_id_by_name,
    match_traded_items,
    parse_listing_quality,
    parse_marketplace_average_rows,
    parse_uex_number,
    rank_traded_items,
    reshape_marketplace_history_rows,
)

logger = logging.getLogger("uexbot.marketplace")

# How often to snapshot /marketplace_trends into the accumulating traded-items index. Matched
# to that endpoint's own 1h client-side cache TTL - polling faster wouldn't see fresher data,
# just repeat the same cached response.
ITEM_ACTIVITY_SNAPSHOT_HOURS = 1

CURRENCY_CHOICES = [
    app_commands.Choice(name="UEC", value="UEC"),
    app_commands.Choice(name="WIF", value="WIF"),
    app_commands.Choice(name="MGS", value="MGS"),
]
OPERATION_CHOICES = [
    app_commands.Choice(name="Sell", value="sell"),
    app_commands.Choice(name="Buy", value="buy"),
]
TYPE_CHOICES = [
    app_commands.Choice(name="Item", value="item"),
    app_commands.Choice(name="Service", value="service"),
    app_commands.Choice(name="Contract", value="contract"),
]
DEFAULT_LANGUAGE = "en_US"

# Verified against live /marketplace_prices_history data: quality_tier is a 0-7 bucket of the
# 0-1000 `quality` field, NOT evenly spaced - see bot/uex/client.py:get_marketplace_prices_history.
QUALITY_TIER_CHOICES = [
    app_commands.Choice(name="Q0", value=0),
    app_commands.Choice(name="Q1-499", value=1),
    app_commands.Choice(name="Q500-599", value=2),
    app_commands.Choice(name="Q600-699", value=3),
    app_commands.Choice(name="Q700-799", value=4),
    app_commands.Choice(name="Q800-899", value=5),
    app_commands.Choice(name="Q900-949", value=6),
    app_commands.Choice(name="Q950-1000", value=7),
]
QUALITY_TIER_LABELS = {choice.value: choice.name for choice in QUALITY_TIER_CHOICES}

# Discord embed fields cap at 1024 chars; 9 of these lines at their widest (8-digit
# prices, longest tier label) plus the "...and N more" overflow note still fits. Most
# items only have a handful of tier/currency combos, so the cap rarely bites at all.
MAX_AVERAGE_LINES_PER_SIDE = 9


async def item_name_autocomplete(interaction: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
    try:
        items = await interaction.client.uex.get_items()
    except UexApiError:
        return []
    current_lower = current.lower()
    matches = [i for i in items if current_lower in (i.get("name") or "").lower()][:25]
    return [app_commands.Choice(name=(i.get("name") or "")[:100], value=i.get("name") or "") for i in matches]


async def traded_item_autocomplete(interaction: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
    """Autocomplete scoped to items the bot has actually observed being traded on the
    Marketplace (bot/db/database.py: marketplace_item_activity, built by
    Marketplace.snapshot_item_activity below), ranked by combined negotiation+listing
    activity - instead of the full item catalog, which runs to thousands of entries most of
    which are never listed on the Marketplace. Falls back to the full-catalog substring
    search (item_name_autocomplete) when nothing in the traded index matches yet, so a
    legitimate item that just hasn't shown up in a snapshot yet is never unreachable.
    """
    activity_rows = await interaction.client.db.list_marketplace_item_activity()
    ranked = rank_traded_items(activity_rows)
    matches = match_traded_items(ranked, current, limit=25)
    if matches:
        return [app_commands.Choice(name=m["item_name"][:100], value=m["item_name"]) for m in matches]
    return await item_name_autocomplete(interaction, current)


async def category_autocomplete(interaction: discord.Interaction, current: str) -> list[app_commands.Choice[int]]:
    # The category list depends on which `type` the user already picked in this same
    # command invocation - fall back to "item" if they haven't gotten to that field yet.
    chosen_type = getattr(interaction.namespace, "type", None) or "item"
    try:
        categories = await interaction.client.uex.get_categories(type=chosen_type)
    except UexApiError:
        return []
    current_lower = current.lower()
    matches = [c for c in categories if current_lower in (c.get("name") or "").lower()][:25]
    return [app_commands.Choice(name=(c.get("name") or "")[:100], value=c.get("id")) for c in matches]


class ConfirmListingView(discord.ui.View):
    """Confirm/cancel gate in front of the real POST - times out safely if ignored."""

    def __init__(self, bot: commands.Bot, secret_key: str, payload: dict, author_id: int) -> None:
        super().__init__(timeout=180)
        self.bot = bot
        self.secret_key = secret_key
        self.payload = payload
        self.author_id = author_id
        self.resolved = False

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.author_id:
            await interaction.response.send_message("Only the person who started this listing can confirm it.", ephemeral=True)
            return False
        return True

    async def on_timeout(self) -> None:
        self.resolved = True
        for item in self.children:
            item.disabled = True

    @discord.ui.button(label="Post listing", style=discord.ButtonStyle.green)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        self.resolved = True
        for item in self.children:
            item.disabled = True
        await interaction.response.edit_message(view=self)

        try:
            created = await self.bot.uex.post_marketplace_advertise(secret_key=self.secret_key, **self.payload)
        except UexApiError as exc:
            await interaction.followup.send(f"Failed to create the listing: {exc}", ephemeral=True)
            return

        listing_id = created.get("id") if isinstance(created, dict) else None
        confirmation = "Listing posted to UEX Marketplace."
        if listing_id is not None:
            confirmation += f" Listing id: **{listing_id}** (use /marketplace-delete-listing to remove it later)."
        await interaction.followup.send(confirmation, ephemeral=True)

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.grey)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        self.resolved = True
        for item in self.children:
            item.disabled = True
        await interaction.response.edit_message(view=self)
        await interaction.followup.send("Cancelled - nothing was posted.", ephemeral=True)


class ListingDetailsModal(discord.ui.Modal, title="Marketplace listing details"):
    listing_title = discord.ui.TextInput(label="Title", max_length=140, required=True)
    price = discord.ui.TextInput(label="Price (whole number)", max_length=12, required=True)
    unit = discord.ui.TextInput(
        label="Unit", placeholder="e.g. unit, scu, crate, hour, day, contract", max_length=32, required=True
    )
    description = discord.ui.TextInput(label="Description", style=discord.TextStyle.paragraph, max_length=2000, required=True)

    def __init__(self, bot: commands.Bot, base_payload: dict) -> None:
        super().__init__()
        self.bot = bot
        self.base_payload = base_payload

    async def on_submit(self, interaction: discord.Interaction) -> None:
        try:
            price_value = int(str(self.price.value).strip())
            if price_value <= 0:
                raise ValueError
        except ValueError:
            await interaction.response.send_message("Price must be a positive whole number. Try again.", ephemeral=True)
            return

        secret_key = await self.bot.db.get_user_secret_key(interaction.user.id)
        if not secret_key:
            await interaction.response.send_message(
                "You need to /link-uex-account before posting a listing - it's your account it gets posted under.",
                ephemeral=True,
            )
            return

        payload = dict(self.base_payload)
        payload.update(
            {
                "title": str(self.listing_title.value),
                "price": price_value,
                "unit": str(self.unit.value),
                "description": str(self.description.value),
                "language": DEFAULT_LANGUAGE,
            }
        )

        embed = discord.Embed(title="Confirm marketplace listing", color=discord.Color.orange())
        embed.add_field(name="Title", value=payload["title"], inline=False)
        embed.add_field(name="Operation", value=payload["operation"].title(), inline=True)
        embed.add_field(name="Type", value=payload["type"].title(), inline=True)
        embed.add_field(name="Price", value=f"{price_value:,} {payload['currency']} / {payload['unit']}", inline=True)
        embed.add_field(name="Description", value=payload["description"][:1000], inline=False)
        embed.set_footer(
            text="This will be posted publicly to the real UEX Marketplace, visible to all UEX users, "
            "not just this server. Click Post listing to confirm, or Cancel to back out."
        )

        view = ConfirmListingView(self.bot, secret_key=secret_key, payload=payload, author_id=interaction.user.id)
        await interaction.response.send_message(embed=embed, view=view, ephemeral=True)


class Marketplace(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.snapshot_item_activity.start()

    def cog_unload(self) -> None:
        self.snapshot_item_activity.cancel()

    @tasks.loop(hours=ITEM_ACTIVITY_SNAPSHOT_HOURS)
    async def snapshot_item_activity(self) -> None:
        """Merge the current /marketplace_trends snapshot (~100 items) into the accumulating
        traded-items index. UEX only ever exposes its live top-negotiation-activity window at
        once, so this index grows past that ~100 ceiling over days/weeks as different items
        rotate through that window across polls - see marketplace_item_activity in
        bot/db/database.py."""
        try:
            rows = await self.bot.uex.get_marketplace_trends()
        except UexApiError as exc:
            logger.warning("Failed to snapshot marketplace item activity: %s", exc)
            return
        activity = extract_item_activity(rows)
        if not activity:
            return
        await self.bot.db.upsert_marketplace_item_activity(activity)

        # Second half of the snapshot: learn which indexed items have an in-game quality at
        # all, from the bulk averages dump (rows are per quality_tier - any tier >= 1 row
        # means the item is quality-bearing). Runs after the activity upsert so items new in
        # this very snapshot get flagged in the same pass. Any failure here - API error or a
        # surprise in the undocumented _all payload shape - must degrade to "flags stay
        # stale until the next hourly run", never kill this task loop (an uncaught exception
        # would stop ALL future snapshots, autocomplete index included).
        quality_ids: set[int] = set()
        try:
            average_rows = await self.bot.uex.get_marketplace_prices_averages_all()
            quality_ids = extract_quality_item_ids(average_rows)
            if quality_ids:
                await self.bot.db.mark_items_have_quality(sorted(quality_ids))
        except UexApiError as exc:
            logger.warning("Failed to refresh marketplace quality flags: %s", exc)
        except Exception:
            logger.exception("Unexpected error refreshing marketplace quality flags")

        total = await self.bot.db.count_marketplace_item_activity()
        logger.info(
            "Marketplace item activity snapshot: %d items updated, %d total tracked, %d quality-bearing seen",
            len(activity), total, len(quality_ids),
        )

    @snapshot_item_activity.before_loop
    async def before_snapshot_item_activity(self) -> None:
        await self.bot.wait_until_ready()

    @app_commands.command(
        name="marketplace-index-status",
        description="How many Marketplace items the bot's autocomplete index has learned about so far.",
    )
    async def marketplace_index_status(self, interaction: discord.Interaction) -> None:
        count = await self.bot.db.count_marketplace_item_activity()
        if count == 0:
            await interaction.response.send_message(
                "No Marketplace activity tracked yet - the first snapshot happens shortly after the bot starts, "
                f"then every {ITEM_ACTIVITY_SNAPSHOT_HOURS}h after that.",
                ephemeral=True,
            )
            return
        quality_count = await self.bot.db.count_quality_flagged_items()
        await interaction.response.send_message(
            f"Tracking **{count}** Marketplace items observed in UEX's trending activity so far "
            f"(grows over time - UEX only exposes ~100 at once, refreshed every {ITEM_ACTIVITY_SNAPSHOT_HOURS}h). "
            f"**{quality_count}** of them are known to have an in-game quality (ores etc) - that's what "
            "makes /items-to-sell offer a quality picker. "
            "This index powers autocomplete on /marketplace-search, /marketplace-alert-add, and /marketplace-post.",
            ephemeral=True,
        )

    @app_commands.command(name="marketplace-search", description="Search active UEX Marketplace listings.")
    @app_commands.describe(
        query="Item name or keyword",
        operation="Filter to buy or sell listings",
        min_quality="Optional: only listings with quality at least this (seller-set, UEX's 0-100 scale)",
        max_quality="Optional: only listings with quality at most this (seller-set, UEX's 0-100 scale)",
    )
    @app_commands.choices(operation=OPERATION_CHOICES)
    @app_commands.autocomplete(query=traded_item_autocomplete)
    async def marketplace_search(
        self,
        interaction: discord.Interaction,
        query: str,
        operation: app_commands.Choice[str] | None = None,
        min_quality: float | None = None,
        max_quality: float | None = None,
    ) -> None:
        await interaction.response.defer()

        try:
            items = await self.bot.uex.get_items()
        except UexApiError:
            items = []
        id_item = find_item_id_by_name(items, query)

        try:
            if id_item is not None:
                listings = await self.bot.uex.get_marketplace_listings(
                    id_item=id_item, operation=operation.value if operation else None
                )
            else:
                listings = await self.bot.uex.get_marketplace_listings(operation=operation.value if operation else None)
                listings = filter_listings_by_keyword(listings, query)
        except UexApiError as exc:
            await interaction.followup.send(f"UEX API error: {exc}")
            return

        listings = exclude_sold_out(listings)
        listings = filter_listings_by_quality(listings, min_quality, max_quality)[:8]
        if not listings:
            quality_note = ""
            if min_quality is not None or max_quality is not None:
                quality_note = " in that quality range (most listings today don't have a quality value set at all)"
            await interaction.followup.send(f"No active listings found for '{query}'{quality_note}.")
            return

        embed = discord.Embed(title=f"Marketplace: {query}", color=discord.Color.teal())
        for listing in listings:
            price = parse_uex_number(listing.get("price"))
            currency = listing.get("currency", "UEC")
            seller = listing.get("user_username") or listing.get("user_name") or "unknown seller"
            location = listing.get("location") or "location not listed"
            stock = listing.get("in_stock")
            stock_text = f" · stock {stock}" if stock is not None else ""
            quality = parse_listing_quality(listing.get("quality"))
            quality_text = f" · quality {quality:.0f}" if quality is not None else ""
            price_text = f"{listing.get('operation', '?').title()} · {price:,.0f} {currency}" if price is not None else "Price n/a"
            embed.add_field(
                name=listing.get("title", "Untitled listing")[:256],
                value=price_text + f"\nby {seller} · {location}{stock_text}{quality_text}",
                inline=False,
            )
        footer = "UEX Marketplace · player-to-player listings"
        if min_quality is not None or max_quality is not None:
            footer += " · quality filter applied (0-100 scale, only listings the seller set a quality on)"
        embed.set_footer(text=footer)
        await interaction.followup.send(embed=embed)

    @app_commands.command(name="marketplace-trending", description="Most actively negotiated Marketplace items right now.")
    async def marketplace_trending(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer()
        try:
            rows = await self.bot.uex.get_marketplace_trends()
        except UexApiError as exc:
            await interaction.followup.send(f"UEX API error: {exc}")
            return

        if not rows:
            await interaction.followup.send("No marketplace trend data available right now.")
            return

        embed = discord.Embed(title="Trending on the UEX Marketplace", color=discord.Color.gold())
        lines = []
        for i, r in enumerate(rows[:10], start=1):
            sell = parse_uex_number(r.get("price_avg_sell"))
            sell_text = f"{sell:,.0f} {r.get('currency', 'UEC')}" if sell else "n/a"
            lines.append(
                f"**{i}. {r.get('item_name', 'Unknown')}** — {r.get('negotiations_count', 0)} negotiations · "
                f"{r.get('total_listings_count', 0)} active listings · avg sell {sell_text}"
            )
        embed.description = "\n".join(lines)
        embed.set_footer(text="UEX Marketplace · sorted by negotiation activity")
        await interaction.followup.send(embed=embed)

    @app_commands.command(
        name="marketplace-movers",
        description="Marketplace items whose average sell price has swung furthest vs their own trailing-month average.",
    )
    async def marketplace_movers(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer()
        try:
            rows = await self.bot.uex.get_marketplace_trends()
        except UexApiError as exc:
            await interaction.followup.send(f"UEX API error: {exc}")
            return

        gainers, losers = compute_marketplace_movers(rows, limit=10)
        if not gainers and not losers:
            await interaction.followup.send("No notable Marketplace price movers right now.")
            return

        embed = discord.Embed(title="Marketplace Price Movers", color=discord.Color.purple())
        if gainers:
            embed.add_field(
                name="Trending up",
                value="\n".join(
                    f"**{m.item_name}** +{m.pct_change:.1f}% ({m.current_avg_sell:,.0f} UEC)" for m in gainers
                ),
                inline=False,
            )
        if losers:
            embed.add_field(
                name="Trending down",
                value="\n".join(
                    f"**{m.item_name}** {m.pct_change:.1f}% ({m.current_avg_sell:,.0f} UEC)" for m in losers
                ),
                inline=False,
            )
        embed.set_footer(text="Current avg sell price vs. each item's own trailing-month average · UEX Marketplace data")
        await interaction.followup.send(embed=embed)

    @app_commands.command(
        name="marketplace-average",
        description="Average Marketplace prices for an item — current, 7-day and 30-day rolling — per quality tier.",
    )
    @app_commands.describe(
        item="Item name, e.g. 'Laranite Raw'",
        operation="Optional: only sell-side (what sellers ask) or buy-side (what buyers offer) averages",
        quality_tier="Optional: only this ore-quality tier",
    )
    @app_commands.choices(operation=OPERATION_CHOICES, quality_tier=QUALITY_TIER_CHOICES)
    @app_commands.autocomplete(item=traded_item_autocomplete)
    async def marketplace_average(
        self,
        interaction: discord.Interaction,
        item: str,
        operation: app_commands.Choice[str] | None = None,
        quality_tier: app_commands.Choice[int] | None = None,
    ) -> None:
        await interaction.response.defer()

        # Resolve to an exact catalog id when possible; otherwise pass the raw name through -
        # /marketplace_prices_averages accepts item_name and matches server-side, so a query
        # that doesn't resolve locally still has a shot (unlike /marketplace_prices_history,
        # which /marketplace-history has to resolve to an id first).
        try:
            items = await self.bot.uex.get_items()
        except UexApiError:
            items = []
        id_item = find_item_id_by_name(items, item)

        try:
            if id_item is not None:
                rows = await self.bot.uex.get_marketplace_prices_averages(
                    id_item=id_item,
                    operation=operation.value if operation is not None else None,
                    quality_tier=quality_tier.value if quality_tier is not None else None,
                )
            else:
                rows = await self.bot.uex.get_marketplace_prices_averages(
                    item_name=item,
                    operation=operation.value if operation is not None else None,
                    quality_tier=quality_tier.value if quality_tier is not None else None,
                )
        except UexApiError as exc:
            await interaction.followup.send(f"UEX API error: {exc}")
            return

        entries = parse_marketplace_average_rows(rows)
        if not entries:
            filter_notes = []
            if operation is not None:
                filter_notes.append(f"{operation.value}-side")
            if quality_tier is not None:
                filter_notes.append(f"tier {quality_tier.name}")
            filter_text = f" ({', '.join(filter_notes)})" if filter_notes else ""
            await interaction.followup.send(
                f"No Marketplace average price data for '{item}'{filter_text} - averages only exist "
                "for items with Marketplace listing activity."
            )
            return

        display_name = entries[0].item_name or item
        embed = discord.Embed(title=f"{display_name} — Marketplace price averages", color=discord.Color.teal())

        by_operation: dict[str, list[MarketplaceAverageEntry]] = {}
        for entry in entries:
            by_operation.setdefault(entry.operation, []).append(entry)

        def _fmt(value: float | None) -> str:
            return f"{value:,.0f}" if value is not None else "n/a"

        side_headers = {
            "sell": "Sell listings — what sellers ask",
            "buy": "Buy listings — what buyers offer",
        }
        for op, op_entries in by_operation.items():
            lines = []
            for entry in op_entries[:MAX_AVERAGE_LINES_PER_SIDE]:
                tier_label = QUALITY_TIER_LABELS.get(entry.quality_tier, "No tier")
                lines.append(
                    f"**{tier_label}** — 30d avg **{_fmt(entry.price_avg_month)} {entry.currency}**/{entry.unit}"
                    f" · 7d {_fmt(entry.price_avg_week)} · now {_fmt(entry.price_avg)}"
                    f" · {entry.listings_count} listings"
                )
            overflow = len(op_entries) - MAX_AVERAGE_LINES_PER_SIDE
            if overflow > 0:
                lines.append(f"…and {overflow} more tier/currency combinations")
            embed.add_field(
                name=side_headers.get(op, f"{op.title()} listings" if op else "Listings"),
                value="\n".join(lines),
                inline=False,
            )

        footer = "UEX Marketplace rolling averages · updated hourly · 7d/30d fall back to the current average when an item has little history"
        if quality_tier is not None:
            footer += f" · quality tier: {quality_tier.name}"
        embed.set_footer(text=footer)
        await interaction.followup.send(embed=embed)

    @app_commands.command(
        name="marketplace-history",
        description="Chart Marketplace price history for an item, optionally filtered to one ore-quality tier.",
    )
    @app_commands.describe(
        item="Item name, e.g. 'Laranite Raw'",
        quality_tier="Optional: only show price changes from listings in this quality tier",
        operation="Optional: only show buy-side or sell-side price changes",
    )
    @app_commands.choices(quality_tier=QUALITY_TIER_CHOICES, operation=OPERATION_CHOICES)
    @app_commands.autocomplete(item=traded_item_autocomplete)
    async def marketplace_history(
        self,
        interaction: discord.Interaction,
        item: str,
        quality_tier: app_commands.Choice[int] | None = None,
        operation: app_commands.Choice[str] | None = None,
    ) -> None:
        await interaction.response.defer()

        try:
            items = await self.bot.uex.get_items()
        except UexApiError:
            items = []
        id_item = find_item_id_by_name(items, item)
        if id_item is None:
            await interaction.followup.send(
                f"Couldn't resolve '{item}' to a single catalog item - try picking an exact match from autocomplete."
            )
            return

        try:
            rows = await self.bot.uex.get_marketplace_prices_history(
                id_item=id_item,
                quality_tier=quality_tier.value if quality_tier else None,
                operation=operation.value if operation else None,
            )
        except UexApiError as exc:
            await interaction.followup.send(f"UEX API error: {exc}")
            return

        if not rows:
            tier_note = f" in tier {quality_tier.name}" if quality_tier else ""
            await interaction.followup.send(f"No Marketplace price history found for '{item}'{tier_note}.")
            return

        history_rows = reshape_marketplace_history_rows(rows)
        tier_label = quality_tier.name if quality_tier else "all qualities"
        chart = render_price_history_chart(
            commodity_name=item, terminal_name=f"UEX Marketplace ({tier_label})", history_rows=history_rows
        )
        if chart is None:
            await interaction.followup.send(f"No plottable Marketplace price history found for '{item}'.")
            return

        file = discord.File(chart, filename="marketplace_history.png")
        footer = "UEX Marketplace price history · one point per listed price change, not a fixed interval"
        if quality_tier:
            footer += f" · quality tier: {quality_tier.name}"
        embed = discord.Embed(title=f"{item} — Marketplace Price History", color=discord.Color.teal())
        embed.set_footer(text=footer)
        embed.set_image(url="attachment://marketplace_history.png")
        await interaction.followup.send(embed=embed, file=file)

    @app_commands.command(name="my-favorites", description="Your favorited UEX Marketplace listings.")
    async def my_favorites(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        secret_key = await self.bot.db.get_user_secret_key(interaction.user.id)
        if not secret_key:
            await interaction.followup.send("You haven't linked a UEX account yet. Run /link-uex-account first.")
            return

        try:
            rows = await self.bot.uex.get_marketplace_favorites(secret_key=secret_key)
        except UexApiError as exc:
            await interaction.followup.send(f"UEX API error: {exc}")
            return

        if not rows:
            await interaction.followup.send("You have no favorited Marketplace listings.")
            return

        lines = []
        for f in rows[:15]:
            price = parse_uex_number(f.get("price"))
            price_text = f"{price:,.0f} {f.get('currency', 'UEC')}" if price is not None else "price n/a"
            title = f.get("title") or f.get("listing_title") or "Untitled listing"
            sold_note = " (sold out)" if f.get("is_sold_out") else ""
            lines.append(f"#{f.get('id')} — **{title}** · {price_text}{sold_note}")
        await interaction.followup.send("\n".join(lines))

    @app_commands.command(name="my-negotiations", description="Your own active UEX Marketplace deals.")
    async def my_negotiations(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        secret_key = await self.bot.db.get_user_secret_key(interaction.user.id)
        if not secret_key:
            await interaction.followup.send("You haven't linked a UEX account yet. Run /link-uex-account first.")
            return

        try:
            rows = await self.bot.uex.get_marketplace_negotiations(secret_key=secret_key)
        except UexApiError as exc:
            await interaction.followup.send(f"UEX API error: {exc}")
            return

        if not rows:
            await interaction.followup.send("No active marketplace negotiations found on your account.")
            return

        lines = []
        for n in rows[:15]:
            role = "selling" if n.get("is_listing_advertiser") else "buying"
            status = "closed" if n.get("date_closed") else "open"
            price = parse_uex_number(n.get("price"))
            price_text = f"{price:,.0f}" if price is not None else "?"
            lines.append(
                f"#{n.get('id')} {role} — {n.get('listing_title', 'Untitled')} · "
                f"{price_text} {n.get('currency', 'UEC')} · {status}"
            )
        await interaction.followup.send("\n".join(lines))

    @app_commands.command(name="marketplace-post", description="Create a new UEX Marketplace listing (opens a form, then asks you to confirm before posting).")
    @app_commands.describe(
        operation="Whether you're selling or buying",
        type="What kind of listing this is",
        category="Listing category",
        currency="Currency for the price",
        item="Optional: link this listing to a specific catalog item",
    )
    @app_commands.choices(operation=OPERATION_CHOICES, type=TYPE_CHOICES, currency=CURRENCY_CHOICES)
    @app_commands.autocomplete(category=category_autocomplete, item=traded_item_autocomplete)
    async def marketplace_post(
        self,
        interaction: discord.Interaction,
        operation: app_commands.Choice[str],
        type: app_commands.Choice[str],
        category: int,
        currency: app_commands.Choice[str],
        item: str | None = None,
    ) -> None:
        secret_key = await self.bot.db.get_user_secret_key(interaction.user.id)
        if not secret_key:
            await interaction.response.send_message(
                "You need to /link-uex-account before posting a listing - it's your account it gets posted under.",
                ephemeral=True,
            )
            return

        base_payload = {
            "operation": operation.value,
            "type": type.value,
            "id_category": category,
            "currency": currency.value,
        }

        if item:
            try:
                items = await self.bot.uex.get_items()
                id_item = find_item_id_by_name(items, item)
                if id_item is not None:
                    base_payload["id_item"] = id_item
            except UexApiError:
                pass  # optional field - fine to skip if lookup fails

        await interaction.response.send_modal(ListingDetailsModal(self.bot, base_payload))

    @app_commands.command(name="marketplace-delete-listing", description="Delete one of your own UEX Marketplace listings.")
    @app_commands.describe(listing_id="The listing id to delete (shown when it was created, or via /marketplace-search)")
    async def marketplace_delete_listing(self, interaction: discord.Interaction, listing_id: int) -> None:
        secret_key = await self.bot.db.get_user_secret_key(interaction.user.id)
        if not secret_key:
            await interaction.response.send_message("You haven't linked a UEX account yet. Run /link-uex-account first.", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True)
        try:
            await self.bot.uex.delete_marketplace_listing(listing_id=listing_id, secret_key=secret_key)
        except UexApiError as exc:
            await interaction.followup.send(f"Couldn't delete listing #{listing_id}: {exc}")
            return

        await interaction.followup.send(f"Listing #{listing_id} deleted (if it existed and belonged to you).")


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Marketplace(bot))
