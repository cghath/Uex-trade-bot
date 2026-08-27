"""Personal catalog-item inventory, sellability, and guarded UEX auto-posting."""
from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timezone
from typing import Any
from zoneinfo import ZoneInfo

import discord
from discord import app_commands
from discord.ext import commands, tasks

from bot.uex.exceptions import UexApiError
from bot.uex.inventory import (
    DEFAULT_MARKETPLACE_TIMEZONE,
    _flag,
    _integer,
    build_inventory_listing_payload,
    extract_listing_id,
    next_posting_time,
    quality_label,
    recommend_balanced_price,
    recommend_posting_window,
)
from bot.uex.marketplace import find_item_id_by_name, marketplace_item_url, parse_uex_number

logger = logging.getLogger("uexbot.personal_inventory")

POST_CHECK_MINUTES = 5
INVENTORY_PAGE_SIZE = 10
SELECTION_PAGE_SIZE = 25
MAX_BATCH_POSTS = 10
MAX_TRACKED_POSTS_PER_CYCLE = 50
RECONCILE_FETCH_BATCH_SIZE = 10

# Fresh listings may not be visible via GET /marketplace_listings yet if UEX staff approval
# is still pending. This grace period is an unvalidated guess, not an observed figure - we
# have no confirmed data on real approval latency. Tune once that's actually been observed;
# until then, treat the exact duration as a placeholder rather than a load-bearing constant.
LISTING_APPROVAL_GRACE_SECONDS = 7200

PRICING_STRATEGY_LABELS = {
    "balanced": "at the recommended price",
    "undercut": "10% below the recommended price",
    "premium": "10% above the recommended price",
}

ITEM_UNIT_CHOICES = [
    app_commands.Choice(name=name.title(), value=name)
    for name in ("unit", "box", "crate", "cscu", "dozen", "hundred", "pack", "pair", "scu", "set", "stack", "thousand")
]


async def inventory_item_autocomplete(
    interaction: discord.Interaction, current: str
) -> list[app_commands.Choice[str]]:
    query = current.strip().lower()
    activity = await interaction.client.db.list_marketplace_item_activity()
    activity.sort(
        key=lambda row: (row.get("negotiations_count") or 0) + (row.get("listings_count") or 0),
        reverse=True,
    )
    items = [
        {"name": row.get("item_name"), "id": row.get("id_item")}
        for row in activity
    ]
    cached_catalog = interaction.client.uex.get_cached_item_catalog()
    known_ids = {item.get("id") for item in items}
    items.extend(item for item in cached_catalog if item.get("id") not in known_ids)
    matches = [item for item in items if query in (item.get("name") or "").lower()][:25]
    return [
        app_commands.Choice(name=str(item.get("name") or "")[:100], value=str(item.get("name") or ""))
        for item in matches
        if item.get("name")
    ]


