"""Shared by tests that check every view in the bot: import every cog, then list the
BotView subclasses defined in the bot package."""
import importlib
import pkgutil

import bot.cogs
from bot.discord_ui import BotView


def _subclasses(cls):
    for sub in cls.__subclasses__():
        yield sub
        yield from _subclasses(sub)


def all_bot_views() -> list[type]:
    for module in pkgutil.iter_modules(bot.cogs.__path__):
        importlib.import_module(f"bot.cogs.{module.name}")
    return [cls for cls in _subclasses(BotView) if cls.__module__.startswith("bot.")]
