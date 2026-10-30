"""Event cards (the owner's design, 2026-10-30): every Discord scheduled event on Star Tech
Dynamics gets a sign-up card in #events - members on phones couldn't find Discord's own Events
list. Rules and wording live in bot/uex/events.py.

- **A card per occurrence**, posted when the event is made (or found on start-up), pinging Org
  Events: title, picture, organizer, description, start in each reader's own time, Ready by,
  the voice channel, and a roster. A repeating event gets a fresh card each time; its regulars
  are put on it by themselves.
- **Joining**: the role menu (or Join, for an event without roles), Maybe, Leave, Voice and
  Interested on Discord. A full role puts you on its wait list; the first one waiting moves up,
  with a DM, when a spot opens. The private reply after joining asks which ship you're bringing
  and offers "Sign me up every time" on a repeating event.
- **The organizer and Event Managers** get Start (pings the roster with the voice channel and
  marks the Discord event live) and End, and set roles with /event-roles. The event's creator,
  the Event Manager role, admins and anyone who can manage events count.
- **Changes**: an hour before, the roster is reminded. A new time in Discord updates the card and
  pings the roster once; a cancelled event says so. A card nobody ended closes 12 hours after its
  start.

Buttons and menus are DynamicItems carrying the card's id, so they work after a restart.
"""
from __future__ import annotations

import asyncio
import logging
import re
import time
from collections import defaultdict

import discord
from discord import app_commands
from discord.ext import commands, tasks

from bot.delivery import send_dm
from bot.discord_ui import BotLayoutView, BotModal, BotView
from bot.uex.events import (
    CANCELLED, ENDED, GOING, MAYBE, OPEN, REMIND_BEFORE, STARTED, WAITING, EventRole, Signup, announce_text,
    cancelled_text, details_text, ended_text, joined_text, next_in_line, parse_roles, ping_messages, placed_status,
    SHIP_CHOICES, promoted_text, reminder_text, repeats_text, roster_text, ship_matches, start_text, time_change_text,
)
from bot.uex.welcome import EVENTS_CHANNEL_ID

logger = logging.getLogger("uexbot.event_cards")

# The Event Manager role: can Start, End and set roles on any event.
EVENT_MANAGER_ROLE_ID = 1565664577088131234
ORG_EVENTS_ROLE = "Org Events"  # the role picker's ping role, by its saved name
CLOSE_AFTER = 12 * 3600         # seconds after its start that a card nobody ended is closed
SYNC_EVERY = 10                 # minutes between re-reading every event (the loop ticks each minute)
SHIP_MENU = 22                  # ships in the menu, leaving room for Not sure yet and Another ship
NOT_SURE, OTHER_SHIP = "~none", "~other"
BUTTON_TEMPLATE = r"ev:(?P<action>join|maybe|leave|start|end|every|stopevery):(?P<card>\d+)"
MENU_TEMPLATE = r"ev:(?P<kind>role|ship):(?P<card>\d+)"
CLOSED = "This op's sign-ups are closed."
NOT_MANAGER = "Only the organizer or an Event Manager can do that."


def _signups(rows: list[dict]) -> list[Signup]:
    return [Signup(r["user_id"], r["status"], r["role"], r["ship"]) for r in rows]


def _roles(rows: list[tuple[str, int]]) -> list[EventRole]:
    return [EventRole(name, capacity) for name, capacity in rows]


def _event_url(card: dict) -> str:
    return f"https://discord.com/events/{card['guild_id']}/{card['event_id']}"


def _voice_url(card: dict) -> str | None:
    if not card["voice_channel_id"]:
        return None
    return f"https://discord.com/channels/{card['guild_id']}/{card['voice_channel_id']}"


def _card_mentions(org_role) -> discord.AllowedMentions:
    """A card mentions Org Events and nobody else - on its post and on every redraw."""
    return discord.AllowedMentions(everyone=False, users=False, roles=[org_role] if org_role else False)


def moved_on(card: dict, starts_at: int, now: float, repeating: bool) -> bool:
    """Whether an event's new start time means Discord moved on to its next occurrence (close this
    card; the next gets its own) rather than an organizer changing the time (follow it, roster
    kept). Moved on: the op was started, or a repeating event whose start has passed now starts
    at least CLOSE_AFTER later - a late op pushed back, or any one-off event, keeps its card."""
    if card["state"] == STARTED:
        return True
    return repeating and card["starts_at"] <= now and starts_at - card["starts_at"] >= CLOSE_AFTER


def _card_ids(components) -> set[int]:
    """The card ids on a message's buttons and menus, however deep in its layout."""
    found: set[int] = set()
    for component in components or ():
        match = re.fullmatch(BUTTON_TEMPLATE, getattr(component, "custom_id", None) or "") or re.fullmatch(
            MENU_TEMPLATE, getattr(component, "custom_id", None) or "")
        if match:
            found.add(int(match["card"]))
        found |= _card_ids(getattr(component, "children", None))
        if getattr(component, "accessory", None) is not None:
            found |= _card_ids([component.accessory])
    return found


