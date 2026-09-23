"""Ship Parts Finder (/ship-parts-finder, in design - not yet finalized): for one ship,
browse its real component slots (power plant, coolers, shields, quantum drive, turrets,
missile racks, radar, life support - see bot/uex/ship_parts.py's PORT_TYPE_TO_UEX_CATEGORY
for the full, deliberately partial list) and lock in a specific part per slot into a
private, persistent shopping list - same spirit as the blueprint shopping list
(bot/cogs/blueprint_planner.py), whose ShoppingService/ShoppingView pattern this reuses
directly rather than reinventing.

Loadout data (which slots exist, what size/type fits) comes from the Star Citizen Wiki API
(api.star-citizen.wiki) - UEX has no equivalent (its own id_vehicle FK on /items is
cosmetics-only, confirmed empirically against live catalog data). Candidate parts for a
slot are found in UEX's own item catalog (category+size match) and filtered to ones UEX
reports a real, current shop listing for. Stats and price/terminal for each candidate come
from the wiki's own /items/{uuid} detail endpoint, which embeds UEX-sourced pricing
directly (uex_prices.purchase) - verified live that its terminal_id values are UEX's own
real terminal ids, not an independent copy. Component hardware prices move far less often
than commodity market prices, so this embedded copy is used directly rather than a second
live UEX price lookup per candidate.

Every mapped category's per-component stat sub-block has been checked live except
LifeSupportGenerator, which genuinely has none (confirmed, not a lookup gap - see
_format_stat_block's docstring). PowerPlant/Cooler/Shield/QuantumDrive/Turret/Radar key
their block by the item's own `type` field; MissileLauncher keys it by `sub_type` instead
("MissileRack" -> "missile_rack") - _format_stat_block checks both rather than hardcoding
one shape.

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
from bot.uex.ship_parts import ShipPort, candidate_items_for_port, filter_to_sold_items, group_ports_by_category, parse_ports, sold_item_ids
from bot.uex.ships import resolve_ship
from bot.wiki_api import WikiApiClient, WikiApiError

logger = logging.getLogger("uexbot.ship_parts_finder")
NO_MENTIONS = discord.AllowedMentions.none()

REFERENCE_REFRESH_HOURS = 24
# Component hardware prices move far less often than commodity market prices (deliberate
# design decision, not a placeholder) - see module docstring.
DETAIL_CACHE_SECONDS = 24 * 3600
DETAIL_CACHE_MAX = 500
# One wiki call per candidate item - batched so a slot with many real candidates doesn't
# serialize dozens of live lookups, same pattern as /ingame-item-finder's _fetch_distances.
DETAIL_BATCH_SIZE = 8
MAX_CANDIDATES_SHOWN = 15


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


def _snake_case(camel: str) -> str:
    return "".join(["_" + c.lower() if c.isupper() else c for c in camel]).lstrip("_")


def _format_stat_block(detail: dict) -> str:
    """Whatever functional stat sub-object the wiki has for this component, shown as plain
    key: value lines. Checked live for every mapped category (module docstring has the
    verification note): PowerPlant/Cooler/Shield/QuantumDrive/Turret/Radar all key their
    stat block by the item's own `type` field snake-cased (e.g. "power_plant") - but
    MissileLauncher's is keyed by `sub_type` instead ("MissileRack" -> "missile_rack"), and
    LifeSupportGenerator genuinely has no dedicated stat block at all (confirmed live, not
    a lookup bug - only general physical properties like mass/dimension exist for it), so
    this degrades to no stat line for that one category rather than guessing at a key that
    isn't there.

    Quantum drives are a special case, found live in testing: the single most important
    stat - travel speed - lives nested two levels deep
    (quantum_drive.standard_jump.drive_speed_formatted), not as a top-level scalar like
    every other category's headline stat. The generic top-level-only scan below explicitly
    skips dict/list values, so it silently dropped speed entirely until this was added -
    confirmed missing, not just unformatted."""
    for raw_key in (detail.get("type"), detail.get("sub_type")):
        if not raw_key:
            continue
        key = _snake_case(str(raw_key))
        block = detail.get(key)
        if not isinstance(block, dict):
            continue
        lines: list[str] = []
        if key == "quantum_drive":
            standard_jump = block.get("standard_jump")
            if isinstance(standard_jump, dict) and standard_jump.get("drive_speed_formatted"):
                lines.append(f"speed: {standard_jump['drive_speed_formatted']}")
            travel_time = block.get("travel_time_10gm")
            if isinstance(travel_time, dict) and travel_time.get("formatted"):
                lines.append(f"10 Gm in: {travel_time['formatted']}")
        lines.extend(
            f"{k}: {v}" for k, v in block.items() if v is not None and not isinstance(v, (dict, list))
        )
        if lines:
            return " · ".join(lines[:4])
    return ""


def _cheapest_purchase(detail: dict) -> dict:
    purchases = ((detail.get("uex_prices") or {}).get("purchase")) or []
    if not purchases:
        return {}
    return min(purchases, key=lambda p: p.get("price_buy") if p.get("price_buy") is not None else float("inf"))


def _format_port_label(port: ShipPort) -> str:
    """A physical slot's own name, e.g. "hardpoint_weapon_gun_class1_left_wing", made
    readable - not curated per-ship, just a mechanical cleanup of the raw wiki port name."""
    label = port.name.removeprefix("hardpoint_").replace("_", " ").strip().title()
    size = f"S{port.size_min}" if port.size_min == port.size_max else f"S{port.size_min}-{port.size_max}"
    return f"{label or port.name} ({size})"


def _format_candidate_line(detail: dict, *, selected: bool = False) -> str:
    """'**Name** — Price aUEC @ Shop · Distance' as the primary line, matching
    /ingame-item-finder's own proven plain-text format (bot/uex/item_finder.py's
    format_item_listing_line) rather than a monospace column table - that table shipped
    for this exact bot once, broke live once real name-length variance showed up (a fixed
    width truncated distinct names to identical text; widening it made Discord wrap the
    row instead of scrolling, breaking alignment anyway), and was replaced with plain text
    for good. A second, shorter line carries the details a shop listing doesn't need but a
    component comparison does: size/grade/manufacturer and the stat highlight."""
    name = detail.get("name") or "Unknown"
    cheapest = _cheapest_purchase(detail)
    price = cheapest.get("price_buy")
    terminal = cheapest.get("terminal_name")
    distance = detail.get("_distance_gm")
    marker = "✅ " if selected else ""
    price_part = f"{price:,.0f} aUEC @ {terminal}" if price is not None and terminal else "price unknown"
    distance_part = f"{distance:.1f} Gm" if distance is not None else "distance unknown"
    primary = f"{marker}**{name}** — {price_part} · {distance_part}"

    details = []
    size = detail.get("size")
    if size is not None:
        details.append(f"S{size}")
    grade = detail.get("grade")
    if grade:
        details.append(f"Grade {grade}")
    manufacturer = (detail.get("manufacturer") or {}).get("name") if isinstance(detail.get("manufacturer"), dict) else None
    if manufacturer:
        details.append(manufacturer)
    stat_line = _format_stat_block(detail)
    if stat_line:
        details.append(stat_line)
    if selected:
        details.append('selected - press "Lock in selected part" to save it')

    return f"{primary}\n{' · '.join(details)}" if details else primary


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
            price = f"{entry['price_buy']:,.0f} aUEC" if entry.get("price_buy") is not None else "price unknown"
            terminal = entry.get("terminal_name") or "unknown shop"
            port_label = entry["port_name"].removeprefix("hardpoint_").replace("_", " ").strip().title() or entry["port_name"]
            lines.append(f"• {entry['category']} ({port_label}): {entry['item_name']} - {price} @ {terminal}")
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
        await interaction.followup.send(f"Locked in **{item_name}** for {category} in {thread.mention}.", ephemeral=True)
        return True


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
        self.category_select = _CategorySelect(self)
        self.add_item(self.category_select)

    def _selection_summary(self) -> str:
        # Plain-text mirror of the dropdowns' own state, independent of whether Discord's
        # collapsed-Select `default=True` rendering actually shows the pick in a given
        # client - requested directly by the user as a fallback after the dropdown-collapse
        # bug, so this stays even once that fix is confirmed working.
        if self.category is None:
            return ""
        parts = [f"Category: **{self.category}**"]
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
        ports = self.grouped_ports.get(self.category, [])
        if len(ports) > 1 and self.selected_port is None:
            return f"{header}\n\n{summary}\n\n**{self.category}** has {len(ports)} separate slots on this ship - pick one below."
        slot_label = f" - {_format_port_label(self.selected_port)}" if len(ports) > 1 and self.selected_port else ""
        if not self.candidates:
            return f"{header}\n\n{summary}\n\nNo currently-sold {self.category}{slot_label} options found for this ship."
        lines = [header, "", summary, "", f"**{self.category}{slot_label}**"]
        for detail in self.candidates[:MAX_CANDIDATES_SHOWN]:
            lines.append(_format_candidate_line(detail, selected=detail is self.selected_candidate))
        if len(self.candidates) > MAX_CANDIDATES_SHOWN:
            lines.append(f"...and {len(self.candidates) - MAX_CANDIDATES_SHOWN} more, showing the first {MAX_CANDIDATES_SHOWN}.")
        return "\n".join(lines)[:1900]

    def _remove_items(self, *types: type) -> None:
        for child in [c for c in self.children if isinstance(c, types)]:
            self.remove_item(child)

    async def show_category(self, interaction: discord.Interaction, category: str) -> None:
        self.category = category
        self.selected_port = None
        self.selected_candidate = None
        self.candidates = []
        self._remove_items(_SlotSelect, _PartSelect)
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
        self.candidates = []
        if port is not None:
            try:
                self.candidates = await self.cog.candidates_for_port(
                    port, limit=MAX_CANDIDATES_SHOWN, origin_id=self.origin_terminal[0],
                )
            except (UexApiError, WikiApiError) as exc:
                await interaction.edit_original_response(content=f"Couldn't load {self.category} options: {exc}", view=self)
                return
        self._remove_items(_PartSelect)
        if self.candidates:
            self.add_item(_PartSelect(self, self.candidates))
        await interaction.edit_original_response(content=self.text(), view=self)

    async def lock_in_selected(self, interaction: discord.Interaction) -> None:
        if self.selected_candidate is None or self.category is None or self.selected_port is None:
            await interaction.response.send_message("Pick a part first.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        detail = self.selected_candidate
        cheapest = _cheapest_purchase(detail)
        await self.cog.shopping.lock_in(
            interaction, self.vehicle["id"], self.vehicle.get("name") or "", self.category,
            self.selected_port.name, detail.get("_uex_id"), detail.get("name") or "unknown",
            cheapest.get("terminal_id"), cheapest.get("terminal_name"), cheapest.get("price_buy"),
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
        options = [discord.SelectOption(label=category[:100], value=category)
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


class _PartSelect(discord.ui.Select):
    def __init__(self, parent: PartsBrowserView, candidates: list[dict]) -> None:
        options = [
            discord.SelectOption(label=(c.get("name") or "Unknown")[:100], value=str(i))
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
                raw_ports = await self._wiki.get_vehicle_ports(name)
                ports = parse_ports(raw_ports)
                await self.bot.db.replace_ship_parts_reference(
                    id_vehicle, name,
                    [{"name": p.name, "port_type": p.port_type, "size_min": p.size_min, "size_max": p.size_max} for p in ports],
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
            return [ShipPort(name=r["port_name"], port_type=r["port_type"], size_min=r["size_min"], size_max=r["size_max"])
                    for r in rows]
        # Collector hasn't run for this ship yet (fresh deploy, or a ship added since the
        # last daily cycle) - fall back to a live lookup rather than a dead end.
        raw_ports = await self._wiki.get_vehicle_ports(vehicle.get("name") or "")
        return parse_ports(raw_ports)

    async def _item_detail_cached(self, item_uuid: str) -> dict | None:
        now = time.monotonic()
        cached = self._detail_cache.get(item_uuid)
        if cached and cached[0] > now:
            return cached[1]
        try:
            detail = await self._wiki.get_item_detail(item_uuid)
        except WikiApiError:
            return None
        if len(self._detail_cache) >= DETAIL_CACHE_MAX:
            self._detail_cache.pop(next(iter(self._detail_cache)))
        self._detail_cache[item_uuid] = (now + DETAIL_CACHE_SECONDS, detail)
        return detail

    async def candidates_for_port(self, port: ShipPort, *, limit: int, origin_id: int | None = None) -> list[dict]:
        catalog = await self.bot.uex.get_item_catalog()
        candidates = candidate_items_for_port(catalog, port)
        price_rows = await self.bot.uex.get_items_prices_all()
        candidates = filter_to_sold_items(candidates, sold_item_ids(price_rows))
        candidates = [row for row in candidates if row.get("uuid")][:limit]
        details: list[dict] = []
        for start in range(0, len(candidates), DETAIL_BATCH_SIZE):
            batch = candidates[start:start + DETAIL_BATCH_SIZE]
            results = await asyncio.gather(
                *(self._item_detail_cached(row["uuid"]) for row in batch), return_exceptions=True,
            )
            for row, result in zip(batch, results):
                if isinstance(result, Exception) or result is None:
                    continue
                result = dict(result)
                result["_uex_id"] = row.get("id")
                details.append(result)
        if origin_id is not None:
            details = await self._attach_distances(details, origin_id)
        return details

    async def _attach_distances(self, candidates: list[dict], origin_id: int) -> list[dict]:
        """Real distance (gigameters) from the player's given location to each candidate's
        own cheapest listing, batched via asyncio.gather - mirrors /ingame-item-finder's
        own _fetch_distances. Without this, the required `location` input had no effect on
        anything shown, a real gap an outside audit caught before merge. Candidates sort
        closest-first, with unknown distance sorting last (matches item_finder's
        established convention, not a new one)."""
        terminal_ids: dict[int, int | None] = {}
        for detail in candidates:
            terminal_id = _cheapest_purchase(detail).get("terminal_id")
            terminal_ids[id(detail)] = terminal_id

        to_fetch = sorted({tid for tid in terminal_ids.values() if tid is not None and tid != origin_id})
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

        for detail in candidates:
            terminal_id = terminal_ids[id(detail)]
            detail["_distance_gm"] = distances.get(terminal_id) if terminal_id is not None else None
        candidates.sort(key=lambda d: (d["_distance_gm"] is None, d["_distance_gm"] or 0.0))
        return candidates

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
