"""/blueprint-search material:'s reply - what an ore or mineral crafts (the owner's pick, option C,
2026-10-07): a header with the count per category, a button per category, then that category's
blueprints a page at a time. Armor is listed as sets, picked from a menu, its pieces then buttons;
everything else is picked from a menu. Picking a blueprint opens its usual /blueprint-search
reply (Blueprints.open_blueprint), as a new message, so this list stays to pick another.

Sorting and wording are pure logic in bot/uex/blueprint_materials.py; this is the Discord side."""
from __future__ import annotations

import logging
from typing import Awaitable, Callable

import discord

from bot.discord_ui import LAYOUT_TEXT_LIMIT, BotLayoutView
from bot.uex.blueprint_materials import (
    ARMOR,
    ArmorSet,
    MaterialUse,
    by_category,
    category_entries,
    category_title,
    entry_line,
    header,
    pages,
)

logger = logging.getLogger("uexbot.material_pages")

# Public, so a plain channel edit greys it out when idle, as CraftLaunchView's 15 minutes do.
MATERIAL_IDLE_SECONDS = 15 * 60
EXPIRED_NOTE = "⏸️ Closed after 15 minutes idle - run `/blueprint-search material:` again to pick from it."
PIECES_PER_ROW = 5  # Discord's buttons per row; the biggest real set has 5 pieces

# Sends the picked blueprint's /blueprint-search reply as a new message (the click is already answered).
Opener = Callable[[discord.Interaction, MaterialUse], Awaitable[None]]


class MaterialUsesView(BotLayoutView):
    """The material's blueprints, one category at a time. Only the player who ran the command can
    use it; anyone can read it. Each redraw rebuilds the items (render)."""

    def __init__(self, material: str, uses: list[MaterialUse], *, owner_id: int, opener: Opener) -> None:
        super().__init__(timeout=MATERIAL_IDLE_SECONDS)
        self.material = material
        self.grouped = by_category(uses)
        if not self.grouped:
            raise ValueError("a material view needs at least one blueprint")
        self.owner_id = owner_id
        self.opener = opener
        self.category = next(iter(self.grouped))
        self._entries: dict[str, list[ArmorSet | MaterialUse]] = {}
        self.page = 0
        self.picked: ArmorSet | None = None
        self.expired = False
        self.render()

    # -- what's showing --------------------------------------------------------------------------

    @property
    def entries(self) -> list[ArmorSet | MaterialUse]:
        if self.category not in self._entries:
            self._entries[self.category] = category_entries(self.category, self.grouped[self.category])
        return self._entries[self.category]

    @property
    def pages(self) -> list[list]:
        return pages(self.entries)

    def page_entries(self) -> list[ArmorSet | MaterialUse]:
        return self.pages[min(self.page, len(self.pages) - 1)]

    def blocks(self) -> tuple[str, ...]:
        """The container's text: the header, the category's page, and the page line."""
        entries, page = self.entries, self.page_entries()
        listing = "\n".join([category_title(self.category, self.grouped[self.category], entries),
                             *(entry_line(entry) for entry in page)])
        blocks = [header(self.material, self.grouped), listing]
        count = len(self.pages)
        if count > 1:
            later = sum(len(p) for p in self.pages[self.page + 1:])
            line = f"-# Page {self.page + 1} of {count}"
            if later:
                line += f" · {later} more on the next page{'s' if count - self.page > 2 else ''}"
            blocks.append(line)
        return tuple(discord.utils.escape_mentions(block) for block in blocks)

    def text(self) -> str:
        """The page as plain text (what the layout shows), the idle note under it once idle."""
        return "\n\n".join([*self.blocks(), *([EXPIRED_NOTE] if self.expired else [])])

    def render(self) -> None:
        self.clear_items()
        container = discord.ui.Container(accent_colour=discord.Colour.blurple())
        for number, block in enumerate(self.blocks()):
            if number:
                container.add_item(discord.ui.Separator())
            container.add_item(discord.ui.TextDisplay(block))
        self.add_item(container)
        if self.expired:
            self.add_item(discord.ui.TextDisplay(EXPIRED_NOTE))
        if len(self.grouped) > 1:
            self.add_item(discord.ui.ActionRow(*(_CategoryButton(self, category) for category in self.grouped)))
        self.add_item(discord.ui.ActionRow(_EntrySelect(self)))
        if self.picked is not None:
            pieces = self.picked.pieces
            for start in range(0, len(pieces), PIECES_PER_ROW):
                self.add_item(discord.ui.ActionRow(
                    *(_PieceButton(self, piece, use) for piece, use in pieces[start:start + PIECES_PER_ROW])))
        if len(self.pages) > 1:
            self.add_item(discord.ui.ActionRow(_PageButton(self, -1), _PageButton(self, +1)))

    def fits(self) -> bool:
        """Whether every category's every page fits Discord's layout limits (4,000 characters, 40
        components), each with its biggest set picked - checked before sending, so no click can
        later hit a page Discord refuses. Leaves the view as it was."""
        state = (self.category, self.page, self.picked)
        try:
            for category in self.grouped:
                self.category, self.picked = category, None
                for page in range(len(self.pages)):
                    self.page = page
                    sets = [e for e in self.page_entries() if isinstance(e, ArmorSet) and len(e.pieces) > 1]
                    self.picked = max(sets, key=lambda s: len(s.pieces)) if sets else None
                    self.render()
                    if self.content_length() > LAYOUT_TEXT_LIMIT:
                        return False
            return True
        except ValueError:  # over 40 components
            return False
        finally:
            self.category, self.page, self.picked = state
            self.render()

    # -- clicks ----------------------------------------------------------------------------------

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        await super().interaction_check(interaction)
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message(
                "This list belongs to whoever ran it - run `/blueprint-search material:` for your own.", ephemeral=True)
            return False
        return True

    async def _redraw(self, interaction: discord.Interaction) -> None:
        self.render()
        await interaction.response.edit_message(view=self)

    async def show_category(self, interaction: discord.Interaction, category: str) -> None:
        self.category, self.page, self.picked = category, 0, None
        await self._redraw(interaction)

    async def turn_page(self, interaction: discord.Interaction, delta: int) -> None:
        self.page = max(0, min(len(self.pages) - 1, self.page + delta))
        self.picked = None
        await self._redraw(interaction)

    async def pick(self, interaction: discord.Interaction, entry: ArmorSet | MaterialUse) -> None:
        """A blueprint opens; an armor set of one piece opens that piece; a bigger set shows its
        pieces as buttons, and picking another set swaps them."""
        if isinstance(entry, ArmorSet) and len(entry.pieces) > 1:
            self.picked = entry
            await self._redraw(interaction)
            return
        await self.open(interaction, entry.pieces[0][1] if isinstance(entry, ArmorSet) else entry)

    async def open(self, interaction: discord.Interaction, use: MaterialUse) -> None:
        """Answer the click by redrawing (the menu goes back to its placeholder, so the same
        blueprint can be picked again), then send the blueprint's reply as a new message."""
        await self._redraw(interaction)
        await self.opener(interaction, use)

    async def on_timeout(self) -> None:
        self.expired = True
        self.render()
        await self.grey_out()


