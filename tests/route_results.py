"""Read back the one results message a route command sends (bot/route_pages.py).

Route commands used to send an intro embed and then one message per route; tests read the
intro's footer and each route's embed from those sends. Now there's one message: the intro
is its header text and each route is a page, so tests read the pages from its view (or,
for a single untrackable route sent without buttons, from the message itself). A layout
route page (/multi-stop-route) has no embed: its title and footer are its first and last
text blocks, and its text is all of them."""
from __future__ import annotations

from dataclasses import dataclass
from types import SimpleNamespace as NS
from typing import Any

import discord

from bot.route_pages import RoutePage, RoutePaging


def _calls(sent: Any) -> list[tuple[tuple, dict]]:
    """(args, kwargs) for each send, from a fake followup's `sent` list, a mock's
    call_args_list, or the mock itself."""
    items = getattr(sent, "call_args_list", sent)
    return [(item.args, item.kwargs) if hasattr(item, "kwargs") else item for item in items]


@dataclass
class RouteResults:
    header: str
    pages: list[RoutePage]
    view: RoutePaging | None

    @property
    def embeds(self) -> list[discord.Embed]:
        return [page.embed for page in self.pages if page.embed is not None]

    @property
    def titles(self) -> list[str]:
        """Each route's title: its embed's, or a layout page's heading."""
        return [page.embed.title if page.embed is not None else page.blocks[0].splitlines()[0].removeprefix("## ")
                for page in self.pages if page.embed is not None or page.blocks]

    @property
    def footers(self) -> list[str]:
        """Each route's small print: its embed footer, or a layout page's last block."""
        return [page.embed.footer.text or "" if page.embed is not None else page.blocks[-1].removeprefix("-# ")
                for page in self.pages if page.embed is not None or page.blocks]

    @property
    def text_pages(self) -> list[str]:
        return [page.text for page in self.pages if page.embed is None]

    def field_text(self) -> str:
        """Every field value of every route embed."""
        return "\n".join(field.value or "" for embed in self.embeds for field in embed.fields)

    def all_text(self) -> str:
        """Everything a player could read: the header, every embed, every text page."""
        parts = [self.header]
        for embed in self.embeds:
            parts += [embed.title or "", embed.description or "", embed.footer.text or ""]
            parts += [f"{field.name}\n{field.value}" for field in embed.fields]
        return "\n".join([*parts, *self.text_pages])

    def embed_calls(self) -> list[NS]:
        """Each route embed dressed as a send call (`.kwargs["embed"]`), for tests written
        against the old one-message-per-route shape."""
        return [NS(args=(), kwargs={"embed": embed}) for embed in self.embeds]


def route_results(sent: Any) -> RouteResults | None:
    """The route results message among `sent` (the last one), or None if none was sent."""
    for _args, kwargs in reversed(_calls(sent)):
        view = kwargs.get("view")
        if isinstance(view, RoutePaging):
            return RouteResults(view.header, view.pages, view)
        if kwargs.get("embed") is not None:
            return RouteResults(kwargs.get("content") or "", [RoutePage(kwargs["embed"], "")], None)
    return None
