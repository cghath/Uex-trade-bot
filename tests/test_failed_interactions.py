"""Audit REL-13, REL-16 and REL-17 - interactions that fail must still answer the player.

- REL-13: there was no command-tree or view error handler anywhere, so a command that raised
  after deferring stayed on "thinking..." forever, and a failing button looked like it did
  nothing. Ship Parts Finder also kept stale parts on screen after an unexpected load
  failure, and said nothing when it couldn't post its browser in the player's thread.
- REL-16: the private-thread get-or-create had no lock, so two quick calls each made a thread.
- REL-17: a date-format `Retry-After` on a 429 raised ValueError out of the UEX client, and a
  huge value had no cap.
"""
import asyncio
import importlib
import inspect
import pkgutil
from datetime import datetime, timezone
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock

import discord
import httpx
import pytest
from cryptography.fernet import Fernet
from discord import app_commands
from discord.ext import commands

import bot.cogs
from bot.cogs import blueprint_planner, ship_parts_finder
from bot.cogs.blueprint_planner import ShoppingService
from bot.cogs.ship_parts_finder import (
    PartsBrowserView,
    ShipPartsFinder,
    ShipPartsShoppingService,
)
from bot.config import Config
from bot.db.database import Database
from bot.discord_ui import (
    UNEXPECTED_ERROR_MESSAGE,
    BotModal,
    BotView,
    on_app_command_error,
)
from bot.main import UexBot
from bot.uex import client as client_module
from bot.uex.client import RETRY_AFTER_MAX_SECONDS, UexClient, retry_after_seconds
from bot.uex.exceptions import UexApiError
from bot.uex.ship_parts import ShipPort


def _http_error(status=500):
    return discord.HTTPException(NS(status=status, reason="x"), "x")


def _interaction(*, done, command="ship-parts-finder"):
    return NS(
        command=NS(qualified_name=command) if command else None,
        response=NS(is_done=lambda: done, send_message=AsyncMock()),
        followup=NS(send=AsyncMock()),
    )


# ---- slash commands --------------------------------------------------------------------

def test_a_command_that_fails_after_deferring_replaces_thinking_with_a_message():
    interaction = _interaction(done=True)
    asyncio.run(on_app_command_error(interaction, app_commands.AppCommandError("boom")))
    interaction.followup.send.assert_awaited_once_with(UNEXPECTED_ERROR_MESSAGE, ephemeral=True)
    interaction.response.send_message.assert_not_awaited()


def test_a_command_that_fails_before_answering_gets_a_direct_reply():
    interaction = _interaction(done=False)
    asyncio.run(on_app_command_error(interaction, app_commands.AppCommandError("boom")))
    interaction.response.send_message.assert_awaited_once_with(UNEXPECTED_ERROR_MESSAGE, ephemeral=True)


def test_a_missing_permission_names_the_permission_and_the_command():
    interaction = _interaction(done=False, command="set-digest-channel")
    asyncio.run(on_app_command_error(interaction, app_commands.MissingPermissions(["manage_guild"])))
    assert interaction.response.send_message.await_args.args[0] == (
        "You need the Manage Server permission to use `/set-digest-channel`."
    )


def test_an_expired_interaction_is_only_logged():
    interaction = _interaction(done=True)
    interaction.followup.send.side_effect = _http_error(404)
    asyncio.run(on_app_command_error(interaction, app_commands.AppCommandError("boom")))


def test_the_bot_registers_the_handler_on_its_command_tree(tmp_path):
    config = Config(discord_bot_token="x", discord_dev_guild_id=None, uex_app_token="t", uex_secret_key=None,
                    database_path=tmp_path / "bot.sqlite3", scanner_steal_threshold=0.5)
    bot = UexBot(config)
    try:
        assert bot.tree.on_error is on_app_command_error
    finally:
        asyncio.run(bot.uex.aclose())


def _bot_modules():
    return [importlib.import_module(f"bot.cogs.{m.name}") for m in pkgutil.iter_modules(bot.cogs.__path__)]


