"""Ship Parts Finder cog (in design): the shopping-thread service/view layer
(bot/cogs/ship_parts_finder.py), mirroring bot/cogs/blueprint_planner.py's own test
harness, plus the candidate-resolution pipeline end to end against fake UEX/wiki clients."""
import asyncio
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock

from cryptography.fernet import Fernet
import discord

from bot.cogs import ship_parts_finder
from bot.cogs.ship_parts_finder import ShipPartsFinder, ShipPartsShoppingService, ShipPartsShoppingView, _format_candidate_line, _format_stat_block
from bot.db.database import Database
from bot.uex.ship_parts import ShipPort


# -- _format_stat_block: type-keyed, sub_type-keyed, and genuinely absent -------------------

def test_format_stat_block_reads_the_type_keyed_block():
    detail = {"type": "PowerPlant", "power_plant": {"power_segment_generation": 14, "power_output": None}}
    assert _format_stat_block(detail) == "power_segment_generation: 14"


def test_format_stat_block_falls_back_to_the_sub_type_keyed_block():
    # MissileLauncher's real stat block is keyed "missile_rack" (its sub_type), not
    # "missile_launcher" (its type) - confirmed live, see the module docstring.
    detail = {"type": "MissileLauncher", "sub_type": "MissileRack", "missile_rack": {"missile_count": 2, "missile_size": 2}}
    assert _format_stat_block(detail) == "missile_count: 2 · missile_size: 2"


def test_format_stat_block_returns_empty_when_no_block_exists():
    # LifeSupportGenerator genuinely has no dedicated stat block - confirmed live, not a bug.
    detail = {"type": "LifeSupportGenerator", "sub_type": "UNDEFINED", "dimension": {"x": 1}}
    assert _format_stat_block(detail) == ""


def test_format_stat_block_skips_nested_dict_and_list_values():
    detail = {"type": "Shield", "shield": {"max_health": 2244, "reserve_pool": {"regen_rate": 1065}, "tags": [1, 2]}}
    assert _format_stat_block(detail) == "max_health: 2244"


def test_format_stat_block_surfaces_quantum_drive_speed_despite_being_nested_two_levels_deep():
    # Real shape confirmed live (Expedition QD): speed and travel time live under
    # standard_jump/travel_time_10gm, not as top-level scalars like every other category's
    # headline stat - the generic scan alone would silently drop them entirely.
    detail = {
        "type": "QuantumDrive",
        "quantum_drive": {
            "quantum_fuel_requirement": 0.0098,
            "jump_range_formatted": "Unlimited",
            "standard_jump": {"drive_speed": 189309100, "drive_speed_formatted": "189.3 Mm/s"},
            "travel_time_10gm": {"seconds": 68, "formatted": "1:07"},
        },
    }
    result = _format_stat_block(detail)
    assert "speed: 189.3 Mm/s" in result
    assert "10 Gm in: 1:07" in result
    # Fuel-mechanic internals with no comparison value to a player - excluded outright.
    assert "quantum_fuel_requirement" not in result


def test_format_stat_block_quantum_drive_degrades_when_speed_data_is_missing():
    # No standard_jump/travel_time_10gm, and the one field present is an excluded
    # fuel-mechanic internal - nothing useful to show, so this degrades to empty rather
    # than falling back to a raw internal constant.
    detail = {"type": "QuantumDrive", "quantum_drive": {"quantum_fuel_requirement": 0.0098}}
    assert _format_stat_block(detail) == ""


def test_format_stat_block_prefers_a_formatted_sibling_over_a_raw_sentinel_value():
    # Real live bug: jump_range's raw value is literally float32's max (a "no limit"
    # sentinel) with a clean jump_range_formatted ("Unlimited") sitting right next to it -
    # the raw 3.402823e+38 must never be shown when the formatted sibling exists.
    detail = {
        "type": "QuantumDrive",
        "quantum_drive": {
            "jump_range": 3.402823e+38,
            "jump_range_formatted": "Unlimited",
            "disconnect_range": 34693,
            "disconnect_range_formatted": "35 km",
        },
    }
    result = _format_stat_block(detail)
    assert "jump_range: Unlimited" in result
    assert "disconnect_range: 35 km" in result
    assert "3.402823e" not in result
    assert "34693" not in result


def test_format_stat_block_formatted_sibling_preference_applies_to_any_category():
    # Not QD-specific - any mapped category's block gets this preference if it ever
    # carries a raw/formatted pair.
    detail = {"type": "Shield", "shield": {"max_health": 2244, "max_health_formatted": "2.24k"}}
    assert _format_stat_block(detail) == "max_health: 2.24k"


# -- _format_candidate_line ------------------------------------------------------------------

