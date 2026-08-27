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
    PriceRecommendation,
    _flag,
    _integer,
    build_inventory_listing_payload,
    extract_listing_id,
    quality_label,
    recommend_balanced_price,
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

# No-interest discount cycle: a listing that's been up this long with unsold stock and no
# open negotiation gets relisted 5% below its current price, compounding each cycle, down
# to (never below) its minimum_price. UEX's own listings run 60 days, so this is the bot
# proactively deleting and reposting well before natural expiration - not waiting on UEX.
RELIST_DISCOUNT_INTERVAL_HOURS = 48
RELIST_DISCOUNT_RATE = 0.95

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

        scheduled_for = datetime.now(timezone.utc)
        specs: list[dict[str, Any]] = []
        embed = discord.Embed(
            title="Authorize automatic UEX posting",
            description=(
                "Each stack posts within the next few minutes once you confirm below (UEX staff approval after "
                "that is outside the bot's control). The price is recalculated right before posting and can "
                "never go below your manual minimum."
            ),
            color=discord.Color.orange(),
        )
        for row in selected:
            available = int(row["quantity"]) - int(row["reserved_quantity"])
            if available <= 0:
                continue
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
            embed.add_field(
                name=f"#{row['id']} · {row['item_name']}"[:256],
                value=(
                    f"Qty **{available}** · preview **{price.price:,} UEC/{row['unit']}** "
                    f"(minimum {int(row['minimum_price']):,})"
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


class CustomPriceModal(discord.ui.Modal, title="Enter a custom price"):
    price_input: discord.ui.TextInput = discord.ui.TextInput(
        label="Price per unit (UEC)",
        placeholder="e.g. 1750000",
        style=discord.TextStyle.short,
        max_length=15,
    )

    def __init__(self, view: "PostNowView") -> None:
        super().__init__()
        self.view = view

    async def on_submit(self, interaction: discord.Interaction) -> None:
        raw = self.price_input.value.strip().replace(",", "")
        try:
            price = int(raw)
        except ValueError:
            await interaction.response.send_message("That's not a whole number - try again.", ephemeral=True)
            return
        if price <= 0:
            await interaction.response.send_message("Price must be a positive whole number.", ephemeral=True)
            return
        minimum_price = int(self.view.entry["minimum_price"])
        if price < minimum_price:
            await interaction.response.send_message(
                f"That's below your minimum of **{minimum_price:,}** UEC - raise the price, or lower your "
                "minimum first with `/inventory-set-minimum`.",
                ephemeral=True,
            )
            return
        self.view.pricing_strategy = "custom"
        self.view.custom_price = price
        for option in self.view.choose_pricing_strategy.options:
            option.default = option.value == "custom"
        await interaction.response.edit_message(
            content=f"Pricing strategy: **custom — {price:,} UEC/unit**.", view=self.view,
        )


class PostNowView(discord.ui.View):
    def __init__(self, cog: "PersonalInventory", author_id: int, entry: dict[str, Any], quantity: int) -> None:
        super().__init__(timeout=180)
        self.cog = cog
        self.author_id = author_id
        self.entry = entry
        self.quantity = quantity
        self.pricing_strategy = "balanced"
        self.custom_price: int | None = None
        self.resolved = False

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.author_id:
            await interaction.response.send_message("Only the inventory owner can confirm this.", ephemeral=True)
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
            discord.SelectOption(label="Enter a custom price...", value="custom"),
        ],
    )
    async def choose_pricing_strategy(self, interaction: discord.Interaction, select: discord.ui.Select) -> None:
        chosen = select.values[0]
        if chosen == "custom":
            await interaction.response.send_modal(CustomPriceModal(self))
            return
        self.pricing_strategy = chosen
        self.custom_price = None
        for option in select.options:
            option.default = option.value == self.pricing_strategy
        await interaction.response.edit_message(
            content=f"Pricing strategy: **{PRICING_STRATEGY_LABELS[self.pricing_strategy]}** shown above.",
            view=self,
        )

    @discord.ui.button(label="Post now", style=discord.ButtonStyle.green)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        self.disable()
        await interaction.response.edit_message(view=self)
        spec: dict[str, Any] = {
            "inventory_id": int(self.entry["id"]),
            "quantity": self.quantity,
            "scheduled_for": datetime.now(timezone.utc),
            "auto_relist": True,
            "pricing_strategy": self.pricing_strategy,
        }
        if self.pricing_strategy == "custom":
            spec["custom_price"] = self.custom_price
        try:
            job_ids = await self.cog.bot.db.create_inventory_post_jobs(self.author_id, [spec])
        except ValueError as exc:
            await interaction.followup.send(f"Nothing was posted: {exc}", ephemeral=True)
            return
        job_id = job_ids[0]

        if not await self.cog.bot.db.claim_inventory_post_job(job_id):
            await interaction.followup.send(
                f"Job #{job_id} was created but something else already claimed it - check `/inventory-cancel-post`.",
                ephemeral=True,
            )
            return

        job = await self.cog.bot.db.get_inventory_post_job(self.author_id, job_id)
        result = await self.cog._post_one_job(job, notify=False)
        await interaction.followup.send(_format_post_now_result(job_id, job, result), ephemeral=True)

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        self.disable()
        await interaction.response.edit_message(view=self)
        await interaction.followup.send("Cancelled - nothing was posted.", ephemeral=True)