def test_no_cog_answers_command_errors_itself():
    """The tree's handler always answers, so a cog handler that also replied would send the
    player two messages (digest's did, for missing permissions)."""
    cogs = [cls for module in _bot_modules() for _, cls in inspect.getmembers(module, inspect.isclass)
            if issubclass(cls, commands.Cog) and cls.__module__ == module.__name__]
    assert cogs
    overriding = [cls.__name__ for cls in cogs if cls.cog_app_command_error is not commands.Cog.cog_app_command_error]
    assert overriding == []


# ---- views and modals ------------------------------------------------------------------

def _all_subclasses(cls):
    for sub in cls.__subclasses__():
        yield sub
        yield from _all_subclasses(sub)


def test_every_view_and_modal_in_the_bot_answers_when_it_fails():
    _bot_modules()
    views = [c for c in _all_subclasses(discord.ui.View) if c.__module__.startswith("bot.")]
    modals = [c for c in _all_subclasses(discord.ui.Modal) if c.__module__.startswith("bot.")]
    assert len(views) > 20 and len(modals) > 5
    assert [c.__name__ for c in views if not issubclass(c, BotView)] == []
    assert [c.__name__ for c in modals if not issubclass(c, BotModal)] == []


def test_a_failing_button_or_form_tells_the_player():
    async def run():
        clicked = _interaction(done=False)
        await BotView().on_error(clicked, RuntimeError("boom"), NS())
        submitted = _interaction(done=True)
        await BotModal(title="x").on_error(submitted, RuntimeError("boom"))
        return clicked, submitted

    clicked, submitted = asyncio.run(run())
    clicked.response.send_message.assert_awaited_once_with(UNEXPECTED_ERROR_MESSAGE, ephemeral=True)
    submitted.followup.send.assert_awaited_once_with(UNEXPECTED_ERROR_MESSAGE, ephemeral=True)


# ---- Ship Parts Finder -----------------------------------------------------------------

VEHICLE = {"id": 100, "name": "Perseus"}
RADAR = ShipPort(name="hardpoint_radar", port_type="Radar", size_min=2, size_max=2)


@pytest.mark.parametrize("error", [RuntimeError("boom"), UexApiError("503")])
def test_a_failed_part_load_keeps_the_browser_and_says_to_pick_again(error):
    async def run():
        cog = NS(candidates_for_port=AsyncMock(side_effect=error))
        view = PartsBrowserView(cog, VEHICLE, (5, "Omega Pro"), {"Radar": [RADAR]})
        interaction = NS(response=NS(defer=AsyncMock()), edit_original_response=AsyncMock())
        await view.show_category(interaction, "Radar")
        return view, interaction

    view, interaction = asyncio.run(run())
    content = interaction.edit_original_response.await_args.kwargs["content"]
    assert "⚠️ Couldn't load Radar options right now. Pick it again to retry." in content
    assert "Selected so far" in content, "the browser stays, not a bare error line"
    assert view.candidates == []


def test_the_slot_cap_is_said_not_silent():
    ports = [ShipPort(name=f"hardpoint_turret_{i}", port_type="Turret", size_min=2, size_max=2) for i in range(27)]
    view = PartsBrowserView(NS(), VEHICLE, (5, "Omega Pro"), {"Turrets": ports})
    view.category = "Turrets"
    assert "This ship has 27 Gun Mounts slots; only the first 25 can be listed." in view.text()