class _CategoryButton(discord.ui.Button):
    """'Armor 51': the category showing is pressed in; every other is one click away."""

    def __init__(self, parent: MaterialUsesView, category: str) -> None:
        showing = category == parent.category
        super().__init__(label=f"{category} {len(parent.grouped[category])}",
                         style=discord.ButtonStyle.primary if showing else discord.ButtonStyle.secondary,
                         disabled=showing)
        self.parent_view, self.category = parent, category

    async def callback(self, interaction: discord.Interaction) -> None:
        await self.parent_view.show_category(interaction, self.category)


class _EntrySelect(discord.ui.Select):
    """The page's sets (armor) or blueprints. The picked set stays shown."""

    def __init__(self, parent: MaterialUsesView) -> None:
        self.parent_view = parent
        self.entries = parent.page_entries()
        options = []
        for index, entry in enumerate(self.entries[:25]):
            if isinstance(entry, ArmorSet):
                pieces = ", ".join(piece for piece, _ in entry.pieces if piece)
                options.append(discord.SelectOption(label=entry.name[:100], value=str(index),
                                                    description=pieces[:100] or None, default=entry == parent.picked))
            else:
                options.append(discord.SelectOption(label=entry.name[:100], value=str(index)))
        armor = parent.category == ARMOR
        super().__init__(placeholder="Pick an armor set" if armor else "Pick a blueprint", options=options)

    async def callback(self, interaction: discord.Interaction) -> None:
        await self.parent_view.pick(interaction, self.entries[int(self.values[0])])


class _PieceButton(discord.ui.Button):
    def __init__(self, parent: MaterialUsesView, piece: str, use: MaterialUse) -> None:
        super().__init__(label=(piece.capitalize() or use.name)[:80], style=discord.ButtonStyle.primary)
        self.parent_view, self.use = parent, use

    async def callback(self, interaction: discord.Interaction) -> None:
        await self.parent_view.open(interaction, self.use)


class _PageButton(discord.ui.Button):
    def __init__(self, parent: MaterialUsesView, delta: int) -> None:
        at_edge = parent.page == 0 if delta < 0 else parent.page >= len(parent.pages) - 1
        super().__init__(label="◀ Previous" if delta < 0 else "Next ▶", style=discord.ButtonStyle.secondary,
                         disabled=at_edge)
        self.parent_view, self.delta = parent, delta

    async def callback(self, interaction: discord.Interaction) -> None:
        await self.parent_view.turn_page(interaction, self.delta)


def text_listing(material: str, uses: list[MaterialUse]) -> str:
    """Everything as plain text, every category in full: what's sent when Discord refuses the
    layout (split into messages by the caller)."""
    grouped = by_category(uses)
    sections = [header(material, grouped)]
    for category, category_uses in grouped.items():
        entries = category_entries(category, category_uses)
        sections.append("\n".join([category_title(category, category_uses, entries), *map(entry_line, entries)]))
    return discord.utils.escape_mentions("\n\n".join(sections))