def test_format_candidate_line_shows_the_cheapest_listing():
    detail = {
        "name": "PowerBolt", "size": 1, "grade": "C", "manufacturer": {"name": "Lightning Power Ltd."},
        "type": "PowerPlant", "power_plant": {"power_segment_generation": 14},
        "uex_prices": {"purchase": [
            {"price_buy": 22051, "terminal_name": "Dumper's Depot - GrimHEX"},
            {"price_buy": 18701, "terminal_name": "Dumper's Depot - Area 18"},
        ]},
    }
    line = _format_candidate_line(detail)
    assert "**PowerBolt**" in line and "S1" in line and "Grade C" in line and "Lightning Power Ltd." in line
    assert "18,701 aUEC @ Dumper's Depot - Area 18" in line
    assert "power_segment_generation: 14" in line
    assert "—" in line, "primary line matches /ingame-item-finder's proven em-dash format"


def test_format_candidate_line_handles_no_price_data():
    detail = {"name": "Mystery Part", "type": "Cooler"}
    assert "price unknown" in _format_candidate_line(detail)


class FakeMessage:
    id = 200

    def __init__(self):
        self.edit = AsyncMock()


class FakeThread:
    id = 100
    mention = "<#100>"
    archived = False

    def __init__(self, *, fail_add=False):
        self.message = FakeMessage()
        self.add_user = AsyncMock(side_effect=RuntimeError("add failed") if fail_add else None)
        self.send = AsyncMock(return_value=self.message)
        self.fetch_message = AsyncMock(return_value=self.message)
        self.edit = AsyncMock()
        self.delete = AsyncMock()


class FakeChannel:
    def __init__(self, thread):
        self.thread = thread
        self.create_thread = AsyncMock(return_value=thread)


def _interaction(channel, *, user_id=1, interaction_id=300):
    return NS(
        id=interaction_id, guild_id=10, channel=channel,
        user=NS(id=user_id, display_name="Pilot"),
        response=NS(send_message=AsyncMock(), defer=AsyncMock()),
        followup=NS(send=AsyncMock()),
    )


def _lock_in(service, interaction):
    return service.lock_in(
        interaction, 100, "Avenger Stalker", "Power Plants", "hardpoint_power_plant",
        500, "PowerBolt", 139, "Platinum Bay - HUR-L5", 19998.0,
    )


# -- ShipPartsShoppingService: thread lifecycle ------------------------------------------

def test_complete_private_thread_lock_in_and_restart_reuse(tmp_path, monkeypatch):
    async def run():
        path, key = tmp_path / "ship_parts.sqlite", Fernet(Fernet.generate_key())
        db = Database(path, key)
        await db.init()
        thread = FakeThread()
        channel = FakeChannel(thread)
        monkeypatch.setattr(ship_parts_finder.discord, "TextChannel", FakeChannel)
        monkeypatch.setattr(ship_parts_finder.discord, "Thread", FakeThread)
        bot = NS(db=db, get_channel=lambda _id: None, fetch_channel=AsyncMock())
        interaction = _interaction(channel)
        assert await _lock_in(ShipPartsShoppingService(bot), interaction)
        first = await db.get_ship_parts_entries(1, 10)

        restarted = Database(path, key)
        await restarted.init()
        bot2 = NS(db=restarted, get_channel=lambda _id: thread, fetch_channel=AsyncMock())
        # Locking a second part in a different category reuses the saved thread.
        await ShipPartsShoppingService(bot2).lock_in(
            _interaction(channel, interaction_id=301), 100, "Avenger Stalker", "Coolers", "hardpoint_cooler_left",
            600, "Bracer", 115, "Dumper's Depot - GrimHEX", 5000.0,
        )
        return first, channel, thread, restarted

    entries, channel, thread, restarted = asyncio.run(run())
    assert len(entries) == 1 and entries[0]["item_name"] == "PowerBolt"
    assert channel.create_thread.await_count == 1, "restart reuses the saved private thread"
    assert thread.add_user.await_count == 2
    final = asyncio.run(restarted.get_ship_parts_entries(1, 10))
    assert {row["category"] for row in final} == {"Power Plants", "Coolers"}


def test_relocking_the_same_slot_replaces_it_not_a_second_entry(tmp_path, monkeypatch):
    async def run():
        db = Database(tmp_path / "relock.sqlite", Fernet(Fernet.generate_key()))
        await db.init()
        thread = FakeThread()
        channel = FakeChannel(thread)
        monkeypatch.setattr(ship_parts_finder.discord, "TextChannel", FakeChannel)
        bot = NS(db=db, get_channel=lambda _id: thread, fetch_channel=AsyncMock())
        service = ShipPartsShoppingService(bot)
        await _lock_in(service, _interaction(channel))
        await service.lock_in(
            _interaction(channel, interaction_id=301), 100, "Avenger Stalker", "Power Plants", "hardpoint_power_plant",
            501, "Atlas", 114, "Dumper's Depot - Area 18", 21000.0,
        )
        return await db.get_ship_parts_entries(1, 10)

    entries = asyncio.run(run())
    assert len(entries) == 1 and entries[0]["item_name"] == "Atlas"


