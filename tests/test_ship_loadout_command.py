"""/ship-loadout end to end (bot/cogs/ship_parts_finder.py): the command and the parts
browser's "Recommend a loadout" button, through the finder's real candidates_for_port, a real
database and fake UEX/wiki clients - posted in the private thread, every profile switch,
keep-stock lines, the kept-gimbal gun rule, the power warning, "Add all to shopping list",
paging, and what the player is told when the wiki or UEX doesn't answer.

The ship is an Avenger Titan as the live wiki has it (4.10.1): an S4 nose gimbal holding a
Revenant Gatling, two S3 wing gimbals holding Omnisky IX cannons, a power plant, two coolers, a
shield and a missile rack. Stock details come from tests/test_ship_loadout.py; the parts for
sale are made up in the same shape."""
import asyncio
import sys
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock

import discord
from cryptography.fernet import Fernet
from discord import app_commands

from bot.cogs import ship_parts_finder
from bot.cogs.help import CATEGORIES
from bot.cogs.ship_parts_finder import (
    LOADOUT_EXPIRED_NOTE, MAX_CUSTOM_ID_CHARS, MESSAGE_LIMIT, WIKI_SILENT_FOR_SLOT, LoadoutDoneButton, LoadoutView,
    PartsBrowserView, ShipPartsFinder, _LoadoutDoneStub, _LoadoutPageButton, _ProfileButton, loadout_done_custom_id,
)
from bot.db.database import Database
from bot.uex.ship_loadout import (
    NO_STATS, NOTHING_BEATS_STOCK, PROFILES, STOCK_IS_BEST, STOCK_RACKS, STOCK_UNKNOWN, LoadoutSlot, group_slots,
    purchases,
)
from bot.uex.ship_parts import ShipPort
from bot.wiki_api import WikiDuplicateNameError, WikiUnavailableError
from tests.test_ship_loadout import (
    BRACER, BULWARK, ENDURANCE, MSD_322, OMNISKY, REVENANT, VARIPUCK_S3, VARIPUCK_S4, _cooler, _gimbal, _gun, _plant,
    _shield,
)
from tests.test_ship_parts_finder import FakeChannel, FakeThread, _price

VEHICLE = {"id": 1, "name": "Avenger Titan"}
NOSE, LEFT_WING, RIGHT_WING = ("hardpoint_weapon_class2_nose", "hardpoint_weapon_gun_class1_left_wing",
                               "hardpoint_weapon_gun_class1_right_wing")
GUN_PORT = "hardpoint_class_2"  # the gun slot inside every stock VariPuck gimbal

# For sale. Their numbers decide each profile's pick; see the expectations in the tests.
MANTIS = _gun("Mantis GT-220 Gatling", 853.3, uuid="mantis")
M5A = _gun("M5A Cannon", 930, uuid="m5a")
TARANTULA = _gun("Tarantula GT-870 Mk3", 900, size=4, uuid="tarantula")  # S4, weaker than the stock Revenant
BADGER = _gun("CF-227 Badger Repeater", 2000, size=2, uuid="badger")  # S2: fits no gun slot here
SUNFLARE = _plant("SunFlare", 16, uuid="sunflare", em=9000, health=280)
JS300 = _plant("JS-300", 12, uuid="js300", em=8000, health=400)
GLACIER = _cooler("Glacier", 40, uuid="glacier", ir=9000, health=250, power=8)
HEATSAFE = _cooler("HeatSafe", 30, uuid="heatsafe", ir=2330, health=150)
RHADA = _shield("5SA 'Rhada'", 3000, uuid="rhada")
RHADA_TWIN = _shield("Rhada Twin", 3000, uuid="rhada-twin")  # as good as the Rhada, dearer, at a nearer shop
CLOAK = _shield("Cloak", 1900, uuid="cloak", em=250)
GIMBAL_FOR_SALE = dict(_gimbal(3), uuid="shop-gimbal")  # a mount: never what a gun slot is filled with

SOLD = [  # (UEX id, detail, category, price, terminal)
    (1, MANTIS, "Guns", 24045, 11), (2, M5A, "Guns", 69137, 12), (3, TARANTULA, "Guns", 30000, 11),
    (4, BADGER, "Guns", 5000, 11), (5, SUNFLARE, "Power Plants", 60500, 13), (6, JS300, "Power Plants", 9000, 11),
    (7, GLACIER, "Coolers", 50000, 12), (8, HEATSAFE, "Coolers", 30800, 13),
    (9, RHADA, "Shield Generators", 15000, 11), (10, RHADA_TWIN, "Shield Generators", 20000, 12),
    (11, CLOAK, "Shield Generators", 31500, 12), (12, GIMBAL_FOR_SALE, "Turrets", 2000, 11),
]
DISTANCES = {11: 40.0, 12: 5.0, 13: 20.0}

PORTS = [
    {"name": NOSE, "port_type": "Turret", "size_min": 4, "size_max": 4, "accepts_guns": True,
     "equipped_uuid": VARIPUCK_S4["uuid"]},
    *({"name": wing, "port_type": "Turret", "size_min": 3, "size_max": 3, "accepts_guns": True,
       "equipped_uuid": VARIPUCK_S3["uuid"]} for wing in (LEFT_WING, RIGHT_WING)),
    {"name": "hardpoint_power_plant", "port_type": "PowerPlant", "size_min": 1, "size_max": 1,
     "equipped_uuid": ENDURANCE["uuid"]},
    *({"name": f"hardpoint_cooler_{side}", "port_type": "Cooler", "size_min": 1, "size_max": 1,
       "equipped_uuid": BRACER["uuid"]} for side in ("left", "right")),
    {"name": "hardpoint_shield_generator_left", "port_type": "Shield", "size_min": 1, "size_max": 1,
     "equipped_uuid": BULWARK["uuid"]},
    {"name": "hardpoint_weapon_missilerack_left_wing", "port_type": "MissileLauncher", "size_min": 3, "size_max": 3,
     "equipped_uuid": MSD_322["uuid"]},
]
# GET /vehicles/{uuid}'s nested ports: the only place each gimbal's stock gun is named.
STOCK_TREE = [
    {"name": NOSE, "equipped_item_uuid": VARIPUCK_S4["uuid"],
     "ports": [{"name": GUN_PORT, "equipped_item_uuid": REVENANT["uuid"]}]},
    *({"name": wing, "equipped_item_uuid": VARIPUCK_S3["uuid"],
       "ports": [{"name": GUN_PORT, "equipped_item_uuid": OMNISKY["uuid"]}]} for wing in (LEFT_WING, RIGHT_WING)),
    *({"name": p["name"], "equipped_item_uuid": p["equipped_uuid"], "ports": None} for p in PORTS[3:]),
]


def _wiki(*, unavailable=(), stock_tree=STOCK_TREE, extra=()):
    """Every stock and sold part's detail by uuid (plus `extra` details); a uuid in
    `unavailable` acts like the wiki not answering. `stock_tree` may be an exception to raise
    instead."""
    details = {d["uuid"]: d for d in (ENDURANCE, BRACER, BULWARK, REVENANT, OMNISKY, VARIPUCK_S3, VARIPUCK_S4, MSD_322,
                                      *extra)}
    details.update({detail["uuid"]: detail for _, detail, *_ in SOLD})

    async def get_item_detail(uuid):
        if uuid in unavailable:
            raise WikiUnavailableError("no answer")
        if uuid not in details:
            raise ship_parts_finder.WikiApiError("not found")
        return details[uuid]

    async def get_vehicle_stock_ports(name):
        if isinstance(stock_tree, Exception):
            raise stock_tree
        return stock_tree if name == VEHICLE["name"] else []

    return NS(
        get_item_detail=AsyncMock(side_effect=get_item_detail),
        find_item_detail_by_name=AsyncMock(return_value=None),
        find_item_variants_by_name=AsyncMock(return_value=[]),
        get_vehicle_stock_ports=AsyncMock(side_effect=get_vehicle_stock_ports),
    )


def _uex(vehicles=None):
    catalog = [{"id": id_item, "uuid": detail["uuid"], "category": category, "size": str(detail["size"]),
                "name": detail["name"]} for id_item, detail, category, _, _ in SOLD]
    prices = [_price(id_item, price, terminal) for id_item, _, _, price, terminal in SOLD]
    return NS(
        get_vehicles=AsyncMock(return_value=[VEHICLE] if vehicles is None else vehicles),
        get_item_catalog=AsyncMock(return_value=catalog),
        get_items_prices_all=AsyncMock(return_value=prices),
        get_terminal_distance=AsyncMock(side_effect=lambda origin, dest: (
            {"distance": DISTANCES[dest]} if dest in DISTANCES else None)),
    )