def _snapshot(event: discord.ScheduledEvent) -> dict:
    return {"name": event.name, "description": event.description,
            "cover_url": event.cover_image.url if event.cover_image else None,
            "creator_id": event.creator_id, "voice_channel_id": event.channel_id}


def build_card(card: dict, roles: list[EventRole], signups: list[Signup], regulars: set[int],
               org_mention: str | None) -> BotLayoutView:
    """The whole card: a message of layout components (no embed)."""
    view = BotLayoutView(timeout=None)
    view.add_item(discord.ui.TextDisplay(announce_text(org_mention)))
    box = discord.ui.Container()
    box.add_item(discord.ui.TextDisplay(f"## [{discord.utils.escape_markdown(card['name'])}]({_event_url(card)})"))
    if card["cover_url"]:
        box.add_item(discord.ui.MediaGallery(discord.MediaGalleryItem(card["cover_url"])))
    box.add_item(discord.ui.TextDisplay(details_text(
        creator_id=card["creator_id"], description=card["description"], starts_at=card["starts_at"],
        voice_channel_id=card["voice_channel_id"], repeats=card["repeats"], regulars=len(regulars),
        state=card["state"])))
    box.add_item(discord.ui.Separator())
    box.add_item(discord.ui.TextDisplay(roster_text(roles, signups, regulars)))
    if card["state"] in (OPEN, STARTED):
        card_id = card["card_id"]
        if roles:
            box.add_item(discord.ui.ActionRow(RoleMenu(card_id, roles, signups)))
        row = discord.ui.ActionRow()
        if not roles:
            row.add_item(CardButton(card_id, "join"))
        row.add_item(CardButton(card_id, "maybe"))
        row.add_item(CardButton(card_id, "leave"))
        if _voice_url(card):
            row.add_item(discord.ui.Button(label="Voice", url=_voice_url(card)))
        row.add_item(discord.ui.Button(label="Interested on Discord", url=_event_url(card)))
        box.add_item(row)
        box.add_item(discord.ui.Separator())
        who = f"<@{card['creator_id']}> or an Event Manager" if card["creator_id"] else "An Event Manager"
        box.add_item(discord.ui.TextDisplay(f"-# Only {who} can use these:"))
        managers = discord.ui.ActionRow()
        if card["state"] == OPEN:
            managers.add_item(CardButton(card_id, "start"))
        managers.add_item(CardButton(card_id, "end"))
        box.add_item(managers)
    view.add_item(box)
    return view


_BUTTONS = {
    "join": ("Join", discord.ButtonStyle.primary),
    "maybe": ("Maybe", discord.ButtonStyle.secondary),
    "leave": ("Leave", discord.ButtonStyle.danger),
    "start": ("Start", discord.ButtonStyle.success),
    "end": ("End", discord.ButtonStyle.secondary),
    "every": ("Sign me up every time", discord.ButtonStyle.secondary),
    "stopevery": ("Stop signing me up every time", discord.ButtonStyle.secondary),
}


class CardButton(discord.ui.DynamicItem[discord.ui.Button], template=BUTTON_TEMPLATE):
    """Every button on a card and its private replies; the custom id carries the card's id."""

    def __init__(self, card_id: int, action: str) -> None:
        label, style = _BUTTONS[action]
        super().__init__(discord.ui.Button(label=label, style=style, custom_id=f"ev:{action}:{card_id}"))
        self.card_id, self.action = card_id, action

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item: discord.ui.Button, match, /):
        return cls(int(match["card"]), match["action"])

    async def callback(self, interaction: discord.Interaction) -> None:
        cog = interaction.client.get_cog("EventCards")
        await getattr(cog, f"on_{self.action}")(interaction, self.card_id)


class RoleMenu(discord.ui.DynamicItem[discord.ui.Select], template=MENU_TEMPLATE):
    """The card's role menu (kind "role") and the private ship menu (kind "ship")."""

    def __init__(self, card_id: int, roles: list[EventRole] | None = None, signups: list[Signup] | None = None,
                 *, kind: str = "role", options: list[discord.SelectOption] | None = None) -> None:
        if options is None:
            options = [discord.SelectOption(label=f"{r.name} ({sum(1 for s in signups or [] if s.status == GOING and s.role == r.name)}"
                                                  f"/{r.capacity})" + (" - full, join the wait list"
                                                                       if placed_status(roles, signups or [], 0, r.name) == WAITING else ""),
                                            value=r.name) for r in roles or []] or [discord.SelectOption(label="-")]
        placeholder = "Pick a role to join" if kind == "role" else "Pick a ship"
        super().__init__(discord.ui.Select(custom_id=f"ev:{kind}:{card_id}", options=options, placeholder=placeholder))
        self.card_id, self.kind = card_id, kind

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item: discord.ui.Select, match, /):
        return cls(int(match["card"]), kind=match["kind"])

    async def callback(self, interaction: discord.Interaction) -> None:
        cog = interaction.client.get_cog("EventCards")
        value = self.item.values[0] if self.item.values else ""
        if self.kind == "role":
            await cog.on_join(interaction, self.card_id, role=value)
        else:
            await cog.on_ship(interaction, self.card_id, value)


