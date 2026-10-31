"""The welcome DM a new member of Star Tech Dynamics gets (the owner's idea, 2026-10-14). Pure
wording; bot/cogs/welcome.py sends it.

The owner's decisions: a DM, sent once, that welcomes them and names a handful of useful
commands and the bot's channel - nothing more ("just let them get started and something useful
and don't overload them"), so no pointer to /intro. The commands are the ones other players use
most (/multi-stop-route, /ingame-item-finder, /ship-loadout) plus /ship-parts-finder (to compare
parts), /where-to-mine and /blueprint-search, the owner's picks.

Since 2026-10-30 (the owner's pick of the layouts offered) it first points to the server's new
places - say hi in #👋welcome, ops in #📅events, roles in Channels & Roles or the role panels,
the rules - then the commands. It's sent once the member has accepted the rules, so the rules
are there to look back at, not a step left to do.
"""
from __future__ import annotations

from bot.uex.anniversaries import COMMUNITY_NAME

WELCOME_CHANNEL_ID = 1565606894482096158  # #👋welcome
EVENTS_CHANNEL_ID = 1565625163347656704   # #📅events
ROLES_CHANNEL_ID = 1565594422228557914    # #🙋‍♂️pick-your-roles🙋‍♂️
RULES_CHANNEL_ID = 1565609767181754399    # #📜rules

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
             f"• Say hi in <#{WELCOME_CHANNEL_ID}> and tell us what you fly",
             f"• Upcoming ops are in <#{EVENTS_CHANNEL_ID}>",
             f"• Pick your roles and pings any time in Channels & Roles or <#{ROLES_CHANNEL_ID}>",
             f"• Server rules: <#{RULES_CHANNEL_ID}>",
             "",
             f"Our bot can help too (use it in <#{channel_id}>):"]
    lines += [f"• `/{command}`: {what}" for command, what in HIGHLIGHTS]
    return "\n".join(lines)