def test_partial_thread_setup_is_deleted_and_does_not_save_an_entry(tmp_path, monkeypatch):
    async def run():
        db = Database(tmp_path / "failed.sqlite", Fernet(Fernet.generate_key()))
        await db.init()
        thread = FakeThread(fail_add=True)
        channel = FakeChannel(thread)
        monkeypatch.setattr(ship_parts_finder.discord, "TextChannel", FakeChannel)
        bot = NS(db=db, get_channel=lambda _id: None, fetch_channel=AsyncMock())
        interaction = _interaction(channel)
        saved = await _lock_in(ShipPartsShoppingService(bot), interaction)
        return saved, await db.get_ship_parts_entries(1, 10), await db.get_ship_parts_thread(1, 10), thread, interaction

    saved, entries, mapping, thread, interaction = asyncio.run(run())
    assert not saved and entries == [] and mapping is None
    thread.delete.assert_awaited_once()
    assert "couldn't create" in interaction.followup.send.await_args.args[0]


def test_persistent_controls_reject_a_non_owner(tmp_path):
    async def run():
        db = Database(tmp_path / "owner.sqlite", Fernet(Fernet.generate_key()))
        await db.init()
        await db.set_ship_parts_thread(1, 10, 100, 200)
        service = ShipPartsShoppingService(NS(db=db))
        interaction = _interaction(NS(id=100), user_id=2)
        row = await ShipPartsShoppingView(service)._owner(interaction)
        return row, interaction

    row, interaction = asyncio.run(run())
    assert row is None
    interaction.response.send_message.assert_awaited_once_with(
        "This shopping list belongs to another player.", ephemeral=True,
    )


# -- Remove a part (per-entry removal, not just Clear list) ---------------------------------

def test_remove_button_reports_when_the_list_is_empty():
    async def run():
        db = NS(get_ship_parts_thread_owner=AsyncMock(return_value={"user_id": 1, "guild_id": 10}),
                get_ship_parts_entries=AsyncMock(return_value=[]))
        service = ShipPartsShoppingService(NS(db=db))
        view = ShipPartsShoppingView(service)
        interaction = _interaction(NS(id=100), user_id=1)
        await view.remove_button.callback(interaction)
        return interaction

    interaction = asyncio.run(run())
    interaction.response.send_message.assert_awaited_once_with(
        "Your list is empty - nothing to remove.", ephemeral=True,
    )


def test_remove_button_opens_a_select_populated_from_current_entries():
    async def run():
        entries = [
            {"user_id": 1, "guild_id": 10, "id_vehicle": 100, "vehicle_name": "Avenger Stalker",
             "category": "Power Plants", "port_name": "hardpoint_power_plant", "item_name": "PowerBolt"},
            {"user_id": 1, "guild_id": 10, "id_vehicle": 100, "vehicle_name": "Avenger Stalker",
             "category": "Turrets", "port_name": "hardpoint_turret_left", "item_name": "VariPuck S3"},
        ]
        db = NS(get_ship_parts_thread_owner=AsyncMock(return_value={"user_id": 1, "guild_id": 10}),
                get_ship_parts_entries=AsyncMock(return_value=entries))
        service = ShipPartsShoppingService(NS(db=db))
        view = ShipPartsShoppingView(service)
        interaction = _interaction(NS(id=100), user_id=1)
        await view.remove_button.callback(interaction)
        return interaction

    interaction = asyncio.run(run())
    kwargs = interaction.response.send_message.await_args.kwargs
    assert kwargs["ephemeral"] is True
    select = kwargs["view"].children[0]
    assert [o.label for o in select.options] == ["PowerBolt (Power Plants)", "VariPuck S3 (Turrets)"]
    assert [o.description for o in select.options] == ["Avenger Stalker - Power Plant", "Avenger Stalker - Turret Left"]


def test_remove_button_notes_truncation_past_25_entries():
    async def run():
        entries = [
            {"user_id": 1, "guild_id": 10, "id_vehicle": 100, "vehicle_name": "Avenger Stalker",
             "category": "Power Plants", "port_name": f"hp_{i}", "item_name": f"Part {i}"}
            for i in range(30)
        ]
        db = NS(get_ship_parts_thread_owner=AsyncMock(return_value={"user_id": 1, "guild_id": 10}),
                get_ship_parts_entries=AsyncMock(return_value=entries))
        service = ShipPartsShoppingService(NS(db=db))
        view = ShipPartsShoppingView(service)
        interaction = _interaction(NS(id=100), user_id=1)
        await view.remove_button.callback(interaction)
        return interaction

    interaction = asyncio.run(run())
    args, kwargs = interaction.response.send_message.await_args
    assert "showing the first 25 of 30" in args[0]
    assert len(kwargs["view"].children[0].options) == 25


