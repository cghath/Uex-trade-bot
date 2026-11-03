"""Audit UX-7: a view's buttons and menus stop working when it times out, but they looked
exactly as usable as before, and a click just showed "This interaction failed". Every
BotView now greys its controls out on its message when it times out.

The message can only be edited through something that still reaches it: the freshest click
that updated it, the interaction that sent it, the sent message, or - for a public message -
a plain channel edit, since interaction tokens expire after 15 minutes."""
import asyncio
import inspect
import json
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock

import discord
import httpx
from cryptography.fernet import Fernet

from bot.cogs.blueprints import Blueprints, SearchResult
from bot.cogs.marketplace import ConfirmListingView, Marketplace
from bot.cogs.personal_inventory import SetMinimumPricesView
from bot.cogs.prices import Prices
from bot.cogs.route_progression import RouteProgression
from bot.cogs.ship_parts_finder import PartsBrowserView
from bot.db.database import Database
from bot.discord_ui import AlertRemovePickerView, BotView, send_alert_remove_picker
from bot.route_pages import RoutePaging
from bot.uex.blueprint_crafting import Recipe
from bot.uex.client import UexClient
from tests.bot_views import all_bot_views
from tests.test_route_send_shape import _MULTI_STOP_ROWS, _transport

UPDATE = discord.InteractionResponseType.message_update
NEW_REPLY = discord.InteractionResponseType.channel_message


def _http_error(status=401):
    return discord.HTTPException(NS(status=status, reason="x"), "x")


def _view():
    view = BotView()
    view.add_item(discord.ui.Button(label="Go"))
    view.add_item(discord.ui.Select(options=[discord.SelectOption(label="a")]))
    return view


def _click(response_type):
    return NS(response=NS(type=response_type), edit_original_response=AsyncMock())


def _message(*, ephemeral, edit_error=None):
    partial = NS(edit=AsyncMock())
    channel = NS(get_partial_message=lambda message_id: partial)
    return NS(id=42, flags=NS(ephemeral=ephemeral), channel=channel,
              edit=AsyncMock(side_effect=edit_error)), partial


# ---- how a view greys out ----------------------------------------------------------------

def test_timing_out_greys_out_every_control_on_the_message():
    async def run():
        view = _view()
        view.origin = NS(edit_original_response=AsyncMock())
        await view.on_timeout()
        return view

    view = asyncio.run(run())
    assert all(child.disabled for child in view.children)
    view.origin.edit_original_response.assert_awaited_once_with(view=view)


def test_the_freshest_click_that_updated_the_message_is_tried_first():
    async def run():
        view = _view()
        view.origin = NS(edit_original_response=AsyncMock())
        updated, new_reply = _click(UPDATE), _click(NEW_REPLY)
        for click in (updated, new_reply):
            assert await view.interaction_check(click)
        assert await view.grey_out()
        return view, updated, new_reply

    view, updated, new_reply = asyncio.run(run())
    updated.edit_original_response.assert_awaited_once_with(view=view)
    new_reply.edit_original_response.assert_not_awaited()  # its reply is a different message
    view.origin.edit_original_response.assert_not_awaited()


def test_an_expired_token_falls_back_to_a_plain_channel_edit_for_a_public_message():
    async def run():
        view = _view()
        view.origin = NS(edit_original_response=AsyncMock(side_effect=_http_error()))
        view.message, partial = _message(ephemeral=False, edit_error=_http_error())
        assert await view.grey_out()
        return view, partial

    view, partial = asyncio.run(run())
    partial.edit.assert_awaited_once_with(view=view)


def test_an_ephemeral_message_past_its_token_is_left_alone_without_raising():
    async def run():
        view = _view()
        view.message, partial = _message(ephemeral=True, edit_error=_http_error())
        return await view.grey_out(), partial

    edited, partial = asyncio.run(run())
    assert edited is False
    partial.edit.assert_not_awaited()


def test_views_that_check_clicks_still_record_them():
    overriding = [cls for cls in all_bot_views() if cls.interaction_check is not BotView.interaction_check]
    assert AlertRemovePickerView in overriding and ConfirmListingView in overriding
    missing = [cls.__name__ for cls in overriding
               if "super().interaction_check(" not in inspect.getsource(cls.interaction_check)]
    assert missing == []

    async def run():
        view = AlertRemovePickerView(alerts=[{"id": 1, "label": "x"}], author_id=7,
                                     remove_callback=AsyncMock())
        click = _click(UPDATE)
        click.user = NS(id=7)
        assert await view.interaction_check(click)
        return view, click

    view, click = asyncio.run(run())
    assert view._clicks == [click]