def test_the_command_says_so_when_it_cant_post_the_browser_in_the_thread():
    async def run():
        db = NS(resolve_terminal_id_by_name=AsyncMock(return_value=(5, "Omega Pro")))
        cog = ShipPartsFinder(NS(db=db, uex=NS(get_vehicles=AsyncMock(return_value=[VEHICLE]))),
                              wiki_client=NS(), start_refresh=False)
        cog._ports_for_vehicle = AsyncMock(return_value=[RADAR])
        cog.turret_gun_lookups_unanswered = lambda ports: 0
        thread = NS(id=1, mention="<#1>", send=AsyncMock(side_effect=_http_error()))
        cog.shopping._thread = AsyncMock(return_value=thread)
        interaction = NS(response=NS(defer=AsyncMock()), followup=NS(send=AsyncMock()))
        await ShipPartsFinder.ship_parts_finder.callback(cog, interaction, "Perseus", "Omega Pro")
        return cog, interaction

    cog, interaction = asyncio.run(run())
    assert interaction.followup.send.await_args.args[0] == "I couldn't post the browser in <#1>. Please try again."
    assert cog._browsers == {}


# ---- REL-16: one private thread per player, even on a double click ----------------------

class _Thread:
    id = 900
    mention = "<#900>"
    archived = False

    def __init__(self):
        self.add_user = AsyncMock()
        self.send = AsyncMock(return_value=NS(id=901))


class _Channel:
    def __init__(self):
        self.thread = _Thread()
        self.created = 0

    async def create_thread(self, **kwargs):
        self.created += 1
        await asyncio.sleep(0)
        return self.thread


@pytest.mark.parametrize("service_cls", [ShipPartsShoppingService, ShoppingService])
def test_two_quick_calls_share_one_private_thread(tmp_path, monkeypatch, service_cls):
    for module in (ship_parts_finder, blueprint_planner):
        monkeypatch.setattr(module.discord, "TextChannel", _Channel)
        monkeypatch.setattr(module.discord, "Thread", _Thread)

    async def run():
        db = Database(tmp_path / "threads.sqlite3", Fernet(Fernet.generate_key()))
        await db.init()
        channel = _Channel()
        service = service_cls(NS(db=db, get_channel=lambda _id: channel.thread, fetch_channel=AsyncMock()))
        interaction = NS(guild_id=10, channel=channel, user=NS(id=1, display_name="Pilot"))
        threads = await asyncio.gather(service._thread(interaction), service._thread(interaction))
        return channel, threads

    channel, threads = asyncio.run(run())
    assert channel.created == 1
    assert threads == [channel.thread, channel.thread]


# ---- REL-17: Retry-After ---------------------------------------------------------------

NOW = datetime(2026, 9, 29, 12, 0, 0, tzinfo=timezone.utc)


@pytest.mark.parametrize("header, expected", [
    ("5", 5.0),
    ("Tue, 29 Sep 2026 12:00:30 GMT", 30.0),
    ("Tue, 29 Sep 2026 11:00:00 GMT", 0.0),  # already past: go now
    ("99999", RETRY_AFTER_MAX_SECONDS),
    ("Tue, 29 Sep 2026 14:00:00 GMT", RETRY_AFTER_MAX_SECONDS),
    ("soon", 4.0), ("-3", 4.0), ("nan", 4.0), ("inf", 4.0), ("", 4.0), (None, 4.0),
])
def test_retry_after_reads_seconds_or_a_date_and_is_capped(header, expected):
    assert retry_after_seconds(header, 2, NOW) == expected


def test_a_date_retry_after_no_longer_breaks_the_request(monkeypatch):
    responses = [
        httpx.Response(429, headers={"Retry-After": "Wed, 21 Oct 2015 07:28:00 GMT"}),
        httpx.Response(429, headers={"Retry-After": "99999"}),
        httpx.Response(200, json={"status": "ok", "data": [{"id": 1}]}),
    ]
    waits = []

    async def fake_sleep(seconds):
        waits.append(seconds)

    monkeypatch.setattr(client_module.asyncio, "sleep", fake_sleep)

    async def run():
        client = UexClient(app_token="test", base_url="https://uex.test")
        await client._client.aclose()
        client._client = httpx.AsyncClient(transport=httpx.MockTransport(lambda request: responses.pop(0)))
        try:
            return await client._request("GET", "terminals")
        finally:
            await client.aclose()

    assert asyncio.run(run()) == [{"id": 1}]
    assert waits == [0.0, RETRY_AFTER_MAX_SECONDS]