def test_remove_entry_select_removes_one_entry_and_refreshes_the_list():
    async def run():
        entry = {"user_id": 1, "guild_id": 10, "id_vehicle": 100, "vehicle_name": "Avenger Stalker",
                 "category": "Power Plants", "port_name": "hardpoint_power_plant", "item_name": "PowerBolt"}
        db = NS(remove_ship_parts_entry=AsyncMock())
        service = ShipPartsShoppingService(NS(db=db))
        service.refresh = AsyncMock()
        select = ship_parts_finder._RemoveEntrySelect(service, [entry])
        select._values = ["0"]
        interaction = _interaction(NS(id=100), user_id=1)
        await select.callback(interaction)
        return db, service, interaction

    db, service, interaction = asyncio.run(run())
    db.remove_ship_parts_entry.assert_awaited_once_with(1, 10, 100, "Power Plants", "hardpoint_power_plant")
    service.refresh.assert_awaited_once()
    interaction.followup.send.assert_awaited_once_with("Removed **PowerBolt** (Power Plants).", ephemeral=True)


def test_remove_entry_select_reports_when_the_db_delete_fails():
    async def run():
        entry = {"user_id": 1, "guild_id": 10, "id_vehicle": 100, "vehicle_name": "Avenger Stalker",
                 "category": "Power Plants", "port_name": "hardpoint_power_plant", "item_name": "PowerBolt"}
        db = NS(remove_ship_parts_entry=AsyncMock(side_effect=RuntimeError("db down")))
        service = ShipPartsShoppingService(NS(db=db))
        service.refresh = AsyncMock()
        select = ship_parts_finder._RemoveEntrySelect(service, [entry])
        select._values = ["0"]
        interaction = _interaction(NS(id=100), user_id=1)
        await select.callback(interaction)
        return service, interaction

    service, interaction = asyncio.run(run())
    service.refresh.assert_not_awaited()
    assert "couldn't remove" in interaction.followup.send.await_args.args[0]


def test_oversized_private_list_uses_one_safe_attachment():
    async def run():
        thread = FakeThread()
        db = NS(get_ship_parts_thread=AsyncMock(return_value={"message_id": 200}))
        service = ShipPartsShoppingService(NS(db=db))
        service.render = AsyncMock(return_value=["x" * 1900, "@everyone second page"])
        await service.refresh(thread, 1, 10)
        return thread

    thread = asyncio.run(run())
    kwargs = thread.message.edit.await_args.kwargs
    assert len(kwargs["content"]) < 2000 and kwargs["allowed_mentions"] == ship_parts_finder.NO_MENTIONS
    assert len(kwargs["attachments"]) == 1
    assert kwargs["attachments"][0].filename == "ship-parts-list.txt"


# -- render ---------------------------------------------------------------------------------

def test_render_groups_entries_by_ship():
    async def run():
        db = NS(get_ship_parts_entries=AsyncMock(return_value=[
            {"vehicle_name": "Avenger Stalker", "category": "Power Plants", "port_name": "hardpoint_power_plant",
             "item_name": "PowerBolt", "price_buy": 19998.0, "terminal_name": "Platinum Bay - HUR-L5"},
            {"vehicle_name": "Cutlass Black", "category": "Shield Generators", "port_name": "hardpoint_shield",
             "item_name": "Shimmer", "price_buy": None, "terminal_name": None},
        ]))
        return await ShipPartsShoppingService(NS(db=db)).render(1, 10)

    pages = asyncio.run(run())
    text = "\n".join(pages)
    assert "**Avenger Stalker**" in text and "**Cutlass Black**" in text
    assert "PowerBolt" in text and "19,998 aUEC" in text and "Power Plant" in text
    assert "price unknown" in text and "unknown shop" in text


# -- candidates_for_port: full pipeline against fake UEX/wiki clients ------------------------

def test_candidates_for_port_filters_sold_items_and_batches_wiki_lookups():
    async def run():
        catalog = [
            {"id": 1, "uuid": "u1", "category": "Power Plants", "size": "1", "name": "Sold"},
            {"id": 2, "uuid": "u2", "category": "Power Plants", "size": "1", "name": "Unsold"},
            {"id": 3, "uuid": "u3", "category": "Power Plants", "size": "5", "name": "WrongSize"},
        ]
        price_rows = [{"id_item": 1}]  # only item 1 has a real listing
        uex = NS(
            get_item_catalog=AsyncMock(return_value=catalog),
            get_items_prices_all=AsyncMock(return_value=price_rows),
        )
        wiki = NS(get_item_detail=AsyncMock(return_value={"uuid": "u1", "name": "Sold", "type": "PowerPlant"}))
        bot = NS(uex=uex)
        cog = ShipPartsFinder(bot, wiki_client=wiki, start_refresh=False)
        port = ShipPort(name="hp", port_type="PowerPlant", size_min=1, size_max=1)
        return await cog.candidates_for_port(port, limit=10), wiki

    candidates, wiki = asyncio.run(run())
    assert len(candidates) == 1 and candidates[0]["name"] == "Sold" and candidates[0]["_uex_id"] == 1
    wiki.get_item_detail.assert_awaited_once_with("u1")


