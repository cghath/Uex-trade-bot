"""A route command's results as one message, one route per page (audit UX-6).

Route commands used to post an intro plus a message per route - up to 11 public messages
for one /top-routes. Now it's a single message:

- ◀ ▶ page through the routes. Only the player who ran the command can page: the routes
  were worked out for their ship, budget and saved settings.
- **Track this route** starts tracking whichever route is showing, for anyone.

Each page carries one route's own embed, so the message never holds more than one route's
worth of embed text. That matters: putting every route's embed in one message is what hit
Discord's combined 6,000-character embed limit before, leaving commands stuck on
"thinking...". A route too long for an embed becomes plain-text pages instead, and a page
whose embed Discord refuses anyway is shown as its plain text.

/multi-stop-route's pages are laid out with Discord's layout components instead of embeds
(the owner's pick, 2026-10-04): a page's text blocks in a container, divided by lines, with
the same buttons under it (RouteLayoutPagesView). A route too long for one layout becomes
text pages, and if Discord refuses the layout the routes go out as text pages.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import discord

from bot.delivery import MAX_MESSAGE_CHARS, fit_lines
from bot.discord_ui import LAYOUT_TEXT_LIMIT, BotLayoutView, BotView
from bot.uex.route_presentation import chunk_lines

if TYPE_CHECKING:
    from bot.cogs.route_progression import RouteProgression, TrackableRoute

logger = logging.getLogger("uexbot.route_pages")

PAGES_IDLE_SECONDS = 15 * 60
EXPIRED_NOTE = (f"-# ⏸️ These buttons closed after {PAGES_IDLE_SECONDS // 60} idle minutes - "
                "run the command again to page through or track these routes.")
NO_MENTIONS = discord.AllowedMentions.none()
# Room kept for a "(part 2 of 3)" line when a long route is split over several text pages.
_PART_NOTE_ROOM = 40
# Room kept in a layout page for the text above its container: EXPIRED_NOTE once the buttons
# close, or a short header.
_LAYOUT_NOTE_ROOM = 300


@dataclass
class RoutePage:
    """One page: a route's embed (None for a text-only page), its plain-text version, what
    Track this route starts (None when it can't be tracked), and, for a route laid out
    with layout components, its text blocks (see layout_pages)."""

    embed: discord.Embed | None
    text: str
    route: TrackableRoute | None = None
    blocks: tuple[str, ...] = ()


def text_pages(text: str, *, route: TrackableRoute | None = None, header: str = "") -> list[RoutePage]:
    """A route too long for an embed, as plain-text pages - split over as many as it takes,
    so nothing is dropped."""
    room = MAX_MESSAGE_CHARS - len(header) - 2 - _PART_NOTE_ROOM
    chunks = chunk_lines(text.splitlines(), max_length=max(room, 200))
    if len(chunks) == 1:
        return [RoutePage(None, chunks[0], route)]
    return [RoutePage(None, f"{chunk}\n-# (part {i} of {len(chunks)})", route)
            for i, chunk in enumerate(chunks, 1)]


def layout_pages(blocks: tuple[str, ...], *, route: TrackableRoute | None = None) -> list[RoutePage]:
    """A route as one layout page - its blocks in a container, divided by lines - or, past
    Discord's 4,000 characters of text in one layout, as text pages, so nothing is dropped.
    Its text version is the blocks joined by blank lines: the same markdown (headings, small
    print) reads the same in a plain message."""
    text = "\n\n".join(blocks)
    if len(text) + _LAYOUT_NOTE_ROOM > LAYOUT_TEXT_LIMIT:
        return text_pages(text, route=route)
    return [RoutePage(None, text, route, blocks)]


class RoutePaging:
    """What both route views do: page through the routes for the player who ran the command,
    and Track this route for anyone. A view renders the page showing with render(), which
    returns the message fields to edit (none for a layout, whose items are the message)."""

    pages: list[RoutePage]
    owner_id: int
    tracking_cog: RouteProgression | None
    header: str
    index: int
    as_text: set[int]

    def _start(self, pages: list[RoutePage], owner_id: int, tracking_cog: RouteProgression | None,
               header: str) -> None:
        self.pages = pages
        self.owner_id = owner_id
        self.tracking_cog = tracking_cog
        self.header = header
        self.index = 0
        # Pages Discord refused as laid out; shown as their plain text from then on.
        self.as_text = set()

    @property
    def has_controls(self) -> bool:
        return len(self.pages) > 1 or self._trackable(0)

    def _trackable(self, index: int) -> bool:
        return self.tracking_cog is not None and self.pages[index].route is not None

    def render(self, *, note: str | None = None) -> dict[str, Any]:
        raise NotImplementedError

    async def _turn(self, interaction: discord.Interaction, delta: int) -> None:
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message(
                f"These routes were worked out for <@{self.owner_id}>'s ship and settings - run the command "
                "yourself to page through your own.",
                ephemeral=True, allowed_mentions=NO_MENTIONS,
            )
            return
        self.index = max(0, min(len(self.pages) - 1, self.index + delta))
        try:
            await interaction.response.edit_message(**self.render(), view=self)
        except discord.HTTPException:
            if interaction.response.is_done():
                raise
            logger.warning("Discord refused route page %s; showing it as text", self.index + 1)
            self.as_text.add(self.index)
            await interaction.response.edit_message(**self.render(), view=self)

    async def _track(self, interaction: discord.Interaction) -> None:
        page = self.pages[self.index]
        if self.tracking_cog is None or page.route is None:
            await interaction.response.send_message("This route can't be tracked.", ephemeral=True)
            return
        await self.tracking_cog.start_tracking(interaction, page.route)

    async def on_timeout(self) -> None:
        await self.grey_out(**self.render(note=EXPIRED_NOTE))  # type: ignore[attr-defined]


class RoutePagesView(RoutePaging, BotView):
    def __init__(self, pages: list[RoutePage], *, owner_id: int, tracking_cog: RouteProgression | None,
                 header: str = "") -> None:
        super().__init__(timeout=PAGES_IDLE_SECONDS)
        self._start(pages, owner_id, tracking_cog, header)
        if len(pages) < 2:
            for item in (self.previous_page, self.position, self.next_page):
                self.remove_item(item)
        self._refresh_buttons()

    def _refresh_buttons(self) -> None:
        last = len(self.pages) - 1
        self.previous_page.disabled = self.index == 0
        self.next_page.disabled = self.index >= last
        self.position.label = f"{self.index + 1} / {len(self.pages)}"
        self.track.disabled = not self._trackable(self.index)

    def render(self, *, note: str | None = None) -> dict[str, Any]:
        """The message fields for the page showing."""
        self._refresh_buttons()
        head = "\n".join(part for part in (self.header, note) if part)
        page = self.pages[self.index]
        if page.embed is not None and self.index not in self.as_text:
            return {"content": head or None, "embed": page.embed}
        room = MAX_MESSAGE_CHARS - len(head) - 2
        body = fit_lines(page.text.splitlines(), limit=max(room, 100))
        return {"content": f"{head}\n\n{body}" if head else body, "embed": None}

    @discord.ui.button(label="◀", style=discord.ButtonStyle.secondary, row=0)
    async def previous_page(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        await self._turn(interaction, -1)

    @discord.ui.button(label="1 / 1", style=discord.ButtonStyle.secondary, disabled=True, row=0)
    async def position(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        pass  # a label, never clickable

    @discord.ui.button(label="▶", style=discord.ButtonStyle.secondary, row=0)
    async def next_page(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        await self._turn(interaction, 1)

    @discord.ui.button(label="Track this route", style=discord.ButtonStyle.blurple, row=0)
    async def track(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        await self._track(interaction)


class RouteLayoutPagesView(RoutePaging, BotLayoutView):
    """RoutePagesView for routes laid out with layout components: the page's blocks in a
    container, divided by lines, and the same buttons under it. A text page (a route too
    long for one layout, or one Discord refused) shows as one block. The whole message is
    this view, so render() rebuilds its items rather than returning content or an embed."""

    def __init__(self, pages: list[RoutePage], *, owner_id: int, tracking_cog: RouteProgression | None,
                 header: str = "") -> None:
        super().__init__(timeout=PAGES_IDLE_SECONDS)
        self._start(pages, owner_id, tracking_cog, header)
        self.previous_page = discord.ui.Button(label="◀", style=discord.ButtonStyle.secondary)
        self.previous_page.callback = self._previous
        self.position = discord.ui.Button(label="1 / 1", style=discord.ButtonStyle.secondary, disabled=True)
        self.next_page = discord.ui.Button(label="▶", style=discord.ButtonStyle.secondary)
        self.next_page.callback = self._next
        self.track = discord.ui.Button(label="Track this route", style=discord.ButtonStyle.blurple)
        self.track.callback = self._track
        self.render()

    async def _previous(self, interaction: discord.Interaction) -> None:
        await self._turn(interaction, -1)

    async def _next(self, interaction: discord.Interaction) -> None:
        await self._turn(interaction, 1)

    def render(self, *, note: str | None = None) -> dict[str, Any]:
        """Rebuild the items for the page showing; there are no other message fields."""
        self.clear_items()
        if head := "\n".join(part for part in (self.header, note) if part):
            self.add_item(discord.ui.TextDisplay(head))
        page = self.pages[self.index]
        blocks = page.blocks if page.blocks and self.index not in self.as_text else (page.text,)
        container = discord.ui.Container(accent_colour=discord.Colour.green())
        for number, block in enumerate(blocks):
            if number:
                container.add_item(discord.ui.Separator())
            container.add_item(discord.ui.TextDisplay(block))
        self.add_item(container)
        if self.has_controls:
            self.previous_page.disabled = self.index == 0
            self.next_page.disabled = self.index >= len(self.pages) - 1
            self.position.label = f"{self.index + 1} / {len(self.pages)}"
            self.track.disabled = not self._trackable(self.index)
            paging = [self.previous_page, self.position, self.next_page] if len(self.pages) > 1 else []
            self.add_item(discord.ui.ActionRow(*paging, self.track))
        return {}


async def _send_layout(interaction: discord.Interaction, pages: list[RoutePage], *,
                       tracking_cog: RouteProgression | None, header: str) -> bool:
    """Send the routes as a layout; False if Discord refused it (nothing was sent)."""
    try:
        view = RouteLayoutPagesView(pages, owner_id=interaction.user.id, tracking_cog=tracking_cog, header=header)
    except ValueError as exc:  # past Discord's 40 components
        logger.warning("The routes don't fit a layout (%s); sending text pages", exc)
        return False
    try:
        message = await interaction.followup.send(view=view, wait=True, allowed_mentions=NO_MENTIONS)
    except discord.HTTPException as exc:
        view.stop()
        logger.warning("Discord refused the route layout (%s); sending text pages", exc)
        return False
    if view.has_controls:
        view.message = message
    else:
        view.stop()
    return True


async def send_route_pages(interaction: discord.Interaction, pages: list[RoutePage], *,
                           tracking_cog: RouteProgression | None, header: str = "", omitted: int = 0) -> None:
    """Send a route command's results as one paged message (see the module docstring)."""
    if omitted:
        note = f"-# {omitted} more route{'s' if omitted != 1 else ''} omitted - too large to display."
        header = f"{header}\n{note}" if header else note
    if not pages:
        await interaction.followup.send(header or "No routes to show.", allowed_mentions=NO_MENTIONS)
        return
    if any(page.blocks for page in pages):
        if await _send_layout(interaction, pages, tracking_cog=tracking_cog, header=header):
            return
        # The same routes as text pages, which any message can carry.
        pages = [text_page for page in pages
                 for text_page in (text_pages(page.text, route=page.route, header=header) if page.blocks else [page])]
    view = RoutePagesView(pages, owner_id=interaction.user.id, tracking_cog=tracking_cog, header=header)
    controls = {"view": view} if view.has_controls else {}

    def first_page() -> dict[str, Any]:
        # A new message has nothing to clear, so empty fields are left out (a page turn
        # passes embed=None to remove the previous page's embed).
        return {key: value for key, value in view.render().items() if value is not None}

    try:
        message = await interaction.followup.send(**first_page(), **controls, wait=True,
                                                  allowed_mentions=NO_MENTIONS)
    except discord.HTTPException:
        logger.warning("Discord refused the first route page's embed; sending it as text")
        view.as_text.add(0)
        message = await interaction.followup.send(**first_page(), **controls, wait=True,
                                                  allowed_mentions=NO_MENTIONS)
    if controls:
        view.message = message
    else:
        view.stop()