async def _cog(tmp_path, monkeypatch, *, wiki=None, uex=None, ports=PORTS):
    db = Database(tmp_path / "loadout.sqlite", Fernet(Fernet.generate_key()))
    await db.init()
    if ports:
        await db.replace_ship_parts_reference(VEHICLE["id"], VEHICLE["name"], ports)
    db.resolve_terminal_id_by_name = AsyncMock(return_value=(99, "Area 18 TDD"))
    thread = FakeThread()
    monkeypatch.setattr(ship_parts_finder.discord, "TextChannel", FakeChannel)
    monkeypatch.setattr(ship_parts_finder.discord, "Thread", FakeThread)
    bot = NS(db=db, uex=uex or _uex(), get_channel=lambda _id: thread, fetch_channel=AsyncMock())
    cog = ShipPartsFinder(bot, wiki_client=wiki or _wiki(), start_refresh=False)
    return cog, thread


def _interaction(thread, *, user_id=1, guild_id=10):
    return NS(
        id=300, guild_id=guild_id, channel=FakeChannel(thread), user=NS(id=user_id, display_name="Pilot"),
        response=NS(defer=AsyncMock(), send_message=AsyncMock(), edit_message=AsyncMock()),
        followup=NS(send=AsyncMock()), edit_original_response=AsyncMock(),
    )


async def _run_command(cog, thread, *, ship="Avenger Titan", profile=None, location=None):
    interaction = _interaction(thread)
    choice = app_commands.Choice(name=profile, value=profile) if profile else None
    await cog.ship_loadout.callback(cog, interaction, ship, choice, location)
    return interaction


def _posted(thread):
    """(content, view) of the loadout message sent into the thread."""
    call = next(c for c in reversed(thread.send.await_args_list) if isinstance(c.kwargs.get("view"), LoadoutView))
    return call.kwargs["content"], call.kwargs["view"]


def _line(text, label):
    return next(line for line in text.splitlines() if line.startswith(f"**{label}**"))


async def _switch(view, profile, *, user_id=1):
    click = _interaction(None, user_id=user_id)
    button = next(c for c in view.children if isinstance(c, _ProfileButton) and c.profile == profile)
    if await view.interaction_check(click):
        await button.callback(click)
    return click


# -- the command -------------------------------------------------------------------------------

def test_the_command_posts_a_balanced_loadout_in_the_private_thread(tmp_path, monkeypatch):
    async def run():
        cog, thread = await _cog(tmp_path, monkeypatch)
        interaction = await _run_command(cog, thread)
        return interaction, thread

    interaction, thread = asyncio.run(run())
    interaction.response.defer.assert_awaited_once_with(ephemeral=True)
    text, view = _posted(thread)
    assert view.message is thread.message, "kept, so idling can grey it out with a plain edit"
    assert view.profile == "Balanced" and text.startswith("**Avenger Titan** recommended loadout · **Balanced**")
    assert _line(text, "S4 Nose Gun") == ("**S4 Nose Gun** · keep stock **Revenant Gatling** (1,266 DPS) - "
                                         f"{STOCK_IS_BEST}")
    assert _line(text, "2x S3 Wing Gun").startswith(
        "**2x S3 Wing Gun** → **M5A Cannon** · 930 DPS (was 547 DPS stock) · 69,137 aUEC each · Shop 12")
    assert "**S1 Power Plant** → **SunFlare** · 16 power pips (was 15 power pips stock)" in text
    assert "**2x S1 Cooler** → **Glacier** · 40 cooling segments (was 34 cooling segments stock)" in text
    assert "**S1 Shield Generator Left** → **5SA 'Rhada'** · 3,000 shield HP (was 2,160 shield HP stock)" in text
    assert _line(text, "S3 Left Wing Missile Rack").endswith(f"keep stock **MSD-322 Missile Rack** (2x S2 missiles) - "
                                                        f"{STOCK_RACKS}")
    # 2x 69,137 + 60,500 + 2x 50,000 + 15,000
    assert "**Total: 313,774 aUEC** for 6 parts" in text
    assert "Gm" not in text and "ties go to the cheaper part" in text, "no location: no distances, cheapest wins ties"
    assert len(text) <= MESSAGE_LIMIT
    pointer = interaction.followup.send.await_args
    assert thread.mention in pointer.args[0] and pointer.kwargs["ephemeral"] is True


def test_the_profile_option_opens_that_profile(tmp_path, monkeypatch):
    async def run():
        cog, thread = await _cog(tmp_path, monkeypatch)
        await _run_command(cog, thread, profile="Budget")
        return thread

    text, view = _posted(asyncio.run(run()))
    assert view.profile == "Budget" and "· **Budget**" in text


def test_the_profile_choices_and_option_texts_fit_discords_limits():
    command = ShipPartsFinder.ship_loadout
    assert command.name == "ship-loadout" and len(command.description) <= 100
    for param in command.parameters:
        assert len(str(param.description)) <= 100
    profile = next(p for p in command.parameters if p.name == "profile")
    assert [c.value for c in profile.choices] == list(PROFILES)
    assert all(len(c.name) <= 100 for c in profile.choices)
    assert [p.name for p in command.parameters if not p.required] == ["profile", "location"]


def test_the_gun_stays_in_the_stock_gimbal_and_fits_the_gimbals_own_gun_slot(tmp_path, monkeypatch):
    async def run():
        cog, thread = await _cog(tmp_path, monkeypatch)
        spy = AsyncMock(side_effect=cog.candidates_for_port)
        cog.candidates_for_port = spy
        await _run_command(cog, thread)
        return thread, spy

    thread, spy = asyncio.run(run())
    text, view = _posted(thread)
    gun_calls = [c for c in spy.await_args_list if c.kwargs["category"] == "Guns"]
    # One load per distinct gun slot shape: the S4 nose gimbal's, then the S3 wings' (shared).
    assert [(c.args[0].name, c.args[0].size_max) for c in gun_calls] == [
        (f"{NOSE}/{GUN_PORT}", 4), (f"{LEFT_WING}/{GUN_PORT}", 3)]
    assert all(c.kwargs["category"] != "Turrets" for c in spy.await_args_list), "the stock mount is never replaced"
    assert "VariPuck" not in text and "Badger" not in text and "Tarantula" not in text
    wings = next(g for g in view.groups if g.label == "2x S3 Wing Gun")
    assert [s.entry_port_name for s in wings.slots] == [LEFT_WING, RIGHT_WING], "saved where the browser saves a gun"
    assert wings.stock["name"] == "Omnisky IX Cannon", "the stock gun inside the gimbal, from the vehicle tree"


def test_slots_of_one_shape_with_different_stock_guns_are_two_lines_but_one_lookup(tmp_path, monkeypatch):
    """Like the Gladius's nose and wings: the same slot shape, different stock guns. Each gets
    its own line (keep-stock and "was X" differ), but their parts are loaded once."""
    tree = [dict(row) for row in STOCK_TREE]
    tree[2] = dict(tree[2], ports=[{"name": GUN_PORT, "equipped_item_uuid": MANTIS["uuid"]}])  # the right wing

    async def run():
        cog, thread = await _cog(tmp_path, monkeypatch, wiki=_wiki(stock_tree=tree))
        spy = AsyncMock(side_effect=cog.candidates_for_port)
        cog.candidates_for_port = spy
        await _run_command(cog, thread)
        await _switch(_posted(thread)[1], "Budget")
        return thread, spy

    thread, spy = asyncio.run(run())
    _, view = _posted(thread)
    s3_loads = [c for c in spy.await_args_list if c.kwargs["category"] == "Guns" and c.args[0].size_max == 3]
    assert len(s3_loads) == 1
    text = view.text()
    left = next(line for line in text.splitlines() if line.startswith("**S3 Left Wing Gun**"))
    right = next(line for line in text.splitlines() if line.startswith("**S3 Right Wing Gun**"))
    assert left.startswith("**S3 Left Wing Gun** → **Mantis GT-220 Gatling** · 853 DPS (was 547 DPS stock)")
    # An equal Mantis isn't an upgrade, so Budget's best-value gun that beats it is the M5A.
    assert right.startswith("**S3 Right Wing Gun** → **M5A Cannon** · 930 DPS (was 853 DPS stock)")


def test_a_fixed_gun_is_replaced_by_a_gun_of_the_slots_own_size(tmp_path, monkeypatch):
    m3a = _gun("M3A Cannon", 304, size=3, uuid="m3a")
    ports = [{"name": "hardpoint_weapon", "port_type": "WeaponGun", "size_min": 3, "size_max": 3, "accepts_guns": True,
              "equipped_uuid": "m3a"}]

    async def run():
        wiki = _wiki(stock_tree=[{"name": "hardpoint_weapon", "equipped_item_uuid": "m3a", "ports": None}], extra=[m3a])
        cog, thread = await _cog(tmp_path, monkeypatch, wiki=wiki, ports=ports)
        await _run_command(cog, thread)
        return thread

    text, view = _posted(asyncio.run(run()))
    assert [s.entry_port_name for g in view.groups for s in g.slots] == ["hardpoint_weapon"]
    assert "→ **M5A Cannon** · 930 DPS (was 304 DPS stock)" in text