def test_candidates_for_port_skips_a_failed_wiki_detail_lookup():
    async def run():
        catalog = [{"id": 1, "uuid": "u1", "category": "Power Plants", "size": "1", "name": "X"}]
        uex = NS(
            get_item_catalog=AsyncMock(return_value=catalog),
            get_items_prices_all=AsyncMock(return_value=[{"id_item": 1}]),
        )
        wiki = NS(get_item_detail=AsyncMock(side_effect=ship_parts_finder.WikiApiError("boom")))
        cog = ShipPartsFinder(NS(uex=uex), wiki_client=wiki, start_refresh=False)
        port = ShipPort(name="hp", port_type="PowerPlant", size_min=1, size_max=1)
        return await cog.candidates_for_port(port, limit=10)

    assert asyncio.run(run()) == []


def test_item_detail_cache_reuses_a_warm_entry_without_a_second_wiki_call():
    async def run():
        wiki = NS(get_item_detail=AsyncMock(return_value={"uuid": "u1", "name": "X"}))
        cog = ShipPartsFinder(NS(), wiki_client=wiki, start_refresh=False)
        first = await cog._item_detail_cached("u1")
        second = await cog._item_detail_cached("u1")
        return first, second, wiki

    first, second, wiki = asyncio.run(run())
    assert first == second == {"uuid": "u1", "name": "X"}
    wiki.get_item_detail.assert_awaited_once()


# -- _ports_for_vehicle: DB reference vs. live fallback --------------------------------------

def test_ports_for_vehicle_falls_back_to_a_live_wiki_lookup_when_reference_is_cold():
    async def run():
        db = NS(get_ship_parts_reference=AsyncMock(return_value=[]))
        wiki = NS(get_vehicle_ports=AsyncMock(return_value=[
            {"name": "hp_power", "type": "PowerPlant", "sizes": {"min": 1, "max": 1}},
        ]))
        cog = ShipPartsFinder(NS(db=db), wiki_client=wiki, start_refresh=False)
        return await cog._ports_for_vehicle({"id": 100, "name": "Avenger Stalker"}), wiki

    ports, wiki = asyncio.run(run())
    assert ports == [ShipPort(name="hp_power", port_type="PowerPlant", size_min=1, size_max=1)]
    wiki.get_vehicle_ports.assert_awaited_once_with("Avenger Stalker")


def test_ports_for_vehicle_prefers_the_warm_db_reference_over_a_live_call():
    async def run():
        db = NS(get_ship_parts_reference=AsyncMock(return_value=[
            {"port_name": "hp_power", "port_type": "PowerPlant", "size_min": 1, "size_max": 1},
        ]))
        wiki = NS(get_vehicle_ports=AsyncMock())
        cog = ShipPartsFinder(NS(db=db), wiki_client=wiki, start_refresh=False)
        return await cog._ports_for_vehicle({"id": 100, "name": "Avenger Stalker"}), wiki

    ports, wiki = asyncio.run(run())
    assert ports == [ShipPort(name="hp_power", port_type="PowerPlant", size_min=1, size_max=1)]
    wiki.get_vehicle_ports.assert_not_awaited()


# -- /ship-parts-finder command: resolution failure paths -----------------------------------

def test_command_reports_an_unresolvable_location():
    async def run():
        db = NS(resolve_terminal_id_by_name=AsyncMock(return_value=None))
        cog = ShipPartsFinder(NS(db=db), wiki_client=NS(), start_refresh=False)
        cog.bot = NS(db=db)
        interaction = _interaction(NS(), user_id=1)
        await cog.ship_parts_finder.callback(cog, interaction, "Avenger Stalker", "Nowhere")
        return interaction

    interaction = asyncio.run(run())
    interaction.response.defer.assert_awaited_once_with(ephemeral=True)
    assert "Nowhere" in interaction.followup.send.await_args.args[0]


def test_command_reports_an_unresolvable_ship():
    async def run():
        db = NS(resolve_terminal_id_by_name=AsyncMock(return_value=(1, "Some Terminal")))
        uex = NS(get_vehicles=AsyncMock(return_value=[{"id": 1, "name": "Cutlass Black"}]))
        cog = ShipPartsFinder(NS(db=db, uex=uex), wiki_client=NS(), start_refresh=False)
        cog.bot = NS(db=db, uex=uex)
        interaction = _interaction(NS(), user_id=1)
        await cog.ship_parts_finder.callback(cog, interaction, "NotAShip", "Some Terminal")
        return interaction

    interaction = asyncio.run(run())
    assert "NotAShip" in interaction.followup.send.await_args.args[0]


