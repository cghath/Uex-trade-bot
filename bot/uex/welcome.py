"""The welcome DM a new member of Star Tech Dynamics gets (the owner's idea, 2026-10-14). Pure
wording; bot/cogs/welcome.py sends it.

The owner's decisions: a DM, sent once, that welcomes them and names a handful of useful
commands and the bot's channel - nothing more ("just let them get started and something useful
and don't overload them"), so no pointer to /intro. The commands are the ones other players use
most (/multi-stop-route, /ingame-item-finder, /ship-loadout) plus /ship-parts-finder (to compare
parts), /where-to-mine and /blueprint-search, the owner's picks.
"""
from __future__ import annotations

from bot.uex.anniversaries import COMMUNITY_NAME

HIGHLIGHTS = (
    ("multi-stop-route", "the best trade run for your ship"),
    ("ingame-item-finder", "where to buy any weapon, armor or other item"),
    ("ship-loadout", "the best parts for your ship"),
    ("ship-parts-finder", "compare parts for your ship and where to buy them"),
    ("where-to-mine", "where to find an ore or mineral"),
    ("blueprint-search", "where to get a blueprint and what it takes"),
)


def welcome_text(name: str, channel_id: int) -> str:
    lines = [f"👋 **Welcome to {COMMUNITY_NAME}, {name}!**",
             "We're glad you're here. A few things our bot can help you with:"]
    lines += [f"• `/{command}`: {what}" for command, what in HIGHLIGHTS]
    lines.append(f"You can use it in <#{channel_id}>.")
    return "\n".join(lines)
