"""Event cards (bot/uex/events.py, bot/cogs/event_cards.py).

Guarantees under test:
  1. Rules - "Miner 6, Hauler 2" reads as roles (blank or none: no roles), bad input says why;
     a full role puts you on its wait list and the first one waiting moves up; repeats in words
     without a (UTC) weekday; a typed ship name means one ship or asks which.
  2. The card - roles with [taken/spots], full, the wait list, Maybe, ships, regulars; Join only
     without roles; Voice and Interested links; no buttons once it's over; a long roster is cut
     with a count, not mid-line.
  3. Posting - one card per occurrence pinging Org Events and nobody else, regulars already on
     it, never posted twice (not even after it closed); a redraw keeps the Org Events mention; a
     post that failed is made at the next sync; a new time pings the roster once, and a late op
     pushed back keeps its card and roster; a past occurrence of a repeating event closes and the
     next one gets its own card; an op under way is started, whatever its start time says; a
     cancelled event says so, and one deleted while the bot was down is found and closed.
  4. Members - joining, switching roles, Maybe and back, leaving moves the wait list up with a
     DM; ships from the menu or typed; every time.
  5. Organizers - only the creator, Event Managers or admins can Start/End or set roles; Start
     pings the roster once and starts the Discord event; /event-roles moves people off a role
     that's gone and up from a wait list that got more room; reminders go out once.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock

import discord
from cryptography.fernet import Fernet

from bot.cogs.event_cards import (
    BUTTON_TEMPLATE, EVENT_MANAGER_ROLE_ID, EVENTS_CHANNEL_ID, MENU_TEMPLATE, NOT_MANAGER, OTHER_SHIP, CardButton,
    EventCards, RoleMenu, build_card,
)
from bot.db.database import Database
from bot.discord_ui import LAYOUT_TEXT_LIMIT
from bot.uex.events import (
    ENDED, GOING, MAYBE, OPEN, PING_CHARS, PING_USERS, ROSTER_BUDGET, STARTED, WAITING, EventRole, Signup, details_text,
    next_in_line, parse_roles, ping_messages, placed_status, repeats_text, roster_text, ship_matches,
)

GUILD_ID = 1132378045232197694
ORG_ROLE = 77
START = int(datetime(2026, 11, 3, 0, 30, tzinfo=timezone.utc).timestamp())
WEEKLY = {"frequency": 2, "interval": 1, "by_weekday": [1]}


# -- 1. rules ---------------------------------------------------------------------------------------

def test_roles_read_from_one_line_and_bad_input_says_why():
    assert parse_roles("Miner 6, Hauler 2,  Escort   2") == (
        [EventRole("Miner", 6), EventRole("Hauler", 2), EventRole("Escort", 2)], None)
    assert parse_roles("Cargo Iron Clad 2\nSalvager 15")[0] == [EventRole("Cargo Iron Clad", 2), EventRole("Salvager", 15)]
    assert parse_roles("") == ([], None) and parse_roles(" None ") == ([], None)
    for bad, says in (("Miner", "`Miner`"), ("Miner 0", "1 to 99"), ("Miner 100", "1 to 99"),
                      ("Miner 2, miner 3", "listed twice"), (", ".join(f"R{i} 1" for i in range(11)), "Up to 10"),
                      ("X" * 41 + " 2", "40 characters")):
        roles, problem = parse_roles(bad)
        assert roles == [] and says in problem


def test_a_full_role_waits_and_the_first_waiting_moves_up():
    roles = [EventRole("Escort", 2)]
    signups = [Signup(1, GOING, "Escort"), Signup(2, GOING, "Escort"), Signup(3, WAITING, "Escort"),
               Signup(4, WAITING, "Escort")]
    assert placed_status(roles, signups, 5, "Escort") == WAITING
    assert placed_status(roles, signups, 1, "Escort") == GOING          # your own spot isn't counted
    assert placed_status([], signups, 5, None) == GOING                 # no roles, no limit
    assert next_in_line(roles, signups, "Escort") is None               # still full
    assert next_in_line(roles, signups[1:], "Escort") == 3              # a spot opened: first in line


def test_repeats_in_words_without_a_weekday():
    assert repeats_text(WEEKLY) == "Every week"
    assert repeats_text({"frequency": 2, "interval": 2}) == "Every other week"
    assert repeats_text({"frequency": 3, "interval": 1}) == "Every day"
    assert repeats_text({"frequency": 1, "interval": 3}) == "Every 3 months"
    assert repeats_text(None) is None


def test_a_typed_ship_means_one_ship_or_asks():
    # Names as UEX really has them: a short name and a full name with the maker.
    ships = [("Aurora Mk I MR", "RSI Aurora Mk I MR"), ("Aurora Mk I LN", "RSI Aurora Mk I LN"),
             ("Aurora Mk II", "RSI Aurora Mk II"), ("Arrastra", "Argo Arrastra"),
             ("C2 Hercules Starlifter", "Crusader C2 Hercules Starlifter"), ("Prospector", "MISC Prospector"),
             ("Cutlass Black", "Drake Cutlass Black"), ("Cutlass Black Best In Show Edition", None),
             ("F7C Hornet Mk I", "Anvil F7C Hornet Mk I"), ("F7C Hornet Mk II", "Anvil F7C Hornet Mk II")]
    assert ship_matches(ships, "arrastra") == ["Arrastra"]
    assert ship_matches(ships, "  c2 ") == ["C2 Hercules Starlifter"]
    assert ship_matches(ships, "hercules c2") == ["C2 Hercules Starlifter"]          # any order
    assert ship_matches(ships, "misc prospector") == ["Prospector"]                  # with the maker
    assert ship_matches(ships, "crusader hercules") == ["C2 Hercules Starlifter"]    # the maker, part of a name
    assert ship_matches(ships, "aurora mr") == ["Aurora Mk I MR"]                    # words, not one string
    assert ship_matches(ships, "aurora") == ["Aurora Mk II", "Aurora Mk I LN", "Aurora Mk I MR"]  # shortest first
    assert ship_matches(ships, "drake cutlass black") == ["Cutlass Black"]           # a full name is exact
    assert ship_matches(ships, "cutlass black") == ["Cutlass Black"]
    assert ship_matches(ships, "hornet mk2") == ["F7C Hornet Mk II"]                 # Mk II as mk2
    assert ship_matches(ships, "hornet mk 1") == ["F7C Hornet Mk I"]                 # and not Mk II's I
    assert ship_matches(ships, "polaris") == [] and ship_matches(ships, " ") == []
    assert ship_matches([("Arrow", None), ("Arrowhead", None)], "ARROW") == ["Arrow"]   # a whole name beats a longer one


# -- 2. the card ------------------------------------------------------------------------------------

def test_the_roster_shows_roles_the_wait_list_maybe_ships_and_regulars():
    roles = [EventRole("Miner", 6), EventRole("Escort", 1)]
    signups = [Signup(1, GOING, "Miner", "Prospector"), Signup(2, GOING, "Escort", "Arrow"),
               Signup(3, WAITING, "Escort"), Signup(4, MAYBE, "Miner"), Signup(5, MAYBE, None),
               Signup(6, GOING, None)]
    assert roster_text(roles, signups, regulars={1}) == "\n".join([
        "**Roster** - 3 going, 1 waiting, 2 maybe", "",
        "**Miner [1/6]**", "✓ <@1> · Prospector 🔁", "",
        "**Escort [1/1]** full", "✓ <@2> · Arrow", "",
        "**Going - no role yet**", "✓ <@6>", "",
        "**Wait list (1)**", "1. <@3> - Escort", "",
        "**Maybe (2)**", "? <@4> - Miner", "? <@5> - no role yet"])
    assert roster_text([], [Signup(1, GOING), Signup(2, MAYBE)], set()) == \
        "**Roster** - 1 going, 1 maybe\n\n**Going (1)**\n✓ <@1>\n\n**Maybe (1)**\n? <@2>"
    assert roster_text([], [], set()) == "**Roster** - nobody yet. Be the first!"


def test_a_long_roster_is_cut_with_a_count_not_mid_line():
    signups = [Signup(10 ** 17 + i, GOING, None, "Drake Corsair") for i in range(200)]
    text = roster_text([], signups, set())
    assert len(text) <= ROSTER_BUDGET and text.endswith("more on the roster.")
    shown = text.count("✓")
    assert text.endswith(f"…and {200 - shown} more on the roster.")


def test_details_say_when_where_and_how_often():
    text = details_text(creator_id=9, description="Cracking Rocks, Crafting Gear", starts_at=START,
                        voice_channel_id=55, repeats="Every week", regulars=2, state=OPEN)
    assert text.split("\n") == ["-# Organized by <@9>", "Cracking Rocks, Crafting Gear",
                                f"**Starts** <t:{START}:F> (<t:{START}:R>)", f"**Ready by** <t:{START - 900}:t>",
                                "**Where** <#55>", "**Repeats** Every week - 2 regulars signed up every time"]
    assert details_text(creator_id=None, description=None, starts_at=START, voice_channel_id=None, repeats=None,
                        regulars=0, state=ENDED).endswith("**This op has ended.**")


def _card(**over):
    card = {"card_id": 5, "guild_id": GUILD_ID, "event_id": 42, "starts_at": START, "name": "Mining Mondays",
            "description": "Cracking Rocks", "cover_url": "https://cdn.example/cover.png", "creator_id": 9,
            "voice_channel_id": 55, "repeats": "Every week", "channel_id": EVENTS_CHANNEL_ID, "message_id": 700,
            "state": OPEN, "reminded": 0}
    card.update(over)
    return card


def _items(view):
    return list(view.walk_children())


def test_the_card_has_the_right_controls():
    async def go():
        view = build_card(_card(), [EventRole("Miner", 6)], [], set(), "<@&77>")
        items = _items(view)
        ids = [getattr(i, "custom_id", None) for i in items if getattr(i, "custom_id", None)]
        assert ids == ["ev:role:5", "ev:maybe:5", "ev:leave:5", "ev:start:5", "ev:end:5"]
        links = {i.label: i.url for i in items if isinstance(i, discord.ui.Button) and i.url}
        assert links == {"Voice": f"https://discord.com/channels/{GUILD_ID}/55",
                         "Interested on Discord": f"https://discord.com/events/{GUILD_ID}/42"}
        texts = [i.content for i in items if isinstance(i, discord.ui.TextDisplay)]
        assert texts[0] == "<@&77> a new op is on the schedule."
        assert texts[1] == f"## [Mining Mondays](https://discord.com/events/{GUILD_ID}/42)"
        assert any(isinstance(i, discord.ui.MediaGallery) for i in items)
        no_roles = [getattr(i, "custom_id", None) for i in _items(build_card(_card(state=STARTED), [], [], set(), None))]
        assert "ev:join:5" in no_roles and "ev:role:5" not in no_roles and "ev:start:5" not in no_roles
        ended = [getattr(i, "custom_id", None) for i in _items(build_card(_card(state=ENDED), [], [], set(), None))]
        assert not any(ended)
    asyncio.run(go())


def test_the_biggest_card_still_fits_discords_limits():
    async def go():
        roles = [EventRole(f"{i} " + "R" * 38, 99) for i in range(10)]
        signups = [Signup(10 ** 18 + i, [GOING, WAITING, MAYBE][i % 3], roles[i % 10].name, "X" * 60) for i in range(300)]
        card = _card(name="N" * 100, description="D" * 5000, repeats="Every other week")
        view = build_card(card, roles, signups, {s.user_id for s in signups}, "<@&77>")
        assert view.content_length() <= LAYOUT_TEXT_LIMIT
        assert view.total_children_count <= 40
    asyncio.run(go())


def test_custom_ids_match_the_startup_templates():
    import re
    for action in ("join", "maybe", "leave", "start", "end", "every", "stopevery"):
        assert re.fullmatch(BUTTON_TEMPLATE, f"ev:{action}:12")
    assert re.fullmatch(MENU_TEMPLATE, "ev:role:12") and re.fullmatch(MENU_TEMPLATE, "ev:ship:12")
    button = asyncio.run(CardButton.from_custom_id(None, None, re.fullmatch(BUTTON_TEMPLATE, "ev:maybe:12")))
    assert (button.card_id, button.action) == (12, "maybe")
    menu = asyncio.run(RoleMenu.from_custom_id(None, None, re.fullmatch(MENU_TEMPLATE, "ev:ship:12")))
    assert (menu.card_id, menu.kind) == (12, "ship")


# -- fakes -----------------------------------------------------------------------------------------

class FakeChannel:
    def __init__(self, cid, guild=None, name="Star Citizen 1"):
        self.id, self.guild, self.name, self.sent, self.edits = cid, guild, name, [], []
        self.fail_sends = 0

    async def send(self, content=None, **kwargs):
        if self.fail_sends:
            self.fail_sends -= 1
            raise discord.HTTPException(NS(status=503, reason="Service Unavailable"), "upstream")
        self.sent.append((content, kwargs))
        return NS(id=900 + len(self.sent))

    def history(self, limit=None):
        async def messages():
            for message in reversed(getattr(self, "posted", [])):
                yield message
        return messages()

    def get_partial_message(self, message_id):
        async def edit(**kwargs):
            self.edits.append((message_id, kwargs))
        return NS(edit=edit)


class FakeUser:
    def __init__(self, user_id, roles=(), perms=None):
        self.id, self.roles, self.dms = user_id, [NS(id=r) for r in roles], []
        self.guild_permissions = perms or discord.Permissions.none()

    async def send(self, content=None, **kwargs):
        self.dms.append(content)


def _event(event_id=42, *, start=START, status=discord.EventStatus.scheduled, name="Mining Mondays"):
    return NS(id=event_id, guild_id=GUILD_ID, name=name, description="Cracking Rocks", cover_image=None, creator_id=9,
              channel_id=55, start_time=datetime.fromtimestamp(start, timezone.utc), status=status,
              start=AsyncMock(), end=AsyncMock())


class World:
    def __init__(self, db, events=(), rule=WEEKLY):
        self.db = db
        self.events = {e.id: e for e in events}
        self.guild = NS(id=GUILD_ID, get_scheduled_event=self.events.get,
                        fetch_scheduled_event=AsyncMock(side_effect=discord.NotFound(
                            NS(status=404, reason="Not Found"), "Unknown Guild Scheduled Event")))
        self.guild.scheduled_events = list(self.events.values())
        self.channel = FakeChannel(EVENTS_CHANNEL_ID, self.guild)
        self.voice = FakeChannel(55, self.guild)
        self.users: dict[int, FakeUser] = {}
        self.bot = NS(db=db, get_channel={EVENTS_CHANNEL_ID: self.channel, 55: self.voice}.get,
                      get_guild=lambda gid: self.guild, http=NS(get_scheduled_event=AsyncMock(
                          return_value={"recurrence_rule": rule})),
                      get_user=self.user, uex=NS(get_vehicles=AsyncMock(return_value=[
                          {"name": "Aurora MR"}, {"name": "Aurora LN"}, {"name": "Arrastra"}])))
        self.cog = EventCards(self.bot, start_loop=False)
        self.bot.get_cog = lambda name: self.cog

    def user(self, user_id, roles=(), perms=None):
        if user_id not in self.users:
            self.users[user_id] = FakeUser(user_id, roles, perms)
        return self.users[user_id]

    def interaction(self, user_id, **kw):
        return NS(user=self.user(user_id, **kw), guild=self.guild, client=self.bot,
                  response=NS(defer=AsyncMock(), send_modal=AsyncMock()), followup=NS(send=AsyncMock()))


def _run(tmp_path, body):
    async def go():
        db = Database(tmp_path / "ev.sqlite3", Fernet(Fernet.generate_key()))
        await db.init()
        await db.save_role_picker_roles(GUILD_ID, {"Org Events": ORG_ROLE})
        await body(db)
    asyncio.run(go())


async def _posted(world, event=None):
    event = event or next(iter(world.events.values()))
    await world.cog.sync(event)
    return await world.db.current_event_card(event.id)


def _reply(inter):
    return inter.followup.send.await_args.args[0]


# -- 3. posting ------------------------------------------------------------------------------------

def test_one_card_per_occurrence_pinging_org_events_with_regulars_on_it(tmp_path):
    async def body(db):
        await db.set_event_roles(42, [("Escort", 1)])
        await db.set_event_regular(42, 1, "Escort", "Arrow")
        await db.set_event_regular(42, 2, "Escort", None)
        world = World(db, [_event()])
        card = await _posted(world)
        assert card["starts_at"] == START and card["repeats"] == "Every week" and card["message_id"] == 901
        content, kwargs = world.channel.sent[0]
        assert content is None and kwargs["allowed_mentions"].roles[0].id == ORG_ROLE
        assert kwargs["allowed_mentions"].users is False and kwargs["allowed_mentions"].everyone is False
        signups = await db.get_event_signups(card["card_id"])
        assert [(s["user_id"], s["status"], s["ship"]) for s in signups] == [(1, GOING, "Arrow"), (2, WAITING, None)]
        await world.cog.sync(world.events[42])
        assert len(world.channel.sent) == 1                        # not posted twice
        await world.cog.close(card, ENDED, announce=False)
        await world.cog.sync(world.events[42])
        assert len(world.channel.sent) == 1                        # nor after it closed
    _run(tmp_path, body)


def test_a_new_time_pings_the_roster_and_a_past_occurrence_makes_way(tmp_path, monkeypatch):
    async def body(db):
        event = _event()
        world = World(db, [event])
        monkeypatch.setattr("bot.cogs.event_cards.time.time", lambda: START - 86400)
        card = await _posted(world)
        await db.save_event_signup(card["card_id"], 1, status=GOING, role=None, queued=1)
        await db.save_event_signup(card["card_id"], 2, status=MAYBE, role=None, queued=2)
        event.start_time = datetime.fromtimestamp(START + 1800, timezone.utc)
        await world.cog.sync(event)
        moved = await db.get_event_card(card["card_id"])
        assert moved["starts_at"] == START + 1800 and moved["reminded"] == 0
        content, kwargs = world.channel.sent[-1]
        assert content.startswith(f"**Time changed:** Mining Mondays now starts <t:{START + 1800}:F> (was <t:{START}:f>)")
        assert content.endswith("<@1> <@2>") and kwargs["reference"].message_id == 901
        # A week later the occurrence is over; Discord moves to the next one.
        monkeypatch.setattr("bot.cogs.event_cards.time.time", lambda: START + 86400)
        event.start_time = datetime.fromtimestamp(START + 7 * 86400, timezone.utc)
        await world.cog.sync(event)
        assert (await db.get_event_card(card["card_id"]))["state"] == ENDED
        nxt = await db.current_event_card(42)
        assert nxt["card_id"] != card["card_id"] and nxt["starts_at"] == START + 7 * 86400
    _run(tmp_path, body)


def test_a_cancelled_event_says_so_to_everyone_on_it(tmp_path):
    async def body(db):
        world = World(db, [_event()])
        card = await _posted(world)
        await db.save_event_signup(card["card_id"], 3, status=WAITING, role="Escort", queued=1)
        await world.cog.on_scheduled_event_delete(world.events[42])
        assert (await db.get_event_card(card["card_id"]))["state"] == "cancelled"
        assert world.channel.sent[-1][0] == "**Mining Mondays** was cancelled.\n<@3>"
    _run(tmp_path, body)


# -- 4. members ------------------------------------------------------------------------------------

def test_joining_a_full_role_waits_and_leaving_moves_the_line_up_with_a_dm(tmp_path):
    async def body(db):
        await db.set_event_roles(42, [("Miner", 6), ("Escort", 1)])
        world = World(db, [_event()])
        card = await _posted(world)
        cid = card["card_id"]
        a = world.interaction(1)
        await world.cog.on_join(a, cid, role="Escort")
        assert _reply(a).startswith("You're in for **Mining Mondays** as **Escort** (1/1).\nWhat are you bringing?")
        b = world.interaction(2)
        await world.cog.on_join(b, cid, role="Escort")
        assert _reply(b).startswith("**Escort** is full - you're **#1 on the wait list**.")
        c = world.interaction(3)
        await world.cog.on_join(c, cid, role="Bogus")
        assert _reply(c) == "Pick a role from the menu on the card."
        await world.cog.on_join(world.interaction(1), cid, role="Miner")   # switching frees Escort
        rows = {s["user_id"]: (s["status"], s["role"]) for s in await db.get_event_signups(cid)}
        assert rows == {1: (GOING, "Miner"), 2: (GOING, "Escort")}
        assert world.users[2].dms[0].startswith("A spot opened up - you're now **Escort** for **Mining Mondays**")
        await world.cog.on_join(world.interaction(3), cid, role="Escort")
        await world.cog.on_leave(world.interaction(2), cid)
        assert world.users[3].dms and (await db.get_event_signups(cid))[-1]["status"] == GOING
        left = world.interaction(9)
        await world.cog.on_leave(left, cid)
        assert _reply(left) == "You're not on this one."
        assert world.channel.edits                                      # the card was redrawn
    _run(tmp_path, body)


def test_maybe_takes_no_spot_and_maybe_again_puts_you_back(tmp_path):
    async def body(db):
        await db.set_event_roles(42, [("Escort", 1)])
        world = World(db, [_event()])
        cid = (await _posted(world))["card_id"]
        await world.cog.on_join(world.interaction(1), cid, role="Escort")
        await world.cog.on_join(world.interaction(2), cid, role="Escort")     # waiting
        first = world.interaction(1)
        await world.cog.on_maybe(first, cid)
        assert _reply(first).startswith("You're on **Maybe**")
        rows = {s["user_id"]: (s["status"], s["role"]) for s in await db.get_event_signups(cid)}
        assert rows == {1: (MAYBE, "Escort"), 2: (GOING, "Escort")}        # 2 moved up
        back = world.interaction(1)
        await world.cog.on_maybe(back, cid)
        assert _reply(back).startswith("**Escort** is full - you're **#1 on the wait list**")
        fresh = world.interaction(3)
        await world.cog.on_maybe(fresh, cid)
        again = world.interaction(3)
        await world.cog.on_maybe(again, cid)
        assert _reply(again) == "You're on **Maybe**. Pick a role from the menu on the card to join."
    _run(tmp_path, body)


def test_ships_from_the_menu_or_typed_and_every_time(tmp_path):
    async def body(db):
        world = World(db, [_event()])
        cid = (await _posted(world))["card_id"]
        await db.set_trading_preferences(1, ship_name="Prospector")
        join = world.interaction(1)
        await world.cog.on_join(join, cid)
        panel = join.followup.send.await_args.kwargs["view"]
        menu = next(i for i in panel.children if getattr(i, "custom_id", "") == f"ev:ship:{cid}")
        assert [o.label for o in menu.item.options] == ["Prospector", "Not sure yet", "Another ship..."]
        assert any(getattr(i, "custom_id", "") == f"ev:every:{cid}" for i in panel.children)
        await world.cog.on_ship(world.interaction(1), cid, "Prospector")
        typed = world.interaction(1)
        await world.cog.on_ship(typed, cid, OTHER_SHIP)
        typed.response.send_modal.assert_awaited_once()
        ask = world.interaction(1)
        await world.cog.typed_ship(ask, cid, "aurora")
        assert _reply(ask) == "Which one?"
        choice = ask.followup.send.await_args.kwargs["view"]
        assert [o.label for o in choice.menu.options] == ["Aurora LN", "Aurora MR"]
        picked = world.interaction(1)
        choice.menu._values = ["Aurora MR"]
        await choice.pick(picked)
        assert _reply(picked) == "Got it - you're bringing the **Aurora MR**."
        done = world.interaction(1)
        await world.cog.typed_ship(done, cid, "arras")
        assert _reply(done) == "Got it - you're bringing the **Arrastra**."
        assert await db.event_ship_history(1) == ["Arrastra", "Aurora MR", "Prospector"]
        every = world.interaction(1)
        await world.cog.on_every(every, cid)
        assert await db.get_event_regulars(42) == [{"user_id": 1, "role": None, "ship": "Arrastra"}]
        await world.cog.on_leave(world.interaction(1), cid)
        await world.cog.on_stopevery(world.interaction(1), cid)
        assert await db.get_event_regulars(42) == []
        nobody = world.interaction(4)
        await world.cog.on_ship(nobody, cid, "Prospector")
        assert _reply(nobody).startswith("You're not on this op")
    _run(tmp_path, body)


# -- 5. organizers ---------------------------------------------------------------------------------

def test_only_organizers_start_and_start_pings_the_roster_once(tmp_path):
    async def body(db):
        world = World(db, [_event()])
        card = await _posted(world)
        cid = card["card_id"]
        await world.cog.on_join(world.interaction(1), cid)
        stranger = world.interaction(2)
        await world.cog.on_start(stranger, cid)
        assert _reply(stranger) == NOT_MANAGER
        manager = world.interaction(3, roles=[EVENT_MANAGER_ROLE_ID])
        await world.cog.on_start(manager, cid)
        assert _reply(manager) == "Started - the roster has been pinged."
        world.events[42].start.assert_awaited_once()
        content, kwargs = world.channel.sent[-1]
        assert content == "**Mining Mondays is starting now** - jump in <#55>.\n<@1>"
        assert kwargs["view"].children[0].label == "Join Star Citizen 1"
        organizer = world.interaction(9)
        await world.cog.on_start(organizer, cid)
        assert _reply(organizer) == "It's already started." and len(world.channel.sent) == 2
        admin = world.interaction(4, perms=discord.Permissions(administrator=True))
        await world.cog.on_end(admin, cid)
        assert (await db.get_event_card(cid))["state"] == ENDED
        assert world.channel.sent[-1][0] == "**Mining Mondays** has ended. Thanks for flying!"
        late = world.interaction(1)
        await world.cog.on_join(late, cid)
        assert _reply(late) == "This op's sign-ups are closed."
    _run(tmp_path, body)


def test_event_roles_moves_people_off_a_gone_role_and_up_a_longer_line(tmp_path):
    async def body(db):
        await db.set_event_roles(42, [("Escort", 1), ("Scout", 2)])
        world = World(db, [_event()])
        cid = (await _posted(world))["card_id"]
        for uid, role in ((1, "Escort"), (2, "Escort"), (3, "Escort"), (4, "Scout")):
            await world.cog.on_join(world.interaction(uid), cid, role=role)
        stranger = world.interaction(5)
        await EventCards.event_roles.callback(world.cog, stranger, "42", "Escort 3")
        assert _reply(stranger) == NOT_MANAGER
        bad = world.interaction(9)
        await EventCards.event_roles.callback(world.cog, bad, "42", "Escort")
        assert "Miner 6" in _reply(bad)
        good = world.interaction(9)
        await EventCards.event_roles.callback(world.cog, good, "42", "Escort 3")
        assert _reply(good).startswith("Roles set for **Mining Mondays**:\nEscort - 3 spots\n")
        rows = {s["user_id"]: (s["status"], s["role"]) for s in await db.get_event_signups(cid)}
        assert rows == {1: (GOING, "Escort"), 2: (GOING, "Escort"), 3: (GOING, "Escort"), 4: (GOING, None)}
        assert world.users[2].dms and world.users[3].dms
        none = world.interaction(9)
        await EventCards.event_roles.callback(world.cog, none, "42", "none")
        assert _reply(none) == "**Mining Mondays** has no roles now - people just press Join."
    _run(tmp_path, body)


def test_reminders_go_out_once_and_unended_cards_close(tmp_path, monkeypatch):
    async def body(db):
        world = World(db, [_event()])
        card = await _posted(world)
        await db.save_event_signup(card["card_id"], 1, status=GOING, role=None, queued=1)
        world.cog._ticks = 1                                           # skip the re-read of every event
        monkeypatch.setattr("bot.cogs.event_cards.time.time", lambda: START - 1800)
        await world.cog.tick.coro(world.cog)
        await world.cog.tick.coro(world.cog)
        assert not await db.claim_event_reminder(card["card_id"])     # claimed once, never again
        reminders = [c for c, _ in world.channel.sent if c and f"starts <t:{START}:R>" in c]
        assert reminders == [f"**Mining Mondays** starts <t:{START}:R>, at <t:{START}:t>, in <#55>.\n<@1>"]
        monkeypatch.setattr("bot.cogs.event_cards.time.time", lambda: START + 13 * 3600)
        await world.cog.tick.coro(world.cog)
        assert (await db.get_event_card(card["card_id"]))["state"] == ENDED
    _run(tmp_path, body)

def test_a_redraw_keeps_the_org_events_mention(tmp_path):
    async def body(db):
        world = World(db, [_event()])
        card = await _posted(world)
        await world.cog.refresh(card["card_id"])
        _, kwargs = world.channel.edits[-1]
        mentions = kwargs["allowed_mentions"]
        assert [r.id for r in mentions.roles] == [ORG_ROLE]   # Discord re-reads mentions on an edit
        assert mentions.users is False and mentions.everyone is False
    _run(tmp_path, body)


def test_a_card_whose_post_failed_is_posted_at_the_next_sync(tmp_path):
    async def body(db):
        world = World(db, [_event()])
        world.channel.fail_sends = 1
        card = await _posted(world)
        assert card["message_id"] is None and world.channel.sent == []
        await world.cog.sync(world.events[42])
        card = await db.get_event_card(card["card_id"])
        assert card["message_id"] == 901 and len(world.channel.sent) == 1
        assert world.channel.sent[0][1]["allowed_mentions"].roles[0].id == ORG_ROLE
        assert await db.current_event_card(42) == card                 # the same card, not a second one
        await world.cog.sync(world.events[42])
        assert len(world.channel.sent) == 1                            # and only once
    _run(tmp_path, body)


def test_a_late_op_pushed_back_keeps_its_card_and_roster(tmp_path, monkeypatch):
    async def body(db):
        for rule, event_id in ((None, 42), (WEEKLY, 43)):              # a one-off, and a weekly op
            event = _event(event_id)
            world = World(db, [event], rule=rule)
            card = await _posted(world)
            await db.save_event_signup(card["card_id"], 1, status=GOING, role=None, queued=1)
            monkeypatch.setattr("bot.cogs.event_cards.time.time", lambda: START + 600)   # 10 minutes late
            event.start_time = datetime.fromtimestamp(START + 1800, timezone.utc)
            await world.cog.sync(event)
            moved = await db.current_event_card(event_id)
            assert moved["card_id"] == card["card_id"] and moved["starts_at"] == START + 1800
            assert [s["user_id"] for s in await db.get_event_signups(card["card_id"])] == [1]
            assert world.channel.sent[-1][0].startswith("**Time changed:**")
            assert sum(1 for c, _ in world.channel.sent if c is None) == 1      # no second card or Org ping
            monkeypatch.undo()
    _run(tmp_path, body)


def test_an_op_under_way_is_started_whatever_its_start_time_says(tmp_path, monkeypatch):
    async def body(db):
        event = _event()
        world = World(db, [event])
        card = await _posted(world)
        await db.save_event_signup(card["card_id"], 1, status=GOING, role=None, queued=1)
        monkeypatch.setattr("bot.cogs.event_cards.time.time", lambda: START - 1200)
        event.status = discord.EventStatus.active                      # started 20 minutes early
        event.start_time = datetime.fromtimestamp(START - 1200, timezone.utc)
        await world.cog.sync(event)
        now = await db.get_event_card(card["card_id"])
        assert now["state"] == STARTED and await db.current_event_card(42) == now
        texts = [c for c, _ in world.channel.sent if c]
        assert not any("Time changed" in c for c in texts) and any("<@1>" in c for c in texts)
        await world.cog.sync(event)                                    # the next re-read changes nothing
        assert (await db.get_event_card(card["card_id"]))["state"] == STARTED
    _run(tmp_path, body)


def test_an_event_deleted_while_the_bot_was_down_is_found_and_closed(tmp_path, monkeypatch):
    async def body(db):
        ahead, done, unsure = _event(42), _event(43, name="Contested Tuesdays"), _event(44, name="Salvage Sundays")
        old = _event(45, name="Last Week")
        world = World(db, [unsure, ahead, done, old])     # the one Discord won't answer about comes first
        cards = {e.id: await _posted(world, e) for e in (unsure, ahead, done, old)}
        await db.save_event_signup(cards[42]["card_id"], 1, status=GOING, role=None, queued=1)
        await db.set_event_card_state(cards[43]["card_id"], STARTED, only_from=(OPEN,))
        await db.set_event_card_state(cards[45]["card_id"], ENDED, only_from=(OPEN,))
        world.guild.scheduled_events = []                              # all three gone from Discord's list
        not_found = discord.NotFound(NS(status=404, reason="Not Found"), "Unknown Guild Scheduled Event")
        world.guild.fetch_scheduled_event.side_effect = lambda event_id: (
            _raise(discord.HTTPException(NS(status=500, reason="Error"), "boom")) if event_id == 44
            else _raise(not_found))
        monkeypatch.setattr("bot.cogs.event_cards.time.time", lambda: START - 7200)
        await world.cog.tick.coro(world.cog)
        assert (await db.get_event_card(cards[42]["card_id"]))["state"] == "cancelled"
        assert world.channel.sent[-1][0] == "**Mining Mondays** was cancelled.\n<@1>"
        assert (await db.get_event_card(cards[43]["card_id"]))["state"] == ENDED     # under way: just over
        assert (await db.get_event_card(cards[44]["card_id"]))["state"] == OPEN      # Discord didn't answer
        asked = sorted(c.args[0] for c in world.guild.fetch_scheduled_event.await_args_list)
        assert asked == [42, 43, 44]                                   # never about a card already closed
    _run(tmp_path, body)

def test_a_card_posted_but_never_saved_is_found_not_posted_again(tmp_path):
    async def body(db):
        world = World(db, [_event()])
        card = await _posted(world)
        await db.update_event_card(card["card_id"], message_id=None)    # the bot stopped before saving it
        layout = NS(children=[NS(children=[NS(custom_id=f"ev:join:{card['card_id']}", children=None)])])
        world.channel.posted = [NS(id=901, author=NS(bot=True), components=[layout]),
                                NS(id=950, author=NS(bot=True), components=[])]
        await world.cog.sync(world.events[42])
        assert (await db.get_event_card(card["card_id"]))["message_id"] == 901
        assert len(world.channel.sent) == 1 and world.channel.edits[-1][0] == 901   # adopted and redrawn
    _run(tmp_path, body)


def test_a_card_whose_post_failed_is_posted_when_the_op_starts(tmp_path):
    async def body(db):
        event = _event()
        world = World(db, [event])
        world.channel.fail_sends = 1
        card = await _posted(world)
        event.status = discord.EventStatus.active
        await world.cog.sync(event)
        card = await db.get_event_card(card["card_id"])
        assert card["message_id"] == 901 and card["state"] == STARTED
        assert world.channel.sent[0][0] is None                         # the card, then the start ping
    _run(tmp_path, body)


def test_last_weeks_unclosed_card_is_closed_not_started_after_downtime(tmp_path, monkeypatch):
    async def body(db):
        event = _event()
        world = World(db, [event])
        card = await _posted(world)
        await db.save_event_signup(card["card_id"], 1, status=GOING, role=None, queued=1)
        monkeypatch.setattr("bot.cogs.event_cards.time.time", lambda: START + 7 * 86400 + 600)
        event.status = discord.EventStatus.active                      # next week's op is on
        event.start_time = datetime.fromtimestamp(START + 7 * 86400, timezone.utc)
        await world.cog.sync(event)
        assert (await db.get_event_card(card["card_id"]))["state"] == ENDED
        assert [c for c, _ in world.channel.sent if c] == []            # last week's roster isn't pinged
    _run(tmp_path, body)


def test_a_card_stored_without_its_repeat_rule_asks_again(tmp_path, monkeypatch):
    async def body(db):
        event = _event()
        world = World(db, [event])
        world.bot.http.get_scheduled_event.side_effect = discord.HTTPException(
            NS(status=500, reason="Error"), "boom")
        card = await _posted(world)
        assert card["repeats"] is None
        world.bot.http.get_scheduled_event.side_effect = None          # Discord answers this time: weekly
        monkeypatch.setattr("bot.cogs.event_cards.time.time", lambda: START + 3600)
        event.start_time = datetime.fromtimestamp(START + 7 * 86400, timezone.utc)
        await world.cog.sync(event)
        assert (await db.get_event_card(card["card_id"]))["state"] == ENDED
        assert (await db.get_event_card(card["card_id"]))["repeats"] == "Every week"
        assert (await db.current_event_card(42))["starts_at"] == START + 7 * 86400
    _run(tmp_path, body)


def test_a_failing_event_check_never_stops_reminders(tmp_path, monkeypatch):
    async def body(db):
        broken, gone, fine = _event(41, name="Gone Op"), _event(43, name="Deleted Op"), _event(42)
        world = World(db, [broken, gone, fine])
        await _posted(world, broken)
        deleted = await _posted(world, gone)
        card = await _posted(world, fine)
        await db.save_event_signup(card["card_id"], 1, status=GOING, role=None, queued=1)
        world.guild.scheduled_events = [fine]
        not_found = discord.NotFound(NS(status=404, reason="Not Found"), "Unknown Guild Scheduled Event")
        world.guild.fetch_scheduled_event.side_effect = lambda event_id: _raise(
            asyncio.TimeoutError() if event_id == 41 else not_found)
        monkeypatch.setattr("bot.cogs.event_cards.time.time", lambda: START - 1800)
        await world.cog.tick.coro(world.cog)
        assert any(c and f"starts <t:{START}:R>" in c for c, _ in world.channel.sent)  # this same minute
        assert (await db.get_event_card(deleted["card_id"]))["state"] == "cancelled"   # the next card still checked
        await world.cog.tick.coro(world.cog)
        assert world.guild.fetch_scheduled_event.await_count == 2      # one re-read, not one a minute
    _run(tmp_path, body)


def test_a_big_roster_is_pinged_in_messages_discord_accepts():
    text = "**Mining Mondays is starting now** - jump in <#55>."
    ids = [10**18 + n for n in range(250)]                       # real ids are ~19 digits
    messages = ping_messages(text, ids)
    assert messages[0][0].startswith(text + "\n<@")
    assert all(len(content) <= PING_CHARS and len(batch) <= PING_USERS for content, batch in messages)
    assert [uid for _, batch in messages for uid in batch] == ids   # everyone once, in order
    for content, batch in messages:                               # each message pings exactly its own
        assert content.count("<@") == len(batch) and all(f"<@{uid}>" in content for uid in batch)
    assert ping_messages(text, []) == [(text, [])]
    short = ping_messages(text, list(range(1, 251)))              # short mentions: the 100-member cap binds first
    assert [len(batch) for _, batch in short] == [100, 100, 50]


def test_a_roster_past_100_gets_every_ping(tmp_path):
    async def body(db):
        world = World(db, [_event()])
        card = await _posted(world)
        for n in range(130):
            await db.save_event_signup(card["card_id"], 10**18 + n, status=GOING, role=None, queued=n)
        await world.cog.ping_roster(card, "**Mining Mondays is starting now**!")
        pings = world.channel.sent[1:]
        assert len(pings) == 2
        pinged = [o.id for _, kwargs in pings for o in kwargs["allowed_mentions"].users]
        assert sorted(pinged) == [10**18 + n for n in range(130)]
        assert all(len(kwargs["allowed_mentions"].users) <= 100 and kwargs["reference"].message_id == 901
                   for _, kwargs in pings)
    _run(tmp_path, body)


def test_a_time_change_inside_the_hour_is_its_reminder(tmp_path, monkeypatch):
    async def body(db):
        event = _event()
        world = World(db, [event])
        monkeypatch.setattr("bot.cogs.event_cards.time.time", lambda: START - 7200)
        card = await _posted(world)
        await db.save_event_signup(card["card_id"], 1, status=GOING, role=None, queued=1)
        event.start_time = datetime.fromtimestamp(START - 5400, timezone.utc)   # moved to 30 minutes from now
        await world.cog.sync(event)
        world.cog._ticks = 1
        await world.cog.tick.coro(world.cog)
        texts = [c for c, _ in world.channel.sent if c]
        assert len(texts) == 1 and texts[0].startswith("**Time changed:**")    # one ping, not two
    _run(tmp_path, body)



def _raise(exc):
    raise exc



def test_command_surface_fits_discords_limits():
    cmd = EventCards.event_roles
    assert len(cmd.name) <= 32 and len(str(cmd.description)) <= 100
    assert all(len(str(p.description)) <= 100 for p in cmd.parameters)