class InventoryEntrySelect(discord.ui.Select):
    def __init__(self, owner: "InventorySelectionView") -> None:
        self.owner = owner
        page_rows = owner.page_rows
        options = []
        for row in page_rows:
            available = int(row["quantity"]) - int(row["reserved_quantity"])
            score = row.get("sellability_score")
            score_text = f"{float(score):.0f}/100" if score is not None else "collecting"
            options.append(
                discord.SelectOption(
                    label=f"#{row['id']} · {row['item_name']}"[:100],
                    value=str(row["id"]),
                    description=f"{available} available · sellability {score_text}"[:100],
                    default=int(row["id"]) in owner.selected_ids,
                )
            )
        super().__init__(
            placeholder="Check the inventory stacks to schedule",
            min_values=0,
            max_values=len(options),
            options=options,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        page_ids = {int(row["id"]) for row in self.owner.page_rows}
        chosen = {int(value) for value in self.values}
        proposed = (self.owner.selected_ids - page_ids) | chosen
        warning = ""
        if len(proposed) > MAX_BATCH_POSTS:
            keep = set(sorted(proposed)[:MAX_BATCH_POSTS])
            proposed = keep
            warning = f" Maximum {MAX_BATCH_POSTS} stacks per batch."
        self.owner.selected_ids = proposed
        self.owner.rebuild()
        await interaction.response.edit_message(
            content=self.owner.status_text + warning,
            view=self.owner,
        )


class InventorySelectionView(discord.ui.View):
    def __init__(self, cog: "PersonalInventory", author_id: int, rows: list[dict[str, Any]]) -> None:
        super().__init__(timeout=300)
        self.cog = cog
        self.author_id = author_id
        self.rows = rows
        self.page = 0
        self.selected_ids: set[int] = set()
        self.rebuild()

    @property
    def page_count(self) -> int:
        return max(1, (len(self.rows) + SELECTION_PAGE_SIZE - 1) // SELECTION_PAGE_SIZE)

    @property
    def page_rows(self) -> list[dict[str, Any]]:
        start = self.page * SELECTION_PAGE_SIZE
        return self.rows[start : start + SELECTION_PAGE_SIZE]

    @property
    def status_text(self) -> str:
        return (
            f"Inventory page **{self.page + 1}/{self.page_count}** · "
            f"**{len(self.selected_ids)}** stack(s) selected. "
            "Only unreserved quantities with a minimum price can be authorized."
        )

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.author_id:
            await interaction.response.send_message("This inventory menu belongs to someone else.", ephemeral=True)
            return False
        return True

    def rebuild(self) -> None:
        self.clear_items()
        if self.page_rows:
            self.add_item(InventoryEntrySelect(self))

        previous = discord.ui.Button(label="Previous", style=discord.ButtonStyle.secondary, disabled=self.page == 0)
        previous.callback = self.previous_page
        self.add_item(previous)

        following = discord.ui.Button(
            label="Next", style=discord.ButtonStyle.secondary, disabled=self.page >= self.page_count - 1
        )
        following.callback = self.next_page
        self.add_item(following)

        schedule = discord.ui.Button(label="Review selected", style=discord.ButtonStyle.green)
        schedule.callback = self.review_selected
        self.add_item(schedule)

    async def previous_page(self, interaction: discord.Interaction) -> None:
        self.page = max(0, self.page - 1)
        self.rebuild()
        await interaction.response.edit_message(content=self.status_text, view=self)

    async def next_page(self, interaction: discord.Interaction) -> None:
        self.page = min(self.page_count - 1, self.page + 1)
        self.rebuild()
        await interaction.response.edit_message(content=self.status_text, view=self)

    async def review_selected(self, interaction: discord.Interaction) -> None:
        if not self.selected_ids:
            await interaction.response.send_message("Select at least one inventory stack first.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)

        selected = [row for row in self.rows if int(row["id"]) in self.selected_ids]
        missing_floor = [row for row in selected if not row.get("minimum_price")]
        if missing_floor:
            ids = ", ".join(f"#{row['id']} {row['item_name']}" for row in missing_floor)
            await interaction.followup.send(
                f"Set a manual minimum price first for: {ids}. Use `/inventory-set-minimum`.",
                ephemeral=True,
            )
            return

        timing_rows = await self.cog.bot.db.get_marketplace_timing_rows()
        specs: list[dict[str, Any]] = []
        embed = discord.Embed(
            title="Authorize automatic UEX posting",
            description=(
                "Each stack will post in its next recommended Eastern-time window. The price is recalculated "
                "immediately before posting and can never go below your manual minimum."
            ),
            color=discord.Color.orange(),
        )
        for row in selected:
            available = int(row["quantity"]) - int(row["reserved_quantity"])
            if available <= 0:
                continue
            window = recommend_posting_window(timing_rows, id_item=int(row["id_item"]))
            if window is None:
                await interaction.followup.send(
                    "There is not enough hourly Marketplace history to calculate a posting window yet.",
                    ephemeral=True,
                )
                return
            scheduled_for = next_posting_time(window)
            average_rows = await self.cog.bot.db.get_item_tier_stats(int(row["id_item"]))
            own_prices = await self.cog.bot.db.get_inventory_completed_unit_prices(
                user_id=self.author_id,
                id_item=int(row["id_item"]), quality=int(row["quality"]), unit=str(row["unit"])
            )
            price = recommend_balanced_price(
                listings=[],
                average_rows=average_rows,
                quality=int(row["quality"]),
                unit=str(row["unit"]),
                minimum_price=int(row["minimum_price"]),
                own_completed_unit_prices=own_prices,
            )
            specs.append(
                {
                    "inventory_id": int(row["id"]),
                    "quantity": available,
                    "scheduled_for": scheduled_for,
                    "auto_relist": True,
                }
            )
            local_time = scheduled_for.astimezone(ZoneInfo(DEFAULT_MARKETPLACE_TIMEZONE))
            embed.add_field(
                name=f"#{row['id']} · {row['item_name']}"[:256],
                value=(
                    f"Qty **{available}** · preview **{price.price:,} UEC/{row['unit']}** "
                    f"(minimum {int(row['minimum_price']):,})\n"
                    f"Next window: **{window.label} ET** · scheduled {local_time.strftime('%a %b %d, %I:%M %p')} ET "
                    f"· timing confidence {window.confidence.lower()}"
                )[:1024],
                inline=False,
            )

        if not specs:
            await interaction.followup.send("None of those stacks currently has unreserved inventory.", ephemeral=True)
            return
        embed.set_footer(
            text=(
                "This one confirmation authorizes posting and guarded 48-hour repricing/relisting until sold or cancelled. "
                "Ambiguous UEX results stop and ask you; they are never retried blindly. "
                "Pick a pricing strategy below before authorizing."
            )
        )
        await interaction.followup.send(
            embed=embed,
            view=AuthorizeScheduleView(self.cog, self.author_id, specs),
            ephemeral=True,
        )


class AuthorizeScheduleView(discord.ui.View):
    def __init__(self, cog: "PersonalInventory", author_id: int, specs: list[dict[str, Any]]) -> None:
        super().__init__(timeout=180)
        self.cog = cog
        self.author_id = author_id
        self.specs = specs
        self.resolved = False
        self.pricing_strategy = "balanced"

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.author_id:
            await interaction.response.send_message("Only the inventory owner can authorize these posts.", ephemeral=True)
            return False
        return True

    def disable(self) -> None:
        self.resolved = True
        for child in self.children:
            child.disabled = True

    @discord.ui.select(
        placeholder="Pricing strategy: at the recommended price (default)",
        options=[
            discord.SelectOption(label="Post at the recommended price", value="balanced", default=True),
            discord.SelectOption(label="Post 10% below the recommended price", value="undercut"),
            discord.SelectOption(label="Post 10% above the recommended price", value="premium"),
        ],
    )
    async def choose_pricing_strategy(self, interaction: discord.Interaction, select: discord.ui.Select) -> None:
        self.pricing_strategy = select.values[0]
        for option in select.options:
            option.default = option.value == self.pricing_strategy
        await interaction.response.edit_message(
            content=f"Pricing strategy: **{PRICING_STRATEGY_LABELS[self.pricing_strategy]}** shown above.",
            view=self,
        )

    @discord.ui.button(label="Authorize scheduled posts", style=discord.ButtonStyle.green)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        self.disable()
        await interaction.response.edit_message(view=self)
        for spec in self.specs:
            spec["pricing_strategy"] = self.pricing_strategy
        try:
            job_ids = await self.cog.bot.db.create_inventory_post_jobs(self.author_id, self.specs)
        except ValueError as exc:
            await interaction.followup.send(f"Nothing was scheduled: {exc}", ephemeral=True)
            return
        await interaction.followup.send(
            "Scheduled safely. Job ids: " + ", ".join(f"**#{job_id}**" for job_id in job_ids) + ".",
            ephemeral=True,
        )

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        self.disable()
        await interaction.response.edit_message(view=self)
        await interaction.followup.send("Cancelled—no inventory was reserved or scheduled.", ephemeral=True)


class PersonalInventory(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.process_inventory_posts.start()

    def cog_unload(self) -> None:
        self.process_inventory_posts.cancel()

    @app_commands.command(name="inventory-add", description="Add a catalogued item stack to your personal inventory.")
    @app_commands.describe(
        item="Exact catalogued item name",
        quantity="How many you currently have",
        location="Where the stack is stored",
        quality="Item quality from 0 to 1000; use 0 when quality does not apply",
        unit="How UEX should price and count it",
        minimum_price="Optional now, but required before automatic posting",
        notes="Optional private inventory notes; these are never posted publicly to UEX",
    )
    @app_commands.choices(unit=ITEM_UNIT_CHOICES)
    @app_commands.autocomplete(item=inventory_item_autocomplete)
    async def inventory_add(
        self,
        interaction: discord.Interaction,
        item: str,
        quantity: app_commands.Range[int, 1, 1_000_000],
        location: str,
        quality: app_commands.Range[int, 0, 1000] = 0,
        unit: app_commands.Choice[str] | None = None,
        minimum_price: app_commands.Range[int, 1, 2_000_000_000] | None = None,
        notes: str | None = None,
    ) -> None:
        await interaction.response.defer(ephemeral=True)
        try:
            items = await self.bot.uex.get_item_catalog()
        except UexApiError as exc:
            await interaction.followup.send(f"UEX could not load the item catalog: {exc}", ephemeral=True)
            return
        id_item = find_item_id_by_name(items, item)
        catalog_item = next((row for row in items if _integer(row.get("id")) == id_item), None)
        if id_item is None or not catalog_item or _integer(catalog_item.get("id_category")) is None:
            await interaction.followup.send(
                "That did not resolve to one catalogued item. Pick an exact autocomplete result and try again.",
                ephemeral=True,
            )
            return
        inventory_id = await self.bot.db.add_inventory_item(
            user_id=interaction.user.id,
            id_item=id_item,
            id_category=int(catalog_item["id_category"]),
            item_name=str(catalog_item.get("name") or item),
            item_slug=catalog_item.get("slug"),
            quantity=int(quantity),
            quality=int(quality),
            location=location[:200],
            unit=unit.value if unit else "unit",
            minimum_price=int(minimum_price) if minimum_price is not None else None,
            notes=notes[:1500] if notes else None,
        )
        url = marketplace_item_url(id_item)
        floor_note = (
            f" Minimum: **{int(minimum_price):,} UEC/{unit.value if unit else 'unit'}**."
            if minimum_price is not None
            else " Set a minimum with `/inventory-set-minimum` before scheduling it."
        )
        await interaction.followup.send(
            f"Added inventory **#{inventory_id}**: [{catalog_item.get('name')}]({url}) · "
            f"qty **{quantity}** · quality **{quality_label(int(quality))}** · {location}.{floor_note}",
            ephemeral=True,
        )

    @app_commands.command(name="inventory", description="Review your inventory, UEX item links, and sellability ratings.")
    @app_commands.describe(page="Page number when you have more than ten inventory stacks")
    async def inventory(self, interaction: discord.Interaction, page: int = 1) -> None:
        rows = await self.bot.db.list_inventory(interaction.user.id)
        rows = [row for row in rows if int(row["quantity"]) > 0 or int(row["reserved_quantity"]) > 0]
        if not rows:
            await interaction.response.send_message(
                "Your inventory is empty. Add a catalogued item with `/inventory-add`.", ephemeral=True
            )
            return
        page_count = max(1, (len(rows) + INVENTORY_PAGE_SIZE - 1) // INVENTORY_PAGE_SIZE)
        page = max(1, min(int(page), page_count))
        start = (page - 1) * INVENTORY_PAGE_SIZE
        embed = discord.Embed(
            title=f"Personal inventory · page {page}/{page_count}",
            description=(
                "Item names open matching UEX postings, including sold-out rows UEX still exposes "
                "(useful asking-price evidence, not proof of the final deal price). "
                "Sellability is the same 0–100 rating used by `/liquidity-rank`."
            ),
            color=discord.Color.blurple(),
        )
        for row in rows[start : start + INVENTORY_PAGE_SIZE]:
            available = int(row["quantity"]) - int(row["reserved_quantity"])
            score = row.get("sellability_score")
            score_text = f"**{float(score):.0f}/100**" if score is not None else "still collecting"
            minimum = row.get("minimum_price")
            minimum_text = f"{int(minimum):,} UEC/{row['unit']}" if minimum else "not set ⚠️"
            link = marketplace_item_url(int(row["id_item"]))
            value = (
                f"[{row['item_name']}]({link}) · quality {quality_label(int(row['quality']))}\n"
                f"Qty **{row['quantity']}** · available **{available}** · reserved **{row['reserved_quantity']}** "
                f"· {row['location']}\n"
                f"Sellability {score_text} · minimum **{minimum_text}**"
            )
            if row.get("active_job_count"):
                value += f" · active posting jobs **{row['active_job_count']}**"
            if row.get("notes"):
                value += f"\nPrivate notes: {str(row['notes'])[:300]}"
            embed.add_field(name=f"Inventory #{row['id']}", value=value[:1024], inline=False)
        embed.set_footer(text="Use /inventory-sell to check off stacks for guarded automatic posting.")
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @app_commands.command(name="inventory-set-minimum", description="Set the hard UEC price floor for an inventory stack.")
    @app_commands.describe(inventory_id="Number shown by /inventory", minimum_price="Never post below this UEC price per unit")
    async def inventory_set_minimum(
        self,
        interaction: discord.Interaction,
        inventory_id: int,
        minimum_price: app_commands.Range[int, 1, 2_000_000_000],
    ) -> None:
        changed = await self.bot.db.set_inventory_minimum_price(
            interaction.user.id, inventory_id, int(minimum_price)
        )
        entry = await self.bot.db.get_inventory_item(interaction.user.id, inventory_id) if changed else None
        message = (
            f"Inventory #{inventory_id} will never post or relist below "
            f"**{int(minimum_price):,} UEC per {entry['unit']}**. "
            "A listing already live on UEX keeps its current price until cancelled or relisted."
            if changed
            else f"Inventory #{inventory_id} was not found in your inventory."
        )
        await interaction.response.send_message(message, ephemeral=True)

    @app_commands.command(name="inventory-remove", description="Remove an unreserved quantity from your personal inventory.")
    @app_commands.describe(inventory_id="Number shown by /inventory", quantity="How many to remove")
    async def inventory_remove(
        self,
        interaction: discord.Interaction,
        inventory_id: int,
        quantity: app_commands.Range[int, 1, 1_000_000],
    ) -> None:
        try:
            remaining = await self.bot.db.remove_inventory_quantity(
                interaction.user.id, inventory_id, int(quantity)
            )
        except ValueError as exc:
            await interaction.response.send_message(str(exc), ephemeral=True)
            return
        if remaining is None:
            message = f"Inventory #{inventory_id} was not found in your inventory."
        else:
            message = f"Removed **{quantity}** from inventory #{inventory_id}; **{remaining}** remain."
        await interaction.response.send_message(message, ephemeral=True)

    @app_commands.command(name="inventory-sell", description="Check off inventory stacks for best-time automatic UEX posting.")
    async def inventory_sell(self, interaction: discord.Interaction) -> None:
        if not await self.bot.db.has_linked_uex_account(interaction.user.id):
            await interaction.response.send_message(
                "Link your own UEX account with `/link-uex-account` before scheduling public listings.",
                ephemeral=True,
            )
            return
        rows = await self.bot.db.list_inventory(interaction.user.id)
        available = [
            row for row in rows
            if int(row["quantity"]) - int(row["reserved_quantity"]) > 0
        ]
        if not available:
            await interaction.response.send_message(
                "You have no unreserved inventory to schedule. Add items with `/inventory-add`.", ephemeral=True
            )
            return
        view = InventorySelectionView(self, interaction.user.id, available)
        await interaction.response.send_message(content=view.status_text, view=view, ephemeral=True)

    @app_commands.command(name="best-posting-time", description="Estimate the strongest Marketplace posting window from collected history.")
    @app_commands.describe(item="Optional exact catalogued item; otherwise show the overall market window")
    @app_commands.autocomplete(item=inventory_item_autocomplete)
    async def best_posting_time(self, interaction: discord.Interaction, item: str | None = None) -> None:
        await interaction.response.defer(ephemeral=True)
        id_item = None
        if item:
            try:
                items = await self.bot.uex.get_item_catalog()
            except UexApiError as exc:
                await interaction.followup.send(f"UEX could not load the item catalog: {exc}", ephemeral=True)
                return
            id_item = find_item_id_by_name(items, item)
            if id_item is None:
                await interaction.followup.send(
                    "Pick an exact catalogued item from autocomplete and try again.", ephemeral=True
                )
                return
        rows = await self.bot.db.get_marketplace_timing_rows()
        window = recommend_posting_window(rows, id_item=id_item)
        if window is None:
            await interaction.followup.send(
                "There is not enough hourly Marketplace history yet. Leave the PC collector running and try again later.",
                ephemeral=True,
            )
            return
        subject = item or "Overall UEX Marketplace"
        embed = discord.Embed(
            title=f"{subject} · best posting window",
            description=f"**{window.label} Eastern Time**",
            color=discord.Color.teal(),
        )
        embed.add_field(name="Evidence scope", value=window.scope.title(), inline=True)
        embed.add_field(name="Confidence", value=window.confidence, inline=True)
        embed.add_field(name="History collected", value=f"{window.days_observed} local days", inline=True)
        embed.add_field(
            name="Why this window",
            value=(
                f"Weighted demand changes: **{window.demand_events:,.1f}** · "
                f"new competing sell-listing changes: **{window.new_sell_listings:,.0f}**."
            ),
            inline=False,
        )
        if window.confidence == "Low":
            embed.add_field(
                name="⚠️ Early estimate",
                value="Less than 14 days of history is available. Keep collecting before treating this as a stable pattern.",
                inline=False,
            )
        embed.set_footer(text="Demand uses positive hourly changes in successful/open negotiations; it is not a guarantee of sale.")
        await interaction.followup.send(embed=embed, ephemeral=True)

    @app_commands.command(name="inventory-confirm-sale", description="Resolve a tracked listing when UEX cannot prove how many sold.")
    @app_commands.describe(job_id="Posting job number from the bot's warning", quantity_sold="How many actually sold; use 0 if none sold")
    async def inventory_confirm_sale(
        self,
        interaction: discord.Interaction,
        job_id: int,
        quantity_sold: app_commands.Range[int, 0, 1_000_000],
    ) -> None:
        try:
            result = await self.bot.db.confirm_ambiguous_inventory_sale(
                interaction.user.id, job_id, int(quantity_sold)
            )
        except ValueError as exc:
            await interaction.response.send_message(str(exc), ephemeral=True)
            return
        if not result:
            await interaction.response.send_message(
                f"Job #{job_id} is not one of your listings awaiting confirmation.", ephemeral=True
            )
            return

        relist_note = ""
        if result["unsold"] and result["auto_relist"]:
            timing_rows = await self.bot.db.get_marketplace_timing_rows()
            entry = await self.bot.db.get_inventory_item(interaction.user.id, result["inventory_id"])
            window = recommend_posting_window(timing_rows, id_item=int(entry["id_item"])) if entry else None
            if window:
                scheduled_for = next_posting_time(window)
                try:
                    new_jobs = await self.bot.db.create_inventory_post_jobs(
                        interaction.user.id,
                        [
                            {
                                "inventory_id": result["inventory_id"],
                                "quantity": result["unsold"],
                                "scheduled_for": scheduled_for,
                                "auto_relist": True,
                                "relist_count": result["relist_count"],
                                "pricing_strategy": result["pricing_strategy"],
                            }
                        ],
                    )
                except ValueError as exc:
                    await interaction.response.send_message(
                        f"Recorded **{result['sold']}** sold for job #{job_id}, but the **{result['unsold']}** "
                        f"unsold remainder could not be rescheduled: {exc}. Use `/inventory-sell` to reschedule it manually.",
                        ephemeral=True,
                    )
                    return
                relist_note = f" The **{result['unsold']}** unsold item(s) were safely rescheduled as job #{new_jobs[0]}."
        await interaction.response.send_message(
            f"Recorded **{result['sold']}** sold for job #{job_id}.{relist_note}", ephemeral=True
        )

    @app_commands.command(name="inventory-cancel-post", description="Cancel a pending or active automatic inventory post.")
    @app_commands.describe(job_id="Posting job number shown when the stack was scheduled")
    async def inventory_cancel_post(self, interaction: discord.Interaction, job_id: int) -> None:
        job = await self.bot.db.get_inventory_post_job(interaction.user.id, job_id)
        if not job:
            await interaction.response.send_message(f"Inventory job #{job_id} was not found.", ephemeral=True)
            return
        if job["status"] == "pending":
            cancelled = await self.bot.db.cancel_pending_inventory_post(interaction.user.id, job_id)
            message = (
                f"Cancelled job #{job_id}; its reserved inventory is available again."
                if cancelled
                else f"Job #{job_id} changed state before it could be cancelled."
            )
            await interaction.response.send_message(message, ephemeral=True)
            return
        if job["status"] in {"listed", "needs_confirmation"} and job.get("listing_id"):
            secret_key = await self.bot.db.get_user_secret_key(interaction.user.id)
            if not secret_key:
                await interaction.response.send_message(
                    "Relink your UEX account before cancelling the public listing.", ephemeral=True
                )
                return
            await interaction.response.defer(ephemeral=True)
            try:
                listing_rows = await self.bot.uex.get_marketplace_listings(id=int(job["listing_id"]), use_cache=False)
                if not listing_rows:
                    await self.bot.db.mark_inventory_post_needs_confirmation(
                        job_id, "Cancellation requested, but UEX no longer exposed final stock"
                    )
                    await interaction.followup.send(
                        "UEX no longer exposes that listing, so the bot cannot safely decide what remains. "
                        f"Nothing was relisted or released; resolve job #{job_id} with `/inventory-confirm-sale`.",
                        ephemeral=True,
                    )
                    return
                current_stock = _integer(listing_rows[0].get("in_stock"))
                if current_stock is None:
                    await interaction.followup.send(
                        "UEX returned the listing without a remaining-stock value. Cancellation stopped so your "
                        "local inventory is not guessed; try again later or use `/inventory-confirm-sale` if it disappears.",
                        ephemeral=True,
                    )
                    return
                sold_out = _flag(listing_rows[0].get("is_sold_out"))
                # Delete on UEX before touching any local state: if this raises, nothing below
                # has run yet, so there's nothing to leave inconsistent or roll back.
                await self.bot.uex.delete_marketplace_listing(
                    listing_id=int(job["listing_id"]), secret_key=secret_key
                )
            except UexApiError as exc:
                await interaction.followup.send(
                    f"UEX could not confirm deletion of listing #{job['listing_id']}: {exc}",
                    ephemeral=True,
                )
                return
            outcome = await self.bot.db.record_inventory_listing_stock(
                job_id,
                in_stock=current_stock,
                sold_out=sold_out,
            )
            released = await self.bot.db.cancel_tracked_inventory_listing(
                interaction.user.id, int(job["listing_id"])
            )
            if outcome and outcome["sold_delta"]:
                stock_note = f" UEX reports **{outcome['sold_delta']}** sold since the last check; the rest was released."
            elif released:
                stock_note = " Unsold inventory was released."
            else:
                stock_note = " UEX had already reported the listing sold out, so no stock was released."
            await interaction.followup.send(
                f"Deleted UEX listing #{job['listing_id']} and cancelled job #{job_id}.{stock_note}",
                ephemeral=True,
            )
            return
        await interaction.response.send_message(
            f"Job #{job_id} is **{job['status']}** and has no cancellable public or pending post.",
            ephemeral=True,
        )

    @tasks.loop(minutes=POST_CHECK_MINUTES)
    async def process_inventory_posts(self) -> None:
        try:
            await self._post_due_jobs()
            await self._reconcile_listed_jobs()
        except Exception:
            logger.exception("Personal inventory posting cycle failed")

    @process_inventory_posts.before_loop
    async def before_process_inventory_posts(self) -> None:
        await self.bot.wait_until_ready()

    async def _post_due_jobs(self) -> None:
        for stale in await self.bot.db.flag_stale_inventory_post_jobs():
            await self._notify_user(
                int(stale["user_id"]),
                f"Inventory job #{stale['id']} for **{stale['item_name']}** was interrupted while posting. "
                "The result is unknown, so the bot stopped and will not retry. Check UEX, then resolve it with "
                f"`/inventory-confirm-sale job_id:{stale['id']}`.",
            )
        for job in await self.bot.db.list_due_inventory_post_jobs():
            if not await self.bot.db.claim_inventory_post_job(int(job["id"])):
                continue
            await self._post_one_job(job)

    async def _post_one_job(self, job: dict[str, Any]) -> None:
        secret_key = await self.bot.db.get_user_secret_key(int(job["user_id"]))
        if not secret_key:
            await self.bot.db.mark_inventory_post_failed(int(job["id"]), "UEX account is no longer linked")
            await self._notify_user(
                int(job["user_id"]), f"Inventory job #{job['id']} was not posted because your UEX account is no longer linked."
            )
            return

        try:
            sell_rows, buy_rows, average_rows = await asyncio.gather(
                self.bot.uex.get_marketplace_listings(id_item=job["id_item"], operation="sell"),
                self.bot.uex.get_marketplace_listings(id_item=job["id_item"], operation="buy"),
                self.bot.uex.get_marketplace_prices_averages(
                    id_item=job["id_item"], operation="sell", currency="UEC"
                ),
            )
            own_prices = await self.bot.db.get_inventory_completed_unit_prices(
                user_id=int(job["user_id"]),
                id_item=int(job["id_item"]), quality=int(job["quality"]), unit=str(job["unit"])
            )
            recommendation = recommend_balanced_price(
                listings=sell_rows + buy_rows,
                average_rows=average_rows,
                quality=int(job["quality"]),
                unit=str(job["unit"]),
                minimum_price=int(job["minimum_price"]),
                own_completed_unit_prices=own_prices,
                strategy=job.get("pricing_strategy", "balanced"),
            )
            payload = build_inventory_listing_payload(
                job, quantity=int(job["quantity"]), price=recommendation.price
            )
        except Exception as exc:
            # No write has happened yet, so this failure is known-safe to release.
            message = str(exc)
            await self.bot.db.mark_inventory_post_failed(int(job["id"]), message, ambiguous=False)
            await self._notify_user(
                int(job["user_id"]),
                f"Inventory job #{job['id']} for **{job['item_name']}** could not prepare fresh pricing. "
                f"Nothing was posted and its reservation was released.\n{message[:500]}",
            )
            return

        try:
            created = await self.bot.uex.post_marketplace_advertise(secret_key=secret_key, **payload)
        except UexApiError as exc:
            message = str(exc)
            lowered = message.lower()
            definitely_rejected = (
                lowered.startswith("uex api error")
                or lowered.startswith("uex auth error")
                or "quota reached" in lowered
            )
            ambiguous = not definitely_rejected
            await self.bot.db.mark_inventory_post_failed(int(job["id"]), message, ambiguous=ambiguous)
            action = (
                "UEX may have received it, so the bot stopped without retrying. Check the linked item page, then use "
                f"`/inventory-confirm-sale job_id:{job['id']}` with the actual quantity sold."
                if ambiguous
                else "Nothing will retry automatically; the reserved quantity has been released."
            )
            await self._notify_user(
                int(job["user_id"]),
                f"Inventory job #{job['id']} for **{job['item_name']}** could not be posted. {action}\n{message[:500]}",
            )
            return

        listing_id = extract_listing_id(created)
        if listing_id is None:
            await self.bot.db.mark_inventory_post_failed(
                int(job["id"]), "UEX returned no id_listing after POST", ambiguous=True
            )
            await self._notify_user(
                int(job["user_id"]),
                f"UEX did not return a listing id for inventory job #{job['id']}. The bot stopped without retrying; "
                f"check [{job['item_name']}]({marketplace_item_url(int(job['id_item']))}) manually.",
            )
            return

        listing_url = created.get("url") if isinstance(created, dict) else None
        date_expiration = _integer(created.get("date_expiration")) if isinstance(created, dict) else None
        await self.bot.db.mark_inventory_post_listed(
            int(job["id"]),
            listing_id=listing_id,
            listing_url=listing_url,
            posted_price=recommendation.price,
            date_expiration=date_expiration,
        )
        floor_note = " · manual floor applied" if recommendation.floor_applied else ""
        await self._notify_user(
            int(job["user_id"]),
            f"Posted **{job['item_name']}** to UEX: qty **{job['quantity']}** at "
            f"**{recommendation.price:,} UEC/{job['unit']}** · listing #{listing_id} · "
            f"pricing confidence {recommendation.confidence.lower()}{floor_note}.",
        )

    async def _reconcile_listed_jobs(self) -> None:
        # Fifty listing reads plus at most ten due jobs' three pricing reads stays below
        # UEX's 120-request/minute ceiling even before normal endpoint caching helps. Reads
        # are gathered concurrently in bounded batches; the per-job writes/notifications
        # below stay sequential since they're cheap local DB calls, not network round trips.
        jobs = await self.bot.db.list_tracked_inventory_posts(limit=MAX_TRACKED_POSTS_PER_CYCLE)
        if not jobs:
            return

        async def _fetch_negotiations(user_id: int) -> tuple[int, list[dict[str, Any]]]:
            secret = await self.bot.db.get_user_secret_key(user_id)
            if not secret:
                return user_id, []
            try:
                return user_id, await self.bot.uex.get_marketplace_negotiations(secret_key=secret)
            except UexApiError:
                return user_id, []

        negotiation_results = await asyncio.gather(
            *(_fetch_negotiations(user_id) for user_id in {int(job["user_id"]) for job in jobs})
        )
        negotiations_by_user: dict[int, dict[int, dict[str, Any]]] = {}
        for user_id, rows in negotiation_results:
            best_by_listing: dict[int, dict[str, Any]] = {}
            for row in rows:
                listing_id = _integer(row.get("id_listing"))
                if listing_id is None:
                    continue
                current = best_by_listing.get(listing_id)
                candidate_key = (
                    1 if _integer(row.get("date_closed")) else 0,
                    _integer(row.get("date_modified")) or 0,
                )
                current_key = (
                    1 if current and _integer(current.get("date_closed")) else 0,
                    (_integer(current.get("date_modified")) or 0) if current else 0,
                )
                if current is None or candidate_key > current_key:
                    best_by_listing[listing_id] = row
            negotiations_by_user[user_id] = best_by_listing

        async def _fetch_listing(listing_id: int) -> tuple[int, list[dict[str, Any]] | None]:
            try:
                rows = await self.bot.uex.get_marketplace_listings(id=listing_id, use_cache=False)
                return listing_id, rows
            except UexApiError:
                return listing_id, None

        listing_ids = sorted({int(job["listing_id"]) for job in jobs if job["status"] != "sold"})
        listings_by_id: dict[int, list[dict[str, Any]] | None] = {}
        for start in range(0, len(listing_ids), RECONCILE_FETCH_BATCH_SIZE):
            batch = listing_ids[start : start + RECONCILE_FETCH_BATCH_SIZE]
            for listing_id, rows in await asyncio.gather(*(_fetch_listing(lid) for lid in batch)):
                listings_by_id[listing_id] = rows

        timing_rows: list[dict[str, Any]] | None = None

        for job in jobs:
            job_id = int(job["id"])
            listing_id = int(job["listing_id"])
            negotiation = negotiations_by_user.get(int(job["user_id"]), {}).get(listing_id)
            deal_value = parse_uex_number(negotiation.get("deal_value")) if negotiation else None
            date_closed = _integer(negotiation.get("date_closed")) if negotiation else None
            if negotiation and deal_value and date_closed:
                await self.bot.db.record_inventory_deal_value(
                    listing_id,
                    deal_value=float(deal_value),
                    currency=negotiation.get("deal_value_currency"),
                    date_closed=date_closed,
                )
            if job["status"] == "sold":
                continue
            rows = listings_by_id.get(listing_id)
            if rows is None:
                continue
            if not rows:
                # Fresh listings may be awaiting UEX approval and therefore not visible yet.
                # Give that path a grace period; after that, disappearance is ambiguous (sold,
                # deleted, or expired) and must never trigger a blind duplicate relist.
                updated = _parse_db_time(job.get("updated_at"))
                if updated and (datetime.now(timezone.utc) - updated).total_seconds() < LISTING_APPROVAL_GRACE_SECONDS:
                    continue
                await self.bot.db.mark_inventory_post_needs_confirmation(
                    job_id, "Listing disappeared from UEX without a final remaining-stock value"
                )
                await self._notify_user(
                    int(job["user_id"]),
                    f"UEX no longer shows listing #{listing_id} for **{job['item_name']}**, but did not prove how many sold. "
                    f"Automatic relisting stopped. Use `/inventory-confirm-sale job_id:{job_id}` after checking UEX.",
                )
                continue

            listing = rows[0]
            stock = _integer(listing.get("in_stock"))
            outcome = None
            if stock is not None:
                outcome = await self.bot.db.record_inventory_listing_stock(
                    job_id, in_stock=stock, sold_out=_flag(listing.get("is_sold_out"))
                )
                if outcome and outcome["sold_delta"]:
                    await self._notify_user(
                        int(job["user_id"]),
                        f"UEX reports **{outcome['sold_delta']}** sold from listing #{listing_id} "
                        f"for **{job['item_name']}**; **{outcome['remaining']}** remain listed.",
                    )
                if outcome and outcome["status"] == "sold":
                    continue

            expiration = _integer(listing.get("date_expiration")) or _integer(job.get("date_expiration"))
            if expiration and time.time() >= expiration and stock is not None and stock > 0:
                if timing_rows is None:
                    timing_rows = await self.bot.db.get_marketplace_timing_rows()
                window = recommend_posting_window(timing_rows, id_item=int(job["id_item"]))
                if not window:
                    continue
                new_id = await self.bot.db.expire_and_relist_inventory_post(
                    job_id, next_posting_time(window)
                )
                if new_id:
                    await self._notify_user(
                        int(job["user_id"]),
                        f"Listing #{listing_id} expired with **{stock}** explicitly remaining. A fresh-price relist "
                        f"was safely scheduled as job #{new_id} for the next {window.label} ET window.",
                    )

    async def _notify_user(self, user_id: int, message: str) -> None:
        try:
            user = self.bot.get_user(user_id) or await self.bot.fetch_user(user_id)
            await user.send(message)
        except (discord.HTTPException, AttributeError):
            logger.warning("Could not DM inventory update to user %s", user_id)


def _parse_db_time(raw: Any) -> datetime | None:
    if not raw:
        return None
    try:
        value = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except ValueError:
        return None
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(PersonalInventory(bot))
