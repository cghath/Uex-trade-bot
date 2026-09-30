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
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import discord

from bot.delivery import MAX_MESSAGE_CHARS, fit_lines
from bot.discord_ui import BotView
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


@dataclass
class RoutePage:
    """One page: a route's embed (None for a text-only page), its plain-text version, and
    what Track this route starts (None when it can't be tracked)."""

    embed: discord.Embed | None
    text: str
    route: TrackableRoute | None = None


def text_pages(text: str, *, route: TrackableRoute | None = None, header: str = "") -> list[RoutePage]:
    """A route too long for an embed, as plain-text pages - split over as many as it takes,
    so nothing is dropped."""
    room = MAX_MESSAGE_CHARS - len(header) - 2 - _PART_NOTE_ROOM
    chunks = chunk_lines(text.splitlines(), max_length=max(room, 200))
    if len(chunks) == 1:
        return [RoutePage(None, chunks[0], route)]
    return [RoutePage(None, f"{chunk}\n-# (part {i} of {len(chunks)})", route)
            for i, chunk in enumerate(chunks, 1)]


class RoutePagesView(BotView):
    def __init__(self, pages: list[RoutePage], *, owner_id: int, tracking_cog: RouteProgression | None,
                 header: str = "") -> None:
        super().__init__(timeout=PAGES_IDLE_SECONDS)
        self.pages = pages
        self.owner_id = owner_id
        self.tracking_cog = tracking_cog
        self.header = header
        self.index = 0
        # Pages whose embed Discord refused; shown as their plain text from then on.
        self.as_text: set[int] = set()
        if len(pages) < 2:
            for item in (self.previous_page, self.position, self.next_page):
                self.remove_item(item)
        self._refresh_buttons()

    @property
    def has_controls(self) -> bool:
        return len(self.pages) > 1 or self._trackable(0)

    def _trackable(self, index: int) -> bool:
        return self.tracking_cog is not None and self.pages[index].route is not None

    def _refresh_buttons(self) -> None:
        last = len(self.pages) - 1
        self.previous_page.disabled = self.index == 0
        self.next_page.disabled = self.index >= last
        self.position.label = f"{self.index + 1} / {len(self.pages)}"
        self.track.disabled = not self._trackable(self.index)

    def render(self, *, note: str | None = None) -> dict[str, Any]:
        """The message fields for the page showing."""
        head = "\n".join(part for part in (self.header, note) if part)
        page = self.pages[self.index]
        if page.embed is not None and self.index not in self.as_text:
            return {"content": head or None, "embed": page.embed}
        room = MAX_MESSAGE_CHARS - len(head) - 2
        body = fit_lines(page.text.splitlines(), limit=max(room, 100))
        return {"content": f"{head}\n\n{body}" if head else body, "embed": None}

    async def _turn(self, interaction: discord.Interaction, delta: int) -> None:
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message(
                f"These routes were worked out for <@{self.owner_id}>'s ship and settings - run the command "
                "yourself to page through your own.",
                ephemeral=True, allowed_mentions=NO_MENTIONS,
            )
            return
        self.index = max(0, min(len(self.pages) - 1, self.index + delta))
        self._refresh_buttons()
        try:
            await interaction.response.edit_message(**self.render(), view=self)
        except discord.HTTPException:
            if interaction.response.is_done():
                raise
            logger.warning("Discord refused route page %s's embed; showing it as text", self.index + 1)
            self.as_text.add(self.index)
            await interaction.response.edit_message(**self.render(), view=self)

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
        page = self.pages[self.index]
        if self.tracking_cog is None or page.route is None:
            await interaction.response.send_message("This route can't be tracked.", ephemeral=True)
            return
        await self.tracking_cog.start_tracking(interaction, page.route)

    async def on_timeout(self) -> None:
        await self.grey_out(**self.render(note=EXPIRED_NOTE))


async def send_route_pages(interaction: discord.Interaction, pages: list[RoutePage], *,
                           tracking_cog: RouteProgression | None, header: str = "", omitted: int = 0) -> None:
    """Send a route command's results as one paged message (see the module docstring)."""
    if omitted:
        note = f"-# {omitted} more route{'s' if omitted != 1 else ''} omitted - too large to display."
        header = f"{header}\n{note}" if header else note
    if not pages:
        await interaction.followup.send(header or "No routes to show.", allowed_mentions=NO_MENTIONS)
        return
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
