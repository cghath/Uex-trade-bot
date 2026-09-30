"""The default ship lives in /set-trading-preferences (audit UX-15), and nothing the bot
says names a command that doesn't exist (audit MSG-21)."""
from __future__ import annotations

import ast
import asyncio
import inspect
import re
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock

from cryptography.fernet import Fernet
from discord.ext import commands

from bot import main as bot_main
from bot.cogs.trading_preferences import CLEAR_SHIP, TradingPreferences, ship_preference_autocomplete
from bot.db.database import Database

COMMAND_REFERENCE = re.compile(r"(?<![\w/.])/([a-z][a-z0-9]*(?:-[a-z0-9]+)*)(?![\w/.-])")
# API clients whose strings are endpoint paths ("/items", "/vehicles"), not commands.
API_CLIENTS = {Path("bot/wiki_api.py"), Path("bot/uex/client.py")}


def _all_commands() -> set[str]:
    names = set()
    for path in bot_main.INITIAL_COGS:
        module = __import__(path, fromlist=["_"])
        for _, cls in inspect.getmembers(module, inspect.isclass):
            if issubclass(cls, commands.Cog) and cls.__module__ == module.__name__:
                names |= {command.name for command in cls.__cog_app_commands__}
    return names


def _message_strings(tree: ast.AST):
    """Every string constant in a module except docstrings, which describe code, not replies."""
    docstrings = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)) and node.body:
            first = node.body[0]
            if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant):
                docstrings.add(id(first.value))
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in docstrings:
            yield node


def test_nothing_the_bot_says_names_a_command_that_does_not_exist():
    real = _all_commands()
    stale = []
    for path in sorted(Path("bot").rglob("*.py")):
        if path in API_CLIENTS:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in _message_strings(tree):
            for name in COMMAND_REFERENCE.findall(node.value):
                # One letter is a unit ("regens 40/s"), not a command.
                if len(name) > 1 and name not in real:
                    stale.append(f"{path}:{node.lineno} /{name}")
    assert stale == []


def test_the_ship_commands_are_folded_into_trading_preferences():
    real = _all_commands()
    assert {"set-default-ship", "clear-default-ship"} & real == set()
    assert "bot.cogs.ships" not in bot_main.INITIAL_COGS
    ship = next(p for p in TradingPreferences.set_trading_preferences.parameters if p.name == "ship")
    assert "every route command" in ship.description and "clears it" in ship.description


def _autocomplete(current: str) -> list[str]:
    vehicles = [dict(name="Cutlass Black"), dict(name="Caterpillar")]
    interaction = NS(client=NS(uex=NS(get_vehicles=AsyncMock(return_value=vehicles))))
    return [choice.value for choice in asyncio.run(ship_preference_autocomplete(interaction, current))]


def test_the_ship_autocomplete_offers_to_clear_the_ship_first():
    assert _autocomplete("") == [CLEAR_SHIP, "Cutlass Black", "Caterpillar"]
    assert _autocomplete("no def")[0] == CLEAR_SHIP
    assert _autocomplete("cut") == ["Cutlass Black"]


class _Interaction:
    def __init__(self) -> None:
        self.user = NS(id=1)
        self.response = NS(send_message=AsyncMock(), defer=AsyncMock())
        self.followup = NS(send=AsyncMock())


def _set(db, uex, **options):
    cog = TradingPreferences.__new__(TradingPreferences)
    cog.bot = NS(db=db, uex=uex)
    interaction = _Interaction()
    values = dict(ship=None, budget=None, space_only=None, capital_ship_access=None, auto_load_only=None,
                  system=None, risk_tolerance=None)
    values.update(options)
    asyncio.run(cog.set_trading_preferences.callback(cog, interaction, **values))
    return interaction.followup.send.call_args.args[0]


def test_setting_a_ship_confirms_its_cargo_capacity(tmp_path):
    db = Database(tmp_path / "prefs.sqlite3", Fernet(Fernet.generate_key()))
    asyncio.run(db.init())
    uex = NS(get_vehicles=AsyncMock(return_value=[dict(name="Cutlass Black", scu=46)]))
    assert "Default ship: **Cutlass Black** (46 SCU)" in _set(db, uex, ship="Cutlass Black")


def test_choosing_no_default_ship_clears_only_the_ship_without_asking_uex(tmp_path):
    db = Database(tmp_path / "prefs.sqlite3", Fernet(Fernet.generate_key()))
    uex = NS(get_vehicles=AsyncMock(return_value=[dict(name="Cutlass Black", scu=46)]))

    async def seed():
        await db.init()
        await db.set_trading_preferences(1, ship_name="Cutlass Black", space_only=True)

    asyncio.run(seed())
    message = _set(db, uex, ship=CLEAR_SHIP)
    uex.get_vehicles.assert_not_awaited()
    assert "Default ship: **None set**" in message
    prefs = asyncio.run(db.get_trading_preferences(1))
    assert prefs["ship_name"] is None and prefs["space_only"]
