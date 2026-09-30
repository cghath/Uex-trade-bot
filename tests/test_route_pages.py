"""Route results as one paged message (audit UX-6, bot/route_pages.py).

Route commands used to post an intro plus a public message per route. Now it's one
message: ◀ ▶ page through the routes (only for whoever ran the command, since the routes
were worked out for their ship and settings), and Track this route works for anyone."""
from __future__ import annotations

import asyncio
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock

import discord

from bot.delivery import MAX_MESSAGE_CHARS
from bot.route_pages import EXPIRED_NOTE, RoutePage, RoutePagesView, send_route_pages, text_pages

OWNER = 111
SOMEONE_ELSE = 222
HEADER = "**Top routes**\n-# ranked by UEX score"


def _refused() -> discord.HTTPException:
    return discord.HTTPException(NS(status=400, reason="Bad Request"), "Invalid Form Body")


class _Response:
    def __init__(self, refuse_edits: int = 0) -> None:
        self.sent: list[tuple[str, dict]] = []
        self.edits: list[dict] = []
        self.refuse_edits = refuse_edits
        self._done = False

    def is_done(self) -> bool:
        return self._done

    async def send_message(self, content=None, **kwargs) -> None:
        self.sent.append((content, kwargs))
        self._done = True

    async def edit_message(self, **kwargs) -> None:
        if self.refuse_edits:
            self.refuse_edits -= 1
            raise _refused()
        self.edits.append(kwargs)
        self._done = True


class _Followup:
    def __init__(self, refuse_embeds: int = 0) -> None:
        self.sent: list[dict] = []
        self.refuse_embeds = refuse_embeds
        self.messages: list[NS] = []

    async def send(self, content=None, **kwargs):
        if kwargs.get("embed") is not None and self.refuse_embeds:
            self.refuse_embeds -= 1
            raise _refused()
        self.sent.append(dict(kwargs, content=content) if content is not None else kwargs)
        message = NS(id=len(self.sent), edits=[], flags=NS(ephemeral=False))

        async def edit(**edit_kwargs):
            message.edits.append(edit_kwargs)
        message.edit = edit
        message.channel = NS(get_partial_message=lambda _id: NS(edit=edit))
        self.messages.append(message)
        return message


def _interaction(user_id: int = OWNER, *, refuse_edits: int = 0, refuse_embeds: int = 0) -> NS:
    return NS(user=NS(id=user_id), response=_Response(refuse_edits), followup=_Followup(refuse_embeds))


def _pages(count: int, *, trackable: bool = True) -> list[RoutePage]:
    return [RoutePage(discord.Embed(title=f"Route {i}"), f"**Route {i}**\nBuy here, sell there {i}",
                      NS(title=f"route {i}") if trackable else None)
            for i in range(1, count + 1)]


def _tracker() -> NS:
    return NS(start_tracking=AsyncMock())


async def _send(pages, *, tracker=None, header=HEADER, omitted=0, **interaction_kwargs):
    interaction = _interaction(**interaction_kwargs)
    await send_route_pages(interaction, pages, tracking_cog=tracker, header=header, omitted=omitted)
    return interaction


def test_every_route_arrives_in_one_message_showing_the_first():
    async def run():
        pages = _pages(3)
        interaction = await _send(pages, tracker=_tracker())

        assert len(interaction.followup.sent) == 1, interaction.followup.sent
        sent = interaction.followup.sent[0]
        view = sent["view"]
        assert isinstance(view, RoutePagesView)
        assert sent["embed"] is pages[0].embed
        assert sent["content"] == HEADER
        assert sent["wait"] is True, "the message is needed to grey the buttons out later"
        assert view.message is interaction.followup.messages[0]
        assert view.position.label == "1 / 3"
        assert view.previous_page.disabled and not view.next_page.disabled
        assert sent["allowed_mentions"].users is False, "results never ping anyone"

    asyncio.run(run())


def test_only_the_player_who_ran_it_can_turn_the_page():
    async def run():
        pages = _pages(3)
        view = (await _send(pages, tracker=_tracker())).followup.sent[0]["view"]

        stranger = _interaction(SOMEONE_ELSE)
        await view.next_page.callback(stranger)
        assert view.index == 0 and not stranger.response.edits
        (reply, kwargs), = stranger.response.sent
        assert kwargs["ephemeral"] is True and f"<@{OWNER}>" in reply and "yourself" in reply

        owner = _interaction(OWNER)
        await view.next_page.callback(owner)
        edit, = owner.response.edits
        assert view.index == 1
        assert edit["embed"] is pages[1].embed and edit["content"] == HEADER and edit["view"] is view
        assert view.position.label == "2 / 3" and not view.previous_page.disabled

        await view.next_page.callback(_interaction(OWNER))
        assert view.index == 2 and view.next_page.disabled
        await view.previous_page.callback(_interaction(OWNER))
        assert view.index == 1

    asyncio.run(run())


