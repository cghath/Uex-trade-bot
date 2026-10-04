"""Interactive recipe configuration and owner-private shopping-list delivery."""
from __future__ import annotations

import asyncio
import io
import logging
from decimal import Decimal
from typing import TYPE_CHECKING

import discord

from bot.discord_ui import LAYOUT_TEXT_LIMIT, BotLayoutView, BotView
from bot.uex.blueprint_crafting import Ingredient, Recipe, aggregate, quality_choice_label

if TYPE_CHECKING:
    from bot.cogs.blueprints import Blueprints

logger = logging.getLogger("uexbot.blueprint_planner")
NO_MENTIONS = discord.AllowedMentions.none()


def _amount(value: str) -> str:
    number = Decimal(value)
    return f"{number:f}".rstrip("0").rstrip(".") if number % 1 else f"{number:.0f}"


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


class ShoppingService:
    def __init__(self, bot) -> None:
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
        saved = await self.bot.db.get_blueprint_thread(interaction.user.id, interaction.guild_id)
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
                    logger.warning("Could not reuse blueprint thread %s", thread.id)

        if not isinstance(interaction.channel, discord.TextChannel):
            return None
        thread = None
        try:
            thread = await interaction.channel.create_thread(
                name=f"Blueprint list - {interaction.user.display_name}"[:100],
                type=discord.ChannelType.private_thread, invitable=False, auto_archive_duration=1440,
            )
            await thread.add_user(interaction.user)
            message = await thread.send("Your private blueprint list is ready.", view=ShoppingView(self),
                                        allowed_mentions=NO_MENTIONS)
            await self.bot.db.set_blueprint_thread(interaction.user.id, interaction.guild_id, thread.id, message.id)
            return thread
        except Exception:
            logger.exception("Could not create blueprint shopping thread")
            try:
                await self.bot.db.delete_blueprint_thread(interaction.user.id, interaction.guild_id)
            except Exception:
                logger.exception("Could not clear partial blueprint thread state")
            if thread is not None:
                try:
                    await thread.delete()
                except Exception:
                    logger.warning("Could not delete partial blueprint thread %s", thread.id)
            return None

    async def render(self, user_id: int, guild_id: int) -> list[str]:
        entries = await self.bot.db.get_blueprint_plans(user_id, guild_id)
        if not entries:
            return ["**Combined blueprint shopping list**\nNo blueprints added yet."]
        lines = ["**Combined blueprint shopping list**", "", "**Materials**"]
        for row in aggregate([entry["plan"] for entry in entries]):
            quality = f" · quality {row['quality']}" if row.get("quality") is not None else ""
            lines.append(f"• {row['name']}: {_amount(row['amount'])} {row['unit']}{quality} · game {row['game_version']}")
        lines.extend(["", "**Blueprint plans**"])
        for entry in entries:
            plan = entry["plan"]
            choices = ", ".join(f"{k}={v}" for k, v in sorted(plan["choices"].items())) or "fixed recipe"
            qualities = ", ".join(f"{k}={v}" for k, v in sorted(plan["qualities"].items())) or "unspecified"
            lines.append(f"• #{entry['id']} · Craft {plan['craft_count']} {plan['name']} · UUID {plan['blueprint_uuid']} · "
                         f"game {plan['game_version']} · choices {choices} · qualities {qualities}")
        return _pages(lines)

    async def refresh(self, thread: discord.Thread, user_id: int, guild_id: int) -> None:
        pages = await self.render(user_id, guild_id)
        content = pages[0]
        attachment = None
        if len(pages) > 1:
            content = "**Combined blueprint shopping list**\nThe full list is attached because it exceeds Discord's message limit."
            attachment = discord.File(io.BytesIO("\n".join(pages).encode("utf-8")), filename="blueprint-shopping-list.txt")
        saved = await self.bot.db.get_blueprint_thread(user_id, guild_id)
        message = None
        if saved and saved.get("message_id"):
            try:
                message = await thread.fetch_message(saved["message_id"])
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                pass
        if message is None:
            kwargs = {"file": attachment} if attachment else {}
            message = await thread.send(content, view=ShoppingView(self), allowed_mentions=NO_MENTIONS, **kwargs)
            await self.bot.db.set_blueprint_thread(user_id, guild_id, thread.id, message.id)
        else:
            await message.edit(content=content, attachments=[attachment] if attachment else [],
                               view=ShoppingView(self), allowed_mentions=NO_MENTIONS)

    async def add(self, interaction: discord.Interaction, plan: dict) -> bool:
        if interaction.guild_id is None:
            await interaction.followup.send("Blueprint lists are available in a server.", ephemeral=True)
            return False
        thread = await self._thread(interaction)
        if thread is None:
            await interaction.followup.send("I couldn't create your private blueprint thread. Check thread permissions.",
                                            ephemeral=True)
            return False
        try:
            await self.bot.db.add_blueprint_plan(interaction.user.id, interaction.guild_id, str(interaction.id), plan)
        except Exception:
            logger.exception("Could not save blueprint plan")
            await interaction.followup.send("I couldn't save that plan. Nothing was added; please try again.", ephemeral=True)
            return False
        try:
            await self.refresh(thread, interaction.user.id, interaction.guild_id)
        except Exception:
            # Not just discord.Forbidden/HTTPException - a DB read-back error, a malformed
            # stored plan, or any other rendering failure here must still get a reply, or
            # the interaction is left unanswered ("the application did not respond") even
            # though the plan was already saved. Matches open()'s identical refresh call
            # just below, which already had this broader catch.
            logger.exception("Blueprint list saved but Discord refresh failed")
            await interaction.followup.send(f"Saved, but I couldn't refresh {thread.mention}. Use Refresh list there.",
                                            ephemeral=True)
            return True
        await interaction.followup.send(f"Added to {thread.mention}.", ephemeral=True)
        return True

    async def open(self, interaction: discord.Interaction) -> None:
        thread = await self._thread(interaction)
        if thread is None or interaction.guild_id is None:
            await interaction.followup.send("I couldn't open a private blueprint thread from this channel.", ephemeral=True)
            return
        try:
            await self.refresh(thread, interaction.user.id, interaction.guild_id)
        except Exception:
            # After the defer, an unanswered interaction reads as "the application did not respond", so a
            # Discord permission/HTTP failure (or anything else) must still get a reply.
            logger.exception("Could not open the blueprint list")
            await interaction.followup.send(
                f"I couldn't update {thread.mention}. Check that I can send and edit messages in that thread, "
                "then try again.", ephemeral=True)
            return
        await interaction.followup.send(f"Your blueprint list is in {thread.mention}.", ephemeral=True)