# -- switching profile -------------------------------------------------------------------------

def test_every_profile_switch_repicks_from_the_loaded_parts_without_new_lookups(tmp_path, monkeypatch):
    async def run():
        cog, thread = await _cog(tmp_path, monkeypatch)
        await _run_command(cog, thread)
        _, view = _posted(thread)
        lookups = (cog.bot.uex.get_item_catalog.await_count, cog._wiki.get_item_detail.await_count)
        texts = {}
        for profile in ("Stealth", "Tank", "Budget", "Balanced"):
            click = await _switch(view, profile)
            texts[profile] = click.response.edit_message.await_args.kwargs["content"]
            assert click.response.edit_message.await_args.kwargs["view"] is view
            current = [c for c in view.children if isinstance(c, _ProfileButton) and c.disabled]
            assert [c.profile for c in current] == [profile], "only the profile showing is pressed in"
        after = (cog.bot.uex.get_item_catalog.await_count, cog._wiki.get_item_detail.await_count)
        return texts, lookups, after

    texts, before, after = asyncio.run(run())
    assert before == after, "switching only re-picks: no UEX or wiki calls"

    stealth = texts["Stealth"]
    assert "**2x S1 Cooler** → **HeatSafe** · EM 1,490 / IR 2,330 (was EM 1,490 / IR 7,260 stock)" in stealth
    assert "**S1 Shield Generator Left** → **Cloak** · EM 250 (was EM 1,490 stock)" in stealth
    assert _line(stealth, "S1 Power Plant").endswith(f"keep stock **Endurance** (EM 7,430) - {STOCK_IS_BEST}")
    assert "**2x S3 Wing Gun** → **M5A Cannon** · 930 DPS" in stealth, "guns stay by DPS"

    tank = texts["Tank"]
    assert "**S1 Power Plant** → **JS-300** · 400 component HP (was 270 component HP stock)" in tank
    assert "**2x S1 Cooler** → **Glacier** · 250 component HP (was 180 component HP stock)" in tank
    assert "**S1 Shield Generator Left** → **5SA 'Rhada'** · 3,000 shield HP" in tank, "shields by HP"
    assert "**2x S3 Wing Gun** → **M5A Cannon**" in tank

    budget = texts["Budget"]
    assert "**2x S3 Wing Gun** → **Mantis GT-220 Gatling** · 853 DPS (was 547 DPS stock) · 24,045 aUEC each" in budget
    assert _line(budget, "S4 Nose Gun").endswith(f"keep stock **Revenant Gatling** (1,266 DPS) - {NOTHING_BEATS_STOCK}")
    assert "**S1 Power Plant** → **SunFlare**" in budget, "the cheaper JS-300 doesn't beat stock"
    assert "**Total: 223,590 aUEC** for 6 parts" in budget  # 2x 24,045 + 60,500 + 2x 50,000 + 15,000

    assert "· **Balanced**" in texts["Balanced"] and "**Total: 313,774 aUEC**" in texts["Balanced"]


def test_only_the_player_who_opened_the_loadout_can_use_it(tmp_path, monkeypatch):
    async def run():
        cog, thread = await _cog(tmp_path, monkeypatch)
        await _run_command(cog, thread)
        _, view = _posted(thread)
        click = await _switch(view, "Tank", user_id=2)
        return view, click

    view, click = asyncio.run(run())
    assert view.profile == "Balanced"
    click.response.edit_message.assert_not_awaited()
    assert "/ship-loadout" in click.response.send_message.await_args.args[0]


# -- power ---------------------------------------------------------------------------------------

def test_the_power_line_is_each_profiles_total_pips_never_a_draw_warning(tmp_path, monkeypatch):
    async def run():
        cog, thread = await _cog(tmp_path, monkeypatch)
        await _run_command(cog, thread)
        text, view = _posted(thread)
        stealth = (await _switch(view, "Stealth")).response.edit_message.await_args.kwargs["content"]
        tank = (await _switch(view, "Tank")).response.edit_message.await_args.kwargs["content"]
        return text, stealth, tank

    balanced, stealth, tank = asyncio.run(run())
    # The parts could take 22.1 pips in Balanced and Tank: never compared, nothing is meant to run at max.
    assert "⚡ **16** power pips in total, from the power plant." in balanced
    assert "⚡ **12** power pips in total, from the power plant." in tank
    assert "⚡ **15** power pips in total, from the power plant." in stealth, "Stealth keeps the stock plant"
    for text in (balanced, stealth, tank):
        assert "full load" not in text and "You assign power" not in text


# -- done --------------------------------------------------------------------------------------

def _done_click(cog, view, *, user_id=1, delete=None, message_id=None):
    click = _interaction(None, user_id=user_id)
    click.client = NS(get_cog=lambda name: cog if name == "ShipPartsFinder" else None)
    click.message = NS(id=view.message.id if message_id is None else message_id, delete=delete or AsyncMock())
    click.response.type = discord.InteractionResponseType.deferred_message_update
    return click


async def _press_done(view, click):
    """A click on Done as discord.py dispatches it: through the handler registered at startup."""
    stub = next(child for child in view.children if isinstance(child, _LoadoutDoneStub))
    match = LoadoutDoneButton.__discord_ui_compiled_template__.fullmatch(stub.custom_id)
    await (await LoadoutDoneButton.from_custom_id(click, stub, match)).callback(click)


def _editable(message):
    """The fake thread's message lacks what a timeout's edit reads off a real one."""
    message.flags = NS(ephemeral=False)
    message.channel = NS(get_partial_message=lambda _id: NS(edit=AsyncMock()))
    return message


def test_done_removes_the_loadout_message_and_stops_its_view(tmp_path, monkeypatch):
    async def run():
        cog, thread = await _cog(tmp_path, monkeypatch)
        await _run_command(cog, thread)
        _, view = _posted(thread)
        assert cog._loadouts == {view.message.id: view}
        click = _done_click(cog, view)
        await _press_done(view, click)
        return cog, view, click

    cog, view, click = asyncio.run(run())
    click.message.delete.assert_awaited_once()
    assert view.is_finished(), "stopped, so the idle timeout never edits a deleted message"
    assert cog._loadouts == {}


def test_only_the_player_who_opened_the_loadout_can_remove_it(tmp_path, monkeypatch):
    async def run():
        cog, thread = await _cog(tmp_path, monkeypatch)
        await _run_command(cog, thread)
        _, view = _posted(thread)
        click = _done_click(cog, view, user_id=2)
        await _press_done(view, click)
        return view, click

    view, click = asyncio.run(run())
    click.message.delete.assert_not_awaited()
    assert not view.is_finished()
    assert "/ship-loadout" in click.response.send_message.await_args.args[0]


def test_done_still_works_once_the_loadout_has_gone_idle(tmp_path, monkeypatch):
    async def run():
        cog, thread = await _cog(tmp_path, monkeypatch)
        await _run_command(cog, thread)
        _, view = _posted(thread)
        _editable(view.message)
        await view.on_timeout()
        after_idle = dict(cog._loadouts)
        click = _done_click(cog, view)
        await _press_done(view, click)
        return after_idle, view, click

    after_idle, view, click = asyncio.run(run())
    done = next(child for child in view.children if isinstance(child, _LoadoutDoneStub))
    assert not done.disabled and all(child.disabled for child in view.children if child is not done)
    assert "**Done** still removes this message." in view.message.edit.await_args.kwargs["content"]
    assert after_idle == {}, "an idle view is forgotten as it closes"
    click.message.delete.assert_awaited_once()


def test_done_works_on_a_loadout_posted_before_a_restart(tmp_path, monkeypatch):
    async def run():
        cog, thread = await _cog(tmp_path, monkeypatch)
        await _run_command(cog, thread)
        _, view = _posted(thread)
        restarted = ShipPartsFinder(cog.bot, wiki_client=cog._wiki, start_refresh=False)  # no live views
        click = _done_click(restarted, view)
        await _press_done(view, click)
        return click

    asyncio.run(run()).message.delete.assert_awaited_once()


def test_a_message_discord_wont_delete_greys_out_but_keeps_done(tmp_path, monkeypatch):
    async def run():
        cog, thread = await _cog(tmp_path, monkeypatch)
        await _run_command(cog, thread)
        _, view = _posted(thread)
        _editable(view.message)
        refused = AsyncMock(side_effect=discord.HTTPException(NS(status=403, reason="Forbidden"), "no"))
        click = _done_click(cog, view, delete=refused)
        await _press_done(view, click)
        return view, click

    view, click = asyncio.run(run())
    done = next(child for child in view.children if isinstance(child, _LoadoutDoneStub))
    assert not done.disabled and all(child.disabled for child in view.children if child is not done)
    view.message.edit.assert_awaited_once()
    assert "try **Done** again" in click.followup.send.await_args.args[0]


