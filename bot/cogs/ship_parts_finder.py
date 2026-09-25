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

Candidates are priced, located, and sorted closest-first BEFORE being cut to the display
limit, so the closest shops can't be cut off unseen - the repo's "filter before
truncating" lesson. Display labels live in bot/uex/ship_part_display.py.

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

from bot.cogs.prices import terminal_name_autocomplete
from bot.cogs.ships import ship_name_autocomplete
from bot.uex.exceptions import UexApiError, describe_uex_api_error
from bot.uex.ship_part_display import (
    format_part_page,
    format_port_label,
    list_shared,
    paginate_parts,
    ranked_by_label,
    ranking_stat,
    shop_text,
)
from bot.uex.ship_parts import (
    GUNS_CATEGORY,
    MOUNTS_CATEGORY,
    ShipPort,
    candidate_items_for_port,
    category_label,
    cheapest_listing_by_item,
    group_ports_by_category,
    parse_ports,
    part_fits_port,
    pick_fitting_variant,
    tags_allow,
)
from bot.uex.ships import resolve_ship
from bot.wiki_api import WikiApiClient, WikiApiError

logger = logging.getLogger("uexbot.ship_parts_finder")
NO_MENTIONS = discord.AllowedMentions.none()

REFERENCE_REFRESH_HOURS = 24
# Component hardware prices move far less often than commodity market prices (deliberate
# design decision, not a placeholder) - see module docstring.
DETAIL_CACHE_SECONDS = 24 * 3600
# Every sold part in a browsed category gets its detail loaded now (to rank it), so the
# cache has to hold them all: about 370 sold parts across the 8 categories, plus
# name-lookup misses and variant lists. Entries are slimmed first (_slim_detail).
DETAIL_CACHE_MAX = 1000
# Wiki detail fields no display or fit check reads - dropped before caching, so a full
# cache stays small on the Pi.
_UNUSED_DETAIL_KEYS = frozenset({
    "images", "description", "description_data", "shops", "uex_prices", "blueprint",
    "variants", "entity_tag_map", "entity_tags", "interactions", "dimension", "base_variant",
})
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

    async def _thread(self, interaction: discord.Interaction) -> discord.Thread | None:
        if interaction.guild_id is None:
            return None
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

    async def render(self, user_id: int, guild_id: int) -> list[str]:
        entries = await self.bot.db.get_ship_parts_entries(user_id, guild_id)
        if not entries:
            return ["**Ship parts list**\nNo parts locked in yet."]
        lines = ["**Ship parts list**"]
        current_ship = None
        for entry in entries:
            if entry["vehicle_name"] != current_ship:
                current_ship = entry["vehicle_name"]
                lines.extend(["", f"**{current_ship}**"])
            parts = [f"{entry['price_buy']:,.0f} aUEC" if entry.get("price_buy") is not None else "no shop price on record"]
            shop = shop_text(entry.get("terminal_name"))
            if shop:
                parts.append(shop)
            lines.append(f"• {_entry_slot_label(entry)}: **{entry['item_name']}** — {' · '.join(parts)}")
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


class _RemoveEntryView(discord.ui.View):
    def __init__(self, service: "ShipPartsShoppingService", entries: list[dict]) -> None:
        super().__init__(timeout=300)
        self.add_item(_RemoveEntrySelect(service, entries))


class ShipPartsShoppingView(discord.ui.View):
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
            await interaction.followup.send("List refreshed.", ephemeral=True)

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
        await interaction.response.send_message(
            f"Pick a part to remove{note}:", view=_RemoveEntryView(self.service, entries), ephemeral=True)

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


