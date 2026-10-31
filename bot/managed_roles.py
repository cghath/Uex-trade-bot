"""Roles the bot creates and hands out by itself: voice ranks (bot/cogs/voice_ranks.py) and veteran
roles (bot/cogs/anniversaries.py).

Each is remembered by id (the managed_roles table) once found or created, like the role
picker's roles, rather than looked up by name every time (audit COMM-8): a staff rename of
"Cadet" keeps working instead of the bot making a second "Cadet", and a look-alike role made
later - a staff "Officer" sitting lower in the list - is never picked up. A role is only matched
by name once, the first time (so the roles already on the server are kept): of the roles with that
name, the one most members hold, and never one with permissions - these roles are rewards, never
access, and a same-name role with permissions may be a staff role. A role staff give permissions to
later is still handed out (that's their choice), with a warning in the log once.

Lookups and creation share one lock, and a role created in this run is kept here until
Discord's cache has it (create_role doesn't add it to guild.roles; the gateway event does), so
two callers close together never create the same role twice. Once the cache has it, a role that
then goes missing was deleted, and is matched or created again.

A database error remembering a role is logged, not raised: the id is still remembered for this run.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any

import discord

logger = logging.getLogger("uexbot.managed_roles")


def harmless(role: Any) -> bool:
    """A role that grants no permissions, so handing it out gives nobody any access."""
    permissions = getattr(role, "permissions", None)
    return permissions is not None and permissions.value == 0 and not getattr(role, "managed", False)


class ManagedRoles:
    def __init__(self, bot: Any, *, reason: str) -> None:
        self.bot = bot
        self.reason = reason
        self._lock = asyncio.Lock()
        self._ids: dict[int, dict[str, int]] = {}      # guild id -> the bot's name -> role id
        self._created: dict[int, Any] = {}             # role id -> role, until the cache has it
        self._unsafe_warned: set[int] = set()

    async def ids(self, guild: discord.Guild) -> dict[str, int]:
        """Every remembered role id for this server, by the bot's name for it."""
        if guild.id not in self._ids:
            self._ids[guild.id] = await self.bot.db.get_managed_roles(guild.id)
        return self._ids[guild.id]

    async def get(self, guild: discord.Guild, name: str) -> Any:
        """The role the bot calls `name`: by its remembered id, else matched by name the first
        time, else created. Raises discord.HTTPException when it has to be created and can't be."""
        async with self._lock:
            ids = await self.ids(guild)
            role_id = ids.get(name)
            role = None
            if role_id is not None:
                role = guild.get_role(role_id)
                if role is not None:
                    self._created.pop(role_id, None)  # the cache has it now
                else:
                    role = self._created.get(role_id)
            if role is None:  # never seen, or deleted since
                matches = [r for r in guild.roles if r.name == name and harmless(r)]
                role = max(matches, key=lambda r: len(getattr(r, "members", ())), default=None)
                if role is None:
                    if any(r.name == name for r in guild.roles):
                        logger.warning("A role named %s has permissions, so it isn't used; making the bot's own",
                                       name)
                    role = await guild.create_role(name=name, reason=self.reason)
                    self._created[role.id] = role
                ids[name] = role.id
                try:
                    await self.bot.db.save_managed_role(guild.id, name, role.id)
                except Exception:
                    logger.exception("Couldn't save the id of role %s (%s); remembered for this run only",
                                     role.id, name)
        if not harmless(role) and role.id not in self._unsafe_warned:
            self._unsafe_warned.add(role.id)
            logger.warning("Role %s (%s) has permissions now: everyone who earns it gets them", role.id, name)
        return role