def test_done_is_registered_once_and_never_dispatched_by_the_view():
    custom_id = loadout_done_custom_id(2**63)
    assert len(custom_id) <= MAX_CUSTOM_ID_CHARS
    match = LoadoutDoneButton.__discord_ui_compiled_template__.fullmatch(custom_id)
    assert asyncio.run(LoadoutDoneButton.from_custom_id(NS(), NS(), match)).owner_id == 2**63
    assert not _LoadoutDoneStub(1).is_dispatchable()


# -- scatterguns -------------------------------------------------------------------------------

def test_a_scattergun_for_sale_is_never_recommended_in_any_profile(tmp_path, monkeypatch):
    """Sold cheapest, with the most DPS: every profile, Budget included, still skips it."""
    scatter = _gun("Dominance-3 Scattergun", 5000, uuid="dominance", alpha=1116, kind="Laser Scattergun")
    monkeypatch.setitem(globals(), "SOLD", [*SOLD, (13, scatter, "Guns", 100, 12)])

    async def run():
        cog, thread = await _cog(tmp_path, monkeypatch)
        await _run_command(cog, thread)
        text, view = _posted(thread)
        texts = [text] + [(await _switch(view, p)).response.edit_message.await_args.kwargs["content"]
                          for p in ("Stealth", "Tank", "Budget")]
        return texts, view

    texts, view = asyncio.run(run())
    for text in texts:
        assert "Scattergun" not in text
    assert "**2x S3 Wing Gun** → **M5A Cannon** · 930 DPS (was 547 DPS stock)" in texts[0]
    assert all("dominance" not in str(part.get("uuid")) for _, part in purchases(view.picks))


# -- add all to the shopping list ----------------------------------------------------------------

def test_add_all_saves_every_purchase_and_no_kept_stock_line(tmp_path, monkeypatch):
    async def run():
        cog, thread = await _cog(tmp_path, monkeypatch)
        await _run_command(cog, thread)
        _, view = _posted(thread)
        await _switch(view, "Budget")
        click = _interaction(thread)
        assert await view.interaction_check(click)
        await view.add_all_button.callback(click)
        return cog, click

    cog, click = asyncio.run(run())
    entries = asyncio.run(cog.bot.db.get_ship_parts_entries(1, 10))
    saved = {(e["category"], e["port_name"]): e["item_name"] for e in entries}
    assert saved == {
        ("Guns", LEFT_WING): "Mantis GT-220 Gatling", ("Guns", RIGHT_WING): "Mantis GT-220 Gatling",
        ("Power Plants", "hardpoint_power_plant"): "SunFlare",
        ("Coolers", "hardpoint_cooler_left"): "Glacier", ("Coolers", "hardpoint_cooler_right"): "Glacier",
        ("Shield Generators", "hardpoint_shield_generator_left"): "5SA 'Rhada'",
    }, "the kept nose gun and the rack (nothing sold) are not on the list"
    mantis = next(e for e in entries if e["item_name"] == "Mantis GT-220 Gatling")
    assert (mantis["id_item"], mantis["price_buy"], mantis["id_vehicle"]) == (1, 24045.0, 1)
    assert (mantis["id_terminal"], mantis["terminal_name"]) == (11, "Shop 11"), "each part keeps its shop"
    click.response.defer.assert_awaited_once_with(ephemeral=True)
    reply = click.followup.send.await_args
    assert reply.args[0].startswith("Added 6 parts for **Avenger Titan**") and reply.kwargs["ephemeral"] is True
    assert click.followup.send.await_count == 1, "one reply, not one per part"


def test_add_all_is_off_when_every_slot_keeps_what_it_has(tmp_path, monkeypatch):
    async def run():
        uex = _uex()
        uex.get_items_prices_all = AsyncMock(return_value=[])  # nothing for sale anywhere
        cog, thread = await _cog(tmp_path, monkeypatch, uex=uex)
        await _run_command(cog, thread)
        _, view = _posted(thread)
        click = _interaction(thread)
        await view.add_all(click)
        return thread, view, click

    thread, view, click = asyncio.run(run())
    text, _ = _posted(thread)
    assert view.add_all_button.disabled and "**Nothing to buy**" in text
    assert "Nothing to add" in click.followup.send.await_args.args[0]


def test_add_all_says_how_many_parts_couldnt_be_saved(tmp_path, monkeypatch):
    async def run():
        cog, thread = await _cog(tmp_path, monkeypatch)
        await _run_command(cog, thread)
        _, view = _posted(thread)
        real = cog.bot.db.set_ship_parts_entry
        calls = []

        async def flaky(*args):
            calls.append(args)
            if len(calls) == 2:
                raise RuntimeError("database is locked")
            await real(*args)

        cog.bot.db.set_ship_parts_entry = flaky
        click = _interaction(thread)
        await view.add_all(click)
        return click

    message = asyncio.run(run()).followup.send.await_args.args[0]
    assert message.startswith("Added 5 parts") and "1 couldn't be saved" in message


# -- shops and location --------------------------------------------------------------------------

def test_a_location_sends_ties_to_the_nearest_shop_and_shows_distances(tmp_path, monkeypatch):
    async def run():
        cog, thread = await _cog(tmp_path, monkeypatch)
        await _run_command(cog, thread, location="Area 18 TDD")
        return cog, thread

    cog, thread = asyncio.run(run())
    text, view = _posted(thread)
    # The Rhada and the Rhada Twin hold 3,000 HP each: the Twin's shop is 5 Gm away, the Rhada's 40.
    assert "**S1 Shield Generator Left** → **Rhada Twin** · 3,000 shield HP (was 2,160 shield HP stock) · 20,000 aUEC · " \
           "Shop 12 · 5.0 Gm" in text
    assert "ties go to the shop nearest **Area 18 TDD**" in text
    assert view.origin_terminal == (99, "Area 18 TDD")
    origins = {c.args[0] for c in cog.bot.uex.get_terminal_distance.await_args_list}
    assert origins == {99}


def test_an_unknown_location_is_said_before_any_lookup(tmp_path, monkeypatch):
    async def run():
        cog, thread = await _cog(tmp_path, monkeypatch)
        cog.bot.db.resolve_terminal_id_by_name = AsyncMock(return_value=None)
        interaction = await _run_command(cog, thread, location="Nowhere")
        return cog, thread, interaction

    cog, thread, interaction = asyncio.run(run())
    assert "Nowhere" in interaction.followup.send.await_args.args[0]
    cog.bot.uex.get_vehicles.assert_not_awaited()
    thread.send.assert_not_awaited()


# -- what the player is told when something is missing -------------------------------------------

def test_a_ship_that_matches_nothing_is_said(tmp_path, monkeypatch):
    async def run():
        cog, thread = await _cog(tmp_path, monkeypatch)
        return await _run_command(cog, thread, ship="NotAShip"), thread

    interaction, thread = asyncio.run(run())
    assert "NotAShip" in interaction.followup.send.await_args.args[0]
    thread.send.assert_not_awaited()


def test_uex_down_is_said_instead_of_hanging(tmp_path, monkeypatch):
    async def run():
        uex = _uex()
        uex.get_vehicles = AsyncMock(side_effect=ship_parts_finder.UexApiError("down"))
        cog, thread = await _cog(tmp_path, monkeypatch, uex=uex)
        return await _run_command(cog, thread), thread

    interaction, thread = asyncio.run(run())
    interaction.followup.send.assert_awaited_once()
    thread.send.assert_not_awaited()


def test_a_ship_with_no_supported_slots_is_said(tmp_path, monkeypatch):
    async def run():
        cog, thread = await _cog(tmp_path, monkeypatch, ports=[])
        cog._wiki.get_vehicle_loadout = AsyncMock(return_value=([], []))
        return await _run_command(cog, thread), thread

    interaction, thread = asyncio.run(run())
    assert interaction.followup.send.await_args.args[0] == "No supported component slots found for **Avenger Titan** yet."
    thread.send.assert_not_awaited()


def test_a_ship_the_wiki_lists_twice_is_warned_about_not_shown_as_slotless(tmp_path, monkeypatch):
    """A name the wiki uses for several ships with no plain one among them (the PYAM Exec
    pairs; a ship with editions, like the Cutlass Black, resolves - base_vehicle_row): its
    slots can't be told apart. Said as a known issue, not "no slots"."""
    async def run():
        cog, thread = await _cog(tmp_path, monkeypatch, ports=[])
        cog._wiki.get_vehicle_loadout = AsyncMock(side_effect=WikiDuplicateNameError("Avenger Titan", 2))
        return await _run_command(cog, thread), thread

    interaction, thread = asyncio.run(run())
    assert interaction.followup.send.await_args.args[0] == (
        "⚠️ The Star Citizen Wiki lists more than one ship named **Avenger Titan**, so its component slots "
        "can't be told apart yet. Known issue: parts and loadouts for this ship aren't available for now.")
    thread.send.assert_not_awaited()