class PartsBrowserView(discord.ui.View):
    """Transient (NOT persistent, matches blueprint_planner.py's CraftConfigView) - browsing
    a ship's slots doesn't need to survive a restart, only a locked-in choice does. If this
    view goes stale on a restart, the user just re-runs /ship-parts-finder.

    Category -> [slot, when a category has more than one physical port] -> part, since a
    ship can have several independent slots in one category (e.g. two differently-sized
    turrets) that each need their own choice - collapsing to the category's first port
    silently made those unreachable, a real defect an outside audit caught before merge."""
    def __init__(
        self, cog: "ShipPartsFinder", vehicle: dict, origin_terminal: tuple[int, str],
        grouped_ports: dict[str, list[ShipPort]],
    ) -> None:
        super().__init__(timeout=600)
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
        self.category_select = _CategorySelect(self)
        self.add_item(self.category_select)

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

    def text(self) -> str:
        header = f"**{self.vehicle.get('name')}** parts - pick a category to compare real options."
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
            lines.extend(["", f"Page {self.page + 1} of {len(self.pages)}"])
        return "\n".join(lines)

    def _set_candidates(self, candidates: list[dict]) -> None:
        self.candidates = candidates
        port = self.selected_port
        fixed = port.size_min if port is not None and port.size_min == port.size_max else None
        self._header_shared, self._shared = list_shared(candidates, fixed)
        self.pages = paginate_parts(candidates, shared=self._shared)
        self.page = 0
        self._show_page()

    def _show_page(self) -> None:
        """Rebuild the page's own dropdown (only this page's parts, so it can never hit
        Discord's 25-option limit however big the slot) and the page buttons."""
        self._remove_items(_PartSelect, _PageButton)
        if not self.pages:
            return
        self.add_item(_PartSelect(self, self.pages[self.page]))
        if len(self.pages) > 1:
            self.add_item(_PageButton(self, -1))
            self.add_item(_PageButton(self, +1))

    async def turn_page(self, interaction: discord.Interaction, delta: int) -> None:
        self.page = max(0, min(len(self.pages) - 1, self.page + delta))
        self._show_page()
        await interaction.response.edit_message(content=self.text(), view=self)

    def _remove_items(self, *types: type) -> None:
        for child in [c for c in self.children if isinstance(c, types)]:
            self.remove_item(child)

    async def show_category(self, interaction: discord.Interaction, category: str) -> None:
        self.category = category
        self.selected_port = None
        self.selected_candidate = None
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
        self.selected_port = port
        candidates: list[dict] = []
        if port is not None:
            try:
                candidates = await self.cog.candidates_for_port(
                    port, category=self.category, origin_id=self.origin_terminal[0],
                )
            except (UexApiError, WikiApiError) as exc:
                self._set_candidates([])
                await interaction.edit_original_response(content=f"Couldn't load {self.category} options: {exc}", view=self)
                return
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
                   for i, port in enumerate(ports[:25])]
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