class ShoppingView(BotView):
    """Persistent controls whose callbacks always re-check the stored owner."""
    def __init__(self, service: ShoppingService) -> None:
        super().__init__(timeout=None)
        self.service = service

    async def _owner(self, interaction: discord.Interaction) -> dict | None:
        row = await self.service.bot.db.get_blueprint_thread_owner(getattr(interaction.channel, "id", 0))
        if row is None or row["user_id"] != interaction.user.id:
            await interaction.response.send_message("This shopping list belongs to another player.", ephemeral=True)
            return None
        return row

    @discord.ui.button(label="Refresh list", style=discord.ButtonStyle.secondary,
                       custom_id="blueprint-shopping:refresh")
    async def refresh_button(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        row = await self._owner(interaction)
        if row:
            await interaction.response.defer(ephemeral=True)
            try:
                await self.service.refresh(interaction.channel, row["user_id"], row["guild_id"])
            except Exception:
                logger.exception("Could not refresh the blueprint list")
                await interaction.followup.send(
                    "I couldn't refresh the list. Check that I can edit messages in this thread, then try again.",
                    ephemeral=True)
                return
            await interaction.followup.send("List refreshed.", ephemeral=True)

    @discord.ui.button(label="Clear list", style=discord.ButtonStyle.danger,
                       custom_id="blueprint-shopping:clear")
    async def clear_button(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        row = await self._owner(interaction)
        if row:
            await interaction.response.defer(ephemeral=True)
            try:
                await self.service.bot.db.clear_blueprint_plans(row["user_id"], row["guild_id"])
            except Exception:
                logger.exception("Could not clear the blueprint list")
                await interaction.followup.send(
                    "I couldn't clear your list. Nothing was changed; please try again.", ephemeral=True)
                return
            try:
                await self.service.refresh(interaction.channel, row["user_id"], row["guild_id"])
            except Exception:
                logger.exception("Blueprint list cleared but the Discord refresh failed")
                await interaction.followup.send(
                    "Your list was cleared, but I couldn't update this message. Press Refresh list to redraw it.",
                    ephemeral=True)
                return
            await interaction.followup.send("Blueprint list cleared.", ephemeral=True)


class QualitySelect(discord.ui.Select):
    """One material's quality menu. Each option says what that quality does ("Tungsten 858 -
    Recoil 29% better") and the picked one stays shown, where the menu used to reset to its
    placeholder after every pick."""

    def __init__(self, parent: "_CraftConfig", item: Ingredient, values: tuple[int, ...]) -> None:
        if len(values) > 24:
            indexes = {round(i * (len(values) - 1) / 23) for i in range(24)}
            values = tuple(values[i] for i in sorted(indexes))
        options = [discord.SelectOption(label="Not picked", value="none")]
        options += [discord.SelectOption(label=quality_choice_label(item, value), value=str(value)) for value in values]
        aspect = "" if item.aspect == item.name else f" ({item.aspect})"
        super().__init__(placeholder=f"{item.name} quality{aspect}"[:150], options=options)
        self.parent_view, self.path = parent, item.path

    def show_current(self) -> None:
        current = self.parent_view.qualities.get(self.path)
        for option in self.options:
            option.default = current is not None and option.value == str(current)

    async def callback(self, interaction: discord.Interaction) -> None:
        value = self.values[0]
        if value == "none":
            self.parent_view.qualities.pop(self.path, None)
        else:
            self.parent_view.qualities[self.path] = int(value)
        await self.parent_view.update(interaction)


class ChoiceSelect(discord.ui.Select):
    def __init__(self, parent: "_CraftConfig", group) -> None:
        names = []
        for path in group.children:
            item = next((item for item in parent.recipe.inputs if item.path == path), None)
            names.append(item.name if item else path)
        options = [discord.SelectOption(label=name[:100], value=str(index)) for index, name in enumerate(names[:25])]
        super().__init__(placeholder=f"{group.name}: choose {group.required}"[:150], options=options,
                         min_values=group.required, max_values=group.required)
        self.parent_view, self.path = parent, group.path

    def show_current(self) -> None:
        chosen = {str(index) for index in self.parent_view.choices.get(self.path, ())}
        for option in self.options:
            option.default = option.value in chosen

    async def callback(self, interaction: discord.Interaction) -> None:
        self.parent_view.choices[self.path] = [int(value) for value in self.values]
        await self.parent_view.update(interaction)


# Four menus a page, under the reply and above its buttons; a recipe with more is paged rather than
# having controls silently dropped. (A classic message fits five rows: four menus and the buttons.)
SELECTORS_PER_PAGE = 4


class _CraftConfig:
    """What the Configure crafting reply does in either form: the picks, the menus and their pages,
    and the shopping-list button. CraftLayoutView is the reply (the owner's pick from real-data
    mockups, 2026-10-04: a section per material with what its quality does, then "Your craft");
    CraftConfigView sends the same text as a plain message when Discord refuses the layout."""

    def _start(self, cog: "Blueprints", recipe: Recipe, count: int,
               quality_options: dict[str, tuple[int, ...]]) -> None:
        self.cog, self.recipe, self.count = cog, recipe, count
        self.choices: dict[str, list[int]] = {}
        self.qualities: dict[str, int] = {}
        self.options = {item.path: tuple(value for value in quality_options.get(item.path, ())
                                         if Decimal(value) >= item.min_quality) for item in recipe.inputs}
        # Required material choices first: a plan can't be built without them, while a quality is optional.
        self.selectors: list[discord.ui.Select] = []
        for group in recipe.groups:
            if group.required < len(group.children):
                self.selectors.append(ChoiceSelect(self, group))
        for item in recipe.inputs:
            if item.modifiers and self.options[item.path]:
                self.selectors.append(QualitySelect(self, item, self.options[item.path]))
        self.page = 0
        self.page_count = max(1, -(-len(self.selectors) // SELECTORS_PER_PAGE))

    def page_selectors(self) -> list[discord.ui.Select]:
        start = self.page * SELECTORS_PER_PAGE
        return self.selectors[start:start + SELECTORS_PER_PAGE]

    def blocks(self) -> tuple[str, ...]:
        note = (f"Menus page {self.page + 1} of {self.page_count} - Previous/Next options show the rest."
                if self.page_count > 1 else None)
        return self.recipe.layout_blocks(self.count, self.choices, self.qualities, self.options, note=note)

    async def update(self, interaction: discord.Interaction) -> None:
        raise NotImplementedError

    async def add_plan(self, interaction: discord.Interaction) -> None:
        try:
            plan = self.recipe.plan(self.count, self.choices, self.qualities)
        except ValueError as exc:
            await interaction.response.send_message(str(exc), ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        await self.cog.shopping.add(interaction, plan)

    async def turn_page(self, interaction: discord.Interaction, delta: int) -> None:
        self.page = max(0, min(self.page_count - 1, self.page + delta))
        await self.update(interaction)


class CraftLayoutView(_CraftConfig, BotLayoutView):
    """Configure crafting as a layout: the blocks in a container, the page's menus, then the buttons."""

    def __init__(self, cog: "Blueprints", recipe: Recipe, count: int,
                 quality_options: dict[str, tuple[int, ...]]) -> None:
        # 10 idle minutes, not 15: this reply is ephemeral, so it can only be greyed out
        # through an interaction token, and those last 15 minutes from the click that opened it.
        super().__init__(timeout=600)
        self._start(cog, recipe, count, quality_options)
        self.add_button = discord.ui.Button(label="Add to shopping list", style=discord.ButtonStyle.success)
        self.add_button.callback = self.add_plan
        self.previous_button = discord.ui.Button(label="Previous options", style=discord.ButtonStyle.secondary)
        self.previous_button.callback = self._previous
        self.next_button = discord.ui.Button(label="Next options", style=discord.ButtonStyle.secondary)
        self.next_button.callback = self._next
        self.render()

    async def _previous(self, interaction: discord.Interaction) -> None:
        await self.turn_page(interaction, -1)

    async def _next(self, interaction: discord.Interaction) -> None:
        await self.turn_page(interaction, 1)

    def render(self) -> None:
        self.clear_items()
        container = discord.ui.Container(accent_colour=discord.Colour.blurple())
        for number, block in enumerate(self.blocks()):
            if number:
                container.add_item(discord.ui.Separator())
            container.add_item(discord.ui.TextDisplay(block))
        self.add_item(container)
        for selector in self.page_selectors():
            selector.show_current()
            self.add_item(discord.ui.ActionRow(selector))
        buttons = [self.add_button]
        if self.page_count > 1:
            self.previous_button.disabled = self.page == 0
            self.next_button.disabled = self.page >= self.page_count - 1
            buttons += [self.previous_button, self.next_button]
        self.add_item(discord.ui.ActionRow(*buttons))

    async def update(self, interaction: discord.Interaction) -> None:
        self.render()
        await interaction.response.edit_message(view=self, allowed_mentions=NO_MENTIONS)


class CraftConfigView(_CraftConfig, BotView):
    """Configure crafting as a plain message with the same text, for when Discord refuses the layout."""

    def __init__(self, cog: "Blueprints", recipe: Recipe, count: int,
                 quality_options: dict[str, tuple[int, ...]]) -> None:
        super().__init__(timeout=600)  # see CraftLayoutView
        self._start(cog, recipe, count, quality_options)
        if self.page_count == 1:
            self.remove_item(self.previous_button)
            self.remove_item(self.next_button)
        self._show_page()

    def _show_page(self) -> None:
        for child in [child for child in self.children if isinstance(child, discord.ui.Select)]:
            self.remove_item(child)
        for selector in self.page_selectors():
            selector.show_current()
            self.add_item(selector)
        if self.page_count > 1:
            self.previous_button.disabled = self.page == 0
            self.next_button.disabled = self.page >= self.page_count - 1

    def text(self) -> str:
        return "\n\n".join(self.blocks())[:1900]

    async def update(self, interaction: discord.Interaction) -> None:
        self._show_page()
        await interaction.response.edit_message(content=self.text(), view=self, allowed_mentions=NO_MENTIONS)

    @discord.ui.button(label="Add to shopping list", style=discord.ButtonStyle.success, row=0)
    async def add_button(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        await self.add_plan(interaction)

    @discord.ui.button(label="Previous options", style=discord.ButtonStyle.secondary, row=0)
    async def previous_button(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        await self.turn_page(interaction, -1)

    @discord.ui.button(label="Next options", style=discord.ButtonStyle.secondary, row=0)
    async def next_button(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        await self.turn_page(interaction, 1)


def mined_materials(recipe: Recipe) -> list[str]:
    """The recipe's first five distinct ores, each of which gets a Mine button."""
    seen, names = set(), []
    for item in recipe.inputs:
        if item.ore_uuid and item.ore_uuid not in seen and len(seen) < 5:
            seen.add(item.ore_uuid)
            names.append(item.name)
    return names


async def open_craft_config(cog: "Blueprints", recipe: Recipe, count: int, interaction: discord.Interaction) -> None:
    """The Configure crafting reply, private to whoever clicked: the layout, or the same text as a
    plain message if it doesn't fit (Discord's 40 components or 4,000 characters) or Discord refuses it."""
    await interaction.response.defer(ephemeral=True)
    options = await cog.quality_options(recipe)
    try:
        layout = CraftLayoutView(cog, recipe, count, options)
        if layout.content_length() > LAYOUT_TEXT_LIMIT:
            raise ValueError(f"{layout.content_length()} characters")
    except ValueError as exc:
        logger.warning("Craft configuration doesn't fit a layout (%s); sending text", exc)
    else:
        try:
            layout.message = await interaction.followup.send(
                view=layout, ephemeral=True, allowed_mentions=NO_MENTIONS, wait=True,
            )
            return
        except discord.HTTPException as exc:
            layout.stop()
            logger.warning("Craft configuration layout refused (%s); sending text", exc)
    view = CraftConfigView(cog, recipe, count, options)
    view.message = await interaction.followup.send(
        view.text(), view=view, ephemeral=True, allowed_mentions=NO_MENTIONS, wait=True,
    )


async def add_default_plan(cog: "Blueprints", recipe: Recipe, count: int, interaction: discord.Interaction) -> None:
    try:
        plan = recipe.plan(count, {}, {})
    except ValueError:
        await interaction.response.send_message("Configure the required material choices first.", ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True)
    await cog.shopping.add(interaction, plan)


class CraftLaunchView(BotView):
    """The crafting buttons under /blueprint-search's text-only reply (the fallback when its
    layout doesn't fit or Discord refuses it)."""

    def __init__(self, cog: "Blueprints", recipe: Recipe, count: int) -> None:
        super().__init__(timeout=900)
        self.cog, self.recipe, self.count = cog, recipe, count
        for material in mined_materials(recipe):
            self.add_item(MineButton(cog, material))

    @discord.ui.button(label="Configure crafting", style=discord.ButtonStyle.primary, row=0)
    async def configure(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        await open_craft_config(self.cog, self.recipe, self.count, interaction)

    @discord.ui.button(label="Add to shopping list", style=discord.ButtonStyle.success, row=0)
    async def add_default(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        await add_default_plan(self.cog, self.recipe, self.count, interaction)


class BlueprintResultView(BotLayoutView):
    """/blueprint-search's reply as one layout (the owner's pick, 2026-10-03): `blocks` is the
    header then one text block per giver, divided by lines; then the crafting line with its
    Configure crafting button beside it, and the shopping-list and Mine buttons under it.
    Without a recipe, `crafting` says crafting is unavailable and there are no buttons."""

    def __init__(self, cog: "Blueprints", blocks: tuple[str, ...], crafting: str, recipe: Recipe | None,
                 count: int) -> None:
        super().__init__(timeout=900)
        self.cog, self.recipe, self.count = cog, recipe, count
        container = discord.ui.Container(accent_colour=discord.Colour.blurple())
        container.add_item(discord.ui.TextDisplay(blocks[0]))
        for block in blocks[1:]:
            container.add_item(discord.ui.Separator())
            container.add_item(discord.ui.TextDisplay(block))
        container.add_item(discord.ui.Separator())
        if recipe is None:
            container.add_item(discord.ui.TextDisplay(crafting))
        else:
            configure = discord.ui.Button(label="Configure crafting", style=discord.ButtonStyle.primary)
            configure.callback = self.configure
            container.add_item(discord.ui.Section(discord.ui.TextDisplay(crafting), accessory=configure))
            add = discord.ui.Button(label="Add to shopping list", style=discord.ButtonStyle.success)
            add.callback = self.add_default
            buttons = [add, *(MineButton(cog, material, row=None) for material in mined_materials(recipe))]
            for start in range(0, len(buttons), 5):  # Discord's five buttons to a row
                container.add_item(discord.ui.ActionRow(*buttons[start:start + 5]))
        self.add_item(container)

    async def configure(self, interaction: discord.Interaction) -> None:
        await open_craft_config(self.cog, self.recipe, self.count, interaction)

    async def add_default(self, interaction: discord.Interaction) -> None:
        await add_default_plan(self.cog, self.recipe, self.count, interaction)


class MineButton(discord.ui.Button):
    def __init__(self, cog: "Blueprints", material: str, *, row: int | None = 1) -> None:
        super().__init__(label=f"Mine {material}"[:80], style=discord.ButtonStyle.secondary, row=row)
        self.cog, self.material = cog, material

    async def callback(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        mining = interaction.client.get_cog("MiningLocations")
        if mining is None:
            await interaction.followup.send("Mining locations are temporarily unavailable.", ephemeral=True)
            return
        embed, error = await mining.build_where_to_mine_embed(self.material)
        if error:
            await interaction.followup.send(error, ephemeral=True, allowed_mentions=NO_MENTIONS)
        else:
            await interaction.followup.send(embed=embed, ephemeral=True)