class LowerFloorModal(discord.ui.Modal, title="Set a new minimum price"):
    price_input: discord.ui.TextInput = discord.ui.TextInput(
        label="New minimum price per unit (UEC)",
        placeholder="e.g. 800000",
        style=discord.TextStyle.short,
        max_length=15,
    )

    def __init__(self, view: "FloorReachedView") -> None:
        super().__init__()
        self.view = view

    async def on_submit(self, interaction: discord.Interaction) -> None:
        raw = self.price_input.value.strip().replace(",", "")
        try:
            price = int(raw)
        except ValueError:
            await interaction.response.send_message("That's not a whole number - try again.", ephemeral=True)
            return
        if price <= 0:
            await interaction.response.send_message("Minimum price must be a positive whole number.", ephemeral=True)
            return
        ok = await self.view.cog.bot.db.resume_auto_relist_with_new_floor(
            self.view.job_id, self.view.user_id, price
        )
        self.view.disable()
        content = (
            f"New minimum set to **{price:,}** UEC/unit. Automatic discounting has resumed."
            if ok
            else "Couldn't update that job - it may no longer be listed."
        )
        await interaction.response.edit_message(content=content, embed=None, view=self.view)


class FloorReachedView(discord.ui.View):
    """Sent as a plain DM (not an interaction followup), so it can arrive whenever the 48h
    discount cycle actually hits the floor - possibly hours after any command was run."""

    def __init__(self, cog: "PersonalInventory", job: dict[str, Any]) -> None:
        super().__init__(timeout=None)
        self.cog = cog
        self.job_id = int(job["id"])
        self.user_id = int(job["user_id"])

    def disable(self) -> None:
        for child in self.children:
            child.disabled = True

    @discord.ui.button(label="Keep at floor", style=discord.ButtonStyle.secondary)
    async def keep(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        self.disable()
        await interaction.response.edit_message(
            content="Left as-is at the floor price. Automatic discounting stays off for this listing.",
            embed=None, view=self,
        )

    @discord.ui.button(label="Lower floor & resume", style=discord.ButtonStyle.primary)
    async def lower_floor(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await interaction.response.send_modal(LowerFloorModal(self))

    @discord.ui.button(label="Cancel listing", style=discord.ButtonStyle.danger)
    async def cancel_listing(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        self.disable()
        await interaction.response.edit_message(view=self)
        secret_key = await self.cog.bot.db.get_user_secret_key(self.user_id)
        if not secret_key:
            await interaction.followup.send(
                "Relink your UEX account before cancelling the public listing.", ephemeral=True
            )
            return
        job = await self.cog.bot.db.get_inventory_post_job(self.user_id, self.job_id)
        if not job or job["status"] != "listed":
            await interaction.followup.send("This job is no longer active.", ephemeral=True)
            return
        _, message = await self.cog._cancel_listed_job(job, secret_key=secret_key)
        await interaction.followup.send(message, ephemeral=True)


def _format_post_now_result(job_id: int, job: dict[str, Any], result: dict[str, Any]) -> str:
    if result["success"]:
        floor_note = " · raised to your minimum" if result["floor_applied"] else ""
        confidence_note = "" if result["confidence"] == "Custom" else f" · pricing confidence {result['confidence'].lower()}"
        listing_note = f" · [listing #{result['listing_id']}]({result['listing_url']})" if result.get("listing_url") else f" · listing #{result['listing_id']}"
        return (
            f"Posted **{job['item_name']}** to UEX: qty **{job['quantity']}** at **{result['price']:,} UEC/{job['unit']}**"
            f"{confidence_note}{floor_note}{listing_note}."
        )
    reason = result["reason"]
    if reason == "not_linked":
        return f"Not posted: {result['message']}"
    if reason == "pricing_failed":
        return f"Not posted - couldn't prepare fresh pricing, nothing was reserved or sent to UEX.\n{result['message'][:500]}"
    if reason == "post_failed" and result.get("ambiguous"):
        return (
            f"UEX may have received job #{job_id}, so it was NOT retried automatically. Check the linked item page, "
            f"then use `/inventory-confirm-sale job_id:{job_id}` with the actual quantity sold if it went through.\n"
            f"{result['message'][:500]}"
        )
    if reason == "post_failed":
        return f"UEX rejected the post; nothing will retry automatically and the reservation was released.\n{result['message'][:500]}"
    return f"Job #{job_id} was posted, but UEX didn't confirm a listing id - check manually.\n{result['message'][:500]}"


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
        jobs_by_inventory: dict[int, list[dict[str, Any]]] = {}
        for job in await self.bot.db.list_active_inventory_jobs(interaction.user.id):
            jobs_by_inventory.setdefault(int(job["inventory_id"]), []).append(job)
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
            for job in jobs_by_inventory.get(int(row["id"]), []):
                value += f"\n{_format_job_status(job)}"
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

    @app_commands.command(
        name="inventory-post-now",
        description="Skip the scheduled window and post one inventory stack for sale on UEX right now.",
    )
    @app_commands.describe(inventory_id="The inventory stack number, shown by /inventory")
    async def inventory_post_now(self, interaction: discord.Interaction, inventory_id: int) -> None:
        if not await self.bot.db.has_linked_uex_account(interaction.user.id):
            await interaction.response.send_message(
                "Link your own UEX account with `/link-uex-account` before posting public listings.",
                ephemeral=True,
            )
            return
        entry = await self.bot.db.get_inventory_item(interaction.user.id, inventory_id)
        if not entry:
            await interaction.response.send_message(f"Inventory entry #{inventory_id} was not found.", ephemeral=True)
            return
        available = int(entry["quantity"]) - int(entry["reserved_quantity"])
        if available <= 0:
            await interaction.response.send_message(
                f"#{inventory_id} has no unreserved quantity available to post.", ephemeral=True
            )
            return
        if not entry.get("minimum_price"):
            await interaction.response.send_message(
                f"Set a manual minimum price first for #{inventory_id}. Use `/inventory-set-minimum`.", ephemeral=True
            )
            return

        await interaction.response.defer(ephemeral=True)
        # Live, same-source preview: this calls the exact function _post_one_job will call
        # moments later (not a cached/offline estimate), so what you see here is what
        # actually posts unless the market genuinely moves in the next few seconds.
        try:
            recommendation = await self._fetch_live_price(
                id_item=int(entry["id_item"]),
                quality=int(entry["quality"]),
                unit=str(entry["unit"]),
                minimum_price=int(entry["minimum_price"]),
                user_id=interaction.user.id,
            )
        except UexApiError as exc:
            await interaction.followup.send(
                f"UEX could not price #{inventory_id} right now: {exc}\nNothing was posted - try again shortly.",
                ephemeral=True,
            )
            return
        embed = discord.Embed(
            title="Post now?",
            description=(
                f"**#{inventory_id} · {entry['item_name']}** — qty **{available}**\n"
                f"Recommended price: **{recommendation.price:,} UEC/{entry['unit']}** "
                f"(confidence {recommendation.confidence.lower()} · minimum {int(entry['minimum_price']):,})\n\n"
                "This posts a REAL public UEX listing immediately, not a scheduled one. Pick a pricing strategy "
                "below - at/below/above this recommendation, or enter your own exact price - then confirm."
            ),
            color=discord.Color.orange(),
        )
        await interaction.followup.send(embed=embed, view=PostNowView(self, interaction.user.id, entry, available), ephemeral=True)

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
            try:
                new_jobs = await self.bot.db.create_inventory_post_jobs(
                    interaction.user.id,
                    [
                        {
                            "inventory_id": result["inventory_id"],
                            "quantity": result["unsold"],
                            "scheduled_for": datetime.now(timezone.utc),
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
            _, message = await self._cancel_listed_job(job, secret_key=secret_key)
            await interaction.followup.send(message, ephemeral=True)
            return
        await interaction.response.send_message(
            f"Job #{job_id} is **{job['status']}** and has no cancellable public or pending post.",
            ephemeral=True,
        )

    @app_commands.command(
        name="inventory-resolve-floor",
        description="Resend a working prompt for a listing paused at its floor price with no interest.",
    )
    @app_commands.describe(job_id="Posting job number shown in the original floor-reached DM")
    async def inventory_resolve_floor(self, interaction: discord.Interaction, job_id: int) -> None:
        job = await self.bot.db.get_inventory_post_job(interaction.user.id, job_id)
        if not job:
            await interaction.response.send_message(f"Inventory job #{job_id} was not found.", ephemeral=True)
            return
        if job["status"] != "listed" or job.get("auto_relist"):
            await interaction.response.send_message(
                f"Job #{job_id} isn't currently paused waiting on a decision - nothing to resolve.",
                ephemeral=True,
            )
            return
        embed = discord.Embed(
            title="No interest at your floor price",
            description=(
                f"**{job['item_name']}** (job #{job_id}) is still paused at your minimum of "
                f"**{int(job['minimum_price']):,} UEC/unit** with no negotiation. Pick one below."
            ),
            color=discord.Color.orange(),
        )
        await interaction.response.send_message(embed=embed, view=FloorReachedView(self, job), ephemeral=True)

    async def _cancel_listed_job(self, job: dict[str, Any], *, secret_key: str) -> tuple[bool, str]:
        """Delete a listed job's public UEX listing and release local state. Shared by
        /inventory-cancel-post and the floor-reached DM prompt's Cancel button."""
        job_id = int(job["id"])
        try:
            listing_rows = await self.bot.uex.get_marketplace_listings(id=int(job["listing_id"]), use_cache=False)
            if not listing_rows:
                await self.bot.db.mark_inventory_post_needs_confirmation(
                    job_id, "Cancellation requested, but UEX no longer exposed final stock"
                )
                return False, (
                    "UEX no longer exposes that listing, so the bot cannot safely decide what remains. "
                    f"Nothing was relisted or released; resolve job #{job_id} with `/inventory-confirm-sale`."
                )
            current_stock = _integer(listing_rows[0].get("in_stock"))
            if current_stock is None:
                return False, (
                    "UEX returned the listing without a remaining-stock value. Cancellation stopped so your "
                    "local inventory is not guessed; try again later or use `/inventory-confirm-sale` if it disappears."
                )
            sold_out = _flag(listing_rows[0].get("is_sold_out"))
            # Delete on UEX before touching any local state: if this raises, nothing below
            # has run yet, so there's nothing to leave inconsistent or roll back.
            await self.bot.uex.delete_marketplace_listing(
                listing_id=int(job["listing_id"]), secret_key=secret_key
            )
        except UexApiError as exc:
            return False, f"UEX could not confirm deletion of listing #{job['listing_id']}: {exc}"

        outcome = await self.bot.db.record_inventory_listing_stock(job_id, in_stock=current_stock, sold_out=sold_out)
        released = await self.bot.db.cancel_tracked_inventory_listing(int(job["user_id"]), int(job["listing_id"]))
        if outcome and outcome["sold_delta"]:
            stock_note = f" UEX reports **{outcome['sold_delta']}** sold since the last check; the rest was released."
        elif released:
            stock_note = " Unsold inventory was released."
        else:
            stock_note = " UEX had already reported the listing sold out, so no stock was released."
        return True, f"Deleted UEX listing #{job['listing_id']} and cancelled job #{job_id}.{stock_note}"

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

    async def _fetch_live_price(
        self, *, id_item: int, quality: int, unit: str, minimum_price: int, user_id: int, strategy: str = "balanced",
    ) -> PriceRecommendation:
        """The one place that computes a live, evidence-based price recommendation - used
        both for /inventory-post-now's preview and for the actual post moments later, so
        the two can never silently diverge the way they would if each built its own fetch."""
        sell_rows, buy_rows, average_rows = await asyncio.gather(
            self.bot.uex.get_marketplace_listings(id_item=id_item, operation="sell"),
            self.bot.uex.get_marketplace_listings(id_item=id_item, operation="buy"),
            self.bot.uex.get_marketplace_prices_averages(id_item=id_item, operation="sell", currency="UEC"),
        )
        own_prices = await self.bot.db.get_inventory_completed_unit_prices(
            user_id=user_id, id_item=id_item, quality=quality, unit=unit,
        )
        return recommend_balanced_price(
            listings=sell_rows + buy_rows,
            average_rows=average_rows,
            quality=quality,
            unit=unit,
            minimum_price=minimum_price,
            own_completed_unit_prices=own_prices,
            strategy=strategy,
        )

    async def _post_one_job(self, job: dict[str, Any], *, notify: bool = True) -> dict[str, Any]:
        """Actually POST one due job to UEX. Returns a result dict describing the outcome
        (used directly by /inventory-post-now); notify=True (the background loop's default)
        also DMs the user the same information, which a synchronous caller with its own
        interaction response open should suppress to avoid saying the same thing twice."""
        secret_key = await self.bot.db.get_user_secret_key(int(job["user_id"]))
        if not secret_key:
            await self.bot.db.mark_inventory_post_failed(int(job["id"]), "UEX account is no longer linked")
            message = f"Inventory job #{job['id']} was not posted because your UEX account is no longer linked."
            if notify:
                await self._notify_user(int(job["user_id"]), message)
            return {"success": False, "reason": "not_linked", "message": message}

        try:
            if job.get("pricing_strategy") == "custom":
                # A deliberately typed price, not an algorithmic one - the floor is still
                # enforced (create_inventory_post_jobs already rejected a too-low value at
                # authorization time; this is the same guarantee re-applied defensively in
                # case of any drift between then and now), but there's no "evidence" to
                # report and confidence doesn't apply the way it does for a computed price.
                minimum_price = int(job["minimum_price"])
                custom_price = int(job["custom_price"])
                price = max(custom_price, minimum_price)
                recommendation = PriceRecommendation(
                    price=price, confidence="Custom", evidence=(), floor_applied=price > custom_price,
                )
            else:
                recommendation = await self._fetch_live_price(
                    id_item=int(job["id_item"]),
                    quality=int(job["quality"]),
                    unit=str(job["unit"]),
                    minimum_price=int(job["minimum_price"]),
                    user_id=int(job["user_id"]),
                    strategy=job.get("pricing_strategy", "balanced"),
                )
            payload = build_inventory_listing_payload(
                job, quantity=int(job["quantity"]), price=recommendation.price
            )
        except Exception as exc:
            # No write has happened yet, so this failure is known-safe to release.
            message = str(exc)
            await self.bot.db.mark_inventory_post_failed(int(job["id"]), message, ambiguous=False)
            full_message = (
                f"Inventory job #{job['id']} for **{job['item_name']}** could not prepare fresh pricing. "
                f"Nothing was posted and its reservation was released.\n{message[:500]}"
            )
            if notify:
                await self._notify_user(int(job["user_id"]), full_message)
            return {"success": False, "reason": "pricing_failed", "message": message}

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
            full_message = f"Inventory job #{job['id']} for **{job['item_name']}** could not be posted. {action}\n{message[:500]}"
            if notify:
                await self._notify_user(int(job["user_id"]), full_message)
            return {"success": False, "reason": "post_failed", "ambiguous": ambiguous, "message": message}

        listing_id = extract_listing_id(created)
        if listing_id is None:
            await self.bot.db.mark_inventory_post_failed(
                int(job["id"]), "UEX returned no id_listing after POST", ambiguous=True
            )
            full_message = (
                f"UEX did not return a listing id for inventory job #{job['id']}. The bot stopped without retrying; "
                f"check [{job['item_name']}]({marketplace_item_url(int(job['id_item']))}) manually."
            )
            if notify:
                await self._notify_user(int(job["user_id"]), full_message)
            return {"success": False, "reason": "no_listing_id", "message": "UEX returned no id_listing after POST"}

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
        if notify:
            await self._notify_user(
                int(job["user_id"]),
                f"Posted **{job['item_name']}** to UEX: qty **{job['quantity']}** at "
                f"**{recommendation.price:,} UEC/{job['unit']}** · listing #{listing_id} · "
                f"pricing confidence {recommendation.confidence.lower()}{floor_note}.",
            )
        return {
            "success": True,
            "listing_id": listing_id,
            "listing_url": listing_url,
            "price": recommendation.price,
            "confidence": recommendation.confidence,
            "floor_applied": recommendation.floor_applied,
        }

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

            if job.get("auto_relist") and stock is not None and stock > 0:
                posted_at = _parse_db_time(job.get("created_at"))
                price_age_hours = (
                    (datetime.now(timezone.utc) - posted_at).total_seconds() / 3600 if posted_at else 0
                )
                if price_age_hours >= RELIST_DISCOUNT_INTERVAL_HOURS:
                    has_open_negotiation = negotiation is not None and not _integer(negotiation.get("date_closed"))
                    if has_open_negotiation:
                        await self.bot.db.disable_auto_relist(job_id)
                        await self._notify_user(
                            int(job["user_id"]),
                            f"A negotiation has opened on listing #{listing_id} for **{job['item_name']}** - "
                            "automatic discounting and relisting has paused so it isn't disrupted.",
                        )
                        continue
                    current_price = int(job.get("posted_price") or job["minimum_price"])
                    minimum_price = int(job["minimum_price"])
                    if current_price <= minimum_price:
                        await self.bot.db.disable_auto_relist(job_id)
                        await self._send_floor_reached_prompt(job)
                        continue
                    secret_key = await self.bot.db.get_user_secret_key(int(job["user_id"]))
                    if not secret_key:
                        continue  # can't safely delete the old listing; retry next cycle
                    # Unlike the natural-60-day-expiration path below, this listing is still
                    # genuinely live on UEX - it must be explicitly deleted, not just replaced
                    # locally, or the item ends up double-listed at two different prices.
                    try:
                        await self.bot.uex.delete_marketplace_listing(
                            listing_id=listing_id, secret_key=secret_key
                        )
                    except UexApiError as exc:
                        logger.warning(
                            "Could not delete listing %s for the 48h no-interest relist: %s", listing_id, exc
                        )
                        continue
                    next_price = max(round(current_price * RELIST_DISCOUNT_RATE), minimum_price)
                    new_id = await self.bot.db.expire_and_relist_inventory_post(
                        job_id, datetime.now(timezone.utc), price_override=next_price
                    )
                    if new_id:
                        new_job = await self.bot.db.get_inventory_post_job(int(job["user_id"]), new_id)
                        if new_job and await self.bot.db.claim_inventory_post_job(new_id):
                            await self._post_one_job(new_job, notify=False)
                        await self._notify_user(
                            int(job["user_id"]),
                            f"No interest yet on **{job['item_name']}** after {RELIST_DISCOUNT_INTERVAL_HOURS}h - "
                            f"relisted as job #{new_id} at **{next_price:,}** UEC/unit (was {current_price:,}).",
                        )
                    continue

            expiration = _integer(listing.get("date_expiration")) or _integer(job.get("date_expiration"))
            if expiration and time.time() >= expiration and stock is not None and stock > 0:
                new_id = await self.bot.db.expire_and_relist_inventory_post(
                    job_id, datetime.now(timezone.utc)
                )
                if new_id:
                    await self._notify_user(
                        int(job["user_id"]),
                        f"Listing #{listing_id} expired with **{stock}** explicitly remaining. A fresh-price relist "
                        f"was safely scheduled as job #{new_id}.",
                    )

    async def _notify_user(self, user_id: int, message: str) -> None:
        try:
            user = self.bot.get_user(user_id) or await self.bot.fetch_user(user_id)
            await user.send(message)
        except (discord.HTTPException, AttributeError):
            logger.warning("Could not DM inventory update to user %s", user_id)

    async def _send_floor_reached_prompt(self, job: dict[str, Any]) -> None:
        user_id = int(job["user_id"])
        try:
            user = self.bot.get_user(user_id) or await self.bot.fetch_user(user_id)
        except (discord.HTTPException, AttributeError):
            logger.warning("Could not DM floor-reached prompt to user %s", user_id)
            return
        embed = discord.Embed(
            title="No interest at your floor price",
            description=(
                f"**{job['item_name']}** (job #{job['id']}) has been relisted down to your minimum of "
                f"**{int(job['minimum_price']):,} UEC/unit** with no negotiation yet. Automatic discounting "
                "has stopped - pick one below.\n\nIf these buttons ever stop responding (e.g. after a bot "
                f"restart), run `/inventory-resolve-floor job_id:{job['id']}` for a fresh working prompt."
            ),
            color=discord.Color.orange(),
        )
        try:
            await user.send(embed=embed, view=FloorReachedView(self, job))
        except discord.HTTPException:
            logger.warning("Could not DM floor-reached prompt to user %s", user_id)


def _parse_db_time(raw: Any) -> datetime | None:
    if not raw:
        return None
    try:
        value = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except ValueError:
        return None
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def _format_job_status(job: dict[str, Any]) -> str:
    """One line describing what a posting job is doing right now and when its next
    automatic action happens - shown on /inventory so that's checkable anytime, not just
    something you find out about after the fact via DM."""
    status = job.get("status")
    if status == "pending":
        scheduled = _parse_db_time(job.get("scheduled_for"))
        if scheduled:
            local_time = scheduled.astimezone(ZoneInfo(DEFAULT_MARKETPLACE_TIMEZONE))
            return f"Scheduled: posts {local_time.strftime('%a %b %d, %I:%M %p')} ET"
        return "Scheduled: posting time unknown"
    if status == "posting":
        return "Posting to UEX now..."
    if status == "needs_confirmation":
        return "Needs your input - see `/inventory-confirm-sale`"
    if status == "listed":
        if not job.get("auto_relist"):
            return "Live, paused - see your DMs or `/inventory-resolve-floor`"
        created = _parse_db_time(job.get("created_at"))
        if created:
            remaining_hours = RELIST_DISCOUNT_INTERVAL_HOURS - (
                (datetime.now(timezone.utc) - created).total_seconds() / 3600
            )
            if remaining_hours > 0:
                return f"Live · reprices in ~{remaining_hours:.0f}h if no negotiation by then"
            return "Live · due for a reprice check (next automatic cycle)"
        return "Live"
    return f"Status: {status}"


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(PersonalInventory(bot))