def test_the_wiki_down_for_the_ships_slots_is_said(tmp_path, monkeypatch):
    async def run():
        cog, thread = await _cog(tmp_path, monkeypatch, ports=[])
        cog._wiki.get_vehicle_loadout = AsyncMock(side_effect=WikiUnavailableError("down"))
        return await _run_command(cog, thread)

    message = asyncio.run(run()).followup.send.await_args.args[0]
    assert "Couldn't reach the Star Citizen Wiki" in message and "try again" in message


def test_parts_the_wiki_didnt_answer_for_are_said_not_shown_as_missing_stats(tmp_path, monkeypatch):
    async def run():
        wiki = _wiki(unavailable={"rhada", "rhada-twin", "cloak"})
        cog, thread = await _cog(tmp_path, monkeypatch, wiki=wiki)
        await _run_command(cog, thread)
        return thread

    text, _ = _posted(asyncio.run(run()))
    assert ("⚠️ The Star Citizen Wiki didn't respond for 3 parts: they may be missing, so a better pick may exist. "
            "Try again in a few minutes.") in text
    assert _line(text, "S1 Shield Generator Left").endswith(f"keep stock **Bulwark** (2,160 shield HP) - {WIKI_SILENT_FOR_SLOT}")


def test_without_the_vehicle_tree_the_stock_gun_is_unknown_not_an_empty_slot(tmp_path, monkeypatch):
    async def run():
        cog, thread = await _cog(tmp_path, monkeypatch, wiki=_wiki(stock_tree=WikiUnavailableError("down")))
        await _run_command(cog, thread)
        text, view = _posted(thread)
        budget = (await _switch(view, "Budget")).response.edit_message.await_args.kwargs["content"]
        return text, budget

    text, budget = asyncio.run(run())
    assert "→ **M5A Cannon** · 930 DPS (stock part unknown)" in text
    assert "didn't respond for 3 stock parts: they can't be compared against stock" in text
    assert _line(budget, "2x S3 Wing Gun").endswith(f"keep stock - {STOCK_UNKNOWN}"), \
        "Budget never buys what it can't call an upgrade"


def test_a_gun_slot_whose_mount_the_wiki_didnt_answer_for_is_left_out_and_said(tmp_path, monkeypatch):
    async def run():
        cog, thread = await _cog(tmp_path, monkeypatch, wiki=_wiki(unavailable={VARIPUCK_S3["uuid"]}))
        await _run_command(cog, thread)
        return thread

    text, view = _posted(asyncio.run(run()))
    assert "Wing Gun" not in text and "**S4 Nose Gun**" in text
    assert "didn't respond for 2 gun slots: they are left out, since the mount decides the gun's size" in text


def test_every_slots_lookups_share_one_deadline(tmp_path, monkeypatch):
    """A hanging wiki can't hold the loadout for each slot's own 45 seconds in turn: past the
    shared deadline nothing new starts, and the parts not answered are said."""
    async def run():
        wiki = _wiki()
        answer = wiki.get_item_detail.side_effect

        async def slow_for_sold_parts(uuid):
            if uuid in {detail["uuid"] for _, detail, *_ in SOLD}:
                await asyncio.sleep(30)
            return await answer(uuid)

        wiki.get_item_detail.side_effect = slow_for_sold_parts
        monkeypatch.setattr(ship_parts_finder, "LOADOUT_TIME_BUDGET_SECONDS", 0.3)
        cog, thread = await _cog(tmp_path, monkeypatch, wiki=wiki)
        started = asyncio.get_running_loop().time()
        await _run_command(cog, thread)
        return thread, asyncio.get_running_loop().time() - started

    thread, elapsed = asyncio.run(run())
    text, _ = _posted(thread)
    assert elapsed < 5, f"took {elapsed:.1f}s"
    assert "⚠️ The Star Citizen Wiki didn't respond for 11 parts" in text
    assert _line(text, "S4 Nose Gun").endswith(WIKI_SILENT_FOR_SLOT)


def test_the_vehicle_tree_is_looked_up_once_per_ship(tmp_path, monkeypatch):
    async def run():
        cog, thread = await _cog(tmp_path, monkeypatch)
        await _run_command(cog, thread)
        await _run_command(cog, thread)
        return cog

    assert asyncio.run(run())._wiki.get_vehicle_stock_ports.await_count == 1


# -- the parts browser's button ----------------------------------------------------------------

def test_the_browsers_button_opens_a_loadout_for_the_ship_and_location_being_browsed(tmp_path, monkeypatch):
    async def run():
        cog, thread = await _cog(tmp_path, monkeypatch)
        grouped = {"Power Plants": [ShipPort(name="hardpoint_power_plant", port_type="PowerPlant", size_min=1,
                                             size_max=1)]}
        # A browser rebuilt by Refresh knows only its location's id.
        browser = PartsBrowserView(cog, VEHICLE, (99, ""), grouped)
        cog.bot.db.get_terminal_references_by_ids = AsyncMock(return_value={99: {"terminal_name": "Area 18 TDD"}})
        button = browser.loadout_button
        click = _interaction(thread)
        await button.callback(click)
        return thread, button, click

    thread, button, click = asyncio.run(run())
    assert button.row == 3 and button.label == "Recommend a loadout"
    click.response.defer.assert_awaited_once_with(ephemeral=True, thinking=True)
    text, view = _posted(thread)
    assert view.profile == "Balanced" and view.origin_terminal == (99, "Area 18 TDD") and view.owner_id == 1
    assert "Shop 12 · 5.0 Gm" in text, "the browser's location ranks the shops"
    assert thread.mention in click.followup.send.await_args.args[0]


def test_the_browsers_button_stays_clear_of_its_menus_and_refresh():
    view = PartsBrowserView(NS(), VEHICLE, (5, "Omega Pro"), {"Radar": [ShipPort("hp", "Radar", 1, 1)]})
    assert isinstance(view.children[-1], ship_parts_finder._RefreshStub), "Refresh is still the last button"
    rows = {c.row for c in view.children if isinstance(c, discord.ui.Select)}
    assert 3 not in rows


# -- big ships and going idle -------------------------------------------------------------------

def _many_groups(count):
    slots = [LoadoutSlot(ShipPort(f"hardpoint_shield_generator_number_{i}_with_a_long_name", "Shield", i, i),
                         "Shield Generators", f"hardpoint_shield_generator_number_{i}_with_a_long_name", BULWARK)
             for i in range(1, count + 1)]
    return group_slots(slots)


def test_a_big_ship_pages_its_lines_inside_discords_limit():
    async def run():
        groups = _many_groups(30)
        candidates = {g.fit_key: [dict(RHADA, size=g.port.size_min, _price_buy=15000.0, _uex_id=9,
                                       _terminal_name="Platinum Bay - HUR-L3", _id_terminal=11)] for g in groups}
        view = LoadoutView(NS(), VEHICLE, None, groups, candidates, owner_id=1)
        texts, edges = [], []
        for page in range(len(view.pages)):
            texts.append(view.text())
            edges.append({c.delta: c.disabled for c in view.children if isinstance(c, _LoadoutPageButton)})
            if page < len(view.pages) - 1:
                click = _interaction(None)
                await next(c for c in view.children if isinstance(c, _LoadoutPageButton) and c.delta > 0).callback(click)
        view.expired = True
        expired = view.text()
        return view, texts, edges, expired

    view, texts, edges, expired = asyncio.run(run())
    assert len(view.pages) > 1
    assert edges[0] == {-1: True, 1: False} and edges[-1] == {-1: False, 1: True}, "no turning past either end"
    assert all(len(text) <= MESSAGE_LIMIT for text in texts) and len(expired) <= MESSAGE_LIMIT
    shown = [line for text in texts for line in text.splitlines() if "Shield Generator Number" in line]
    assert len(shown) == 30, "every line on some page, none cut"
    assert f"Page {len(view.pages)} of {len(view.pages)}" in texts[-1]
    assert all("**Total: 450,000 aUEC** for 30 parts" in text for text in texts)


def test_an_idle_loadout_greys_out_and_says_how_to_get_it_back():
    async def run():
        groups = _many_groups(2)
        view = LoadoutView(NS(), VEHICLE, None, groups, {}, owner_id=1)
        view.message = NS(id=7, edit=AsyncMock(), flags=NS(ephemeral=False), channel=NS(get_partial_message=lambda _id: NS(edit=AsyncMock())))
        await view.on_timeout()
        return view

    view = asyncio.run(run())
    assert all(child.disabled for child in view.children if not isinstance(child, _LoadoutDoneStub))
    assert not next(child for child in view.children if isinstance(child, _LoadoutDoneStub)).disabled
    assert LOADOUT_EXPIRED_NOTE in view.message.edit.await_args.kwargs["content"]