class ShipPartsFinder(commands.Cog):
    def __init__(self, bot: commands.Bot, *, wiki_client: WikiApiClient | None = None, start_refresh: bool = True) -> None:
        self.bot = bot
        self._wiki = wiki_client or WikiApiClient()
        self._detail_cache: dict[str, tuple[float, dict]] = {}
        self.shopping = ShipPartsShoppingService(bot)
        if start_refresh:
            self.refresh_reference.start()

    async def cog_load(self) -> None:
        self.bot.add_view(ShipPartsShoppingView(self.shopping))

    def cog_unload(self) -> None:
        self.refresh_reference.cancel()
        try:
            asyncio.get_running_loop().create_task(self._wiki.aclose())
        except RuntimeError:
            pass

    @tasks.loop(hours=REFERENCE_REFRESH_HOURS)
    async def refresh_reference(self) -> None:
        try:
            vehicles = await self.bot.uex.get_vehicles()
        except UexApiError as exc:
            logger.warning("Ship parts reference refresh could not load the vehicle list: %s", exc)
            return
        for vehicle in vehicles:
            try:
                id_vehicle = int(vehicle.get("id"))
                name = vehicle.get("name")
                if not name:
                    continue
                ports = await self._wiki_ports(vehicle)
                await self.bot.db.replace_ship_parts_reference(
                    id_vehicle, name,
                    [{"name": p.name, "port_type": p.port_type, "size_min": p.size_min, "size_max": p.size_max,
                      "accepts_guns": p.accepts_guns, "port_tags": sorted(p.tags)} for p in ports],
                )
            except (TypeError, ValueError, WikiApiError):
                logger.warning("Ship parts reference refresh failed for vehicle %r", vehicle.get("name"))
            except Exception:
                logger.exception("Ship parts reference refresh failed unexpectedly for vehicle %r", vehicle.get("name"))

    @refresh_reference.before_loop
    async def before_refresh_reference(self) -> None:
        await self.bot.wait_until_ready()

    async def _ports_for_vehicle(self, vehicle: dict) -> list[ShipPort]:
        id_vehicle = int(vehicle["id"])
        rows = await self.bot.db.get_ship_parts_reference(id_vehicle)
        if rows:
            return [ShipPort(name=r["port_name"], port_type=r["port_type"], size_min=r["size_min"], size_max=r["size_max"],
                             accepts_guns=bool(r.get("accepts_guns")),
                             tags=frozenset((r.get("port_tags") or "").split()))
                    for r in rows]
        # Collector hasn't run for this ship yet (fresh deploy, or a ship added since the
        # last daily cycle) - fall back to a live lookup rather than a dead end.
        return await self._wiki_ports(vehicle)

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
        uuid for most radars doesn't exist on the wiki, but the name does). Misses are
        cached too, so a part the wiki genuinely lacks isn't re-looked-up every browse."""
        item_uuid = uex_row.get("uuid") or ""
        name = (uex_row.get("name") or "").strip()
        key = item_uuid or f"name:{name.lower()}"
        now = time.monotonic()
        cached = self._detail_cache.get(key)
        if cached and cached[0] > now:
            return cached[1]
        detail = None
        try:
            if item_uuid:
                detail = await self._wiki.get_item_detail(item_uuid)
        except WikiApiError:
            detail = None
        if detail is None and name:
            try:
                detail = await self._wiki.find_item_detail_by_name(name)
            except WikiApiError:
                detail = None
        if len(self._detail_cache) >= DETAIL_CACHE_MAX:
            self._detail_cache.pop(next(iter(self._detail_cache)))
        detail = _slim_detail(detail)
        self._detail_cache[key] = (now + DETAIL_CACHE_SECONDS, detail)
        return detail

    async def candidates_for_port(
        self, port: ShipPort, *, category: str | None = None, limit: int | None = None,
        origin_id: int | None = None,
    ) -> list[dict]:
        """Every sold part that fits this slot under `category`, priced from UEX's own shop
        rows and ranked by the category's key stat, highest first (quantum speed, power
        generation, DPS... - see ship_part_display.ranking_stat), ties going to the closer
        shop, then the cheaper one. The owner asked for this over closest-first once the
        list got page buttons, since nothing is cut off any more.

        Ranking needs every part's wiki detail, and so does the fit check (UEX's size is
        unreliable - see bot/uex/ship_parts.py), so all of them are loaded, batched and
        cached for 24h: a category's first browse is the slow one. `limit` is only an
        optional cap after ranking; the browser passes none, since it pages."""
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
        await self._attach_full_terminal_names(candidates)
        if origin_id is not None:
            await self._attach_distances(candidates, origin_id)
        await self._attach_details(candidates)
        await self._swap_in_fitting_variants([c for c in candidates if not tags_allow(c, port)], port)
        kept = sorted((c for c in candidates if _fits(c, port, category)), key=_rank_key)
        return kept if limit is None else kept[:limit]

    async def _variants_cached(self, name: str) -> list[dict]:
        key = f"variants:{name.lower()}"
        now = time.monotonic()
        cached = self._detail_cache.get(key)
        if cached and cached[0] > now:
            return cached[1]
        try:
            rows = await self._wiki.find_item_variants_by_name(name)
        except WikiApiError:
            return []
        if len(self._detail_cache) >= DETAIL_CACHE_MAX:
            self._detail_cache.pop(next(iter(self._detail_cache)))
        rows = [_slim_detail(row) for row in rows if isinstance(row, dict)]
        self._detail_cache[key] = (now + DETAIL_CACHE_SECONDS, rows)
        return rows

    async def _swap_in_fitting_variants(self, candidates: list[dict], port: ShipPort) -> None:
        """A part that fails the tag check on the detail its UEX uuid led to may still be
        a generic part with ship-specific namesakes (see pick_fitting_variant) - replace
        its wiki fields with the variant that fits this ship, stats included. Only runs
        for the few parts that fail the tag check."""
        named = [c for c in candidates if c.get("name")]
        results = await asyncio.gather(*(self._variants_cached(str(c["name"])) for c in named),
                                       return_exceptions=True)
        for candidate, rows in zip(named, results):
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

    async def _attach_details(self, candidates: list[dict]) -> None:
        """Merge each candidate's wiki detail (stats, grade, maker) into it. A part the wiki
        has no detail for (its UEX uuid missing or not matching the wiki's) keeps a minimal
        name/size from UEX's own row and is still shown, instead of silently vanishing."""
        pending = [c for c in candidates if "_detail_loaded" not in c]
        for start in range(0, len(pending), DETAIL_BATCH_SIZE):
            batch = pending[start:start + DETAIL_BATCH_SIZE]
            results = await asyncio.gather(
                *(self._item_detail_cached(c["_uex_row"]) for c in batch), return_exceptions=True,
            )
            for candidate, detail in zip(batch, results):
                row = candidate["_uex_row"]
                if isinstance(detail, dict):
                    for key, value in detail.items():
                        candidate.setdefault(key, value)
                else:
                    candidate.setdefault("name", row.get("name"))
                    candidate.setdefault("size", row.get("size"))
                candidate["_detail_loaded"] = isinstance(detail, dict)

    async def _attach_distances(self, candidates: list[dict], origin_id: int) -> None:
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
            results = await asyncio.gather(
                *(self.bot.uex.get_terminal_distance(origin_id, tid) for tid in batch), return_exceptions=True,
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

        try:
            ports = await self._ports_for_vehicle(vehicle)
        except WikiApiError as exc:
            await interaction.followup.send(f"Couldn't load {vehicle.get('name')}'s components: {exc}", ephemeral=True)
            return
        grouped = group_ports_by_category(ports)
        if not grouped:
            await interaction.followup.send(
                f"No supported component categories found for **{vehicle.get('name')}** yet.", ephemeral=True,
            )
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
        view = PartsBrowserView(self, vehicle, resolved_location, grouped)
        await thread.send(content=view.text(), view=view, allowed_mentions=NO_MENTIONS)
        await interaction.followup.send(
            f"Opened {thread.mention} - browse **{vehicle.get('name')}**'s parts there.", ephemeral=True,
        )


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(ShipPartsFinder(bot))