def test_command_posts_the_browsing_view_inside_the_thread_not_ephemeral_elsewhere(tmp_path, monkeypatch):
    """Live testing flagged having to jump between an ephemeral reply (wherever the
    command was run) and the separate thread holding the list - the whole point of the
    thread is that browsing and the list live in the SAME place."""
    async def run():
        db_real = Database(tmp_path / "cmd.sqlite", Fernet(Fernet.generate_key()))
        await db_real.init()
        await db_real.replace_ship_parts_reference(1, "Cutlass Black", [
            {"name": "hardpoint_power_plant", "port_type": "PowerPlant", "size_min": 1, "size_max": 1},
        ])
        db_real.resolve_terminal_id_by_name = AsyncMock(return_value=(1, "Some Terminal"))
        thread = FakeThread()
        channel = FakeChannel(thread)
        monkeypatch.setattr(ship_parts_finder.discord, "TextChannel", FakeChannel)
        uex = NS(get_vehicles=AsyncMock(return_value=[{"id": 1, "name": "Cutlass Black"}]))
        cog = ShipPartsFinder(NS(db=db_real, uex=uex), wiki_client=NS(), start_refresh=False)
        cog.bot = NS(db=db_real, uex=uex)
        interaction = _interaction(channel, user_id=1)
        await cog.ship_parts_finder.callback(cog, interaction, "Cutlass Black", "Some Terminal")
        return interaction, thread, channel

    interaction, thread, channel = asyncio.run(run())
    channel.create_thread.assert_awaited_once()
    # Two sends on a brand-new thread: _thread()'s own welcome/list message, then the
    # browsing view posted by the command itself - both belong in the thread, neither in
    # an ephemeral reply elsewhere.
    assert thread.send.await_count == 2
    browsing_call = thread.send.await_args_list[-1]
    assert isinstance(browsing_call.kwargs.get("view"), ship_parts_finder.PartsBrowserView)
    assert "Cutlass Black" in browsing_call.kwargs["content"]
    pointer = interaction.followup.send.await_args
    assert thread.mention in pointer.args[0] and pointer.kwargs["ephemeral"] is True


# -- audit fixes: deferred category select, multi-slot, distance wiring, selected marker ----

def _component_interaction(*, user_id=1):
    return NS(
        user=NS(id=user_id, display_name="Pilot"),
        response=NS(defer=AsyncMock(), edit_message=AsyncMock(), send_message=AsyncMock()),
        edit_original_response=AsyncMock(),
    )


def _detail(name, uuid="u1", price=1000.0, terminal_id=1, terminal_name="Some Shop"):
    return {
        "uuid": uuid, "name": name, "type": "PowerPlant", "_uex_id": 1,
        "uex_prices": {"purchase": [{"price_buy": price, "terminal_id": terminal_id, "terminal_name": terminal_name}]},
    }


def test_show_category_defers_before_the_slow_candidate_lookup():
    async def run():
        seen_defer_count = None

        async def slow_candidates(port, *, limit, origin_id=None):
            nonlocal seen_defer_count
            seen_defer_count = interaction.response.defer.await_count
            return [_detail("PowerBolt")]

        cog = NS(candidates_for_port=slow_candidates)
        port = ShipPort(name="hardpoint_power_plant", port_type="PowerPlant", size_min=1, size_max=1)
        view = ship_parts_finder.PartsBrowserView(cog, {"id": 100, "name": "Avenger Stalker"}, (1, "Origin"),
                                                    {"Power Plants": [port]})
        interaction = _component_interaction()
        await view.show_category(interaction, "Power Plants")
        return interaction, view, seen_defer_count

    interaction, view, seen_defer_count = asyncio.run(run())
    assert seen_defer_count == 1, "the slow lookup must observe defer already awaited, not called after"
    interaction.edit_original_response.assert_awaited_once()
    interaction.response.edit_message.assert_not_awaited()
    assert view.selected_port is not None and view.candidates


def test_category_with_multiple_ports_shows_a_slot_select_without_loading_candidates_yet():
    async def run():
        cog = NS(candidates_for_port=AsyncMock(side_effect=AssertionError("should not load candidates before a slot is chosen")))
        left = ShipPort(name="hardpoint_turret_left", port_type="Turret", size_min=3, size_max=3)
        nose = ShipPort(name="hardpoint_turret_nose", port_type="Turret", size_min=4, size_max=4)
        view = ship_parts_finder.PartsBrowserView(cog, {"id": 100, "name": "Avenger Stalker"}, (1, "Origin"),
                                                    {"Turrets": [left, nose]})
        interaction = _component_interaction()
        await view.show_category(interaction, "Turrets")
        return interaction, view

    interaction, view = asyncio.run(run())
    interaction.response.edit_message.assert_awaited_once()
    interaction.edit_original_response.assert_not_awaited()
    assert any(isinstance(child, ship_parts_finder._SlotSelect) for child in view.children)
    assert view.selected_port is None


def test_selecting_a_slot_loads_candidates_for_that_specific_port():
    async def run():
        calls = []

        async def track(port, *, limit, origin_id=None):
            calls.append(port.name)
            return [_detail("VariPuck S3")]

        cog = NS(candidates_for_port=track)
        left = ShipPort(name="hardpoint_turret_left", port_type="Turret", size_min=3, size_max=3)
        nose = ShipPort(name="hardpoint_turret_nose", port_type="Turret", size_min=4, size_max=4)
        view = ship_parts_finder.PartsBrowserView(cog, {"id": 100, "name": "Avenger Stalker"}, (1, "Origin"),
                                                    {"Turrets": [left, nose]})
        await view.show_slot(_component_interaction(), nose)
        return view, calls

    view, calls = asyncio.run(run())
    assert calls == ["hardpoint_turret_nose"]
    assert view.selected_port.name == "hardpoint_turret_nose"