def test_a_loadout_with_a_location_still_greys_out_when_idle():
    """The location is the view's own `origin_terminal`, never BotView's `origin` (the interaction grey_out edits
    through): stored as `origin`, a (terminal id, name) tuple made grey_out raise AttributeError and the idle
    loadout kept its live-looking buttons. Found porting the loadout to the AI bot (2026-10-02); the test above only
    covered a loadout with no location."""
    async def run():
        view = LoadoutView(NS(), VEHICLE, (99, "Area 18 TDD"), _many_groups(2), {}, owner_id=1)
        view.message = NS(id=7, edit=AsyncMock(), flags=NS(ephemeral=False), channel=NS(get_partial_message=lambda _id: NS(edit=AsyncMock())))
        await view.on_timeout()
        return view

    view = asyncio.run(run())
    assert view.origin is None and view.origin_terminal == (99, "Area 18 TDD")
    assert all(child.disabled for child in view.children if not isinstance(child, _LoadoutDoneStub))
    content = view.message.edit.await_args.kwargs["content"]
    assert LOADOUT_EXPIRED_NOTE in content and "nearest **Area 18 TDD**" in content


def test_ship_loadout_is_listed_with_the_ship_commands_in_intro():
    ships = next(names for title, _, names in CATEGORIES if "Ships" in title)
    assert ships.index("ship-loadout") == ships.index("ship-parts-finder") + 1


# -- review fixes (2026-10-01) -----------------------------------------------------------------

REMOTE_TURRET = {"name": "hardpoint_turret_remote_top", "port_type": "Turret", "size_min": 3, "size_max": 3,
                 "editable": False, "equipped_uuid": "remote-turret"}
# A manned turret the player can swap, whose own two S3 gun slots hold the guns (like the
# Perseus's remote turrets, but editable): the browser offers the turret and its guns apart.
MANNED_TURRET = {"uuid": "manned-turret", "name": "Manned Turret", "type": "Turret", "sub_type": "MannedTurret",
                 "size": 3, "tags": [], "required_tags": [], "durability": {"health": 3000}, "resource_network": None,
                 "ports": [{"name": f"hardpoint_gun_{side}", "type": "WeaponGun", "sizes": {"min": 3, "max": 3},
                            "editable": True, "compatible_types": [{"type": "WeaponGun"}]}
                           for side in ("left", "right")]}
TURRET_PORTS = [{"name": "hardpoint_turret", "port_type": "Turret", "size_min": 3, "size_max": 3,
                 "equipped_uuid": "manned-turret"}]
TURRET_TREE = [{"name": "hardpoint_turret", "equipped_item_uuid": "manned-turret",
                "ports": [{"name": f"hardpoint_gun_{side}", "equipped_item_uuid": OMNISKY["uuid"]}
                          for side in ("left", "right")]}]


# A manned turret the game locks, gimbals and all (the Idris-M's): the finder lists none of it
# ('TurretBase'), and only the vehicle tree shows the unlocked gun inside each gimbal.
LOCKED_MANNED_TURRET = {
    "name": "hardpoint_turret_manned", "type": "TurretBase", "editable": False, "sizes": {"min": 3, "max": 3},
    "equipped_item": {"name": "Manned Turret", "sub_type": "MannedTurret"},
    "ports": [{"name": f"hardpoint_weapon_{side}", "type": "Turret", "editable": False, "sizes": {"min": 3, "max": 3},
               "equipped_item_uuid": VARIPUCK_S3["uuid"],
               "ports": [{"name": GUN_PORT, "type": "WeaponGun", "editable": True, "sizes": {"min": 3, "max": 3},
                          "equipped_item_uuid": OMNISKY["uuid"]}]}
              for side in ("left", "right")],
}


def test_guns_inside_a_locked_turret_are_recommended(tmp_path, monkeypatch):
    async def run():
        cog, thread = await _cog(tmp_path, monkeypatch, wiki=_wiki(stock_tree=[*STOCK_TREE, LOCKED_MANNED_TURRET]))
        await _run_command(cog, thread)
        return _posted(thread)

    text, view = asyncio.run(run())
    assert "**2x S3 Turret Manned Gun** → **M5A Cannon** · 930 DPS (was 547 DPS stock)" in text
    (group,) = [g for g in view.groups if g.port.name.startswith("hardpoint_turret_manned/")]
    assert [s.entry_port_name for s in group.slots] == [
        f"hardpoint_turret_manned/hardpoint_weapon_{side}/{GUN_PORT}" for side in ("left", "right")]


def test_a_turret_whose_guns_the_loadout_already_has_is_not_added_twice(tmp_path, monkeypatch):
    """The browser's own turret (its gun slots from child_gun_ports): typed in the tree like
    the Idris-M's, it still gives two gun slots, not four."""
    tree = [dict(TURRET_TREE[0], type="Turret", editable=True, sizes={"min": 3, "max": 3},
                 ports=[dict(row, type="WeaponGun", editable=True, sizes={"min": 3, "max": 3})
                        for row in TURRET_TREE[0]["ports"]])]

    async def run():
        cog, thread = await _cog(tmp_path, monkeypatch, wiki=_wiki(stock_tree=tree, extra=[MANNED_TURRET]),
                                 ports=TURRET_PORTS)
        await _run_command(cog, thread)
        return _posted(thread)

    text, view = asyncio.run(run())
    assert sum(g.count for g in view.groups if g.category == "Guns") == 2


def test_a_turret_the_wiki_didnt_answer_for_is_said(tmp_path, monkeypatch):
    """Its gun slots never exist (ShipPartsFinder._with_child_gun_ports), so without a note
    the loadout - its total included - would read as complete. The power line is the plants'
    output, which a left-out gun doesn't change."""
    async def run():
        cog, thread = await _cog(tmp_path, monkeypatch, wiki=_wiki(unavailable={"remote-turret"}),
                                 ports=[*PORTS, REMOTE_TURRET])
        await _run_command(cog, thread)
        return thread

    text, view = _posted(asyncio.run(run()))
    assert ("⚠️ The Star Citizen Wiki didn't respond for 1 turret: its guns are left out. "
            "Try again in a few minutes.") in text
    assert "⚡ **16** power pips in total, from the power plant." in text


def test_a_gun_slot_the_vehicle_tree_doesnt_list_is_stock_unknown_not_empty(tmp_path, monkeypatch):
    """The left wing's mount row came back with no ports: what's in its gun slot isn't known,
    so Budget mustn't lose its beats-stock guard. The right wing's row lists the gun slot
    empty: that one really is empty."""
    tree = [dict(row) for row in STOCK_TREE]
    tree[1] = dict(tree[1], ports=None)
    tree[2] = dict(tree[2], ports=[{"name": GUN_PORT, "equipped_item": None}])

    async def run():
        cog, thread = await _cog(tmp_path, monkeypatch, wiki=_wiki(stock_tree=tree))
        await _run_command(cog, thread)
        text, view = _posted(thread)
        budget = (await _switch(view, "Budget")).response.edit_message.await_args.kwargs["content"]
        return text, budget

    text, budget = asyncio.run(run())
    assert _line(text, "S3 Left Wing Gun").startswith(
        "**S3 Left Wing Gun** → **M5A Cannon** · 930 DPS (stock part unknown)")
    assert _line(text, "S3 Right Wing Gun").startswith(
        "**S3 Right Wing Gun** → **M5A Cannon** · 930 DPS · 69,137 aUEC")
    assert "didn't respond" not in text, "a gap in the wiki's data, not an outage to retry"
    assert _line(budget, "S3 Left Wing Gun").endswith(f"keep stock - {STOCK_UNKNOWN}")


def test_a_mount_inside_a_stock_mount_is_kept_down_to_the_gun_slot(tmp_path, monkeypatch):
    """A gun hardpoint holding a turret that holds a gimbal: the gun is compared with the
    stock gun in the gimbal, never with the gimbal's own mount rank ('holds 1x S3')."""
    twin = {"uuid": "twin-turret", "name": "Twin Turret", "type": "Turret", "sub_type": "GunTurret", "size": 4,
            "tags": [], "required_tags": [], "resource_network": None,
            "ports": [{"name": "hardpoint_gimbal", "type": "Turret", "sizes": {"min": 3, "max": 3}, "editable": True,
                       "compatible_types": [{"type": "WeaponGun"}, {"type": "Turret"}]}]}
    ports = [{"name": NOSE, "port_type": "Turret", "size_min": 4, "size_max": 4, "accepts_guns": True,
              "equipped_uuid": "twin-turret"}]
    tree = [{"name": NOSE, "equipped_item_uuid": "twin-turret",
             "ports": [{"name": "hardpoint_gimbal", "equipped_item_uuid": VARIPUCK_S3["uuid"],
                        "ports": [{"name": GUN_PORT, "equipped_item_uuid": OMNISKY["uuid"]}]}]}]

    async def run():
        cog, thread = await _cog(tmp_path, monkeypatch, wiki=_wiki(stock_tree=tree, extra=[twin]), ports=ports)
        await _run_command(cog, thread)
        return thread

    text, view = _posted(asyncio.run(run()))
    (group,) = view.groups
    assert group.port.name == f"{NOSE}/hardpoint_gimbal/{GUN_PORT}" and group.stock["name"] == "Omnisky IX Cannon"
    assert [s.entry_port_name for s in group.slots] == [NOSE], "saved where the browser saves the hardpoint's gun"
    assert "→ **M5A Cannon** · 930 DPS (was 547 DPS stock)" in text
    assert "holds" not in text and "VariPuck" not in text


