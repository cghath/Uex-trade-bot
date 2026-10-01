"""Ship Parts Finder (/ship-parts-finder, still being refined): for one ship, browse its
real component slots (weapons, gun mounts, power plant, coolers, shields, quantum drive,
missile racks, radar - see bot/uex/ship_parts.py) and lock in a specific part per slot
into a private, persistent shopping list - same spirit as the blueprint shopping list
(bot/cogs/blueprint_planner.py), whose ShoppingService/ShoppingView pattern this reuses
directly rather than reinventing.

Loadout data (which slots exist, what size/type fits) comes from the Star Citizen Wiki API
(api.star-citizen.wiki) - UEX has no equivalent (its own id_vehicle FK on /items is
cosmetics-only, confirmed empirically against live catalog data). Candidate parts for a
slot are found in UEX's own item catalog by category and filtered to ones UEX reports a
real, current shop listing for; whether each one fits the slot is decided by the wiki's
size, since UEX's catalog size disagrees with it in almost every category. Price and shop come from UEX's own
/items_prices_all rows, not the copy embedded in the wiki's item detail, which was
missing for many parts UEX really lists. Stats come from the wiki's /items/{uuid} detail;
a part the wiki has no detail for is still shown, just without stats.

Each part is shown at its cheapest shop (with that shop's distance from the player's
location), and the list is ranked by the category's key stat - see candidates_for_port.
Every fitting part is kept and paged, never cut to a display limit (the repo's "filter
before truncating" lesson). Display labels live in bot/uex/ship_part_display.py.

/ship-loadout (and the browser's "Recommend a loadout" button) is built on the same
candidates: one recommended part per slot for a profile - Balanced, Stealth, Tank or Budget
- or "keep stock" where the stock part is already the best pick. Which part to pick is pure
logic in bot/uex/ship_loadout.py; this file loads the slots, stock parts and candidates and
shows the result (LoadoutView).

An outside audit before merge found four real P2s, all fixed: (1) selecting a category ran
the full catalog/price/wiki lookup before acknowledging the interaction, risking Discord's
3s timeout on a cold cache - PartsBrowserView now defers first; (2) a category with more
than one physical port (e.g. two independently-sized turrets) silently used only the first
one, both in the picker and in the DB's primary key - fixed with a slot-selection step and
a port_name column added to ship_parts_shopping_entries' key; (3) the required `location`
input was resolved and stored but never actually used - _attach_distances now computes and
sorts by real distance to each candidate's cheapest listing; (4) the selected part wasn't
visibly marked before locking it in - now shown with a checkmark in the comparison list.
"""
from __future__ import annotations

import asyncio
import io
import logging
import time
from datetime import datetime, timezone

import discord
from discord import app_commands
from discord.ext import commands, tasks

from bot.autocomplete import gather_within
from bot.cogs.prices import terminal_name_autocomplete
from bot.cogs.ships import ship_name_autocomplete
from bot.discord_ui import BotView
from bot.uex.client import cache_interval_text
from bot.uex.exceptions import UexApiError, describe_uex_api_error
from bot.uex.ship_part_display import (
    LIST_BUDGET_CHARS,
    format_part_page,
    format_port_label,
    list_price_text,
    list_shared,
    paginate_parts,
    ranked_by_label,
    ranking_stat,
    shop_text,
)
from bot.uex.ship_loadout import (
    DEFAULT_PROFILE,
    NO_STATS,
    PROFILE_BLURBS,
    PROFILES,
    LoadoutSlot,
    SlotGroup,
    SlotPick,
    group_slots,
    gun_entry_port_name,
    is_gun_mount,
    loadout_gun_ports,
    paginate_lines,
    pick_for_slot,
    pick_line,
    purchases,
    resource_warnings,
    slot_category,
    stock_uuids_by_port,
    total_line,
)
from bot.uex.ship_parts import (
    GUNS_CATEGORY,
    MOUNTS_CATEGORY,
    ShipPort,
    candidate_items_for_port,
    category_label,
    cheapest_listing_by_item,
    child_gun_ports,
    group_ports_by_category,
    parse_ports,
    part_fits_port,
    pick_fitting_variant,
    tags_allow,
)
from bot.uex.ships import resolve_ship
from bot.wiki_api import WikiApiClient, WikiApiError, WikiUnavailableError

logger = logging.getLogger("uexbot.ship_parts_finder")
NO_MENTIONS = discord.AllowedMentions.none()

# Each ship's slots are re-read from the wiki once they're this old. The refresh itself
# checks hourly, so a restart only picks up ships that are due (audit REL-11).
REFERENCE_REFRESH_HOURS = 24
REFERENCE_CHECK_HOURS = 1
# Pause between ships in that crawl. It used to fire ~380 wiki requests in 15 seconds.
WIKI_CRAWL_SPACING_SECONDS = 1.0
# Ships in a row the wiki doesn't answer for before the crawl stops until the next check.
WIKI_CRAWL_MAX_OUTAGES = 5
# Component hardware prices move far less often than commodity market prices (deliberate
# design decision, not a placeholder) - see module docstring.
DETAIL_CACHE_SECONDS = 24 * 3600
# Every sold part in a browsed category gets its detail loaded now (to rank it), so the
# cache has to hold them all: about 370 sold parts across the 8 categories, plus
# name-lookup misses and variant lists. Entries are slimmed first (_slim_detail).
DETAIL_CACHE_MAX = 1000
# How long a lookup the wiki didn't answer (an outage, not a real miss) is skipped before
# being retried - long enough that browsing during an outage doesn't wait on every part
# again, short enough that the finder recovers minutes after the wiki does.
WIKI_OUTAGE_RETRY_SECONDS = 5 * 60
# How long one browse may spend loading a slot's parts. A hanging wiki makes each request
# take ~96s (3 attempts x 30s), a detail miss makes two, and batches run one after
# another, so a cold category could outlast Discord's 15-minute interaction window and
# never show at all (audit REL-6). Past this, no new batch starts: lookups still running
# finish in the background and fill the cache, and the parts left unanswered are counted
# in the browser's "wiki didn't respond" note. A healthy cold load measured 0.2-4.7s.
LOAD_TIME_BUDGET_SECONDS = 45.0
# The browsing view stops listening after this long without a click. Its ↻ Refresh button
# keeps working after that, and after a restart, and rebuilds the browser in place.
BROWSER_IDLE_SECONDS = 30 * 60
REFRESH_LABEL = "↻ Refresh"
REFRESH_HINT = "Buttons not responding? Tap **↻ Refresh**."
EXPIRED_NOTE = (f"⏸️ Closed after {BROWSER_IDLE_SECONDS // 60} minutes idle. Tap **↻ Refresh** to pick up where you left off - "
                "your locked-in parts are saved.")
_REFRESH_PREFIX = "ship-parts-browse:refresh"
REFRESH_TEMPLATE = _REFRESH_PREFIX + r":(?P<vehicle>\d+):(?P<terminal>\d+):(?P<category>.*)"
MAX_CUSTOM_ID_CHARS = 100  # Discord's limit
MAX_SELECT_OPTIONS = 25  # Discord's limit
MESSAGE_LIMIT = 2000  # Discord's limit
# /ship-loadout loads every slot's parts and every stock part, all under this one deadline
# rather than LOAD_TIME_BUDGET_SECONDS for each slot: a dozen slots one after another could
# otherwise wait over ten minutes on a hanging wiki. Past it, nothing new starts, and the
# parts left unanswered are counted in the loadout's "wiki didn't respond" note.
LOADOUT_TIME_BUDGET_SECONDS = 90.0
LOADOUT_IDLE_SECONDS = BROWSER_IDLE_SECONDS
# How many mounts deep a gun hardpoint's stock loadout is followed to its gun slot: a gimbal is
# one, a turret holding gimbals two (the Perseus's remote turrets).
MAX_MOUNT_DEPTH = 3
LOADOUT_EXPIRED_NOTE = (f"⏸️ Closed after {LOADOUT_IDLE_SECONDS // 60} minutes idle. Run `/ship-loadout` again, or "
                        "**Recommend a loadout** in the parts browser - anything you added to your list is saved.")
WIKI_SILENT_FOR_SLOT = "the Star Citizen Wiki didn't respond for this slot's parts - try again in a few minutes"
# Wiki detail fields no display or fit check reads - dropped before caching, so a full
# cache stays small on the Pi.
_UNUSED_DETAIL_KEYS = frozenset({
    "images", "description", "description_data", "shops", "uex_prices", "blueprint",
    "variants", "entity_tag_map", "entity_tags", "interactions", "dimension", "base_variant",
})
class PartCandidates(list):
    """candidates_for_port's result: the ranked parts, plus how many of the slot's sold
    parts the wiki didn't answer for. Those have no stats and no fit check, so they may be
    missing from the list, and the browser says so rather than showing a quietly short one."""

    def __init__(self, parts=(), *, wiki_unavailable: int = 0) -> None:
        super().__init__(parts)
        self.wiki_unavailable = wiki_unavailable


# One wiki call per candidate item - batched so a slot with many real candidates doesn't
# serialize dozens of live lookups, same pattern as /ingame-item-finder's _fetch_distances.
DETAIL_BATCH_SIZE = 8


def _pages(lines: list[str], limit: int = 1900) -> list[str]:
    pages, current = [], ""
    for line in lines:
        line = discord.utils.escape_mentions(str(line))[:limit]
        candidate = f"{current}\n{line}" if current else line
        if len(candidate) > limit:
            pages.append(current)
            current = line
        else:
            current = candidate
    return pages + ([current] if current else [])


def _format_port_label(port: ShipPort) -> str:
    return format_port_label(port.name, port.size_min, port.size_max)


def _slim_detail(detail: dict | None) -> dict | None:
    if not isinstance(detail, dict):
        return detail
    return {k: v for k, v in detail.items() if k not in _UNUSED_DETAIL_KEYS}


def _rank_key(candidate: dict) -> tuple:
    """Best ranking stat first (see ship_part_display.ranking_stat), a part with nothing to
    rank on after every ranked one; ties go to the closer shop, then the cheaper one."""
    stat = ranking_stat(candidate)
    distance = candidate.get("_distance_gm")
    return (stat is None, -(stat[1] if stat else 0.0), distance is None, distance or 0.0,
            candidate.get("_price_buy") or 0.0)


async def _gather_until(deadline: float | None, calls: list) -> list:
    """Run one batch of lookups (zero-argument callables, so nothing is started once time
    is up) and return each result or exception, like gather(return_exceptions=True). With
    a deadline, a lookup still running when it passes comes back as TimeoutError but isn't
    cancelled - it finishes in the background and fills its cache (see gather_within) -
    and a batch reached after the deadline isn't started at all."""
    if deadline is None:
        return await asyncio.gather(*(call() for call in calls), return_exceptions=True)
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        return [TimeoutError("out of time for this browse") for _ in calls]
    return await gather_within(*(call() for call in calls), timeout=remaining)


def _fits(candidate: dict, port: ShipPort, category: str) -> bool:
    wiki_size = candidate.get("size") if candidate.get("_detail_loaded") else None
    return (part_fits_port(port, category, wiki_size=wiki_size, uex_size=candidate["_uex_row"].get("size"))
            and tags_allow(candidate, port))


def _list_heading(category: str, port: ShipPort | None) -> str:
    """'Shield Generators · Shield Generator Left (S1)', or just 'Radar (S1)' when the
    slot's own name only repeats the category's."""
    label = category_label(category)
    if port is None:
        return label
    slot = _format_port_label(port)
    slot_name = format_port_label(port.name).lower()
    if label.lower().startswith(slot_name):
        return f"{label} {slot[len(format_port_label(port.name)):].strip()}".strip()
    return f"{label} · {slot}"