def test_views_with_their_own_timeout_still_grey_out():
    """Only the Ship Parts Finder browser greys out its own way (it adds a note); the route
    pages grey out through grey_out(), adding their own note."""
    own = [cls for cls in all_bot_views() if cls.on_timeout is not BotView.on_timeout]
    missing = [cls.__name__ for cls in own
               if cls is not PartsBrowserView
               and not any(call in inspect.getsource(cls.on_timeout) for call in ("super().on_timeout(", "self.grey_out("))]
    assert missing == []

    async def run():
        view = ConfirmListingView(NS(), secret_key="sk", payload={}, author_id=7)
        view.origin = NS(edit_original_response=AsyncMock())
        await view.on_timeout()
        return view

    view = asyncio.run(run())
    assert view.resolved is True
    view.origin.edit_original_response.assert_awaited_once_with(view=view)


# ---- the views know their message once sent ----------------------------------------------

class _Followup:
    def __init__(self):
        self.sent = []

    async def send(self, *args, **kwargs):
        self.sent.append((args, kwargs))
        return NS(id=len(self.sent), sent_with=kwargs) if kwargs.get("wait") else None


def test_the_alert_remove_menu_knows_its_interaction():
    async def run():
        interaction = NS(user=NS(id=7), response=NS(send_message=AsyncMock()))
        await send_alert_remove_picker(interaction, alerts=[{"id": 1, "label": "x"}],
                                       remove_callback=AsyncMock(), empty_message="none")
        return interaction

    interaction = asyncio.run(run())
    assert interaction.response.send_message.await_args.kwargs["view"].origin is interaction


def test_the_delete_listing_confirmation_knows_its_message(tmp_path):
    def handler(request):
        return httpx.Response(200, json={"status": "ok", "data": [
            {"id": 999, "title": "Laranite", "price": "150", "currency": "UEC", "unit": "unit"}]})

    async def run():
        db = Database(tmp_path / "delete.sqlite3", Fernet(Fernet.generate_key()))
        await db.init()
        await db.set_user_secret_key(7, "sk_test")
        client = UexClient(app_token="test", base_url="https://uex.test")
        await client._client.aclose()
        client._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        cog = Marketplace.__new__(Marketplace)
        cog.bot = NS(db=db, uex=client)
        interaction = NS(user=NS(id=7), response=NS(defer=AsyncMock()), followup=_Followup())
        try:
            await Marketplace.marketplace_delete_listing.callback(cog, interaction, 999)
        finally:
            await client.aclose()
        return interaction

    (_, kwargs), = asyncio.run(run()).followup.sent
    assert kwargs["view"].message.sent_with is kwargs


def test_every_route_tracking_view_knows_its_message(tmp_path):
    async def run():
        db = Database(tmp_path / "routes.sqlite3", Fernet(Fernet.generate_key()))
        await db.init()
        await db.record_terminal_market_snapshot(_MULTI_STOP_ROWS)
        client = UexClient(app_token="test", base_url="https://uex.test")
        await client._client.aclose()
        client._client = httpx.AsyncClient(transport=_transport())
        tracking = RouteProgression.__new__(RouteProgression)
        cog = Prices.__new__(Prices)
        cog.bot = NS(db=db, uex=client, get_cog=lambda name: tracking if name == "RouteProgression" else None)
        interaction = NS(user=NS(id=111), response=NS(defer=AsyncMock()), followup=_Followup())
        try:
            await cog.multi_stop_route.callback(cog, interaction, ship="TestShip")
        finally:
            await client.aclose()
        return interaction

    sent = [kwargs for _, kwargs in asyncio.run(run()).followup.sent if kwargs.get("view")]
    assert sent
    for kwargs in sent:
        assert isinstance(kwargs["view"], RoutePaging)
        assert kwargs["view"].message.sent_with is kwargs


def test_the_blueprint_craft_buttons_know_their_message():
    raw = json.loads((Path(__file__).parent / "fixtures" / "blueprint_crafting_rifle.json").read_text(encoding="utf-8-sig"))
    recipe = Recipe.parse(raw)
    layout = SearchResult("found", ("page",), "Rifle", ("## Rifle",), "**Crafting**", recipe=recipe, craft_quantity=1)
    text = SearchResult("found", ("page",), "Rifle", recipe=recipe, craft_quantity=1)

    async def run(result):
        followup = _Followup()
        await Blueprints.deliver(Blueprints.__new__(Blueprints), lambda **kw: followup.send(wait=True, **kw), result)
        return followup

    for result in (layout, text):
        (_, kwargs), = asyncio.run(run(result)).sent
        assert kwargs["view"].message.sent_with is kwargs


def test_the_minimum_price_menu_stops_once_its_message_becomes_the_authorize_screen():
    async def run():
        authorize = BotView()
        cog = NS(_build_authorize_screen=AsyncMock(return_value=(discord.Embed(title="Authorize"), authorize)))
        row = {"id": 1, "item_name": "Gold", "quantity": 5, "minimum_price": None}
        view = SetMinimumPricesView(cog, 7, [row], [row])
        interaction = NS(response=NS(edit_message=AsyncMock()))
        await view.resolve_minimum_set(interaction, 1, 100)
        return view, authorize, interaction

    view, authorize, interaction = asyncio.run(run())
    assert view.is_finished(), "its own timeout would put its greyed-out buttons back"
    assert authorize.origin is interaction