def test_a_mount_still_found_past_the_depth_limit_is_never_compared_as_a_gun(tmp_path, monkeypatch):
    """Gimbals nested three deep, with the search limited to one level: the slot left at the
    limit still holds a mount, so its stock part is unknown - its mount rank ('holds 1x S3')
    is never set against a gun's DPS."""
    ports = [{"name": NOSE, "port_type": "Turret", "size_min": 3, "size_max": 3, "accepts_guns": True,
              "equipped_uuid": VARIPUCK_S3["uuid"]}]
    tree = [{"name": NOSE, "equipped_item_uuid": VARIPUCK_S3["uuid"],
             "ports": [{"name": GUN_PORT, "equipped_item_uuid": VARIPUCK_S3["uuid"],
                        "ports": [{"name": GUN_PORT, "equipped_item_uuid": VARIPUCK_S3["uuid"]}]}]}]
    monkeypatch.setattr(ship_parts_finder, "MAX_MOUNT_DEPTH", 1)

    async def run():
        cog, thread = await _cog(tmp_path, monkeypatch, wiki=_wiki(stock_tree=tree), ports=ports)
        await _run_command(cog, thread)
        return thread

    text, _ = _posted(asyncio.run(run()))
    assert "→ **M5A Cannon** · 930 DPS (stock part unknown)" in text
    assert "holds" not in text

def test_a_turret_whose_guns_are_in_the_loadout_keeps_the_stock_turret(tmp_path, monkeypatch):
    """Recommending a new turret beside guns sized for the stock one would contradict itself,
    and "Add all" would save both: the turret is kept, like a gimbal."""
    async def run():
        wiki = _wiki(stock_tree=TURRET_TREE, extra=[MANNED_TURRET])
        cog, thread = await _cog(tmp_path, monkeypatch, wiki=wiki, ports=TURRET_PORTS)
        await _run_command(cog, thread)
        _, view = _posted(thread)
        click = _interaction(thread)
        await view.add_all(click)
        return cog, thread

    cog, thread = asyncio.run(run())
    text, view = _posted(thread)
    assert [g.category for g in view.groups] == ["Guns"]
    assert "→ **M5A Cannon** · 930 DPS (was 547 DPS stock)" in text
    assert "VariPuck" not in text
    entries = asyncio.run(cog.bot.db.get_ship_parts_entries(1, 10))
    assert {e["category"] for e in entries} == {"Guns"}
    assert sorted(e["port_name"] for e in entries) == ["hardpoint_turret/hardpoint_gun_left",
                                                        "hardpoint_turret/hardpoint_gun_right"]


def test_a_turrets_gun_slots_without_the_vehicle_tree_are_stock_unknown(tmp_path, monkeypatch):
    """A turret's own gun slot has no stock uuid outside the vehicle tree. One that only takes
    a gun is still its own size, so it's listed - with its stock part unknown and counted -
    rather than read as an empty slot."""
    async def run():
        wiki = _wiki(stock_tree=WikiUnavailableError("down"), extra=[MANNED_TURRET])
        cog, thread = await _cog(tmp_path, monkeypatch, wiki=wiki, ports=TURRET_PORTS)
        await _run_command(cog, thread)
        return thread

    text, _ = _posted(asyncio.run(run()))
    assert "→ **M5A Cannon** · 930 DPS (stock part unknown)" in text
    assert "didn't respond for 2 stock parts: they can't be compared against stock" in text


def test_in_a_dm_the_loadout_is_refused_before_any_slow_lookup(tmp_path, monkeypatch):
    async def run():
        cog, thread = await _cog(tmp_path, monkeypatch)
        interaction = _interaction(thread, guild_id=None)
        await cog.ship_loadout.callback(cog, interaction, "Avenger Titan", None, None)
        return cog, thread, interaction

    cog, thread, interaction = asyncio.run(run())
    assert "available in a server" in interaction.followup.send.await_args.args[0]
    cog._wiki.get_vehicle_stock_ports.assert_not_awaited()
    cog.bot.uex.get_item_catalog.assert_not_awaited()
    thread.send.assert_not_awaited()


def test_a_double_click_on_add_all_saves_once(tmp_path, monkeypatch):
    async def run():
        cog, thread = await _cog(tmp_path, monkeypatch)
        await _run_command(cog, thread)
        _, view = _posted(thread)

        async def slow(*_args):
            await asyncio.sleep(0.05)
            return 6

        cog.shopping.lock_in_many = AsyncMock(side_effect=slow)
        first, second = _interaction(thread), _interaction(thread)
        await asyncio.gather(view.add_all(first), view.add_all(second))
        third = _interaction(thread)
        await view.add_all(third)
        return cog, second, third

    cog, second, third = asyncio.run(run())
    assert cog.shopping.lock_in_many.await_count == 2, "once for the double click, once for the later click"
    assert "Already adding" in second.response.send_message.await_args.args[0]
    second.response.defer.assert_not_awaited()
    third.response.send_message.assert_not_awaited()


def test_a_double_click_on_recommend_a_loadout_builds_one(tmp_path, monkeypatch):
    async def run():
        cog, thread = await _cog(tmp_path, monkeypatch)

        async def slow(*_args):
            await asyncio.sleep(0.05)

        cog.open_loadout = AsyncMock(side_effect=slow)
        browser = PartsBrowserView(cog, VEHICLE, (99, "Area 18 TDD"), {"Radar": [ShipPort("hp", "Radar", 1, 1)]})
        first, second = _interaction(thread), _interaction(thread)
        await asyncio.gather(browser.loadout_button.callback(first), browser.loadout_button.callback(second))
        await browser.loadout_button.callback(_interaction(thread))
        return cog, second

    cog, second = asyncio.run(run())
    assert cog.open_loadout.await_count == 2
    assert "Already building" in second.response.send_message.await_args.args[0]


def test_one_stock_part_the_wiki_didnt_answer_for_is_said_in_the_singular(tmp_path, monkeypatch):
    async def run():
        cog, thread = await _cog(tmp_path, monkeypatch, wiki=_wiki(unavailable={ENDURANCE["uuid"]}))
        await _run_command(cog, thread)
        text, view = _posted(thread)
        budget = (await _switch(view, "Budget")).response.edit_message.await_args.kwargs["content"]
        return text, budget

    text, budget = asyncio.run(run())
    assert ("⚠️ The Star Citizen Wiki didn't respond for 1 stock part: it can't be compared against stock. "
            "Try again in a few minutes.") in text
    assert "**S1 Power Plant** → **SunFlare** · 16 power pips (stock part unknown)" in text
    assert _line(budget, "S1 Power Plant").endswith(f"keep stock - {STOCK_UNKNOWN}")


def test_one_gun_slot_left_out_is_said_in_the_singular(tmp_path, monkeypatch):
    async def run():
        cog, thread = await _cog(tmp_path, monkeypatch, wiki=_wiki(unavailable={VARIPUCK_S4["uuid"]}))
        await _run_command(cog, thread)
        return thread

    text, _ = _posted(asyncio.run(run()))
    assert ("didn't respond for 1 gun slot: it is left out, since the mount decides the gun's size. "
            "Try again in a few minutes.") in text


def test_parts_the_wiki_answered_for_without_stats_say_so_not_that_it_didnt_respond(tmp_path, monkeypatch):
    mystery = {"uuid": "mystery-rack", "name": "Mystery Rack", "type": "MissileLauncher", "sub_type": "MissileRack",
               "size": 3, "tags": [], "required_tags": []}
    monkeypatch.setattr(sys.modules[__name__], "SOLD", [*SOLD, (13, mystery, "Missile Racks", 4000, 11)])
    # The rack slot comes empty here: one with a stock rack keeps it (STOCK_RACKS) before any part is rated.
    rack = PORTS[-1]["name"]
    ports = [*PORTS[:-1], {**PORTS[-1], "equipped_uuid": None}]
    tree = [row for row in STOCK_TREE if row["name"] != rack]

    async def run():
        cog, thread = await _cog(tmp_path, monkeypatch, wiki=_wiki(stock_tree=tree), ports=ports)
        await _run_command(cog, thread)
        return thread

    text, _ = _posted(asyncio.run(run()))
    assert _line(text, "S3 Left Wing Missile Rack").endswith(f"nothing to recommend - {NO_STATS}")
    assert WIKI_SILENT_FOR_SLOT not in text