def test_anyone_can_track_the_route_showing():
    async def run():
        pages = _pages(3)
        tracker = _tracker()
        view = (await _send(pages, tracker=tracker)).followup.sent[0]["view"]
        await view.next_page.callback(_interaction(OWNER))

        stranger = _interaction(SOMEONE_ELSE)
        await view.track.callback(stranger)
        tracker.start_tracking.assert_awaited_once_with(stranger, pages[1].route)

    asyncio.run(run())


def test_track_is_greyed_out_on_a_route_that_cant_be_tracked():
    async def run():
        pages = [*_pages(1), *_pages(1, trackable=False)]
        view = (await _send(pages, tracker=_tracker())).followup.sent[0]["view"]
        assert not view.track.disabled
        await view.next_page.callback(_interaction(OWNER))
        assert view.track.disabled

        without_tracking = (await _send(_pages(2), tracker=None)).followup.sent[0]["view"]
        assert without_tracking.track.disabled, "route tracking isn't loaded"

    asyncio.run(run())


def test_one_route_has_no_paging_buttons():
    async def run():
        trackable = (await _send(_pages(1), tracker=_tracker())).followup.sent[0]
        assert [item.label for item in trackable["view"].children] == ["Track this route"]

        untrackable = (await _send(_pages(1, trackable=False), tracker=_tracker())).followup.sent[0]
        assert "view" not in untrackable, "nothing to click, so no buttons"
        assert untrackable["embed"] is not None and untrackable["content"] == HEADER

    asyncio.run(run())


def test_an_embed_discord_refuses_is_shown_as_its_text():
    async def run():
        pages = _pages(2)
        interaction = await _send(pages, tracker=_tracker(), refuse_embeds=1)
        sent, = interaction.followup.sent
        assert "embed" not in sent
        assert sent["content"].startswith(HEADER) and pages[0].text in sent["content"]
        view = sent["view"]

        turn = _interaction(OWNER, refuse_edits=1)
        await view.next_page.callback(turn)
        edit, = turn.response.edits
        assert edit["embed"] is None and pages[1].text in edit["content"]

    asyncio.run(run())


def test_a_long_route_is_split_over_text_pages_without_losing_a_line():
    lines = [f"Leg {i:03}: buy Agricium at a terminal with a long name, sell it somewhere else" for i in range(90)]
    pages = text_pages("\n".join(lines), route=NS(title="long"), header=HEADER)

    assert len(pages) > 1
    assert all(page.embed is None and page.route.title == "long" for page in pages)
    assert [f"(part {i} of {len(pages)})" in page.text for i, page in enumerate(pages, 1)] == [True] * len(pages)
    shown = [line for page in pages for line in page.text.splitlines() if line.startswith("Leg ")]
    assert shown == lines

    async def run():
        view = RoutePagesView(pages, owner_id=OWNER, tracking_cog=_tracker(), header=HEADER)
        for index in range(len(pages)):
            view.index = index
            content = view.render()["content"]
            assert len(content) <= MAX_MESSAGE_CHARS and "…and" not in content, "a page must fit whole"

    asyncio.run(run())


def test_expired_buttons_grey_out_and_say_so():
    async def run():
        pages = _pages(3)
        interaction = await _send(pages, tracker=_tracker())
        view = interaction.followup.sent[0]["view"]
        await view.next_page.callback(_interaction(OWNER))

        await view.on_timeout()

        edit, = interaction.followup.messages[0].edits
        assert EXPIRED_NOTE in edit["content"] and edit["content"].startswith(HEADER)
        assert edit["embed"] is pages[1].embed, "the route showing stays up"
        assert all(item.disabled for item in view.children)

    asyncio.run(run())


def test_omitted_routes_are_noted_and_an_empty_result_still_answers():
    async def run():
        sent = (await _send(_pages(2), tracker=_tracker(), omitted=2)).followup.sent[0]
        assert "2 more routes omitted" in sent["content"]

        empty = (await _send([], tracker=_tracker(), omitted=1)).followup.sent
        assert len(empty) == 1 and "view" not in empty[0]
        assert empty[0]["content"].startswith(HEADER) and "1 more route omitted" in empty[0]["content"]

    asyncio.run(run())
