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

logger = logging.getLogger("uexbot.delivery")

# Discord rejects a message longer than this outright (HTTP 400, error 50035).
MAX_MESSAGE_CHARS = 2000

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
