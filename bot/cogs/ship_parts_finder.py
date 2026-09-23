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
    isn't there."""
    for raw_key in (detail.get("type"), detail.get("sub_type")):
        if not raw_key:
            continue
        block = detail.get(_snake_case(str(raw_key)))
        if isinstance(block, dict):
            lines = [f"{key}: {value}" for key, value in block.items() if value is not None and not isinstance(value, (dict, list))]
            if lines:
                return " · ".join(lines[:4])
    return ""


def _format_candidate_line(detail: dict) -> str:
    name = detail.get("name") or "Unknown"
    size = detail.get("size")
    grade = detail.get("grade")
    manufacturer = (detail.get("manufacturer") or {}).get("name") if isinstance(detail.get("manufacturer"), dict) else None
    price = None
    terminal = None
    purchases = ((detail.get("uex_prices") or {}).get("purchase")) or []
    if purchases:
        cheapest = min(purchases, key=lambda p: p.get("price_buy") if p.get("price_buy") is not None else float("inf"))
        price = cheapest.get("price_buy")
        terminal = cheapest.get("terminal_name")
    header = f"**{name}**"
    if size is not None:
        header += f" (S{size})"
    if grade:
        header += f" · Grade {grade}"
    if manufacturer:
        header += f" · {manufacturer}"
    price_line = f"{price:,.0f} aUEC @ {terminal}" if price is not None and terminal else "price unknown"
    stat_line = _format_stat_block(detail)
    body = f"{header}\n   {price_line}"
    if stat_line:
        body += f"\n   {stat_line}"
    return body


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
            lines.append(f"• {entry['category']}: {entry['item_name']} - {price} @ {terminal}")
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
        id_item: int, item_name: str, id_terminal: int | None, terminal_name: str | None, price_buy: float | None,
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
                interaction.user.id, interaction.guild_id, id_vehicle, vehicle_name, category,
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
    view goes stale on a restart, the user just re-runs /ship-parts-finder."""
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
        self.candidates: list[dict] = []
        self.selected_candidate: dict | None = None
        self.category_select = _CategorySelect(self)
        self.add_item(self.category_select)

    def text(self) -> str:
        header = f"**{self.vehicle.get('name')}** parts - pick a category to compare real options."
        if self.category is None:
            return header
        if not self.candidates:
            return f"{header}\n\nNo currently-sold {self.category} options found for this ship."
        lines = [header, "", f"**{self.category}**"]
        for detail in self.candidates[:MAX_CANDIDATES_SHOWN]:
            lines.append(_format_candidate_line(detail))
        if len(self.candidates) > MAX_CANDIDATES_SHOWN:
            lines.append(f"...and {len(self.candidates) - MAX_CANDIDATES_SHOWN} more, showing the first {MAX_CANDIDATES_SHOWN}.")
        return "\n".join(lines)[:1900]

    async def show_category(self, interaction: discord.Interaction, category: str) -> None:
        self.category = category
        self.selected_candidate = None
        ports = self.grouped_ports.get(category, [])
        port = ports[0] if ports else None
        self.candidates = []
        if port is not None:
            try:
                self.candidates = await self.cog.candidates_for_port(port, limit=MAX_CANDIDATES_SHOWN)
            except (UexApiError, WikiApiError) as exc:
                await interaction.response.edit_message(content=f"Couldn't load {category} options: {exc}", view=self)
                return
        for child in [c for c in self.children if isinstance(c, _PartSelect)]:
            self.remove_item(child)
        if self.candidates:
            self.add_item(_PartSelect(self, self.candidates))
        await interaction.response.edit_message(content=self.text(), view=self)

    async def lock_in_selected(self, interaction: discord.Interaction) -> None:
        if self.selected_candidate is None or self.category is None:
            await interaction.response.send_message("Pick a part first.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        detail = self.selected_candidate
        purchases = ((detail.get("uex_prices") or {}).get("purchase")) or []
        cheapest = min(purchases, key=lambda p: p.get("price_buy") if p.get("price_buy") is not None else float("inf")) if purchases else {}
        await self.cog.shopping.lock_in(
            interaction, self.vehicle["id"], self.vehicle.get("name") or "", self.category,
            detail.get("_uex_id"), detail.get("name") or "unknown",
            cheapest.get("terminal_id"), cheapest.get("terminal_name"), cheapest.get("price_buy"),
        )

    @discord.ui.button(label="Lock in selected part", style=discord.ButtonStyle.success, row=4)
    async def lock_button(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        await self.lock_in_selected(interaction)


class _CategorySelect(discord.ui.Select):
    def __init__(self, parent: PartsBrowserView) -> None:
        options = [discord.SelectOption(label=category[:100], value=category)
                   for category in list(parent.grouped_ports.keys())[:25]]
        super().__init__(placeholder="Choose a component category", options=options)
        self.parent_view = parent

    async def callback(self, interaction: discord.Interaction) -> None:
        await self.parent_view.show_category(interaction, self.values[0])


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

    async def candidates_for_port(self, port: ShipPort, *, limit: int) -> list[dict]:
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
        return details

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

        view = PartsBrowserView(self, vehicle, resolved_location, grouped)
        await interaction.followup.send(content=view.text(), view=view, ephemeral=True)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(ShipPartsFinder(bot))