def test_locking_in_passes_the_selected_ports_own_name_not_the_categorys_first_port():
    async def run():
        lock_calls = []

        class FakeShopping:
            async def lock_in(self, interaction, id_vehicle, vehicle_name, category, port_name, id_item, item_name,
                              id_terminal, terminal_name, price_buy):
                lock_calls.append(port_name)

        cog = NS(shopping=FakeShopping())
        nose = ShipPort(name="hardpoint_turret_nose", port_type="Turret", size_min=4, size_max=4)
        view = ship_parts_finder.PartsBrowserView(cog, {"id": 100, "name": "Avenger Stalker"}, (1, "Origin"),
                                                    {"Turrets": [nose]})
        view.category = "Turrets"
        view.selected_port = nose
        view.selected_candidate = _detail("VariPuck S4")
        interaction = _component_interaction()
        interaction.response.defer = AsyncMock()
        await view.lock_in_selected(interaction)
        return lock_calls

    assert asyncio.run(run()) == ["hardpoint_turret_nose"]


def test_selected_candidate_is_visibly_marked_in_the_rendered_text():
    cog = NS()
    port = ShipPort(name="hardpoint_power_plant", port_type="PowerPlant", size_min=1, size_max=1)
    view = ship_parts_finder.PartsBrowserView(cog, {"id": 100, "name": "Avenger Stalker"}, (1, "Origin"),
                                                {"Power Plants": [port]})
    view.category = "Power Plants"
    view.selected_port = port
    a, b = _detail("PowerBolt", uuid="ua"), _detail("Atlas", uuid="ub")
    view.candidates = [a, b]
    view.selected_candidate = b
    text = view.text()
    assert "✅ **Atlas**" in text
    assert "✅ **PowerBolt**" not in text and "**PowerBolt**" in text


def test_selection_summary_is_blank_before_any_category_is_chosen():
    cog = NS()
    view = ship_parts_finder.PartsBrowserView(cog, {"id": 100, "name": "Avenger Stalker"}, (1, "Origin"), {})
    assert view._selection_summary() == ""
    assert "Selected so far" not in view.text()


def test_selection_summary_shows_pick_below_for_an_unresolved_slot_or_part():
    cog = NS()
    left = ShipPort(name="hardpoint_turret_left", port_type="Turret", size_min=3, size_max=3)
    nose = ShipPort(name="hardpoint_turret_nose", port_type="Turret", size_min=4, size_max=4)
    view = ship_parts_finder.PartsBrowserView(cog, {"id": 100, "name": "Avenger Stalker"}, (1, "Origin"),
                                                {"Turrets": [left, nose]})
    view.category = "Turrets"
    summary = view._selection_summary()
    assert "Category: **Turrets**" in summary
    assert "Slot: *(pick below)*" in summary
    assert "Part:" not in summary
    assert summary in view.text()


def test_selection_summary_reflects_a_chosen_slot_and_part_plainly_regardless_of_dropdown_state():
    cog = NS()
    port = ShipPort(name="hardpoint_power_plant", port_type="PowerPlant", size_min=1, size_max=1)
    view = ship_parts_finder.PartsBrowserView(cog, {"id": 100, "name": "Avenger Stalker"}, (1, "Origin"),
                                                {"Power Plants": [port]})
    view.category = "Power Plants"
    view.selected_port = port
    view.candidates = [_detail("Atlas", uuid="ua")]
    view.selected_candidate = view.candidates[0]
    summary = view._selection_summary()
    assert "Category: **Power Plants**" in summary
    assert "Slot:" not in summary  # single-port category never shows a redundant slot line
    assert "Part: **Atlas**" in summary
    assert summary in view.text()


# -- _attach_distances ------------------------------------------------------------------------

def test_attach_distances_sorts_closest_first_and_unknown_last():
    async def run():
        uex = NS(get_terminal_distance=AsyncMock(side_effect=lambda origin, dest: {2: {"distance": 5.0}, 3: {"distance": 1.0}}.get(dest)))
        cog = ShipPartsFinder(NS(uex=uex), wiki_client=NS(), start_refresh=False)
        far = _detail("Far", terminal_id=2)
        near = _detail("Near", terminal_id=3)
        unknown = _detail("Unknown", terminal_id=4)
        return await cog._attach_distances([far, near, unknown], origin_id=1)

    result = asyncio.run(run())
    assert [d["name"] for d in result] == ["Near", "Far", "Unknown"]
    assert result[0]["_distance_gm"] == 1.0 and result[1]["_distance_gm"] == 5.0 and result[2]["_distance_gm"] is None