def _part_option_description(candidate: dict) -> str:
    """'6,165 aUEC · GrimHEX (Dumper's Depot) · 32.0 Gm' under a part's dropdown name, so
    parts past the message's own list can still be told apart."""
    bits = []
    if candidate.get("_price_buy"):
        bits.append(f"{float(candidate['_price_buy']):,.0f} aUEC")
    shop = shop_text(candidate.get("_terminal_name"))
    if shop:
        bits.append(shop)
    if candidate.get("_distance_gm") is not None:
        bits.append(f"{float(candidate['_distance_gm']):.1f} Gm")
    return " · ".join(bits)


def _entry_slot_label(entry: dict) -> str:
    """A saved entry's slot, e.g. 'Quantum Drive' or 'Left Wing Gun (weapon)'. Weapons and
    gun mounts can share one physical hardpoint, so those two say which one they are."""
    label = format_port_label(entry["port_name"])
    suffix = {GUNS_CATEGORY: " (weapon)", MOUNTS_CATEGORY: " (mount)"}.get(entry.get("category"), "")
    return f"{label}{suffix}"


class ShipPartsShoppingService:
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        # One get-or-create at a time per player and server (audit REL-16): without it, two
        # quick clicks each found no thread, each created one, and one was left orphaned.
        self._thread_locks: dict[tuple[int, int], asyncio.Lock] = {}

    async def _thread(self, interaction: discord.Interaction) -> discord.Thread | None:
        if interaction.guild_id is None:
            return None
        lock = self._thread_locks.setdefault((interaction.user.id, interaction.guild_id), asyncio.Lock())
        async with lock:
            return await self._get_or_create_thread(interaction)

    async def _get_or_create_thread(self, interaction: discord.Interaction) -> discord.Thread | None:
        saved = await self.bot.db.get_ship_parts_thread(interaction.user.id, interaction.guild_id)
        if saved:
            thread = self.bot.get_channel(saved["thread_id"])
            if thread is None:
                try:
                    thread = await self.bot.fetch_channel(saved["thread_id"])
                except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                    thread = None
            if isinstance(thread, discord.Thread):
                try:
                    if thread.archived:
                        await thread.edit(archived=False)
                    await thread.add_user(interaction.user)
                    return thread
                except (discord.Forbidden, discord.HTTPException):
                    logger.warning("Could not reuse ship parts thread %s", thread.id)

        if not isinstance(interaction.channel, discord.TextChannel):
            return None
        thread = None
        try:
            thread = await interaction.channel.create_thread(
                name=f"Ship parts - {interaction.user.display_name}"[:100],
                type=discord.ChannelType.private_thread, invitable=False, auto_archive_duration=1440,
            )
            await thread.add_user(interaction.user)
            message = await thread.send("Your private ship parts list is ready.", view=ShipPartsShoppingView(self),
                                        allowed_mentions=NO_MENTIONS)
            await self.bot.db.set_ship_parts_thread(interaction.user.id, interaction.guild_id, thread.id, message.id)
            return thread
        except Exception:
            logger.exception("Could not create ship parts shopping thread")
            try:
                await self.bot.db.delete_ship_parts_thread(interaction.user.id, interaction.guild_id)
            except Exception:
                logger.exception("Could not clear partial ship parts thread state")
            if thread is not None:
                try:
                    await thread.delete()
                except Exception:
                    logger.warning("Could not delete partial ship parts thread %s", thread.id)
            return None

    async def _current_prices(self, entries: list[dict]) -> dict[int, tuple[float, str | None]] | None:
        """id_item -> (price, full shop name) at each listed part's cheapest shop right now,
        the same /items_prices_all rows the browser prices from - None if UEX didn't answer,
        so the list falls back to lock-in prices and says so (audit MSG-13)."""
        try:
            cheapest = cheapest_listing_by_item(await self.bot.uex.get_items_prices_all())
        except Exception:
            logger.warning("Couldn't re-price the ship parts list; showing lock-in prices", exc_info=True)
            return None
        listings = {entry["id_item"]: cheapest[entry["id_item"]] for entry in entries
                    if entry.get("id_item") in cheapest}
        references: dict = {}
        terminal_ids = [listing["id_terminal"] for listing in listings.values() if listing.get("id_terminal")]
        if terminal_ids:
            try:
                references = await self.bot.db.get_terminal_references_by_ids(terminal_ids)
            except Exception:
                logger.warning("Couldn't load full shop names for the ship parts list", exc_info=True)
        return {
            id_item: (float(listing["price_buy"]),
                      (references.get(listing.get("id_terminal")) or {}).get("terminal_name")
                      or listing.get("terminal_name"))
            for id_item, listing in listings.items()
        }

    async def render(self, user_id: int, guild_id: int) -> list[str]:
        entries = await self.bot.db.get_ship_parts_entries(user_id, guild_id)
        if not entries:
            return ["**Ship parts list**\nNo parts locked in yet."]
        current = await self._current_prices(entries)
        if current is None:
            note = ("-# UEX's prices didn't load, so these are the prices when each part was locked in. "
                    "Press Refresh list to try again.")
        else:
            note = (f"-# Each part's cheapest shop right now, from UEX's prices "
                    f"(updated every {cache_interval_text('items_prices_all')}).")
        lines = ["**Ship parts list**", note]
        current_ship = None
        for entry in entries:
            if entry["vehicle_name"] != current_ship:
                current_ship = entry["vehicle_name"]
                lines.extend(["", f"**{current_ship}**"])
            price = list_price_text(
                entry.get("price_buy"), entry.get("terminal_name"),
                (current or {}).get(entry.get("id_item")), prices_loaded=current is not None,
            )
            lines.append(f"• {_entry_slot_label(entry)}: **{entry['item_name']}** — {price}")
        return _pages(lines)

    async def refresh(self, thread: discord.Thread, user_id: int, guild_id: int) -> None:
        pages = await self.render(user_id, guild_id)
        content = pages[0]
        attachment = None
        if len(pages) > 1:
            content = "**Ship parts list**\nThe full list is attached because it exceeds Discord's message limit."
            attachment = discord.File(io.BytesIO("\n".join(pages).encode("utf-8")), filename="ship-parts-list.txt")
        saved = await self.bot.db.get_ship_parts_thread(user_id, guild_id)
        message = None
        if saved and saved.get("message_id"):
            try:
                message = await thread.fetch_message(saved["message_id"])
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                pass
        if message is None:
            kwargs = {"file": attachment} if attachment else {}
            message = await thread.send(content, view=ShipPartsShoppingView(self), allowed_mentions=NO_MENTIONS, **kwargs)
            await self.bot.db.set_ship_parts_thread(user_id, guild_id, thread.id, message.id)
        else:
            await message.edit(content=content, attachments=[attachment] if attachment else [],
                               view=ShipPartsShoppingView(self), allowed_mentions=NO_MENTIONS)

    async def lock_in(
        self, interaction: discord.Interaction, id_vehicle: int, vehicle_name: str, category: str,
        port_name: str, id_item: int, item_name: str, id_terminal: int | None, terminal_name: str | None,
        price_buy: float | None,
    ) -> bool:
        if interaction.guild_id is None:
            await interaction.followup.send("Ship parts lists are available in a server.", ephemeral=True)
            return False
        thread = await self._thread(interaction)
        if thread is None:
            await interaction.followup.send("I couldn't create your private ship parts thread. Check thread permissions.",
                                            ephemeral=True)
            return False
        now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        try:
            await self.bot.db.set_ship_parts_entry(
                interaction.user.id, interaction.guild_id, id_vehicle, vehicle_name, category, port_name,
                id_item, item_name, id_terminal, terminal_name, price_buy, now,
            )
        except Exception:
            logger.exception("Could not save ship parts entry")
            await interaction.followup.send("I couldn't save that part. Nothing was added; please try again.", ephemeral=True)
            return False
        try:
            await self.refresh(thread, interaction.user.id, interaction.guild_id)
        except Exception:
            logger.exception("Ship parts entry saved but Discord refresh failed")
            await interaction.followup.send(f"Saved, but I couldn't refresh {thread.mention}. Use Refresh list there.",
                                            ephemeral=True)
            return True
        await interaction.followup.send(f"Locked in **{item_name}** for {category_label(category)} in {thread.mention}.",
                                        ephemeral=True)
        return True

    async def lock_in_many(
        self, interaction: discord.Interaction, id_vehicle: int, vehicle_name: str, entries: list[dict],
    ) -> int:
        """lock_in for several parts at once - /ship-loadout's "Add all to shopping list". One
        thread lookup, one list refresh and one reply, where calling lock_in per part would
        post a reply and redraw the list once per part. Each entry has the keys lock_in takes
        (category, port_name, id_item, item_name, id_terminal, terminal_name, price_buy); one
        in a slot that already has a part replaces it, as a lock-in does. Returns how many
        were saved."""
        if interaction.guild_id is None:
            await interaction.followup.send("Ship parts lists are available in a server.", ephemeral=True)
            return 0
        thread = await self._thread(interaction)
        if thread is None:
            await interaction.followup.send("I couldn't create your private ship parts thread. Check thread permissions.",
                                            ephemeral=True)
            return 0
        now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        saved = 0
        for entry in entries:
            try:
                await self.bot.db.set_ship_parts_entry(
                    interaction.user.id, interaction.guild_id, id_vehicle, vehicle_name, entry["category"],
                    entry["port_name"], entry["id_item"], entry["item_name"], entry["id_terminal"],
                    entry["terminal_name"], entry["price_buy"], now,
                )
            except Exception:
                logger.exception("Could not save a ship parts entry from a loadout")
                continue
            saved += 1
        if not saved:
            await interaction.followup.send("I couldn't save those parts. Nothing was added; please try again.",
                                            ephemeral=True)
            return 0
        failed = len(entries) - saved
        added = f"{saved} part{'s' if saved != 1 else ''}"
        not_saved = f" {failed} couldn't be saved - press it again to retry." if failed else ""
        try:
            await self.refresh(thread, interaction.user.id, interaction.guild_id)
        except Exception:
            logger.exception("Loadout parts saved but the Discord refresh failed")
            await interaction.followup.send(
                f"Saved {added}, but I couldn't refresh {thread.mention}. Use Refresh list there.{not_saved}",
                ephemeral=True)
            return saved
        await interaction.followup.send(
            f"Added {added} for **{vehicle_name}** to {thread.mention}, replacing anything already "
            f"locked in for those slots.{not_saved}", ephemeral=True)
        return saved


class _RemoveEntrySelect(discord.ui.Select):
    """Transient, single-use dropdown for removing one locked-in part at a time, per the
    user's own live feedback that "Clear list" only ever wiping everything wasn't enough.
    Options are captured from the SAME entries list this select was built from, not
    re-fetched in the callback - the instance is short-lived (one pick, then it's done, see
    _RemoveEntryView's timeout) so a stale index is a non-issue in practice, and avoids a
    second DB round trip just to re-derive what's already in hand."""
    def __init__(self, service: "ShipPartsShoppingService", entries: list[dict]) -> None:
        self.service = service
        self.entries = entries[:25]
        options = []
        for i, entry in enumerate(self.entries):
            options.append(discord.SelectOption(
                label=f"{entry['item_name']} ({category_label(entry['category'])})"[:100],
                description=f"{entry['vehicle_name']} - {_entry_slot_label(entry)}"[:100],
                value=str(i),
            ))
        super().__init__(placeholder="Pick a part to remove...", options=options, min_values=1, max_values=1)

    async def callback(self, interaction: discord.Interaction) -> None:
        entry = self.entries[int(self.values[0])]
        await interaction.response.defer(ephemeral=True)
        try:
            await self.service.bot.db.remove_ship_parts_entry(
                entry["user_id"], entry["guild_id"], entry["id_vehicle"], entry["category"], entry["port_name"],
            )
        except Exception:
            logger.exception("Could not remove a ship parts entry")
            await interaction.followup.send("I couldn't remove that part. Nothing was changed; please try again.",
                                            ephemeral=True)
            return
        try:
            await self.service.refresh(interaction.channel, entry["user_id"], entry["guild_id"])
        except Exception:
            logger.exception("Ship parts entry removed but the Discord refresh failed")
            await interaction.followup.send(
                f"Removed **{entry['item_name']}**, but I couldn't update the list message. Press Refresh list.",
                ephemeral=True)
            return
        await interaction.followup.send(f"Removed **{entry['item_name']}** ({category_label(entry['category'])}).", ephemeral=True)