class ShipModal(BotModal, title="What are you bringing?"):
    ship = discord.ui.TextInput(label="Ship", placeholder='Any part of the name, e.g. "arrastra" or "c2"',
                                max_length=60)

    def __init__(self, cog: "EventCards", card_id: int) -> None:
        super().__init__()
        self.cog, self.card_id = cog, card_id

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await self.cog.typed_ship(interaction, self.card_id, str(self.ship.value))


class ShipChoice(BotView):
    """"Which Aurora?" - a menu of the ships a typed name could mean (up to 25). Short-lived."""

    def __init__(self, cog: "EventCards", card_id: int, ships: list[str]) -> None:
        super().__init__(timeout=300)
        self.cog, self.card_id = cog, card_id
        self.menu = discord.ui.Select(placeholder="Pick your ship", options=[
            discord.SelectOption(label=ship[:100], value=ship[:100]) for ship in ships[:SHIP_CHOICES]])
        self.menu.callback = self.pick
        self.add_item(self.menu)

    async def pick(self, interaction: discord.Interaction) -> None:
        await self.cog.set_ship(interaction, self.card_id, self.menu.values[0])


class EventCards(commands.Cog):
    def __init__(self, bot: commands.Bot, *, start_loop: bool = True) -> None:
        self.bot = bot
        self._locks: defaultdict[int, asyncio.Lock] = defaultdict(asyncio.Lock)
        self._ticks = 0
        if start_loop:
            self.tick.start()

    def cog_unload(self) -> None:
        self.tick.cancel()

    async def cog_load(self) -> None:
        self.bot.add_dynamic_items(CardButton, RoleMenu)

    # -- helpers ----------------------------------------------------------------

    def _channel(self):
        return self.bot.get_channel(EVENTS_CHANNEL_ID)

    async def _org_mention(self, guild_id: int) -> tuple[str | None, discord.Object | None]:
        role_id = (await self.bot.db.get_role_picker_roles(guild_id)).get(ORG_EVENTS_ROLE)
        return (f"<@&{role_id}>", discord.Object(role_id)) if role_id else (None, None)

    async def _parts(self, card: dict) -> tuple[list[EventRole], list[Signup], set[int]]:
        roles = _roles(await self.bot.db.get_event_roles(card["event_id"]))
        signups = _signups(await self.bot.db.get_event_signups(card["card_id"]))
        regulars = {r["user_id"] for r in await self.bot.db.get_event_regulars(card["event_id"])}
        return roles, signups, regulars

    async def refresh(self, card_id: int) -> None:
        """Redraw a card from what's stored."""
        card = await self.bot.db.get_event_card(card_id)
        channel = self.bot.get_channel(card["channel_id"]) if card else None
        if channel is None or not card["message_id"]:
            return
        org, org_role = await self._org_mention(card["guild_id"])
        view = build_card(card, *await self._parts(card), org)
        try:  # Discord re-reads mentions on an edit: allow Org Events again or the card stops mentioning it
            await channel.get_partial_message(card["message_id"]).edit(view=view, allowed_mentions=_card_mentions(org_role))
        except discord.HTTPException as exc:
            logger.warning("Couldn't redraw event card %s: %s", card_id, exc)

    async def ping_roster(self, card: dict, text: str, *, waiting_too: bool = False,
                          view: discord.ui.View | None = None) -> None:
        """A reply to the card pinging everyone on it (the wait list only when `waiting_too`)."""
        statuses = (GOING, MAYBE, WAITING) if waiting_too else (GOING, MAYBE)
        ids = [s.user_id for s in _signups(await self.bot.db.get_event_signups(card["card_id"])) if s.status in statuses]
        channel = self.bot.get_channel(card["channel_id"])
        if channel is None:
            return
        reference = discord.MessageReference(message_id=card["message_id"], channel_id=card["channel_id"],
                                             fail_if_not_exists=False) if card["message_id"] else None
        # Past 100 people one message can't ping everyone (Discord's limits): the rest follow
        for index, (content, batch) in enumerate(ping_messages(text, ids)):
            kwargs = {"reference": reference} if reference else {}
            if index == 0 and view is not None:
                kwargs["view"] = view
            try:
                await channel.send(content, allowed_mentions=discord.AllowedMentions(
                    everyone=False, roles=False, users=[discord.Object(i) for i in batch]), **kwargs)
            except discord.HTTPException as exc:
                logger.warning("Couldn't post to event card %s's roster: %s", card["card_id"], exc)

    def _can_manage(self, member, card: dict) -> bool:
        if member.id == card["creator_id"]:
            return True
        if any(role.id == EVENT_MANAGER_ROLE_ID for role in getattr(member, "roles", [])):
            return True
        perms = getattr(member, "guild_permissions", None)
        return perms is not None and (perms.administrator or perms.manage_events)

    async def _open_card(self, interaction: discord.Interaction, card_id: int) -> dict | None:
        card = await self.bot.db.get_event_card(card_id)
        if card is None or card["state"] not in (OPEN, STARTED):
            await interaction.followup.send(CLOSED, ephemeral=True)
            return None
        return card

    async def _promote(self, card: dict, role: str | None) -> None:
        """Move the first one waiting for `role` up into a freed spot and DM them."""
        roles, signups, _ = await self._parts(card)
        user_id = next_in_line(roles, signups, role)
        if user_id is None:
            return
        await self.bot.db.set_event_signup_status(card["card_id"], user_id, GOING, role)
        view = BotView(timeout=None)
        if card["message_id"]:
            view.add_item(discord.ui.Button(label="Open the card", url=(
                f"https://discord.com/channels/{card['guild_id']}/{card['channel_id']}/{card['message_id']}")))
        await send_dm(self.bot, user_id, promoted_text(card["name"], role, card["starts_at"]), view=view,
                      label=f"event card {card['card_id']} wait list")

    async def _panel(self, interaction: discord.Interaction, card: dict, text: str) -> None:
        """The private reply after joining: what you're bringing, and every time on a repeating event."""
        user_id = interaction.user.id
        ships: list[str] = []
        default = await self.bot.db.get_default_ship(user_id)
        if default:
            ships.append(default)
        ships += [s for s in await self.bot.db.event_ship_history(user_id) if s not in ships]
        options = [discord.SelectOption(label=s[:100], value=s[:100],
                                        description="Your default ship" if s == default else "You've brought it before")
                   for s in ships[:SHIP_MENU]]
        options += [discord.SelectOption(label="Not sure yet", value=NOT_SURE),
                    discord.SelectOption(label="Another ship...", value=OTHER_SHIP, description="Type its name")]
        view = BotView(timeout=None)
        view.add_item(RoleMenu(card["card_id"], kind="ship", options=options))
        if card["repeats"]:
            regular = any(r["user_id"] == user_id for r in await self.bot.db.get_event_regulars(card["event_id"]))
            view.add_item(CardButton(card["card_id"], "stopevery" if regular else "every"))
        await interaction.followup.send(text + "\nWhat are you bringing?", view=view, ephemeral=True)

    # -- members ------------------------------------------------------------------

    async def on_join(self, interaction: discord.Interaction, card_id: int, role: str | None = None) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        card = await self._open_card(interaction, card_id)
        if card is None:
            return
        user_id = interaction.user.id
        async with self._locks[card_id]:
            roles, signups, _ = await self._parts(card)
            if roles and role not in {r.name for r in roles}:
                await interaction.followup.send("Pick a role from the menu on the card.", ephemeral=True)
                return
            if not roles:
                role = None
            mine = next((s for s in signups if s.user_id == user_id), None)
            if mine and mine.role == role and mine.status in (GOING, WAITING):
                status = mine.status
            else:
                status = placed_status(roles, signups, user_id, role)
                await self.bot.db.save_event_signup(card_id, user_id, status=status, role=role, queued=time.time())
                if mine and mine.status == GOING and mine.role != role:
                    await self._promote(card, mine.role)
                if any(r["user_id"] == user_id for r in await self.bot.db.get_event_regulars(card["event_id"])):
                    await self.bot.db.set_event_regular(card["event_id"], user_id, role, mine.ship if mine else None)
            signups = _signups(await self.bot.db.get_event_signups(card_id))
        await self.refresh(card_id)
        await self._panel(interaction, card, joined_text(card["name"], status, role, roles, signups, user_id))

    async def on_maybe(self, interaction: discord.Interaction, card_id: int) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        card = await self._open_card(interaction, card_id)
        if card is None:
            return
        user_id = interaction.user.id
        async with self._locks[card_id]:
            roles, signups, _ = await self._parts(card)
            mine = next((s for s in signups if s.user_id == user_id), None)
            if mine is None or mine.status != MAYBE:
                role = mine.role if mine else None
                await self.bot.db.save_event_signup(card_id, user_id, status=MAYBE, role=role, queued=time.time())
                if mine and mine.status == GOING:
                    await self._promote(card, mine.role)
                text = joined_text(card["name"], MAYBE, role, roles, signups, user_id)
            elif roles and mine.role is None:
                text = "You're on **Maybe**. Pick a role from the menu on the card to join."
            else:  # Maybe again: back in
                status = placed_status(roles, signups, user_id, mine.role)
                await self.bot.db.save_event_signup(card_id, user_id, status=status, role=mine.role, queued=time.time())
                signups = _signups(await self.bot.db.get_event_signups(card_id))
                text = joined_text(card["name"], status, mine.role, roles, signups, user_id)
        await self.refresh(card_id)
        await interaction.followup.send(text, ephemeral=True)

    async def on_leave(self, interaction: discord.Interaction, card_id: int) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        card = await self._open_card(interaction, card_id)
        if card is None:
            return
        user_id = interaction.user.id
        async with self._locks[card_id]:
            mine = next((s for s in _signups(await self.bot.db.get_event_signups(card_id)) if s.user_id == user_id), None)
            if mine is None:
                await interaction.followup.send("You're not on this one.", ephemeral=True)
                return
            await self.bot.db.delete_event_signup(card_id, user_id)
            if mine.status == GOING:
                await self._promote(card, mine.role)
        await self.refresh(card_id)
        regular = any(r["user_id"] == user_id for r in await self.bot.db.get_event_regulars(card["event_id"]))
        if regular:
            view = BotView(timeout=None)
            view.add_item(CardButton(card_id, "stopevery"))
            await interaction.followup.send(f"You're off **{card['name']}** this time. You're still signed up every time.",
                                            view=view, ephemeral=True)
        else:
            await interaction.followup.send(f"You're off **{card['name']}**.", ephemeral=True)

    async def on_every(self, interaction: discord.Interaction, card_id: int) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        card = await self.bot.db.get_event_card(card_id)
        if card is None:
            await interaction.followup.send(CLOSED, ephemeral=True)
            return
        mine = next((s for s in _signups(await self.bot.db.get_event_signups(card_id))
                     if s.user_id == interaction.user.id and s.status != MAYBE), None)
        if mine is None:
            await interaction.followup.send("Join first, then you can sign up every time.", ephemeral=True)
            return
        await self.bot.db.set_event_regular(card["event_id"], mine.user_id, mine.role, mine.ship)
        await self.refresh(card_id)
        as_role = f" as **{mine.role}**" if mine.role else ""
        await interaction.followup.send(f"You'll be on every **{card['name']}**{as_role}. Press Leave on any one you "
                                        "can't make.", ephemeral=True)

    async def on_stopevery(self, interaction: discord.Interaction, card_id: int) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        card = await self.bot.db.get_event_card(card_id)
        if card is not None:
            await self.bot.db.delete_event_regular(card["event_id"], interaction.user.id)
            await self.refresh(card_id)
        await interaction.followup.send("You won't be signed up by yourself any more.", ephemeral=True)

    async def on_ship(self, interaction: discord.Interaction, card_id: int, value: str) -> None:
        if value == OTHER_SHIP:
            await interaction.response.send_modal(ShipModal(self, card_id))
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        await self.set_ship(interaction, card_id, None if value == NOT_SURE else value, deferred=True)

    async def typed_ship(self, interaction: discord.Interaction, card_id: int, text: str) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            vehicles = await self.bot.uex.get_vehicles()
        except Exception:
            logger.exception("Couldn't read the ship list for an event card")
            vehicles = []
        ships = [(v["name"], v.get("name_full")) for v in vehicles if v.get("name")]
        found = ship_matches(ships, text) if ships else []
        if not ships:
            await self.set_ship(interaction, card_id, " ".join(text.split())[:60], deferred=True)
        elif len(found) == 1:
            await self.set_ship(interaction, card_id, found[0], deferred=True)
        elif not found:
            await interaction.followup.send(f"I don't know a ship called **{text.strip()}** - check the spelling?",
                                            ephemeral=True)
        elif len(found) > SHIP_CHOICES:
            await interaction.followup.send(f"**{text.strip()}** matches {len(found)} ships - type a bit more of the name.",
                                            ephemeral=True)
        else:
            view = ShipChoice(self, card_id, found)
            view.message = await interaction.followup.send("Which one?", view=view, ephemeral=True, wait=True)

    async def set_ship(self, interaction: discord.Interaction, card_id: int, ship: str | None, *,
                       deferred: bool = False) -> None:
        if not deferred:
            await interaction.response.defer(ephemeral=True, thinking=True)
        card = await self.bot.db.get_event_card(card_id)
        user_id = interaction.user.id
        if card is None or not await self.bot.db.set_event_signup_ship(card_id, user_id, ship):
            await interaction.followup.send("You're not on this op any more - join it first.", ephemeral=True)
            return
        if ship:
            await self.bot.db.record_event_ship(user_id, ship)
        for regular in await self.bot.db.get_event_regulars(card["event_id"]):
            if regular["user_id"] == user_id:
                await self.bot.db.set_event_regular(card["event_id"], user_id, regular["role"], ship)
        await self.refresh(card_id)
        await interaction.followup.send(f"Got it - you're bringing the **{ship}**." if ship else "Got it - no ship yet.",
                                        ephemeral=True)

    # -- organizers -----------------------------------------------------------------

    async def on_start(self, interaction: discord.Interaction, card_id: int) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        card = await self._open_card(interaction, card_id)
        if card is None:
            return
        if not self._can_manage(interaction.user, card):
            await interaction.followup.send(NOT_MANAGER, ephemeral=True)
            return
        started = await self.mark_started(card)
        problem = await self._discord_event(card, "start")
        reply = "Started - the roster has been pinged." if started else "It's already started."
        await interaction.followup.send(reply + (f"\n{problem}" if problem else ""), ephemeral=True)

    async def on_end(self, interaction: discord.Interaction, card_id: int) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        card = await self._open_card(interaction, card_id)
        if card is None:
            return
        if not self._can_manage(interaction.user, card):
            await interaction.followup.send(NOT_MANAGER, ephemeral=True)
            return
        if await self.close(card, ENDED, announce=True):
            problem = await self._discord_event(card, "end")
            await interaction.followup.send("Ended." + (f"\n{problem}" if problem else ""), ephemeral=True)
        else:
            await interaction.followup.send("It's already ended.", ephemeral=True)

    async def _discord_event(self, card: dict, action: str) -> str | None:
        """Start or end the Discord event too. Returns a note for the organizer if that failed."""
        guild = self.bot.get_guild(card["guild_id"])
        event = guild.get_scheduled_event(card["event_id"]) if guild else None
        wanted = discord.EventStatus.scheduled if action == "start" else discord.EventStatus.active
        if event is None or event.status != wanted:
            return None
        try:
            await (event.start() if action == "start" else event.end())
        except discord.HTTPException as exc:
            logger.warning("Couldn't %s Discord event %s: %s", action, card["event_id"], exc)
            return f"I couldn't {action} the Discord event itself - check I have Manage Events."
        return None

    async def mark_started(self, card: dict) -> bool:
        """Open -> started, once: redraw and ping the roster with the voice channel."""
        if not await self.bot.db.set_event_card_state(card["card_id"], STARTED, only_from=(OPEN,)):
            return False
        await self.refresh(card["card_id"])
        view = None
        if _voice_url(card):
            channel = self.bot.get_channel(card["voice_channel_id"])
            view = BotView(timeout=None)
            view.add_item(discord.ui.Button(label=f"Join {channel.name}" if channel else "Join voice", url=_voice_url(card)))
        await self.ping_roster(card, start_text(card["name"], card["voice_channel_id"]), view=view)
        return True

    async def close(self, card: dict, state: str, *, announce: bool) -> bool:
        """Open or started -> ended or cancelled, once."""
        if not await self.bot.db.set_event_card_state(card["card_id"], state, only_from=(OPEN, STARTED)):
            return False
        await self.refresh(card["card_id"])
        if announce:
            text = cancelled_text(card["name"]) if state == CANCELLED else ended_text(card["name"])
            if state == CANCELLED:
                await self.ping_roster(card, text, waiting_too=True)
            else:
                channel = self.bot.get_channel(card["channel_id"])
                if channel is not None:
                    try:
                        await channel.send(text, reference=discord.MessageReference(
                            message_id=card["message_id"], channel_id=card["channel_id"], fail_if_not_exists=False),
                            allowed_mentions=discord.AllowedMentions.none())
                    except discord.HTTPException as exc:
                        logger.warning("Couldn't post event card %s's ending: %s", card["card_id"], exc)
        return True

    @app_commands.command(name="event-roles", description="Organizers: set an event's roles and spots, e.g. Miner 6, Hauler 2.")
    @app_commands.describe(event="Which event", roles="Roles and spots, e.g. Miner 6, Hauler 2, Escort 2 - or none to just Join")
    @app_commands.guild_only()
    async def event_roles(self, interaction: discord.Interaction, event: str, roles: str) -> None:
        await interaction.response.defer(ephemeral=True)
        scheduled = interaction.guild.get_scheduled_event(int(event)) if event.isdigit() else None
        if scheduled is None:
            await interaction.followup.send("Pick the event from the list.", ephemeral=True)
            return
        card = await self.bot.db.current_event_card(scheduled.id)
        creator = {"creator_id": scheduled.creator_id}
        if not self._can_manage(interaction.user, creator):
            await interaction.followup.send(NOT_MANAGER, ephemeral=True)
            return
        parsed, problem = parse_roles(roles)
        if problem:
            await interaction.followup.send(problem, ephemeral=True)
            return
        await self.bot.db.set_event_roles(scheduled.id, [(r.name, r.capacity) for r in parsed])
        names = {r.name for r in parsed}
        for regular in await self.bot.db.get_event_regulars(scheduled.id):
            if regular["role"] not in names and regular["role"] is not None:
                await self.bot.db.set_event_regular(scheduled.id, regular["user_id"], None, regular["ship"])
        if card is not None:
            async with self._locks[card["card_id"]]:
                for signup in _signups(await self.bot.db.get_event_signups(card["card_id"])):
                    if signup.role is not None and signup.role not in names:
                        status = GOING if signup.status == WAITING else signup.status
                        await self.bot.db.set_event_signup_status(card["card_id"], signup.user_id, status, None)
                for role in parsed:  # more spots than before: the wait list moves up
                    for _ in range(role.capacity):
                        roles_now, signups_now, _ = await self._parts(card)
                        if next_in_line(roles_now, signups_now, role.name) is None:
                            break
                        await self._promote(card, role.name)
            await self.refresh(card["card_id"])
        if parsed:
            listed = "\n".join(f"{r.name} - {r.capacity} spot" + ("" if r.capacity == 1 else "s") for r in parsed)
            text = (f"Roles set for **{scheduled.name}**:\n{listed}\nUsed every time until you change them."
                    f" The card in <#{EVENTS_CHANNEL_ID}> is updated.")
        else:
            text = f"**{scheduled.name}** has no roles now - people just press Join."
        await interaction.followup.send(text, ephemeral=True)

    @event_roles.autocomplete("event")
    async def event_autocomplete(self, interaction: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
        events = sorted((e for e in (interaction.guild.scheduled_events if interaction.guild else [])
                         if e.status in (discord.EventStatus.scheduled, discord.EventStatus.active)),
                        key=lambda e: e.start_time)
        current = current.casefold()
        return [app_commands.Choice(name=e.name[:100], value=str(e.id)) for e in events
                if current in e.name.casefold()][:25]

    # -- Discord's events ----------------------------------------------------------

    @commands.Cog.listener()
    async def on_scheduled_event_create(self, event: discord.ScheduledEvent) -> None:
        await self.sync(event)

    @commands.Cog.listener()
    async def on_scheduled_event_update(self, before: discord.ScheduledEvent, after: discord.ScheduledEvent) -> None:
        await self.sync(after)

    @commands.Cog.listener()
    async def on_scheduled_event_delete(self, event: discord.ScheduledEvent) -> None:
        card = await self.bot.db.current_event_card(event.id)
        if card is not None:
            await self.close(card, CANCELLED, announce=True)

    async def sync(self, event: discord.ScheduledEvent) -> None:
        """Bring an event's card in line with the event: post it, follow a new time, close it."""
        channel = self._channel()
        if channel is None or event.guild_id != channel.guild.id:
            return
        try:
            async with self._locks[-event.id]:
                await self._sync(event, channel)
        except Exception:  # an event handler and a loop: log, never raise
            logger.exception("Event card for event %s failed", event.id)

    async def _sync(self, event: discord.ScheduledEvent, channel) -> None:
        card = await self.bot.db.current_event_card(event.id)
        if event.status in (discord.EventStatus.cancelled, discord.EventStatus.completed):
            if card is not None:
                await self.close(card, CANCELLED if event.status == discord.EventStatus.cancelled else ENDED,
                                 announce=event.status == discord.EventStatus.cancelled)
            return
        now = time.time()
        if card is not None and card["starts_at"] <= now - CLOSE_AFTER:
            await self.close(card, ENDED, announce=False)  # an occurrence long past that nobody closed (the bot was down)
            card = None
        if card is not None and event.status == discord.EventStatus.active:
            # Under way: whatever start time Discord now gives it, it's this card's op
            await self.bot.db.update_event_card(card["card_id"], **_snapshot(event))
            if not card["message_id"]:  # its first post failed: post it before it starts
                await self.send_card(card["card_id"], channel, retry=True)
            if card["state"] == OPEN:
                await self.mark_started(await self.bot.db.get_event_card(card["card_id"]))
            else:
                await self.refresh(card["card_id"])
            return
        starts_at = int(event.start_time.timestamp())
        if card is not None and card["starts_at"] != starts_at:
            repeating = bool(card["repeats"]) or (
                card["state"] == OPEN and card["starts_at"] <= now and starts_at - card["starts_at"] >= CLOSE_AFTER
                and await self._repeating(card, event))
            if moved_on(card, starts_at, now, repeating):
                await self.close(card, ENDED, announce=False)  # that one's over; the next gets its own card
                card = None
            else:
                await self.bot.db.move_event_card(card["card_id"], starts_at)
                if starts_at - now <= REMIND_BEFORE:  # the time-change ping is its reminder: not a second ping
                    await self.bot.db.claim_event_reminder(card["card_id"])
                await self.bot.db.update_event_card(card["card_id"], **_snapshot(event))
                await self.refresh(card["card_id"])
                await self.ping_roster(card, time_change_text(event.name, starts_at, card["starts_at"]), waiting_too=True)
                return
        if card is None:
            if event.status != discord.EventStatus.scheduled or await self.bot.db.event_card_exists(event.id, starts_at):
                return
            await self.post(event, channel, starts_at)
            return
        await self.bot.db.update_event_card(card["card_id"], **_snapshot(event))
        if not card["message_id"]:  # its first post failed, or the bot stopped before saving it: post it now
            await self.send_card(card["card_id"], channel, retry=True)
        else:
            await self.refresh(card["card_id"])

    async def _rule(self, event: discord.ScheduledEvent) -> dict | None:
        """The event's repeat rule (discord.py doesn't keep it). Raises HTTPException."""
        return (await self.bot.http.get_scheduled_event(event.guild_id, event.id, False)).get("recurrence_rule")

    async def _repeating(self, card: dict, event: discord.ScheduledEvent) -> bool:
        """Whether a card stored without a repeat rule is for a repeating event after all: the read
        when it was posted can have failed. Saved when it is; assumed when Discord can't say."""
        try:
            text = repeats_text(await self._rule(event))
        except discord.HTTPException as exc:
            logger.warning("Couldn't read event %s's repeat rule: %s", event.id, exc)
            return True
        if text:
            await self.bot.db.update_event_card(card["card_id"], repeats=text)
        return bool(text)

    async def post(self, event: discord.ScheduledEvent, channel, starts_at: int) -> None:
        rule = None
        try:
            rule = await self._rule(event)
        except discord.HTTPException as exc:
            logger.warning("Couldn't read event %s's repeat rule: %s", event.id, exc)
        card_id = await self.bot.db.create_event_card(guild_id=event.guild_id, event_id=event.id, starts_at=starts_at,
                                                      channel_id=channel.id, repeats=repeats_text(rule), **_snapshot(event))
        roles = _roles(await self.bot.db.get_event_roles(event.id))
        for regular in await self.bot.db.get_event_regulars(event.id):  # regulars are on it from the start
            role = regular["role"] if regular["role"] in {r.name for r in roles} else None
            signups = _signups(await self.bot.db.get_event_signups(card_id))
            await self.bot.db.save_event_signup(card_id, regular["user_id"], status=placed_status(roles, signups,
                                                regular["user_id"], role), role=role, queued=time.time(),
                                                ship=regular["ship"])
        await self.send_card(card_id, channel)

    async def send_card(self, card_id: int, channel, *, retry: bool = False) -> None:
        """Post a card's message, pinging Org Events. A failed post is tried again at the next sync
        (`retry`), which first looks for the card already in the channel - posted, but the bot
        stopped before saving its id - so it's never posted twice."""
        if retry and (found := await self._find_posted(channel, card_id)):
            await self.bot.db.update_event_card(card_id, message_id=found)
            await self.refresh(card_id)
            return
        card = await self.bot.db.get_event_card(card_id)
        org, org_role = await self._org_mention(card["guild_id"])
        view = build_card(card, *await self._parts(card), org)
        try:
            message = await channel.send(view=view, allowed_mentions=_card_mentions(org_role))
        except discord.HTTPException as exc:
            logger.warning("Couldn't post the card for event %s: %s", card["event_id"], exc)
            return
        await self.bot.db.update_event_card(card_id, message_id=message.id)

    async def _find_posted(self, channel, card_id: int) -> int | None:
        """The id of a message in the channel carrying this card's buttons, if one is there."""
        try:
            async for message in channel.history(limit=50):
                if message.author.bot and card_id in _card_ids(message.components):
                    return message.id
        except discord.HTTPException as exc:
            logger.warning("Couldn't read #events for card %s: %s", card_id, exc)
        return None

    async def close_missing(self, guild) -> None:
        """Close cards whose Discord event is gone - deleted while the bot was down, so no delete
        reached it. Asked of Discord itself before closing: the event list alone could be stale."""
        present = {event.id for event in guild.scheduled_events}
        for card in await self.bot.db.open_event_cards(guild.id):
            if card["event_id"] in present:
                continue
            try:
                try:
                    event = await guild.fetch_scheduled_event(card["event_id"])
                except discord.NotFound:
                    event = None
                except discord.HTTPException as exc:
                    logger.warning("Couldn't check event %s: %s", card["event_id"], exc)
                    continue
                if event is not None:
                    await self.sync(event)
                    continue
                async with self._locks[-card["event_id"]]:
                    # Not started and still ahead: it was called off. Otherwise it just finished.
                    if card["state"] == OPEN and card["starts_at"] > time.time():
                        await self.close(card, CANCELLED, announce=True)
                    else:
                        await self.close(card, ENDED, announce=False)
            except Exception:  # one card's trouble (a dropped connection, a locked DB) never stops the rest
                logger.exception("Checking event card %s's event failed", card["card_id"])

    # -- the minute loop -------------------------------------------------------------

    @tasks.loop(minutes=1)
    async def tick(self) -> None:
        now = int(time.time())
        try:
            channel = self._channel()
            due = channel is not None and self._ticks % SYNC_EVERY == 0
            self._ticks += 1  # first: a failing re-read mustn't make every minute a re-read
            if due:
                for event in list(channel.guild.scheduled_events):
                    await self.sync(event)
                await self.close_missing(channel.guild)
            for card in await self.bot.db.event_cards_due_reminder(now, REMIND_BEFORE):
                if await self.bot.db.claim_event_reminder(card["card_id"]):
                    await self.ping_roster(card, reminder_text(card["name"], card["starts_at"], card["voice_channel_id"]))
            for card in await self.bot.db.stale_event_cards(now - CLOSE_AFTER):
                await self.close(card, ENDED, announce=False)
        except Exception:
            logger.exception("Event card check failed")

    @tick.before_loop
    async def before_tick(self) -> None:
        await self.bot.wait_until_ready()


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(EventCards(bot))