def test_attach_distances_treats_the_origin_terminal_itself_as_zero():
    async def run():
        uex = NS(get_terminal_distance=AsyncMock())
        cog = ShipPartsFinder(NS(uex=uex), wiki_client=NS(), start_refresh=False)
        here = _detail("Here", terminal_id=1)
        return await cog._attach_distances([here], origin_id=1), uex

    result, uex = asyncio.run(run())
    assert result[0]["_distance_gm"] == 0.0
    uex.get_terminal_distance.assert_not_awaited()


def test_candidates_for_port_attaches_distances_only_when_origin_is_given():
    async def run():
        catalog = [{"id": 1, "uuid": "u1", "category": "Power Plants", "size": "1", "name": "X"}]
        uex = NS(
            get_item_catalog=AsyncMock(return_value=catalog),
            get_items_prices_all=AsyncMock(return_value=[{"id_item": 1}]),
            get_terminal_distance=AsyncMock(return_value={"distance": 2.5}),
        )
        wiki = NS(get_item_detail=AsyncMock(return_value=_detail("X", uuid="u1", terminal_id=9)))
        cog = ShipPartsFinder(NS(uex=uex), wiki_client=wiki, start_refresh=False)
        port = ShipPort(name="hp", port_type="PowerPlant", size_min=1, size_max=1)
        without_origin = await cog.candidates_for_port(port, limit=10)
        with_origin = await cog.candidates_for_port(port, limit=10, origin_id=1)
        return without_origin, with_origin

    without_origin, with_origin = asyncio.run(run())
    assert "_distance_gm" not in without_origin[0]
    assert with_origin[0]["_distance_gm"] == 2.5


# -- _format_port_label -----------------------------------------------------------------------

def test_format_port_label_reads_and_cleans_the_raw_port_name():
    port = ShipPort(name="hardpoint_weapon_gun_class1_left_wing", port_type="Turret", size_min=3, size_max=3)
    assert ship_parts_finder._format_port_label(port) == "Weapon Gun Class1 Left Wing (S3)"


def test_format_port_label_shows_a_size_range_when_min_and_max_differ():
    port = ShipPort(name="hardpoint_turret", port_type="Turret", size_min=2, size_max=4)
    assert "(S2-4)" in ship_parts_finder._format_port_label(port)


# -- dropdowns keep showing the picked value once collapsed, not just the placeholder -------
# Discord only shows a chosen value on a collapsed Select if the matching SelectOption has
# default=True - found live in testing (looked exactly like the pick was lost, even though
# the message text and lock-in both worked correctly underneath).

def test_mark_default_sets_only_the_matching_option():
    options = [discord.SelectOption(label="A", value="a"), discord.SelectOption(label="B", value="b")]
    ship_parts_finder._mark_default(options, "b")
    assert [o.default for o in options] == [False, True]
    ship_parts_finder._mark_default(options, "a")
    assert [o.default for o in options] == [True, False]


def test_category_select_marks_the_chosen_category_as_default():
    async def run():
        cog = NS(candidates_for_port=AsyncMock(return_value=[]))
        port = ShipPort(name="hp", port_type="PowerPlant", size_min=1, size_max=1)
        view = ship_parts_finder.PartsBrowserView(cog, {"id": 100, "name": "X"}, (1, "Origin"),
                                                    {"Power Plants": [port], "Coolers": [port]})
        select = view.category_select
        select._values = ["Coolers"]
        await select.callback(_component_interaction())
        return select

    select = asyncio.run(run())
    assert {o.value: o.default for o in select.options} == {"Power Plants": False, "Coolers": True}


def test_slot_select_marks_the_chosen_slot_as_default():
    async def run():
        cog = NS(candidates_for_port=AsyncMock(return_value=[]))
        left = ShipPort(name="hp_left", port_type="Turret", size_min=3, size_max=3)
        nose = ShipPort(name="hp_nose", port_type="Turret", size_min=4, size_max=4)
        view = ship_parts_finder.PartsBrowserView(cog, {"id": 100, "name": "X"}, (1, "Origin"),
                                                    {"Turrets": [left, nose]})
        select = ship_parts_finder._SlotSelect(view, [left, nose])
        select._values = ["1"]
        await select.callback(_component_interaction())
        return select

    select = asyncio.run(run())
    assert [o.default for o in select.options] == [False, True]


def test_part_select_marks_the_chosen_part_as_default():
    async def run():
        port = ShipPort(name="hp", port_type="PowerPlant", size_min=1, size_max=1)
        view = ship_parts_finder.PartsBrowserView(NS(), {"id": 100, "name": "X"}, (1, "Origin"), {"Power Plants": [port]})
        candidates = [_detail("PowerBolt", uuid="ua"), _detail("Atlas", uuid="ub")]
        select = ship_parts_finder._PartSelect(view, candidates)
        select._values = ["1"]
        await select.callback(_component_interaction())
        return select, view

    select, view = asyncio.run(run())
    assert [o.default for o in select.options] == [False, True]
    assert view.selected_candidate["name"] == "Atlas"
