"""Sending a background notification (an alert, a deal, a negotiation message) and saying
what happened, so the caller can decide whether it's done.

Callers used to catch a failed send, log it, and then mark the notification seen or the
alert fired anyway, which lost it for good after a one-off Discord hiccup. Retrying on
every failure is wrong too: a user with DMs closed would be re-sent the same thing every
poll, forever. So each send returns one of three outcomes:

- DELIVERED: it arrived. Mark it done.
- RETRY: Discord or the network had a temporary problem (5xx, 429, a dropped connection).
  Leave it pending; the next poll tries again.
- UNDELIVERABLE: Discord refused it outright (any other 4xx: DMs closed, missing channel
  permissions, unknown user, message too long). Trying again won't change that, so mark it
  done and stop, logging why.
"""
from __future__ import annotations

import asyncio
import enum
import logging
from typing import Any

import aiohttp
import discord
from discord import app_commands

logger = logging.getLogger("uexbot.delivery")

# Discord rejects a message longer than this outright (HTTP 400, error 50035).
MAX_MESSAGE_CHARS = 2000

# The same "where should this alert arrive?" option on every alert command (audit UX-12),
# stored in each alert table's `scope` column. New alerts default to a DM.
DELIVERY_CHOICES = [
    app_commands.Choice(name="DM me (default)", value="personal"),
    app_commands.Choice(name="Post in this channel and ping me", value="global"),
]
DELIVERY_DESCRIPTION = "Where it arrives: DM me (default), or post in this channel and ping me"


def delivery_scope(choice: app_commands.Choice[str] | None) -> str:
    return choice.value if choice else "personal"


def delivery_note(scope: str) -> str:
    """How an alert's confirmation says where it'll arrive."""
    return "I'll post here and ping you" if scope == "global" else "I'll DM you"


def delivery_label(alert: dict[str, Any]) -> str:
    """Where an existing alert arrives, for /alert-list and /alert-remove."""
    if alert.get("scope") == "global" and alert.get("channel_id") is not None:
        return f"<#{alert['channel_id']}>"
    return "DM"


# Everything a send can raise that says "didn't arrive" rather than "the code is broken".
_SEND_ERRORS = (discord.HTTPException, aiohttp.ClientError, asyncio.TimeoutError, OSError)


class Delivery(enum.Enum):
    DELIVERED = "delivered"
    RETRY = "retry"
    UNDELIVERABLE = "undeliverable"

    @property
    def settled(self) -> bool:
        """True when there's nothing left to do: it arrived, or it never can."""
        return self is not Delivery.RETRY


def classify_send_error(exc: BaseException) -> Delivery:
    """A 4xx other than 429 is Discord saying no to this message or recipient; anything else
    (5xx, 429 after discord.py's own retries, a network error) may work next time."""
    if isinstance(exc, discord.HTTPException):
        status = getattr(exc, "status", 0) or 0
        if 400 <= status < 500 and status != 429:
            return Delivery.UNDELIVERABLE
    return Delivery.RETRY


def fit_message(prefix: str, body: str, *, limit: int = MAX_MESSAGE_CHARS,
                cut_note: str = "… *(cut short - read the rest on UEX)*") -> str:
    """`prefix + body`, with `body` trimmed (and `cut_note` added) if the whole thing would
    exceed Discord's limit - a message over it is refused outright, never delivered."""
    if len(prefix) + len(body) <= limit:
        return prefix + body
    room = limit - len(prefix) - len(cut_note)
    if room <= 0:
        return (prefix + body)[:limit]
    return prefix + body[:room].rstrip() + cut_note


def fit_lines(lines: list[str], *, limit: int = MAX_MESSAGE_CHARS, footer: str | None = None,
              more: str = "…and {n} more.") -> str:
    """As many whole lines as fit under Discord's limit, the rest counted in a "…and N more"
    line rather than cut mid-line, with `footer` always kept last. A plain-text list over
    the limit is refused outright, so the whole command failed (audit UX-13)."""
    tail = [footer] if footer else []
    kept: list[str] = []
    for line in lines:
        remaining = len(lines) - len(kept) - 1
        trial = "\n".join([*kept, line, *([more.format(n=remaining)] if remaining else []), *tail])
        if len(trial) > limit:
            break
        kept.append(line)
    omitted = len(lines) - len(kept)
    return "\n".join([*kept, *([more.format(n=omitted)] if omitted else []), *tail])[:limit]


async def send_dm(bot: Any, user_id: int, content: str | None = None, *, label: str, **send_kwargs: Any) -> Delivery:
    """DM one user. `label` names the notification in the logs (e.g. "price alert #12")."""
    try:
        user = bot.get_user(user_id) or await bot.fetch_user(user_id)
        await user.send(content, **send_kwargs)
        return Delivery.DELIVERED
    except _SEND_ERRORS as exc:
        outcome = classify_send_error(exc)
        logger.warning("Couldn't DM %s to user %s (%s): %s", label, user_id, outcome.value, exc)
        return outcome


async def send_to_channel_or_dm(bot: Any, channel_id: int | None, user_id: int, content: str | None = None, *,
                                label: str, **send_kwargs: Any) -> Delivery:
    """Post in the channel, falling back to a DM when the channel can't be resolved or
    refuses the post (any channel post can 403 - see CLAUDE.md). Delivered if either path
    works; RETRY if either failed only temporarily; UNDELIVERABLE only if neither can ever
    work as things stand."""
    channel_outcome = None
    channel = bot.get_channel(channel_id) if channel_id is not None else None
    if channel is not None:
        try:
            await channel.send(content, **send_kwargs)
            return Delivery.DELIVERED
        except _SEND_ERRORS as exc:
            channel_outcome = classify_send_error(exc)
            logger.warning("Couldn't post %s to channel %s (%s) - trying a DM: %s",
                           label, channel_id, channel_outcome.value, exc)
    dm_outcome = await send_dm(bot, user_id, content, label=label, **send_kwargs)
    if dm_outcome is Delivery.DELIVERED:
        return dm_outcome
    if Delivery.RETRY in (channel_outcome, dm_outcome):
        return Delivery.RETRY
    return Delivery.UNDELIVERABLE


async def send_alert(bot: Any, alert: dict[str, Any], body: str, *, label: str, **send_kwargs: Any) -> Delivery:
    """Send an alert where its owner asked. `body` starts with the alert's own name, e.g.
    "price alert #3 triggered for ...".

    - 'global': posts in the channel the alert was set in and pings the owner there,
      falling back to a DM (send_to_channel_or_dm).
    - Anything else, including no scope at all: a DM."""
    if alert.get("scope") == "global" and alert.get("channel_id") is not None:
        return await send_to_channel_or_dm(bot, alert["channel_id"], alert["user_id"],
                                           f"<@{alert['user_id']}> {body}", label=label, **send_kwargs)
    return await send_dm(bot, alert["user_id"], f"Your {body}", label=label, **send_kwargs)