class _RemoveEntryView(BotView):
    def __init__(self, service: "ShipPartsShoppingService", entries: list[dict]) -> None:
        super().__init__(timeout=300)
        self.add_item(_RemoveEntrySelect(service, entries))


class ShipPartsShoppingView(BotView):
    """Persistent controls whose callbacks always re-check the stored owner - survives a bot
    restart (timeout=None, fixed custom_ids, registered once via cog_load's bot.add_view).
    Mirrors bot/cogs/blueprint_planner.py's ShoppingView exactly."""
    def __init__(self, service: ShipPartsShoppingService) -> None:
        super().__init__(timeout=None)
        self.service = service

    async def _owner(self, interaction: discord.Interaction) -> dict | None:
        row = await self.service.bot.db.get_ship_parts_thread_owner(getattr(interaction.channel, "id", 0))
        if row is None or row["user_id"] != interaction.user.id:
            await interaction.response.send_message("This shopping list belongs to another player.", ephemeral=True)
            return None
        return row

    @discord.ui.button(label="Refresh list", style=discord.ButtonStyle.secondary,
                       custom_id="ship-parts-shopping:refresh")
    async def refresh_button(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        row = await self._owner(interaction)
        if row:
            await interaction.response.defer(ephemeral=True)
            try:
                await self.service.refresh(interaction.channel, row["user_id"], row["guild_id"])
            except Exception:
                logger.exception("Could not refresh the ship parts list")
                await interaction.followup.send(
                    "I couldn't refresh the list. Check that I can edit messages in this thread, then try again.",
                    ephemeral=True)
                return
            await interaction.followup.send("List refreshed with current prices.", ephemeral=True)

    @discord.ui.button(label="Remove a part", style=discord.ButtonStyle.secondary,
                       custom_id="ship-parts-shopping:remove-one")
    async def remove_button(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        row = await self._owner(interaction)
        if row is None:
            return
        entries = await self.service.bot.db.get_ship_parts_entries(row["user_id"], row["guild_id"])
        if not entries:
            await interaction.response.send_message("Your list is empty - nothing to remove.", ephemeral=True)
            return
        note = "" if len(entries) <= 25 else f" (showing the first 25 of {len(entries)})"
        view = _RemoveEntryView(self.service, entries)
        await interaction.response.send_message(
            f"Pick a part to remove{note}:", view=view, ephemeral=True)
        view.origin = interaction

    @discord.ui.button(label="Clear list", style=discord.ButtonStyle.danger,
                       custom_id="ship-parts-shopping:clear")
    async def clear_button(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        row = await self._owner(interaction)
        if row:
            await interaction.response.defer(ephemeral=True)
            try:
                await self.service.bot.db.clear_ship_parts_entries(row["user_id"], row["guild_id"])
            except Exception:
                logger.exception("Could not clear the ship parts list")
                await interaction.followup.send(
                    "I couldn't clear your list. Nothing was changed; please try again.", ephemeral=True)
                return
            try:
                await self.service.refresh(interaction.channel, row["user_id"], row["guild_id"])
            except Exception:
                logger.exception("Ship parts list cleared but the Discord refresh failed")
                await interaction.followup.send(
                    "Your list was cleared, but I couldn't update this message. Press Refresh list to redraw it.",
                    ephemeral=True)
                return
            await interaction.followup.send("Ship parts list cleared.", ephemeral=True)


class PartsBrowserView(BotView):
    """Transient (NOT persistent, matches blueprint_planner.py's CraftConfigView) - browsing
    a ship's slots doesn't need to survive a restart, only a locked-in choice does. If this
    view goes stale on a restart, the user just re-runs /ship-parts-finder.

    Category -> [slot, when a category has more than one physical port] -> part, since a
    ship can have several independent slots in one category (e.g. two differently-sized
    turrets) that each need their own choice - collapsing to the category's first port
    silently made those unreachable, a real defect an outside audit caught before merge.

    Its ↻ Refresh button outlives it: see RefreshBrowserButton."""
    def __init__(
        self, cog: "ShipPartsFinder", vehicle: dict, origin_terminal: tuple[int, str],
        grouped_ports: dict[str, list[ShipPort]],
    ) -> None:
        super().__init__(timeout=BROWSER_IDLE_SECONDS)
        self.cog = cog
        self.vehicle = vehicle
        self.origin_terminal = origin_terminal
        self.grouped_ports = grouped_ports
        self.category: str | None = None
        self.selected_port: ShipPort | None = None
        self.candidates: list[dict] = []
        self.selected_candidate: dict | None = None
        # Pages of whole parts, rebuilt whenever candidates change; nothing is ever cut off.
        self.pages: list[list[dict]] = []
        self.page = 0
        self._header_shared: list[str] = []
        self._shared: list[str] = []
        # Bumped on every category/slot pick. A load only applies its result if it's still
        # the latest one - see _load_candidates.
        self._load_seq = 0
        # Parts in the current list's slot that the wiki didn't answer for (see PartCandidates).
        self.parts_unanswered = 0
        # Turrets whose gun slots are missing because the wiki didn't answer; set by the command.
        self.turret_guns_unanswered = 0
        # The message this view is on (set once it's sent) and whether it has gone idle.
        self.message: discord.Message | None = None
        self.expired = False
        # One-off explanation shown in the header, e.g. a refresh that couldn't reload its category.
        self.notice: str | None = None
        # Set while "Recommend a loadout" is building one, so a double click doesn't build
        # and post two (see loadout_button).
        self._opening_loadout = False
        self.category_select = _CategorySelect(self)
        self.add_item(self.category_select)
        self._add_refresh()

    def _selection_summary(self) -> str:
        # Plain-text mirror of the dropdowns' own state, independent of whether Discord's
        # collapsed-Select `default=True` rendering actually shows the pick in a given
        # client - requested directly by the user as a fallback after the dropdown-collapse
        # bug, so this stays even once that fix is confirmed working.
        if self.category is None:
            return ""
        parts = [f"Category: **{category_label(self.category)}**"]
        ports = self.grouped_ports.get(self.category, [])
        if len(ports) > 1:
            parts.append(f"Slot: **{_format_port_label(self.selected_port)}**" if self.selected_port else "Slot: *(pick below)*")
        if self.selected_port is not None or len(ports) <= 1:
            name = self.selected_candidate.get("name") if self.selected_candidate else None
            parts.append(f"Part: **{name}**" if name else "Part: *(pick below)*")
        return "Selected so far - " + " | ".join(parts)

    def _wiki_notes(self) -> list[str]:
        """Say when the Star Citizen Wiki didn't answer, since a list built without its data
        can be short or wrong-sized - otherwise that looks like a real "none for sale"."""
        notes = [self.notice] if self.notice else []
        slots = len(self.grouped_ports.get(self.category, [])) if self.category else 0
        if slots > MAX_SELECT_OPTIONS:
            # A menu holds 25 options. No ship has more than 12 slots in one category today
            # (checked across all 201 on 2026-09-29), so this only guards against that
            # changing - and says so rather than dropping slots silently (audit REL-17).
            notes.append(f"⚠️ This ship has {slots} {category_label(self.category)} slots; "
                         f"only the first {MAX_SELECT_OPTIONS} can be listed.")
        if self.turret_guns_unanswered:
            notes.append("⚠️ The Star Citizen Wiki didn't respond, so turret guns aren't listed. "
                         "Try again in a few minutes.")
        if self.category is not None and self.parts_unanswered:
            count = f"{self.parts_unanswered} part{'s' if self.parts_unanswered != 1 else ''}"
            notes.append(f"⚠️ The Star Citizen Wiki didn't respond for {count} here, so they may be missing "
                         "or listed without their stats. Try again in a few minutes.")
        return notes

    def text(self) -> str:
        # Always ends with how to recover, since a dead control looks exactly like a live one.
        return f"{self._body()}\n\n{EXPIRED_NOTE if self.expired else REFRESH_HINT}"

    def _body(self) -> str:
        header = "\n".join([f"**{self.vehicle.get('name')}** parts - pick a category to compare real options.",
                            *self._wiki_notes()])
        if self.category is None:
            return header
        summary = self._selection_summary()
        label = category_label(self.category)
        ports = self.grouped_ports.get(self.category, [])
        if len(ports) > 1 and self.selected_port is None:
            return f"{header}\n\n{summary}\n\n**{label}** has {len(ports)} separate slots on this ship - pick one below."
        if not self.candidates:
            return f"{header}\n\n{summary}\n\nNo currently-sold {label.lower()} found for this slot."
        lines = [header, "", summary, "", f"**{_list_heading(self.category, self.selected_port)}**"]
        if self._header_shared:
            lines.append(f"All options: {' · '.join(self._header_shared)}")
        ranked_by = ranked_by_label(self.candidates)
        count = f"{len(self.candidates)} parts" if len(self.candidates) != 1 else "1 part"
        lines.append(f"{count}, best {ranked_by} first" if ranked_by else count)
        page = self.pages[self.page] if self.pages else []
        lines.extend(format_part_page(page, shared=self._shared, selected=self.selected_candidate))
        if len(self.pages) > 1:
            # Says how many more are waiting: live, a player read "7 parts" on a 6-part page
            # and took the one on page 2 (Attrition-5, lowest DPS) as missing.
            footer = f"Page {self.page + 1} of {len(self.pages)}"
            later = sum(len(p) for p in self.pages[self.page + 1:])
            if later:
                footer += f" · {later} more on the next page{'s' if len(self.pages) - self.page > 2 else ''}"
            lines.extend(["", footer])
        return "\n".join(lines)

    def _set_candidates(self, candidates: list[dict]) -> None:
        self.candidates = candidates
        port = self.selected_port
        fixed = port.size_min if port is not None and port.size_min == port.size_max else None
        self._header_shared, self._shared = list_shared(candidates, fixed)
        # Any outage note shares the message's 2,000-char limit, so it comes out of the list's budget.
        notes = sum(len(note) + 1 for note in self._wiki_notes()) + len(EXPIRED_NOTE) + 2
        self.pages = paginate_parts(candidates, shared=self._shared, budget=LIST_BUDGET_CHARS - notes)
        self.page = 0
        self._show_page()

    def _show_page(self) -> None:
        """Rebuild the page's own dropdown (only this page's parts, so it can never hit
        Discord's 25-option limit however big the slot) and the page buttons."""
        self._remove_items(_PartSelect, _PageButton)
        if self.pages:
            self.add_item(_PartSelect(self, self.pages[self.page]))
            if len(self.pages) > 1:
                self.add_item(_PageButton(self, -1))
                self.add_item(_PageButton(self, +1))
        self._add_refresh()

    def _add_refresh(self) -> None:
        """(Re)add ↻ Refresh as the last button, carrying the current category so a refresh
        comes back to it."""
        self._remove_items(_RefreshStub)
        self.add_item(_RefreshStub(refresh_custom_id(self.vehicle.get("id"), self.origin_terminal[0], self.category)))

    async def on_timeout(self) -> None:
        """Grey out everything but ↻ Refresh and say so, instead of leaving controls that
        look usable but answer "didn't respond in time". A restart never gets here, which is
        why the hint line is always on the message."""
        browsers = getattr(self.cog, "_browsers", {})
        if self.message is None or browsers.get(self.message.id) is not self:
            return  # replaced by a refresh; the newer view owns the message now
        browsers.pop(self.message.id, None)
        self.expired = True
        for child in self.children:
            if not isinstance(child, _RefreshStub):
                child.disabled = True
        try:
            await self.message.edit(content=self.text(), view=self)
        except discord.HTTPException as exc:
            logger.info("Couldn't mark an idle ship parts browser as closed: %s", exc)

    async def restore_category(self, category: str) -> None:
        """Refresh's way back to where the player was: the same category, reloaded without an
        interaction of its own (the Refresh click has already deferred)."""
        self.category = category
        _mark_default(self.category_select.options, category)
        ports = self.grouped_ports.get(category, [])
        if len(ports) > 1:
            self._set_candidates([])
            self.add_item(_SlotSelect(self, ports))
            return
        self.selected_port = ports[0] if ports else None
        candidates: list[dict] = []
        if self.selected_port is not None:
            try:
                candidates = await self.cog.candidates_for_port(
                    self.selected_port, category=category, origin_id=self.origin_terminal[0],
                )
            except (UexApiError, WikiApiError) as exc:
                logger.info("Refresh couldn't reload %s options: %s", category, exc)
                self.category = None
                self.selected_port = None
                for option in self.category_select.options:
                    option.default = False
                self.notice = f"⚠️ Couldn't reload {category_label(category)} options right now. Pick it again to retry."
                self._set_candidates([])
                return
        self.parts_unanswered = getattr(candidates, "wiki_unavailable", 0)
        self._set_candidates(candidates)

    async def turn_page(self, interaction: discord.Interaction, delta: int) -> None:
        self.page = max(0, min(len(self.pages) - 1, self.page + delta))
        self._show_page()
        await interaction.response.edit_message(content=self.text(), view=self)

    def _remove_items(self, *types: type) -> None:
        for child in [c for c in self.children if isinstance(c, types)]:
            self.remove_item(child)

    async def show_category(self, interaction: discord.Interaction, category: str) -> None:
        self._load_seq += 1  # supersedes any load still running for the previous pick
        self.category = category
        self.notice = None
        self.selected_port = None
        self.selected_candidate = None
        self.parts_unanswered = 0
        self._set_candidates([])
        self._remove_items(_SlotSelect, _PartSelect, _PageButton)
        ports = self.grouped_ports.get(category, [])
        if len(ports) > 1:
            self.add_item(_SlotSelect(self, ports))
            await interaction.response.edit_message(content=self.text(), view=self)
            return
        await self._load_candidates(interaction, ports[0] if ports else None)

    async def show_slot(self, interaction: discord.Interaction, port: ShipPort) -> None:
        self.selected_candidate = None
        await self._load_candidates(interaction, port)

    async def _load_candidates(self, interaction: discord.Interaction, port: ShipPort | None) -> None:
        # Deferred BEFORE the slow catalog/price/wiki lookups below - a live catalog+price
        # fetch plus batched wiki detail calls can exceed Discord's 3s component-interaction
        # deadline on a cold cache, which surfaced as "Interaction failed" before this fix.
        await interaction.response.defer()
        self._load_seq += 1
        load = self._load_seq
        self.selected_port = port
        self.parts_unanswered = 0
        self.notice = None
        # Drop the previous slot's parts (and their dropdown) now, not when this load
        # finishes - otherwise one of them could be picked and locked in under this slot.
        self._set_candidates([])
        candidates: list[dict] = []
        if port is not None:
            try:
                candidates = await self.cog.candidates_for_port(
                    port, category=self.category, origin_id=self.origin_terminal[0],
                )
            except Exception as exc:
                # Any failure, not only UEX's or the wiki's (audit REL-13): the message used
                # to keep the old parts on screen while this view had already dropped them.
                if not isinstance(exc, (UexApiError, WikiApiError)):
                    logger.exception("Ship parts options failed to load for %s", self.category)
                if load != self._load_seq:
                    return
                self.notice = (f"⚠️ Couldn't load {category_label(self.category)} options right now. "
                               "Pick it again to retry.")
                await interaction.edit_original_response(content=self.text(), view=self)
                return
        if load != self._load_seq:
            # The player picked another category or slot while this one loaded (a cold
            # category's first browse is the slow one). Applying these parts now would list
            # them under the newer pick and let one be locked in there (audit REL-2); that
            # newer pick updates the message itself.
            return
        self.parts_unanswered = getattr(candidates, "wiki_unavailable", 0)
        self._set_candidates(candidates)
        await interaction.edit_original_response(content=self.text(), view=self)

    async def lock_in_selected(self, interaction: discord.Interaction) -> None:
        if self.selected_candidate is None or self.category is None or self.selected_port is None:
            await interaction.response.send_message("Pick a part first.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        detail = self.selected_candidate
        await self.cog.shopping.lock_in(
            interaction, self.vehicle["id"], self.vehicle.get("name") or "", self.category,
            self.selected_port.name, detail.get("_uex_id"), detail.get("name") or "unknown",
            detail.get("_id_terminal"), detail.get("_terminal_name"), detail.get("_price_buy"),
        )

    @discord.ui.button(label="Lock in selected part", style=discord.ButtonStyle.success, row=4)
    async def lock_button(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        await self.lock_in_selected(interaction)

    # Row 3 is always free: the category, slot and part menus take rows 0-2 at most, and row 4
    # is lock-in, the page buttons and ↻ Refresh.
    @discord.ui.button(label="Recommend a loadout", style=discord.ButtonStyle.primary, row=3)
    async def loadout_button(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        # Check-then-set with no await between, as LoadoutView.add_all: a second click while
        # the first loadout is still building would otherwise build and post a second one.
        if self._opening_loadout:
            await interaction.response.send_message("Already building that loadout - one moment.", ephemeral=True)
            return
        self._opening_loadout = True
        try:
            await self.cog.open_loadout(interaction, self.vehicle, self.origin_terminal)
        finally:
            self._opening_loadout = False


def _mark_default(options: list[discord.SelectOption], value: str) -> None:
    """Discord's own collapsed-dropdown display shows the placeholder again after every
    pick UNLESS the chosen SelectOption has default=True - found live in testing (looked
    exactly like a lost selection, even though the message text and lock-in still worked).
    Every other option's default is cleared first, so re-picking a different value doesn't
    leave two options marked."""
    for option in options:
        option.default = option.value == value


class _CategorySelect(discord.ui.Select):
    def __init__(self, parent: PartsBrowserView) -> None:
        options = [discord.SelectOption(label=category_label(category)[:100], value=category)
                   for category in list(parent.grouped_ports.keys())[:25]]
        super().__init__(placeholder="Choose a component category", options=options)
        self.parent_view = parent

    async def callback(self, interaction: discord.Interaction) -> None:
        _mark_default(self.options, self.values[0])
        await self.parent_view.show_category(interaction, self.values[0])


class _SlotSelect(discord.ui.Select):
    """Shown only when a category has more than one physical port on this ship."""
    def __init__(self, parent: PartsBrowserView, ports: list[ShipPort]) -> None:
        options = [discord.SelectOption(label=_format_port_label(port)[:100], value=str(i))
                   for i, port in enumerate(ports[:MAX_SELECT_OPTIONS])]
        super().__init__(placeholder="Choose a slot", options=options)
        self.parent_view = parent
        self._ports = ports

    async def callback(self, interaction: discord.Interaction) -> None:
        _mark_default(self.options, self.values[0])
        await self.parent_view.show_slot(interaction, self._ports[int(self.values[0])])


class _PageButton(discord.ui.Button):
    """Previous/Next, beside "Lock in selected part". A picked part stays picked across
    pages - the "Selected so far" line keeps naming it."""
    def __init__(self, parent: PartsBrowserView, delta: int) -> None:
        at_edge = parent.page == 0 if delta < 0 else parent.page >= len(parent.pages) - 1
        super().__init__(label="◀ Previous" if delta < 0 else "Next ▶", style=discord.ButtonStyle.secondary,
                         row=4, disabled=at_edge)
        self.parent_view = parent
        self.delta = delta

    async def callback(self, interaction: discord.Interaction) -> None:
        await self.parent_view.turn_page(interaction, self.delta)


class _PartSelect(discord.ui.Select):
    def __init__(self, parent: PartsBrowserView, candidates: list[dict]) -> None:
        options = [
            discord.SelectOption(label=(c.get("name") or "Unknown")[:100], value=str(i),
                                 description=_part_option_description(c)[:100] or None,
                                 default=c is parent.selected_candidate)
            for i, c in enumerate(candidates[:25])
        ]
        super().__init__(placeholder="Choose a part to lock in", options=options)
        self.parent_view = parent
        self._candidates = candidates

    async def callback(self, interaction: discord.Interaction) -> None:
        _mark_default(self.options, self.values[0])
        self.parent_view.selected_candidate = self._candidates[int(self.values[0])]
        await interaction.response.edit_message(content=self.parent_view.text(), view=self.parent_view)


class LoadoutView(BotView):
    """/ship-loadout's message, also opened by the parts browser's "Recommend a loadout": one
    recommended part per group of identical slots for a profile (bot/uex/ship_loadout.py),
    with buttons to switch profile, page through a big ship, and add every purchase to the
    private shopping list.

    Every slot's candidates and stock parts are loaded once, when it opens. A profile switch
    only re-picks from them, so it answers at once with no lookups. Posted in the player's
    private ship parts thread like the browser (so a plain message edit, not a 15-minute
    interaction token, greys it out when idle), and answers only the player who opened it."""
    def __init__(
        self, cog: "ShipPartsFinder", vehicle: dict, origin: tuple[int, str] | None, groups: list[SlotGroup],
        candidates: dict[tuple, list[dict]], *, owner_id: int, profile: str = DEFAULT_PROFILE,
        stock_unanswered: int = 0, slots_missing: int = 0, parts_unanswered: int = 0, turrets_unanswered: int = 0,
    ) -> None:
        super().__init__(timeout=LOADOUT_IDLE_SECONDS)
        self.cog = cog
        self.vehicle = vehicle
        # (terminal id, name) of the player's location, or None: ties then go to the cheaper part.
        self.origin = origin
        self.groups = groups
        # fit_key -> that slot's candidates (ShipPartsFinder.candidates_for_port).
        self.candidates = candidates
        self.owner_id = owner_id
        self.profile = profile
        # What the wiki didn't answer for, said in the header: slots whose stock part (listed,
        # without a stock comparison), gun hardpoints whose mount (left out, since the mount
        # decides the gun's size), sold parts (maybe missing, or without stats), and turrets
        # whose own gun slots (left out, as the browser leaves them out - see
        # ShipPartsFinder.turret_gun_lookups_unanswered). Slots left out also make the power
        # and cooling totals partial: their guns still draw both in-game.
        self.stock_unanswered = stock_unanswered
        self.slots_missing = slots_missing
        self.parts_unanswered = parts_unanswered
        self.turrets_unanswered = turrets_unanswered
        # Set while "Add all" is saving, so a double click (or a redelivered interaction)
        # doesn't save and redraw the list twice - see add_all.
        self._adding = False
        self.expired = False
        self.message: discord.Message | None = None
        self.picks: list[SlotPick] = []
        self.pages: list[list[str]] = [[]]
        self.page = 0
        self.repick()

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        await super().interaction_check(interaction)
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message(
                "This loadout belongs to another player - run `/ship-loadout` for your own.", ephemeral=True)
            return False
        return True

    def repick(self) -> None:
        """Pick every slot for the current profile, then page the lines so the whole message,
        header and totals included, stays inside Discord's 2,000 characters."""
        self.picks = [pick_for_slot(group, self.candidates.get(group.fit_key, []), self.profile)
                      for group in self.groups]
        lines = [pick_line(pick, self.profile, reason=self._reason(pick)) for pick in self.picks]
        # Measured with a two-digit page count and the idle note, so neither can push a page over.
        fixed = sum(len(part) + 2 for part in ("\n".join(self._header()), "\n".join(self._footer(99)),
                                               LOADOUT_EXPIRED_NOTE))
        self.pages = paginate_lines(lines, max(MESSAGE_LIMIT - fixed - 10, 400))
        self.page = 0
        self._show_buttons()

    def _reason(self, pick: SlotPick) -> str | None:
        """'No stats' only means the wiki lacks them when it answered; if it didn't, say that."""
        candidates = self.candidates.get(pick.group.fit_key, [])
        if pick.reason == NO_STATS and any(c.get("_detail_unanswered") for c in candidates):
            return WIKI_SILENT_FOR_SLOT
        return None

    def _header(self) -> list[str]:
        if self.origin is None:
            shops = "Each part at its cheapest shop; ties go to the cheaper part."
        else:
            where = f"**{self.origin[1]}**" if self.origin[1] else "your location"
            shops = f"Each part at its cheapest shop; ties go to the shop nearest {where}."
        lines = [f"**{self.vehicle.get('name')}** recommended loadout · **{self.profile}**",
                 f"-# {PROFILE_BLURBS[self.profile][0].upper()}{PROFILE_BLURBS[self.profile][1:]}. {shops}"]
        # (count, noun, what it means for one, for several)
        for count, what, one, many in (
            (self.slots_missing, "gun slot", "it is left out", "they are left out"),
            (self.turrets_unanswered, "turret", "its guns are left out", "their guns are left out"),
            (self.stock_unanswered, "stock part", "it can't be compared against stock",
             "they can't be compared against stock"),
            (self.parts_unanswered, "part", "it may be missing", "they may be missing"),
        ):
            if not count:
                continue
            effect = one if count == 1 else many
            effect += {"gun slot": ", since the mount decides the gun's size",
                       "part": ", so a better pick may exist"}.get(what, "")
            lines.append(f"⚠️ The Star Citizen Wiki didn't respond for {count} {what}{'s' if count != 1 else ''}: "
                         f"{effect}. Try again in a few minutes.")
        return lines

    def _footer(self, page_count: int) -> list[str]:
        lines = [total_line(self.picks)]
        warnings = resource_warnings(self.picks, left_out=self.slots_missing + self.turrets_unanswered)
        if warnings:
            lines.extend(f"⚡ {warning}" for warning in warnings)
            lines.append("-# You assign power in-game, so this is a heads-up, not a reason to change a pick.")
        if page_count > 1:
            lines.append(f"Page {self.page + 1} of {page_count}")
        return lines

    def text(self) -> str:
        page = self.pages[min(self.page, len(self.pages) - 1)]
        sections = ["\n".join(self._header()), "\n".join(page), "\n".join(self._footer(len(self.pages)))]
        if self.expired:
            sections.append(LOADOUT_EXPIRED_NOTE)
        return discord.utils.escape_mentions("\n\n".join(s for s in sections if s))[:MESSAGE_LIMIT]

    def _show_buttons(self) -> None:
        for child in [c for c in self.children if isinstance(c, (_ProfileButton, _LoadoutPageButton))]:
            self.remove_item(child)
        for profile in PROFILES:
            self.add_item(_ProfileButton(self, profile))
        if len(self.pages) > 1:
            self.add_item(_LoadoutPageButton(self, -1))
            self.add_item(_LoadoutPageButton(self, +1))
        self.add_all_button.disabled = not purchases(self.picks)

    async def switch_profile(self, interaction: discord.Interaction, profile: str) -> None:
        self.profile = profile
        self.repick()
        await interaction.response.edit_message(content=self.text(), view=self)

    async def turn_page(self, interaction: discord.Interaction, delta: int) -> None:
        self.page = max(0, min(len(self.pages) - 1, self.page + delta))
        self._show_buttons()
        await interaction.response.edit_message(content=self.text(), view=self)

    async def add_all(self, interaction: discord.Interaction) -> None:
        """Every purchase in the current profile's picks - never a kept-stock line - into the
        private shopping list, each under its slot's own entry name (LoadoutSlot.entry_port_name),
        so it replaces what the browser saved there and vice versa.

        Two already-dispatched clicks would otherwise both save and both redraw the list - and
        when the list message is gone, both post a new one, orphaning one. Checking `_adding`
        and only then setting it is the guard (ConfirmListingView's pattern): nothing awaits
        between the check and the set, so the second click always sees the first one's write."""
        if self._adding:
            await interaction.response.send_message("Already adding these parts - one moment.", ephemeral=True)
            return
        self._adding = True
        try:
            await interaction.response.defer(ephemeral=True)
            entries = [{
                "category": slot.category, "port_name": slot.entry_port_name, "id_item": part.get("_uex_id"),
                "item_name": part.get("name") or "unknown", "id_terminal": part.get("_id_terminal"),
                "terminal_name": part.get("_terminal_name"), "price_buy": part.get("_price_buy"),
            } for slot, part in purchases(self.picks)]
            if not entries:
                await interaction.followup.send("Nothing to add: every slot keeps what it has.", ephemeral=True)
                return
            await self.cog.shopping.lock_in_many(
                interaction, _vehicle_id(self.vehicle), self.vehicle.get("name") or "", entries)
        finally:
            self._adding = False

    @discord.ui.button(label="Add all to shopping list", style=discord.ButtonStyle.success, row=1)
    async def add_all_button(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        await self.add_all(interaction)

    async def on_timeout(self) -> None:
        """Grey every button out and say how to get the loadout back."""
        self.expired = True
        await self.grey_out(content=self.text())


class _ProfileButton(discord.ui.Button):
    """One per profile; the one showing is highlighted and can't be pressed again."""
    def __init__(self, parent: LoadoutView, profile: str) -> None:
        current = profile == parent.profile
        super().__init__(label=profile, style=discord.ButtonStyle.primary if current else discord.ButtonStyle.secondary,
                         row=0, disabled=current)
        self.parent_view = parent
        self.profile = profile

    async def callback(self, interaction: discord.Interaction) -> None:
        await self.parent_view.switch_profile(interaction, self.profile)


class _LoadoutPageButton(discord.ui.Button):
    def __init__(self, parent: LoadoutView, delta: int) -> None:
        at_edge = parent.page == 0 if delta < 0 else parent.page >= len(parent.pages) - 1
        super().__init__(label="◀ Previous" if delta < 0 else "Next ▶", style=discord.ButtonStyle.secondary,
                         row=1, disabled=at_edge)
        self.parent_view = parent
        self.delta = delta

    async def callback(self, interaction: discord.Interaction) -> None:
        await self.parent_view.turn_page(interaction, self.delta)


def _vehicle_id(vehicle: dict) -> int | None:
    try:
        return int(vehicle.get("id"))
    except (TypeError, ValueError):
        return None


def refresh_custom_id(id_vehicle, id_terminal, category: str | None) -> str:
    custom_id = f"{_REFRESH_PREFIX}:{int(id_vehicle)}:{int(id_terminal)}:{category or ''}"
    if len(custom_id) > MAX_CUSTOM_ID_CHARS:  # an unexpectedly long category name: reopen without it
        custom_id = f"{_REFRESH_PREFIX}:{int(id_vehicle)}:{int(id_terminal)}:"
    return custom_id


class _RefreshStub(discord.ui.Button):
    """↻ Refresh as it sits in a live PartsBrowserView. Deliberately NOT dispatchable: every
    click goes to RefreshBrowserButton, registered once at startup, so the same code runs
    whether or not this view is still alive. It can't be a DynamicItem inside the view,
    because discord.py unregisters a view's DynamicItem patterns bot-wide when that view
    closes - the first browser to time out would have killed Refresh on every message."""
    def __init__(self, custom_id: str) -> None:
        super().__init__(label=REFRESH_LABEL, style=discord.ButtonStyle.secondary, row=4, custom_id=custom_id)

    def is_dispatchable(self) -> bool:
        return False


class RefreshBrowserButton(discord.ui.DynamicItem[discord.ui.Button], template=REFRESH_TEMPLATE):
    """Handles ↻ Refresh on any browsing message, even one whose view timed out or was lost
    in a restart: the ship, location and category ride in the button's own custom_id."""
    def __init__(self, id_vehicle: int, id_terminal: int, category: str | None) -> None:
        super().__init__(discord.ui.Button(
            label=REFRESH_LABEL, style=discord.ButtonStyle.secondary, row=4,
            custom_id=refresh_custom_id(id_vehicle, id_terminal, category),
        ))
        self.id_vehicle = id_vehicle
        self.id_terminal = id_terminal
        self.category = category

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item: discord.ui.Button, match, /):
        return cls(int(match["vehicle"]), int(match["terminal"]), match["category"] or None)

    async def callback(self, interaction: discord.Interaction) -> None:
        cog = interaction.client.get_cog("ShipPartsFinder")
        if cog is None:
            await interaction.response.send_message(
                "Ship Parts Finder isn't available right now. Try again in a minute.", ephemeral=True,
            )
            return
        await cog.refresh_browser(interaction, self.id_vehicle, self.id_terminal, self.category)


class ShipPartsFinder(commands.Cog):
    def __init__(self, bot: commands.Bot, *, wiki_client: WikiApiClient | None = None, start_refresh: bool = True) -> None:
        self.bot = bot
        self._wiki = wiki_client or WikiApiClient()
        self._detail_cache: dict[str, tuple[float, dict]] = {}
        # Detail lookups the wiki didn't answer: key -> monotonic time to retry after.
        self._wiki_outages: dict[str, float] = {}
        self.shopping = ShipPartsShoppingService(bot)
        # Live browsing views by message id, so a refresh can retire the one it replaces.
        self._browsers: dict[int, PartsBrowserView] = {}
        # id_vehicle -> (expiry, port path -> stock item uuid) for /ship-loadout: see
        # _vehicle_stock_uuids.
        self._stock_trees: dict[int, tuple[float, dict[str, str]]] = {}
        if start_refresh:
            self.refresh_reference.start()

    async def cog_load(self) -> None:
        self.bot.add_view(ShipPartsShoppingView(self.shopping))
        self.bot.add_dynamic_items(RefreshBrowserButton)

    def cog_unload(self) -> None:
        self.bot.remove_dynamic_items(RefreshBrowserButton)
        self.refresh_reference.cancel()
        try:
            asyncio.get_running_loop().create_task(self._wiki.aclose())
        except RuntimeError:
            pass

    @tasks.loop(hours=REFERENCE_CHECK_HOURS)
    async def refresh_reference(self) -> None:
        """Keep each ship's slots at most REFERENCE_REFRESH_HOURS old. It used to re-read
        every ship from the wiki on every start, ~380 requests in 15 seconds, three times
        on one morning of deploys (audit REL-11). Now only ships that are due are asked
        about, one at a time, so a restart costs nothing and an interrupted crawl resumes
        where it stopped."""
        try:
            vehicles = await self.bot.uex.get_vehicles()
        except UexApiError as exc:
            logger.warning("Ship parts reference refresh could not load the vehicle list: %s", exc)
            return
        try:
            fresh = await self.bot.db.get_fresh_ship_parts_vehicles(REFERENCE_REFRESH_HOURS)
        except Exception:
            logger.exception("Ship parts reference refresh could not read when ships were last refreshed")
            return
        due = [v for v in vehicles if v.get("name") and _vehicle_id(v) is not None and _vehicle_id(v) not in fresh]
        outages = 0
        for index, vehicle in enumerate(due):
            if index:
                await asyncio.sleep(WIKI_CRAWL_SPACING_SECONDS)
            try:
                await self._refresh_vehicle_reference(vehicle)
                outages = 0
            except WikiUnavailableError as exc:
                outages += 1
                logger.warning("Ship parts reference refresh: the wiki didn't answer for %r: %s", vehicle.get("name"), exc)
                if outages >= WIKI_CRAWL_MAX_OUTAGES:
                    logger.warning("Ship parts reference refresh stopped after %d ships the wiki didn't answer for; "
                                   "%d ships left for the next check", outages, len(due) - index - 1)
                    return
            except Exception:
                logger.exception("Ship parts reference refresh failed unexpectedly for vehicle %r", vehicle.get("name"))

    async def _refresh_vehicle_reference(self, vehicle: dict) -> None:
        """Re-read one ship's slots. Raises WikiUnavailableError when the wiki didn't answer
        (the ship stays due); any definite answer, including an error, marks it refreshed."""
        id_vehicle = int(vehicle["id"])
        name = vehicle["name"]
        try:
            ports = await self._wiki_ports(vehicle)
        except WikiUnavailableError:
            raise
        except (TypeError, ValueError, WikiApiError) as exc:
            # A definite failure (a 404, an identity mismatch, data it can't parse) won't
            # change within the hour, so it waits a day like an empty answer does.
            logger.warning("Ship parts reference refresh failed for %r: %s; keeping its saved slots", name, exc)
            ports = []
        if ports:
            await self.bot.db.replace_ship_parts_reference(
                id_vehicle, name,
                [{"name": p.name, "port_type": p.port_type, "size_min": p.size_min, "size_max": p.size_max,
                  "accepts_guns": p.accepts_guns, "port_tags": sorted(p.tags), "editable": p.editable,
                  "required_tags": sorted(p.required_tags), "equipped_uuid": p.equipped_uuid} for p in ports],
            )
        else:
            # No slots is how the wiki answers a ship it doesn't have (concept ships) but also
            # a name it briefly fails to match. Replacing with nothing used to wipe a ship's
            # saved slots on such a blip (audit REL-11), so keep whatever is saved.
            saved = await self.bot.db.get_ship_parts_reference(id_vehicle)
            if saved:
                logger.warning("The wiki returned no slots for %r; keeping its %d saved slots", name, len(saved))
        # Marked either way, so a ship the wiki doesn't have is asked about daily, not hourly.
        await self.bot.db.mark_ship_parts_refreshed(id_vehicle, len(ports))

    @refresh_reference.before_loop
    async def before_refresh_reference(self) -> None:
        await self.bot.wait_until_ready()

    async def _ports_for_vehicle(self, vehicle: dict) -> list[ShipPort]:
        id_vehicle = int(vehicle["id"])
        rows = await self.bot.db.get_ship_parts_reference(id_vehicle)
        if rows:
            ports = [ShipPort(name=r["port_name"], port_type=r["port_type"], size_min=r["size_min"],
                              size_max=r["size_max"], accepts_guns=bool(r.get("accepts_guns")),
                              tags=frozenset((r.get("port_tags") or "").split()),
                              editable=r.get("editable", 1) != 0,
                              required_tags=frozenset((r.get("required_tags") or "").split()),
                              equipped_uuid=r.get("equipped_uuid") or None)
                     for r in rows]
        else:
            # Collector hasn't run for this ship yet (fresh deploy, or a ship added since the
            # last daily cycle) - fall back to a live lookup rather than a dead end.
            ports = await self._wiki_ports(vehicle)
        return await self._with_child_gun_ports(ports)

    async def _with_child_gun_ports(self, ports: list[ShipPort]) -> list[ShipPort]:
        """Add the gun slots inside each turret whose guns aren't in the ship's own slot
        (the Perseus's remote turrets hold 2 S3 guns each). Looked up here, when a ship is
        opened, from the turret's cached wiki detail - not in the daily refresh, which
        would cost a wiki call per turret on all 282 ships. A failed lookup just means no
        gun slots for that turret, never a failed command."""
        parents = [p for p in ports if p.needs_child_gun_ports]
        if not parents:
            return ports
        details = await asyncio.gather(
            *(self._item_detail_cached({"uuid": p.equipped_uuid}) for p in parents), return_exceptions=True,
        )
        children = {p.name: child_gun_ports(p, d if isinstance(d, dict) else None) for p, d in zip(parents, details)}
        result: list[ShipPort] = []
        for port in ports:
            result.append(port)
            result.extend(children.get(port.name, []))
        return result

    async def _wiki_ports(self, vehicle: dict) -> list[ShipPort]:
        """The wiki names some ships with their maker ('MISC Reliant Tana', 'MISC
        Freelancer') where UEX's `name` doesn't ('Reliant Tana') - UEX's own `name_full`
        matches those, so it's tried second (21 of the 88 UEX ships the wiki didn't match
        by name; most of the rest are concept ships the wiki's game data doesn't have)."""
        names = [vehicle.get("name"), vehicle.get("name_full")]
        for name in dict.fromkeys(n.strip() for n in names if isinstance(n, str) and n.strip()):
            raw_ports, vehicle_tags = await self._wiki.get_vehicle_loadout(name)
            if raw_ports:
                return parse_ports(raw_ports, vehicle_tags)
        return []

    async def _item_detail_cached(self, uex_row: dict) -> dict | None:
        """Wiki detail for one UEX catalog row: by its uuid, else by its exact name (UEX's
        uuid for most radars doesn't exist on the wiki, but the name does). A real miss is
        cached too, so a part the wiki genuinely lacks isn't re-looked-up every browse.

        A lookup the wiki didn't answer at all is NOT a miss: caching it as one left parts
        fitted by UEX's wrong size, missile racks gone and turret guns missing for a full
        day after a brief outage (audit REL-1). That raises WikiUnavailableError instead,
        and the key is skipped for WIKI_OUTAGE_RETRY_SECONDS, then looked up again."""
        item_uuid = uex_row.get("uuid") or ""
        name = (uex_row.get("name") or "").strip()
        key = item_uuid or f"name:{name.lower()}"
        now = time.monotonic()
        cached = self._detail_cache.get(key)
        if cached and cached[0] > now:
            return cached[1]
        if self._wiki_outage_active(key, now):
            raise WikiUnavailableError(f"the wiki didn't answer for {name or item_uuid} a few minutes ago")
        detail = None
        unanswered = False
        try:
            if item_uuid:
                detail = await self._wiki.get_item_detail(item_uuid)
        except WikiUnavailableError:
            unanswered = True
        except WikiApiError:
            detail = None
        if detail is None and name:
            try:
                detail = await self._wiki.find_item_detail_by_name(name)
            except WikiUnavailableError:
                unanswered = True
            except WikiApiError:
                detail = None
        if detail is None and unanswered:
            # Either lookup going unanswered means "no detail" isn't a known answer yet.
            self._record_wiki_outage(key, now)
            raise WikiUnavailableError(f"the wiki didn't answer for {name or item_uuid}")
        self._wiki_outages.pop(key, None)
        if len(self._detail_cache) >= DETAIL_CACHE_MAX:
            self._detail_cache.pop(next(iter(self._detail_cache)))
        detail = _slim_detail(detail)
        self._detail_cache[key] = (now + DETAIL_CACHE_SECONDS, detail)
        return detail

    def _record_wiki_outage(self, key: str, now: float) -> None:
        if len(self._wiki_outages) >= DETAIL_CACHE_MAX:
            self._wiki_outages = {k: t for k, t in self._wiki_outages.items() if t > now}
        self._wiki_outages[key] = now + WIKI_OUTAGE_RETRY_SECONDS

    def _wiki_outage_active(self, key: str | None, now: float | None = None) -> bool:
        """Whether the wiki recently failed to answer the detail lookup for `key` (a uuid,
        or "name:<lowercased name>")."""
        return bool(key) and self._wiki_outages.get(key, 0.0) > (time.monotonic() if now is None else now)

    def turret_gun_lookups_unanswered(self, ports: list[ShipPort]) -> int:
        """How many of this ship's turrets have no gun slots listed only because the wiki
        didn't answer their detail lookup - so the browser can say so."""
        return sum(1 for p in ports if p.needs_child_gun_ports and self._wiki_outage_active(p.equipped_uuid))

    async def candidates_for_port(
        self, port: ShipPort, *, category: str | None = None, limit: int | None = None,
        origin_id: int | None = None, time_budget: float | None = LOAD_TIME_BUDGET_SECONDS,
    ) -> list[dict]:
        """Every sold part that fits this slot under `category`, priced from UEX's own shop
        rows and ranked by the category's key stat, highest first (quantum speed, power
        generation, DPS... - see ship_part_display.ranking_stat), ties going to the closer
        shop, then the cheaper one. The owner asked for this over closest-first once the
        list got page buttons, since nothing is cut off any more.

        Ranking needs every part's wiki detail, and so does the fit check (UEX's size is
        unreliable - see bot/uex/ship_parts.py), so all of them are loaded, batched and
        cached for 24h: a category's first browse is the slow one. `limit` is only an
        optional cap after ranking; the browser passes none, since it pages. `time_budget`
        bounds the batched lookups (see LOAD_TIME_BUDGET_SECONDS); None means no limit."""
        category = category or port.uex_category
        catalog = await self.bot.uex.get_item_catalog()
        cheapest = cheapest_listing_by_item(await self.bot.uex.get_items_prices_all())
        candidates: list[dict] = []
        for row in candidate_items_for_port(catalog, port, category):
            try:
                id_item = int(row.get("id"))
            except (TypeError, ValueError):
                continue
            listing = cheapest.get(id_item)
            if listing is None:
                continue
            candidates.append({
                "_uex_row": row, "_uex_id": id_item, "_price_buy": float(listing["price_buy"]),
                "_id_terminal": listing.get("id_terminal"), "_terminal_name": listing.get("terminal_name"),
            })
        deadline = time.monotonic() + time_budget if time_budget is not None else None
        await self._attach_full_terminal_names(candidates)
        if origin_id is not None:
            await self._attach_distances(candidates, origin_id, deadline=deadline)
        await self._attach_details(candidates, deadline=deadline)
        await self._swap_in_fitting_variants([c for c in candidates if not tags_allow(c, port)], port,
                                             deadline=deadline)
        kept = sorted((c for c in candidates if _fits(c, port, category)), key=_rank_key)
        return PartCandidates(kept if limit is None else kept[:limit],
                              wiki_unavailable=sum(1 for c in candidates if c.get("_detail_unanswered")))

    async def _variants_cached(self, name: str) -> list[dict]:
        """Every wiki item sharing this shop name, cached for a day. Like _item_detail_cached,
        a lookup the wiki didn't answer raises WikiUnavailableError and is skipped for
        WIKI_OUTAGE_RETRY_SECONDS - it used to read as "no variants", which quietly dropped
        the part from a tag-restricted slot with no outage note (audit REL-15)."""
        key = f"variants:{name.lower()}"
        now = time.monotonic()
        cached = self._detail_cache.get(key)
        if cached and cached[0] > now:
            return cached[1]
        if self._wiki_outage_active(key, now):
            raise WikiUnavailableError(f"the wiki didn't answer for {name} a few minutes ago")
        try:
            rows = await self._wiki.find_item_variants_by_name(name)
        except WikiUnavailableError:
            self._record_wiki_outage(key, now)
            raise
        except WikiApiError:
            rows = []  # a definite answer, cached like one
        self._wiki_outages.pop(key, None)
        if len(self._detail_cache) >= DETAIL_CACHE_MAX:
            self._detail_cache.pop(next(iter(self._detail_cache)))
        rows = [_slim_detail(row) for row in rows if isinstance(row, dict)]
        self._detail_cache[key] = (now + DETAIL_CACHE_SECONDS, rows)
        return rows

    async def _swap_in_fitting_variants(
        self, candidates: list[dict], port: ShipPort, *, deadline: float | None = None,
    ) -> None:
        """A part that fails the tag check on the detail its UEX uuid led to may still be
        a generic part with ship-specific namesakes (see pick_fitting_variant) - replace
        its wiki fields with the variant that fits this ship, stats included. Only runs
        for the few parts that fail the tag check - which on a PDC or remote-turret slot is
        most of them (up to ~86), so lookups go DETAIL_BATCH_SIZE at a time, once per name,
        instead of all at once (audit REL-15)."""
        named = [c for c in candidates if c.get("name")]
        names = list(dict.fromkeys(str(c["name"]) for c in named))
        variants: dict[str, object] = {}
        for start in range(0, len(names), DETAIL_BATCH_SIZE):
            batch = names[start:start + DETAIL_BATCH_SIZE]
            results = await _gather_until(deadline, [lambda n=n: self._variants_cached(n) for n in batch])
            variants.update(zip(batch, results))
        for candidate in named:
            rows = variants.get(str(candidate["name"]))
            if isinstance(rows, (WikiUnavailableError, TimeoutError)):
                # Counted in the browser's "wiki didn't respond" note: this part may be
                # missing only because its fitting variant couldn't be looked up.
                candidate["_detail_unanswered"] = True
                continue
            variant = pick_fitting_variant(rows, port) if isinstance(rows, list) else None
            if variant is None:
                continue
            for key in [k for k in candidate if not k.startswith("_")]:
                del candidate[key]
            candidate.update({k: v for k, v in variant.items() if not k.startswith("_")})

    async def _attach_full_terminal_names(self, candidates: list[dict]) -> None:
        """/items_prices_all only carries a short terminal name ('Dumper's Area 18'); the
        collected terminal_reference table has the full 'Vendor - Place' one the display
        splits into Place (Vendor). Falls back to the short name if it isn't collected."""
        ids = [c["_id_terminal"] for c in candidates if c.get("_id_terminal") is not None]
        if not ids:
            return
        try:
            references = await self.bot.db.get_terminal_references_by_ids(ids)
        except Exception:
            logger.warning("Couldn't load full terminal names for ship parts", exc_info=True)
            return
        for candidate in candidates:
            reference = references.get(candidate.get("_id_terminal"))
            if reference and reference.get("terminal_name"):
                candidate["_terminal_name"] = reference["terminal_name"]

    async def _attach_details(self, candidates: list[dict], *, deadline: float | None = None) -> None:
        """Merge each candidate's wiki detail (stats, grade, maker) into it. A part the wiki
        has no detail for (its UEX uuid missing or not matching the wiki's) keeps a minimal
        name/size from UEX's own row and is still shown, instead of silently vanishing."""
        pending = [c for c in candidates if "_detail_loaded" not in c]
        for start in range(0, len(pending), DETAIL_BATCH_SIZE):
            batch = pending[start:start + DETAIL_BATCH_SIZE]
            results = await _gather_until(deadline, [lambda c=c: self._item_detail_cached(c["_uex_row"]) for c in batch])
            for candidate, detail in zip(batch, results):
                row = candidate["_uex_row"]
                if isinstance(detail, dict):
                    for key, value in detail.items():
                        candidate.setdefault(key, value)
                else:
                    candidate.setdefault("name", row.get("name"))
                    candidate.setdefault("size", row.get("size"))
                candidate["_detail_loaded"] = isinstance(detail, dict)
                # Out of time counts like no answer: it may still arrive, into the cache.
                candidate["_detail_unanswered"] = isinstance(detail, (WikiUnavailableError, TimeoutError))

    async def _attach_distances(self, candidates: list[dict], origin_id: int, *, deadline: float | None = None) -> None:
        """Real distance (gigameters) from the player's given location to each candidate's
        cheapest shop, batched via asyncio.gather - mirrors /ingame-item-finder's own
        _fetch_distances. None when UEX can't price the pair; never fabricated."""
        to_fetch = sorted({
            c["_id_terminal"] for c in candidates
            if c.get("_id_terminal") is not None and c["_id_terminal"] != origin_id
        })
        distances: dict[int, float | None] = {origin_id: 0.0}
        for start in range(0, len(to_fetch), DETAIL_BATCH_SIZE):
            batch = to_fetch[start:start + DETAIL_BATCH_SIZE]
            results = await _gather_until(
                deadline, [lambda tid=tid: self.bot.uex.get_terminal_distance(origin_id, tid) for tid in batch],
            )
            for tid, result in zip(batch, results):
                if isinstance(result, Exception) or not result:
                    distances[tid] = None
                    continue
                try:
                    distances[tid] = float(result.get("distance"))
                except (TypeError, ValueError):
                    distances[tid] = None
        for candidate in candidates:
            terminal_id = candidate.get("_id_terminal")
            candidate["_distance_gm"] = distances.get(terminal_id) if terminal_id is not None else None

    @app_commands.command(
        name="ship-parts-finder",
        description="Browse and compare real component options for one ship, and build a shopping list.",
    )
    @app_commands.describe(
        ship="Ship name, e.g. 'Avenger Stalker' or 'Cutlass Black' - autocompletes.",
        location="Terminal you're at - autocompletes.",
    )
    @app_commands.autocomplete(ship=ship_name_autocomplete, location=terminal_name_autocomplete)
    async def ship_parts_finder(self, interaction: discord.Interaction, ship: str, location: str) -> None:
        await interaction.response.defer(ephemeral=True)

        resolved_location = await self.bot.db.resolve_terminal_id_by_name(location)
        if resolved_location is None:
            await interaction.followup.send(
                f"Couldn't find a single terminal matching '{location}' - pick one from the autocomplete list.",
                ephemeral=True,
            )
            return

        try:
            vehicles = await self.bot.uex.get_vehicles()
        except UexApiError as exc:
            await interaction.followup.send(describe_uex_api_error(exc), ephemeral=True)
            return
        vehicle = resolve_ship(vehicles, ship)
        if vehicle is None:
            await interaction.followup.send(
                f"Couldn't find a single unambiguous match for '{ship}'. Try the full ship name "
                "and pick from the autocomplete suggestions.", ephemeral=True,
            )
            return

        view = await self._build_browser(vehicle, resolved_location)
        if isinstance(view, str):
            await interaction.followup.send(view, ephemeral=True)
            return

        # Browsing lives inside the same private thread as the locked-in list, not as an
        # ephemeral reply wherever the command happened to be run - live testing flagged
        # having to jump between the two as exactly the "too much mess in the chat" friction
        # this feature was meant to avoid in the first place.
        thread = await self.shopping._thread(interaction)
        if thread is None:
            await interaction.followup.send(
                "I couldn't open your private ship parts thread. Check thread permissions.", ephemeral=True,
            )
            return
        try:
            view.message = await thread.send(content=view.text(), view=view, allowed_mentions=NO_MENTIONS)
        except discord.HTTPException:
            logger.exception("Could not post the ship parts browser in thread %s", thread.id)
            view.stop()
            await interaction.followup.send(
                f"I couldn't post the browser in {thread.mention}. Please try again.", ephemeral=True,
            )
            return
        self._browsers[view.message.id] = view
        await interaction.followup.send(
            f"Opened {thread.mention} - browse **{vehicle.get('name')}**'s parts there.", ephemeral=True,
        )


    async def _ports_or_message(self, vehicle: dict) -> list[ShipPort] | str:
        """The ship's slots, or what to tell the player when the wiki can't give them."""
        try:
            return await self._ports_for_vehicle(vehicle)
        except WikiUnavailableError:
            return (f"Couldn't reach the Star Citizen Wiki for **{vehicle.get('name')}**'s component slots "
                    "right now - try again in a few minutes.")
        except WikiApiError:
            logger.warning("No usable wiki slot data for %s", vehicle.get("name"), exc_info=True)
            return f"The Star Citizen Wiki doesn't list usable component slots for **{vehicle.get('name')}** yet."

    async def _build_browser(self, vehicle: dict, origin_terminal: tuple[int, str]) -> PartsBrowserView | str:
        """A fresh browsing view for one ship, or the message to show instead. Shared by the
        command and ↻ Refresh."""
        ports = await self._ports_or_message(vehicle)
        if isinstance(ports, str):
            return ports
        grouped = group_ports_by_category(ports)
        if not grouped:
            return f"No supported component categories found for **{vehicle.get('name')}** yet."
        view = PartsBrowserView(self, vehicle, origin_terminal, grouped)
        view.turret_guns_unanswered = self.turret_gun_lookups_unanswered(ports)
        return view

    async def refresh_browser(
        self, interaction: discord.Interaction, id_vehicle: int, id_terminal: int, category: str | None,
    ) -> None:
        """Rebuild the browser on the clicked message: same ship, location and category."""
        await interaction.response.defer()
        try:
            await self._rebuild_browser(interaction, id_vehicle, id_terminal, category)
        except Exception:
            # discord.py only logs a dynamic item's exception, which would leave the player
            # on "thinking..." with no way forward.
            logger.exception("Ship parts browser refresh failed")
            try:
                await interaction.followup.send(
                    "Couldn't refresh this browser. Run `/ship-parts-finder` again - your locked-in parts are saved.",
                    ephemeral=True,
                )
            except discord.HTTPException:
                pass

    async def _rebuild_browser(
        self, interaction: discord.Interaction, id_vehicle: int, id_terminal: int, category: str | None,
    ) -> None:
        try:
            vehicles = await self.bot.uex.get_vehicles()
        except UexApiError as exc:
            await interaction.followup.send(describe_uex_api_error(exc), ephemeral=True)
            return
        vehicle = next((v for v in vehicles if str(v.get("id")) == str(id_vehicle)), None)
        if vehicle is None:
            await interaction.followup.send(
                "That ship isn't in UEX's list any more. Run `/ship-parts-finder` again.", ephemeral=True,
            )
            return
        # Only the id is used downstream; the location's name isn't shown by the browser.
        view = await self._build_browser(vehicle, (id_terminal, ""))
        if isinstance(view, str):
            await interaction.followup.send(view, ephemeral=True)
            return
        if category and category in view.grouped_ports:
            await view.restore_category(category)
        message = interaction.message
        # Retire the view being replaced BEFORE the edit registers the new one: stopping it
        # afterwards would drop the new view's own message registration in discord.py.
        old = self._browsers.pop(message.id, None)
        if old is not None:
            old.stop()
        view.message = message
        self._browsers[message.id] = view
        await interaction.edit_original_response(content=view.text(), view=view)

    # -- /ship-loadout --------------------------------------------------------------------------

    @app_commands.command(
        name="ship-loadout",
        description="Recommend a part for every slot on one ship - Balanced, Stealth, Tank or Budget.",
    )
    @app_commands.describe(
        ship="Ship name, e.g. 'Avenger Titan' or 'Gladius' - autocompletes.",
        profile="What to favour. Balanced if left out; you can switch on the loadout itself.",
        location="Terminal you're at, so ties go to the nearest shop - autocompletes. Optional.",
    )
    @app_commands.choices(profile=[
        app_commands.Choice(name=f"{profile} - {PROFILE_BLURBS[profile]}"[:100], value=profile) for profile in PROFILES
    ])
    @app_commands.autocomplete(ship=ship_name_autocomplete, location=terminal_name_autocomplete)
    async def ship_loadout(
        self, interaction: discord.Interaction, ship: str, profile: app_commands.Choice[str] | None = None,
        location: str | None = None,
    ) -> None:
        await interaction.response.defer(ephemeral=True)
        origin = None
        if location:
            origin = await self.bot.db.resolve_terminal_id_by_name(location)
            if origin is None:
                await interaction.followup.send(
                    f"Couldn't find a single terminal matching '{location}' - pick one from the autocomplete list, "
                    "or leave location out.", ephemeral=True,
                )
                return
        try:
            vehicles = await self.bot.uex.get_vehicles()
        except UexApiError as exc:
            await interaction.followup.send(describe_uex_api_error(exc), ephemeral=True)
            return
        vehicle = resolve_ship(vehicles, ship)
        if vehicle is None:
            await interaction.followup.send(
                f"Couldn't find a single unambiguous match for '{ship}'. Try the full ship name "
                "and pick from the autocomplete suggestions.", ephemeral=True,
            )
            return
        await self._post_loadout(interaction, vehicle, origin, profile.value if profile else DEFAULT_PROFILE)

    async def open_loadout(
        self, interaction: discord.Interaction, vehicle: dict, origin_terminal: tuple[int, str],
    ) -> None:
        """The parts browser's "Recommend a loadout": the ship and location being browsed. A
        browser rebuilt by ↻ Refresh has only the location's id, so its name is looked up."""
        await interaction.response.defer(ephemeral=True, thinking=True)
        if not origin_terminal[1]:
            origin_terminal = (origin_terminal[0], await self._terminal_name(origin_terminal[0]))
        await self._post_loadout(interaction, vehicle, origin_terminal, DEFAULT_PROFILE)

    async def _terminal_name(self, id_terminal: int) -> str:
        try:
            references = await self.bot.db.get_terminal_references_by_ids([id_terminal])
        except Exception:
            logger.warning("Couldn't look up terminal %s's name for a loadout", id_terminal, exc_info=True)
            return ""
        return (references.get(id_terminal) or {}).get("terminal_name") or ""

    async def _post_loadout(
        self, interaction: discord.Interaction, vehicle: dict, origin: tuple[int, str] | None, profile: str,
    ) -> None:
        """Build the loadout and post it in the player's private ship parts thread, beside the
        list "Add all" fills, with an ephemeral pointer to it. The interaction is deferred.

        In a DM there's no thread to post in: said before the build, which is the slowest work
        in the feature (every slot's parts and stock parts), not after it."""
        if interaction.guild_id is None:
            await interaction.followup.send(
                "Ship loadouts are available in a server: they're posted in your private ship parts thread there.",
                ephemeral=True,
            )
            return
        view = await self._build_loadout(vehicle, origin, profile, interaction.user.id)
        if isinstance(view, str):
            await interaction.followup.send(view, ephemeral=True)
            return
        thread = await self.shopping._thread(interaction)
        if thread is None:
            view.stop()
            await interaction.followup.send(
                "I couldn't open your private ship parts thread. Check thread permissions.", ephemeral=True,
            )
            return
        try:
            view.message = await thread.send(content=view.text(), view=view, allowed_mentions=NO_MENTIONS)
        except discord.HTTPException:
            logger.exception("Could not post a ship loadout in thread %s", thread.id)
            view.stop()
            await interaction.followup.send(
                f"I couldn't post the loadout in {thread.mention}. Please try again.", ephemeral=True,
            )
            return
        await interaction.followup.send(
            f"Posted a **{profile}** loadout for **{vehicle.get('name')}** in {thread.mention}.", ephemeral=True,
        )

    async def _build_loadout(
        self, vehicle: dict, origin: tuple[int, str] | None, profile: str, owner_id: int,
    ) -> LoadoutView | str:
        """A loadout view for one ship, or the message to show instead. Every lookup shares
        one deadline (LOADOUT_TIME_BUDGET_SECONDS)."""
        deadline = time.monotonic() + LOADOUT_TIME_BUDGET_SECONDS
        ports = await self._ports_or_message(vehicle)
        if isinstance(ports, str):
            return ports
        slots, stock_unanswered, slots_missing = await self._loadout_slots(vehicle, ports, deadline)
        # A turret whose detail the wiki didn't answer has no gun slots in `ports` at all
        # (_with_child_gun_ports): said, as the browser says it, rather than read as complete.
        turrets_unanswered = self.turret_gun_lookups_unanswered(ports)
        groups = group_slots(slots)
        if not groups:
            if slots_missing or turrets_unanswered:
                return (f"Couldn't reach the Star Citizen Wiki for **{vehicle.get('name')}**'s stock parts "
                        "right now - try again in a few minutes.")
            return f"No supported component slots found for **{vehicle.get('name')}** yet."
        try:
            candidates, parts_unanswered = await self._loadout_candidates(groups, origin, deadline)
        except UexApiError as exc:
            return describe_uex_api_error(exc)
        return LoadoutView(
            self, vehicle, origin, groups, candidates, owner_id=owner_id, profile=profile,
            stock_unanswered=stock_unanswered, slots_missing=slots_missing, parts_unanswered=parts_unanswered,
            turrets_unanswered=turrets_unanswered,
        )

    async def _loadout_slots(
        self, vehicle: dict, ports: list[ShipPort], deadline: float,
    ) -> tuple[list[LoadoutSlot], int, int]:
        """Every slot the loadout fills, in the ship's own order, each with its stock part's
        wiki detail. Also returns how many slots' stock parts the wiki didn't answer for (kept,
        with no stock comparison) and how many gun hardpoints were left out because it didn't
        answer for their stock mount (whose own gun slot decides the gun's size).

        A gun hardpoint keeps its stock mount (the owner's call): under a gimbal, the slot is
        the gimbal's own gun slot (loadout_gun_ports), and the gun in it is named only by the
        wiki's single-vehicle endpoint (_vehicle_stock_uuids). A mount inside a stock mount (a
        turret holding gimbals) is kept too, down to the gun slot, up to MAX_MOUNT_DEPTH levels
        - its own mount rank is never compared with a gun's DPS. A turret whose own gun slots
        are in the loadout is kept the same way: recommending a new turret beside guns sized
        for the stock one would contradict itself. A slot the game locks has no category and
        is skipped, as the browser skips it."""
        tree_result, = await _gather_until(deadline, [lambda: self._vehicle_stock_uuids(vehicle)])
        tree = tree_result if isinstance(tree_result, dict) else None
        filled = [(port, category) for port in ports if (category := slot_category(port)) is not None]
        turrets_with_guns = {port.name.rsplit("/", 1)[0] for port, category in filled
                             if category == GUNS_CATEGORY and "/" in port.name}
        filled = [(port, category) for port, category in filled
                  if not (category == MOUNTS_CATEGORY and port.name in turrets_with_guns)]

        def stock_uuid(port: ShipPort) -> str | None:
            return (tree or {}).get(port.name) or port.equipped_uuid

        def stock_state(path: str, uuid: str | None, results: dict, *, nested: bool) -> tuple[dict | None, bool, bool]:
            """(detail, unknown, unanswered) for one slot's stock item."""
            if nested and tree is None:
                # Only the single-vehicle tree names what's inside a mount, and it didn't load.
                return None, True, True
            if nested and path not in tree:
                # The tree loaded but doesn't list this slot (its mount's row had no ports):
                # unknown, never an empty slot - asking again won't change it.
                return None, True, False
            if not uuid:
                return None, False, False  # an empty slot
            result = results.get(uuid)
            if isinstance(result, dict):
                return result, False, False
            # None is a part the wiki doesn't have: unknown, but asking again won't help.
            return None, True, isinstance(result, BaseException)

        def left_out(port: ShipPort) -> bool:
            # A gun hardpoint whose stock item the wiki didn't answer for, when that item could
            # be a mount: the mount decides the gun's size, so there's no slot to fill. One that
            # only takes a gun is still its own size - kept, with its stock part unknown.
            return top[port.name][2] and MOUNTS_CATEGORY in port.categories

        top_results = await self._stock_details([stock_uuid(port) for port, _ in filled], deadline)
        # A turret's own gun slot (child_gun_ports, e.g. the Perseus's) has no stock uuid of its own.
        top = {port.name: stock_state(port.name, stock_uuid(port), top_results,
                                      nested="/" in port.name and not port.equipped_uuid)
               for port, _ in filled}
        gun_slots = {port.name: loadout_gun_ports(port, top[port.name][0])
                     for port, category in filled if category == GUNS_CATEGORY and not left_out(port)}

        # The stock item in each mount's gun slot; where that is itself a mount, its own gun
        # slots replace it and are looked up in turn.
        inner_results: dict[str, object] = {}
        pending = [gun for name, guns in gun_slots.items() for gun in guns if gun.name != name]
        for _ in range(MAX_MOUNT_DEPTH):
            if not pending:
                break
            inner_results.update(await self._stock_details([(tree or {}).get(gun.name) for gun in pending], deadline))
            deeper = {gun.name: loadout_gun_ports(gun, inner_stock) for gun in pending
                      if is_gun_mount(inner_stock := stock_state(gun.name, (tree or {}).get(gun.name), inner_results,
                                                            nested=True)[0])}
            gun_slots = {name: [inner for gun in guns for inner in deeper.get(gun.name, [gun])]
                         for name, guns in gun_slots.items()}
            pending = [inner for inners in deeper.values() for inner in inners]

        slots: list[LoadoutSlot] = []
        stock_unanswered = slots_missing = 0
        for port, category in filled:
            detail, unknown, unanswered = top[port.name]
            if category != GUNS_CATEGORY:
                slots.append(LoadoutSlot(port, category, port.name, detail, unknown))
                stock_unanswered += unanswered
                continue
            if left_out(port):
                slots_missing += 1
                continue
            guns = gun_slots[port.name]
            for gun in guns:
                if gun is port:  # a fixed gun, or an empty hardpoint: the hardpoint itself
                    slots.append(LoadoutSlot(port, GUNS_CATEGORY, port.name, detail, unknown))
                    stock_unanswered += unanswered
                    continue
                gun_detail, gun_unknown, gun_unanswered = stock_state(gun.name, (tree or {}).get(gun.name),
                                                                       inner_results, nested=True)
                if is_gun_mount(gun_detail):
                    # Still a mount past MAX_MOUNT_DEPTH: not a gun to compare against.
                    gun_detail, gun_unknown = None, True
                slots.append(LoadoutSlot(gun, GUNS_CATEGORY, gun_entry_port_name(port, gun, len(guns)),
                                         gun_detail, gun_unknown))
                stock_unanswered += gun_unanswered
        return slots, stock_unanswered, slots_missing

    async def _stock_details(self, uuids: list[str | None], deadline: float) -> dict[str, object]:
        """uuid -> its wiki detail (None when the wiki has no such item), or the exception
        when the wiki didn't answer in time. Batched like the candidates' own details."""
        unique = list(dict.fromkeys(uuid for uuid in uuids if uuid))
        results: dict[str, object] = {}
        for start in range(0, len(unique), DETAIL_BATCH_SIZE):
            batch = unique[start:start + DETAIL_BATCH_SIZE]
            answers = await _gather_until(deadline, [lambda u=u: self._item_detail_cached({"uuid": u}) for u in batch])
            results.update(zip(batch, answers))
        return results

    async def _vehicle_stock_uuids(self, vehicle: dict) -> dict[str, str] | None:
        """Port path -> stock item uuid for one ship, the items inside its stock mounts
        included (stock_uuids_by_port over WikiApiClient.get_vehicle_stock_ports), cached for
        DETAIL_CACHE_SECONDS. None when it isn't known - the wiki didn't answer, or didn't
        resolve the ship by UEX's name or full name - so a gun inside a kept mount reads as
        "stock unknown", never as an empty slot."""
        id_vehicle = _vehicle_id(vehicle)
        now = time.monotonic()
        cached = self._stock_trees.get(id_vehicle)
        if cached and cached[0] > now:
            return cached[1]
        names = [vehicle.get("name"), vehicle.get("name_full")]
        for name in dict.fromkeys(n.strip() for n in names if isinstance(n, str) and n.strip()):
            try:
                raw_ports = await self._wiki.get_vehicle_stock_ports(name)
            except WikiApiError as exc:
                logger.info("No stock loadout from the wiki for %r: %s", name, exc)
                return None
            if raw_ports:
                uuids = stock_uuids_by_port(raw_ports)
                if len(self._stock_trees) >= DETAIL_CACHE_MAX:
                    self._stock_trees.pop(next(iter(self._stock_trees)))
                self._stock_trees[id_vehicle] = (now + DETAIL_CACHE_SECONDS, uuids)
                return uuids
        return None

    async def _loadout_candidates(
        self, groups: list[SlotGroup], origin: tuple[int, str] | None, deadline: float,
    ) -> tuple[dict[tuple, list[dict]], int]:
        """Each distinct slot shape's candidates (fit_key -> candidates_for_port), loaded once
        however many slots share it, and how many sold parts the wiki didn't answer for."""
        candidates: dict[tuple, list[dict]] = {}
        # Per category: every slot shape in one category draws on the same sold parts, so
        # adding them up would count a part the wiki didn't answer for once per shape.
        unanswered: dict[str, int] = {}
        for group in groups:
            if group.fit_key in candidates:
                continue
            found = await self.candidates_for_port(
                group.port, category=group.category, origin_id=origin[0] if origin else None,
                time_budget=max(0.0, deadline - time.monotonic()),
            )
            candidates[group.fit_key] = found
            unanswered[group.category] = max(unanswered.get(group.category, 0), getattr(found, "wiki_unavailable", 0))
        return candidates, sum(unanswered.values())


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(ShipPartsFinder(bot))