def test_add_all_with_nothing_saved_says_so_and_doesnt_redraw_the_list(tmp_path, monkeypatch):
    async def run():
        cog, thread = await _cog(tmp_path, monkeypatch)
        await _run_command(cog, thread)
        _, view = _posted(thread)
        cog.bot.db.set_ship_parts_entry = AsyncMock(side_effect=RuntimeError("database is locked"))
        cog.shopping.refresh = AsyncMock()
        click = _interaction(thread)
        await view.add_all(click)
        return cog, click

    cog, click = asyncio.run(run())
    assert click.followup.send.await_args.args[0].startswith("I couldn't save those parts. Nothing was added")
    cog.shopping.refresh.assert_not_awaited()


def test_lock_in_many_outside_a_server_without_a_thread_or_when_the_redraw_fails(tmp_path, monkeypatch):
    entry = {"category": "Guns", "port_name": LEFT_WING, "id_item": 1, "item_name": "Mantis GT-220 Gatling",
             "id_terminal": 11, "terminal_name": "Shop 11", "price_buy": 24045.0}

    async def run():
        cog, thread = await _cog(tmp_path, monkeypatch)
        dm = _interaction(thread, guild_id=None)
        in_dm = await cog.shopping.lock_in_many(dm, 1, "Avenger Titan", [entry])
        cog.shopping.refresh = AsyncMock(side_effect=RuntimeError("discord down"))
        redraw = _interaction(thread)
        after_failed_redraw = await cog.shopping.lock_in_many(redraw, 1, "Avenger Titan", [entry])
        cog.shopping._thread = AsyncMock(return_value=None)
        no_thread = _interaction(thread)
        without_thread = await cog.shopping.lock_in_many(no_thread, 1, "Avenger Titan", [entry])
        return (in_dm, dm), (after_failed_redraw, redraw), (without_thread, no_thread)

    (in_dm, dm), (redrawn, redraw), (threadless, no_thread) = asyncio.run(run())
    assert in_dm == 0 and dm.followup.send.await_args.args[0] == "Ship parts lists are available in a server."
    assert redrawn == 1 and redraw.followup.send.await_args.args[0].startswith("Saved 1 part, but I couldn't refresh")
    assert threadless == 0 and "couldn't create your private ship parts thread" in no_thread.followup.send.await_args.args[0]


def test_a_thread_that_cant_be_opened_or_posted_in_is_said_and_the_view_stopped(tmp_path, monkeypatch):
    async def run():
        cog, thread = await _cog(tmp_path, monkeypatch)
        built = []
        real_build = cog._build_loadout

        async def build(*args):
            built.append(await real_build(*args))
            return built[-1]

        cog._build_loadout = build
        cog.shopping._thread = AsyncMock(return_value=None)
        no_thread = await _run_command(cog, thread)
        cog.shopping._thread = AsyncMock(return_value=thread)
        thread.send = AsyncMock(side_effect=discord.HTTPException(NS(status=500, reason="x"), "x"))
        send_failed = await _run_command(cog, thread)
        return built, no_thread, send_failed

    built, no_thread, send_failed = asyncio.run(run())
    assert "couldn't open your private ship parts thread" in no_thread.followup.send.await_args.args[0]
    assert "couldn't post the loadout" in send_failed.followup.send.await_args.args[0]
    assert all(view.is_finished() for view in built), "a loadout nobody can see isn't left waiting for clicks"


def test_the_vehicle_tree_is_found_by_uexs_full_name_too(tmp_path, monkeypatch):
    vehicle = {"id": 1, "name": "Avenger Titan", "name_full": "Aegis Avenger Titan"}

    async def run():
        wiki = _wiki()
        wiki.get_vehicle_stock_ports = AsyncMock(side_effect=lambda name: STOCK_TREE if name == vehicle["name_full"]
                                                 else [])
        cog, thread = await _cog(tmp_path, monkeypatch, wiki=wiki, uex=_uex(vehicles=[vehicle]))
        await _run_command(cog, thread)
        return thread

    text, _ = _posted(asyncio.run(run()))
    assert "**2x S3 Wing Gun** → **M5A Cannon** · 930 DPS (was 547 DPS stock)" in text


def test_a_top_level_stock_part_comes_from_the_vehicle_tree_when_the_reference_lacks_it(tmp_path, monkeypatch):
    ports = [dict(p, equipped_uuid=None) if p["name"] == "hardpoint_power_plant" else p for p in PORTS]

    async def run():
        cog, thread = await _cog(tmp_path, monkeypatch, ports=ports)
        await _run_command(cog, thread)
        return thread

    text, _ = _posted(asyncio.run(run()))
    assert "**S1 Power Plant** → **SunFlare** · 16 power pips (was 15 power pips stock)" in text


def test_a_stock_part_the_wiki_doesnt_have_isnt_called_an_outage(tmp_path, monkeypatch):
    ports = [dict(p, equipped_uuid="no-such-plant") if p["name"] == "hardpoint_power_plant" else p for p in PORTS]
    tree = [dict(row, equipped_item_uuid="no-such-plant") if row["name"] == "hardpoint_power_plant" else row
            for row in STOCK_TREE]

    async def run():
        cog, thread = await _cog(tmp_path, monkeypatch, wiki=_wiki(stock_tree=tree), ports=ports)
        await _run_command(cog, thread)
        return thread

    text, _ = _posted(asyncio.run(run()))
    assert "**S1 Power Plant** → **SunFlare** · 16 power pips (stock part unknown)" in text
    assert "didn't respond" not in text, "asking again won't help, so no 'try again'"


def test_a_gun_only_ship_whose_mounts_all_went_unanswered_says_the_wiki_is_down(tmp_path, monkeypatch):
    async def run():
        wiki = _wiki(unavailable={VARIPUCK_S3["uuid"], VARIPUCK_S4["uuid"]})
        cog, thread = await _cog(tmp_path, monkeypatch, wiki=wiki, ports=PORTS[:3])
        return await _run_command(cog, thread), thread

    interaction, thread = asyncio.run(run())
    message = interaction.followup.send.await_args.args[0]
    assert message.startswith("Couldn't reach the Star Citizen Wiki for **Avenger Titan**'s stock parts")
    thread.send.assert_not_awaited()


def test_the_browsers_button_with_a_known_location_name_doesnt_look_it_up(tmp_path, monkeypatch):
    async def run():
        cog, thread = await _cog(tmp_path, monkeypatch)
        cog._terminal_name = AsyncMock(return_value="looked up")
        browser = PartsBrowserView(cog, VEHICLE, (99, "Area 18 TDD"), {"Radar": [ShipPort("hp", "Radar", 1, 1)]})
        await browser.loadout_button.callback(_interaction(thread))
        return cog, thread

    cog, thread = asyncio.run(run())
    cog._terminal_name.assert_not_awaited()
    _, view = _posted(thread)
    assert view.origin_terminal == (99, "Area 18 TDD")


def test_the_browsers_button_without_a_location_opens_a_loadout_without_one(tmp_path, monkeypatch):
    async def run():
        cog, thread = await _cog(tmp_path, monkeypatch)
        cog._terminal_name = AsyncMock(return_value="looked up")
        browser = PartsBrowserView(cog, VEHICLE, None, {"Radar": [ShipPort("hp", "Radar", 1, 1)]})
        await browser.loadout_button.callback(_interaction(thread))
        return cog, thread

    cog, thread = asyncio.run(run())
    cog._terminal_name.assert_not_awaited()
    text, view = _posted(thread)
    assert view.origin_terminal is None and "ties go to the cheaper part" in text and " Gm" not in text


def test_the_browsers_button_still_opens_when_the_location_name_lookup_fails(tmp_path, monkeypatch):
    async def run():
        cog, thread = await _cog(tmp_path, monkeypatch)
        cog.bot.db.get_terminal_references_by_ids = AsyncMock(side_effect=RuntimeError("database is locked"))
        browser = PartsBrowserView(cog, VEHICLE, (99, ""), {"Radar": [ShipPort("hp", "Radar", 1, 1)]})
        await browser.loadout_button.callback(_interaction(thread))
        return thread

    text, view = _posted(asyncio.run(run()))
    assert view.origin_terminal == (99, "") and "ties go to the shop nearest your location" in text


def test_each_stock_part_is_looked_up_once_however_many_slots_hold_it(tmp_path, monkeypatch):
    async def run():
        wiki = _wiki()
        answer = wiki.get_item_detail.side_effect

        async def suspending(uuid):
            await asyncio.sleep(0)  # a real lookup yields, so two at once would both miss the cache
            return await answer(uuid)

        wiki.get_item_detail.side_effect = suspending
        cog, thread = await _cog(tmp_path, monkeypatch, wiki=wiki)
        await _run_command(cog, thread)
        return wiki

    calls = [c.args[0] for c in asyncio.run(run()).get_item_detail.await_args_list]
    assert calls.count(BRACER["uuid"]) == 1 and calls.count(VARIPUCK_S3["uuid"]) == 1
