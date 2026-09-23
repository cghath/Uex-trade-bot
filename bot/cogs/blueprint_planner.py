"""Interactive recipe configuration and owner-private shopping-list delivery."""
from __future__ import annotations

import logging
import io
from decimal import Decimal
from typing import TYPE_CHECKING

import discord

from bot.uex.blueprint_crafting import Recipe, aggregate

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

    async def _thread(self, interaction: discord.Interaction) -> discord.Thread | None:
        if interaction.guild_id is None:
            return None

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


class ShoppingView(discord.ui.View):
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
    def __init__(self, parent: "CraftConfigView", path: str, label: str, values: tuple[int, ...]) -> None:
        if len(values) > 24:
            indexes = {round(i * (len(values) - 1) / 23) for i in range(24)}
            values = tuple(values[i] for i in sorted(indexes))
        options = [discord.SelectOption(label="Unspecified", value="none")]
        options += [discord.SelectOption(label=f"Quality {value}", value=str(value)) for value in values]
        super().__init__(placeholder=f"{label} quality"[:150], options=options)
        self.parent_view, self.path = parent, path

    async def callback(self, interaction: discord.Interaction) -> None:
        value = self.values[0]
        if value == "none":
            self.parent_view.qualities.pop(self.path, None)
        else:
            self.parent_view.qualities[self.path] = int(value)
        await self.parent_view.update(interaction)


class ChoiceSelect(discord.ui.Select):
    def __init__(self, parent: "CraftConfigView", group) -> None:
        names = []
        for path in group.children:
            item = next((item for item in parent.recipe.inputs if item.path == path), None)
            names.append(item.name if item else path)
        options = [discord.SelectOption(label=name[:100], value=str(index)) for index, name in enumerate(names[:25])]
        super().__init__(placeholder=f"{group.name}: choose {group.required}"[:150], options=options,
                         min_values=group.required, max_values=group.required)
        self.parent_view, self.path = parent, group.path

    async def callback(self, interaction: discord.Interaction) -> None:
        self.parent_view.choices[self.path] = [int(value) for value in self.values]
        await self.parent_view.update(interaction)


# Discord allows five rows per message and a select menu takes a whole row. The buttons share row 0, so
# four selectors fit per page; a recipe with more is paged rather than having controls silently dropped.
SELECTORS_PER_PAGE = 4


class CraftConfigView(discord.ui.View):
    def __init__(self, cog: "Blueprints", recipe: Recipe, count: int,
                 quality_options: dict[str, tuple[int, ...]]) -> None:
        super().__init__(timeout=900)
        self.cog, self.recipe, self.count = cog, recipe, count
        self.choices: dict[str, list[int]] = {}
        self.qualities: dict[str, int] = {}
        # Required material choices first: a plan can't be built without them, while a quality is optional.
        self.selectors: list[discord.ui.Select] = []
        for group in recipe.groups:
            if group.required < len(group.children):
                self.selectors.append(ChoiceSelect(self, group))
        for item in recipe.inputs:
            values = tuple(value for value in quality_options.get(item.path, ())
                           if Decimal(value) >= item.min_quality)
            if item.modifiers and values:
                self.selectors.append(QualitySelect(self, item.path, f"{item.aspect} - {item.name}", values))
        self.page = 0
        self.page_count = max(1, -(-len(self.selectors) // SELECTORS_PER_PAGE))
        if self.page_count == 1:
            self.remove_item(self.previous_button)
            self.remove_item(self.next_button)
        self._show_page()

    def _show_page(self) -> None:
        for child in [child for child in self.children if isinstance(child, discord.ui.Select)]:
            self.remove_item(child)
        start = self.page * SELECTORS_PER_PAGE
        for selector in self.selectors[start:start + SELECTORS_PER_PAGE]:
            self.add_item(selector)
        if self.page_count > 1:
            self.previous_button.disabled = self.page == 0
            self.next_button.disabled = self.page >= self.page_count - 1

    def text(self) -> str:
        body = "\n".join(self.recipe.lines(self.count, self.choices, self.qualities))[:1900]
        if self.page_count > 1:
            return f"Options page {self.page + 1} of {self.page_count} - use Previous/Next to reach every choice.\n{body}"
        return body

    async def update(self, interaction: discord.Interaction) -> None:
        await interaction.response.edit_message(content=self.text(), view=self, allowed_mentions=NO_MENTIONS)

    @discord.ui.button(label="Add configured plan", style=discord.ButtonStyle.success, row=0)
    async def add_button(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        try:
            plan = self.recipe.plan(self.count, self.choices, self.qualities)
        except ValueError as exc:
            await interaction.response.send_message(str(exc), ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        await self.cog.shopping.add(interaction, plan)

    @discord.ui.button(label="Previous options", style=discord.ButtonStyle.secondary, row=0)
    async def previous_button(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        self.page = max(0, self.page - 1)
        self._show_page()
        await self.update(interaction)

    @discord.ui.button(label="Next options", style=discord.ButtonStyle.secondary, row=0)
    async def next_button(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        self.page = min(self.page_count - 1, self.page + 1)
        self._show_page()
        await self.update(interaction)


class CraftLaunchView(discord.ui.View):
    def __init__(self, cog: "Blueprints", recipe: Recipe, count: int) -> None:
        super().__init__(timeout=900)
        self.cog, self.recipe, self.count = cog, recipe, count
        seen = set()
        for item in recipe.inputs:
            if item.ore_uuid and item.ore_uuid not in seen and len(seen) < 5:
                seen.add(item.ore_uuid)
                self.add_item(MineButton(cog, item.name))

    @discord.ui.button(label="Configure crafting", style=discord.ButtonStyle.primary, row=0)
    async def configure(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        await interaction.response.defer(ephemeral=True)
        options = await self.cog.quality_options(self.recipe)
        view = CraftConfigView(self.cog, self.recipe, self.count, options)
        await interaction.followup.send(view.text(), view=view, ephemeral=True, allowed_mentions=NO_MENTIONS)

    @discord.ui.button(label="Add to shopping list", style=discord.ButtonStyle.success, row=0)
    async def add_default(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        try:
            plan = self.recipe.plan(self.count, {}, {})
        except ValueError:
            await interaction.response.send_message("Configure the required material choices first.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        await self.cog.shopping.add(interaction, plan)


class MineButton(discord.ui.Button):
    def __init__(self, cog: "Blueprints", material: str) -> None:
        super().__init__(label=f"Mine {material}"[:80], style=discord.ButtonStyle.secondary, row=1)
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
