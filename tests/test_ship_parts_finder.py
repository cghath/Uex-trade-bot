"""Ship Parts Finder cog (in design): the shopping-thread service/view layer
(bot/cogs/ship_parts_finder.py), mirroring bot/cogs/blueprint_planner.py's own test
harness, plus the candidate-resolution pipeline end to end against fake UEX/wiki clients."""
import asyncio
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock

from cryptography.fernet import Fernet
import discord

from bot.cogs import ship_parts_finder
from bot.cogs.ship_parts_finder import ShipPartsFinder, ShipPartsShoppingService, ShipPartsShoppingView
from bot.db.database import Database
from bot.uex.ship_parts import ShipPort


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
    assert [o.label for o in select.options] == ["PowerBolt (Power Plants)", "VariPuck S3 (Gun Mounts)"]
    assert [o.description for o in select.options] == ["Avenger Stalker - Power Plant", "Avenger Stalker - Turret Left (mount)"]


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
    assert "no shop price on record" in text
    assert "HUR-L5 (Platinum Bay)" in text, "shop shown as Place (Vendor), same as /ingame-item-finder"


# -- candidates_for_port: full pipeline against fake UEX/wiki clients ------------------------

def _wiki(details=None, *, by_name=None, variants=None):
    """Fake wiki client: `details` maps uuid -> detail (a missing uuid raises, like the
    real one), `by_name` maps name -> detail, `variants` maps name -> variant rows."""
    details, by_name, variants = details or {}, by_name or {}, variants or {}

    async def get_item_detail(uuid):
        if uuid not in details:
            raise ship_parts_finder.WikiApiError("not found")
        return details[uuid]

    return NS(
        get_item_detail=AsyncMock(side_effect=get_item_detail),
        find_item_detail_by_name=AsyncMock(side_effect=lambda name: by_name.get(name)),
        find_item_variants_by_name=AsyncMock(side_effect=lambda name: variants.get(name, [])),
    )


def _uex(catalog, price_rows, distances=None):
    distances = distances or {}
    return NS(
        get_item_catalog=AsyncMock(return_value=catalog),
        get_items_prices_all=AsyncMock(return_value=price_rows),
        get_terminal_distance=AsyncMock(side_effect=lambda origin, dest: (
            {"distance": distances[dest]} if dest in distances else None)),
    )


def _price(id_item, price, terminal):
    return {"id_item": id_item, "price_buy": price, "id_terminal": terminal, "terminal_name": f"Shop {terminal}"}


def test_candidates_for_port_filters_sold_items_and_checks_the_wikis_size():
    async def run():
        catalog = [
            {"id": 1, "uuid": "u1", "category": "Power Plants", "size": "1", "name": "Sold"},
            {"id": 2, "uuid": "u2", "category": "Power Plants", "size": "1", "name": "Unsold"},
            {"id": 3, "uuid": "u3", "category": "Power Plants", "size": "1", "name": "ReallyS3"},
            {"id": 4, "uuid": "u4", "category": "Power Plants", "size": "3", "name": "ReallyS1"},
        ]
        uex = _uex(catalog, [_price(1, 1000, 5), _price(3, 2000, 5), _price(4, 3000, 5)])
        wiki = _wiki({
            "u1": {"uuid": "u1", "name": "Sold", "size": 1},
            # UEX's size is wrong for both of these - the wiki's decides (confirmed live:
            # FullForce Pro listed S1, really S3; GUARD listed S1, really S3).
            "u3": {"uuid": "u3", "name": "ReallyS3", "size": 3},
            "u4": {"uuid": "u4", "name": "ReallyS1", "size": 1},
        })
        cog = ShipPartsFinder(NS(uex=uex), wiki_client=wiki, start_refresh=False)
        port = ShipPort(name="hp", port_type="PowerPlant", size_min=1, size_max=1)
        return await cog.candidates_for_port(port, limit=10)

    candidates = asyncio.run(run())
    assert [c["name"] for c in candidates] == ["Sold", "ReallyS1"]
    assert candidates[0]["_uex_id"] == 1 and candidates[0]["_price_buy"] == 1000.0


def test_candidates_for_port_keeps_a_part_the_wiki_has_no_detail_for():
    async def run():
        catalog = [{"id": 1, "uuid": "u1", "category": "Power Plants", "size": "1", "name": "X"}]
        cog = ShipPartsFinder(NS(uex=_uex(catalog, [_price(1, 500, 5)])), wiki_client=_wiki(), start_refresh=False)
        port = ShipPort(name="hp", port_type="PowerPlant", size_min=1, size_max=1)
        return await cog.candidates_for_port(port, limit=10)

    candidates = asyncio.run(run())
    assert [c["name"] for c in candidates] == ["X"], "falls back to UEX's own name and size, not dropped"
    assert candidates[0]["_detail_loaded"] is False


def test_candidates_for_port_leaves_out_a_missile_rack_the_wiki_cant_size():
    async def run():
        catalog = [
            {"id": 1, "uuid": "u1", "category": "Missile Racks", "size": "6", "name": "MSD-322"},
            {"id": 2, "uuid": "u2", "category": "Missile Racks", "size": "3", "name": "Unknown Rack"},
        ]
        uex = _uex(catalog, [_price(1, 4760, 5), _price(2, 100, 5)])
        wiki = _wiki({"u1": {"uuid": "u1", "name": "MSD-322", "size": 3}})
        cog = ShipPartsFinder(NS(uex=uex), wiki_client=wiki, start_refresh=False)
        port = ShipPort(name="hp_rack", port_type="MissileLauncher", size_min=3, size_max=3)
        return await cog.candidates_for_port(port, limit=10)

    # UEX lists every MSD rack as size 6, so its size is never trusted for racks.
    assert [c["name"] for c in asyncio.run(run())] == ["MSD-322"]


def test_candidates_for_port_sorts_closest_first_before_cutting_to_the_limit():
    async def run():
        catalog = [{"id": i, "uuid": f"u{i}", "category": "Coolers", "size": "1", "name": f"C{i}"} for i in (1, 2, 3)]
        # Item 3 is the most expensive but at the closest shop; item 1 has no known distance.
        uex = _uex(catalog, [_price(1, 100, 11), _price(2, 200, 12), _price(3, 900, 13)], distances={12: 40.0, 13: 5.0})
        wiki = _wiki({f"u{i}": {"uuid": f"u{i}", "name": f"C{i}", "size": 1} for i in (1, 2, 3)})
        cog = ShipPartsFinder(NS(uex=uex), wiki_client=wiki, start_refresh=False)
        port = ShipPort(name="hp", port_type="Cooler", size_min=1, size_max=1)
        return await cog.candidates_for_port(port, limit=2, origin_id=99)

    candidates = asyncio.run(run())
    assert [c["name"] for c in candidates] == ["C3", "C2"], "the closest shop survives the cut; unknown distance goes last"
    assert candidates[0]["_distance_gm"] == 5.0


def test_candidates_for_port_keeps_loading_details_until_the_limit_is_filled():
    async def run():
        # The 9 closest parts all turn out (by the wiki's size) not to fit; the 10th does.
        catalog = [{"id": i, "uuid": f"u{i}", "category": "Coolers", "size": "1", "name": f"C{i}"} for i in range(10)]
        uex = _uex(catalog, [_price(i, 100, 100 + i) for i in range(10)], distances={100 + i: float(i) for i in range(10)})
        wiki = _wiki({f"u{i}": {"uuid": f"u{i}", "name": f"C{i}", "size": 1 if i == 9 else 2} for i in range(10)})
        cog = ShipPartsFinder(NS(uex=uex), wiki_client=wiki, start_refresh=False)
        port = ShipPort(name="hp", port_type="Cooler", size_min=1, size_max=1)
        return await cog.candidates_for_port(port, limit=1, origin_id=99)

    assert [c["name"] for c in asyncio.run(run())] == ["C9"]


def test_candidates_for_port_leaves_out_another_ships_own_parts():
    async def run():
        catalog = [
            {"id": 1, "uuid": "u1", "category": "Turrets", "size": "4", "name": "Reliant Toshima Turret"},
            {"id": 2, "uuid": "u2", "category": "Turrets", "size": "4", "name": "VariPuck S4 Gimbal Mount"},
        ]
        uex = _uex(catalog, [_price(1, 100, 5), _price(2, 200, 5)])
        wiki = _wiki(
            {
                "u1": {"uuid": "u1", "name": "Reliant Toshima Turret", "size": 4, "required_tags": ["MISC_Reliant_Base"]},
                # Confirmed live: UEX's uuid for the generic shop VariPuck S4 is the wiki's
                # Polaris-only variant, which would both fail the tag check and carry the
                # wrong variant's stats.
                "u2": {"uuid": "u2", "name": "VariPuck S4 Gimbal Mount", "size": 4, "required_tags": ["RSI_Polaris"],
                       "turret": {"mounts": 1, "min_size": 4, "max_size": 4}},
            },
            variants={
                "Reliant Toshima Turret": [{"name": "Reliant Toshima Turret", "required_tags": ["MISC_Reliant_Base"]}],
                "VariPuck S4 Gimbal Mount": [
                    {"uuid": "polaris", "name": "VariPuck S4 Gimbal Mount", "size": 4, "required_tags": ["RSI_Polaris"]},
                    {"uuid": "generic", "name": "VariPuck S4 Gimbal Mount", "size": 4, "required_tags": [],
                     "turret": {"mounts": 1, "min_size": 3, "max_size": 3}},
                ],
            },
        )
        cog = ShipPartsFinder(NS(uex=uex), wiki_client=wiki, start_refresh=False)
        port = ShipPort(name="hardpoint_weapon_class2_nose", port_type="Turret", size_min=4, size_max=4,
                        accepts_guns=True, tags=frozenset({"AEGS_Avenger_Base"}))
        return await cog.candidates_for_port(port, category="Turrets", limit=10)

    candidates = asyncio.run(run())
    assert [c["name"] for c in candidates] == ["VariPuck S4 Gimbal Mount"]
    assert candidates[0]["uuid"] == "generic" and candidates[0]["turret"]["max_size"] == 3, \
        "the fitting variant's own record replaces the ship-specific one"
    assert candidates[0]["_price_buy"] == 200.0, "UEX's own price/shop fields survive the swap"


def test_item_detail_falls_back_to_an_exact_name_lookup():
    async def run():
        # Most radars: UEX's uuid doesn't exist on the wiki, but the same name resolves.
        wiki = _wiki(by_name={"Fleming": {"uuid": "wiki-fleming", "name": "Fleming"}})
        cog = ShipPartsFinder(NS(), wiki_client=wiki, start_refresh=False)
        return await cog._item_detail_cached({"uuid": "uex-fleming", "name": "Fleming"}), wiki

    detail, wiki = asyncio.run(run())
    assert detail == {"uuid": "wiki-fleming", "name": "Fleming"}
    wiki.find_item_detail_by_name.assert_awaited_once_with("Fleming")


def test_item_detail_cache_reuses_a_warm_entry_and_caches_misses_too():
    async def run():
        wiki = _wiki({"u1": {"uuid": "u1", "name": "X"}})
        cog = ShipPartsFinder(NS(), wiki_client=wiki, start_refresh=False)
        first = await cog._item_detail_cached({"uuid": "u1", "name": "X"})
        second = await cog._item_detail_cached({"uuid": "u1", "name": "X"})
        await cog._item_detail_cached({"uuid": "missing", "name": "Nope"})
        await cog._item_detail_cached({"uuid": "missing", "name": "Nope"})
        return first, second, wiki

    first, second, wiki = asyncio.run(run())
    assert first == second == {"uuid": "u1", "name": "X"}
    assert wiki.get_item_detail.await_count == 2, "one for u1, one for the miss - neither repeated"
    wiki.find_item_detail_by_name.assert_awaited_once_with("Nope")


# -- _ports_for_vehicle: DB reference vs. live fallback --------------------------------------

def test_ports_for_vehicle_falls_back_to_a_live_wiki_lookup_when_reference_is_cold():
    async def run():
        db = NS(get_ship_parts_reference=AsyncMock(return_value=[]))
        wiki = NS(get_vehicle_loadout=AsyncMock(return_value=(
            [{"name": "hp_power", "type": "PowerPlant", "sizes": {"min": 1, "max": 1}}], ["AEGS_Avenger_Base"],
        )))
        cog = ShipPartsFinder(NS(db=db), wiki_client=wiki, start_refresh=False)
        return await cog._ports_for_vehicle({"id": 100, "name": "Avenger Stalker"}), wiki

    ports, wiki = asyncio.run(run())
    assert ports == [ShipPort(name="hp_power", port_type="PowerPlant", size_min=1, size_max=1,
                              tags=frozenset({"AEGS_Avenger_Base"}))]
    wiki.get_vehicle_loadout.assert_awaited_once_with("Avenger Stalker")


def test_ports_for_vehicle_retries_with_uexs_full_name():
    async def run():
        # The wiki calls it 'MISC Reliant Tana'; UEX's `name` is 'Reliant Tana' and its
        # `name_full` matches the wiki's.
        db = NS(get_ship_parts_reference=AsyncMock(return_value=[]))
        loadouts = {"MISC Reliant Tana": ([{"name": "hp_power", "type": "PowerPlant", "sizes": {"min": 1, "max": 1}}], [])}
        wiki = NS(get_vehicle_loadout=AsyncMock(side_effect=lambda name: loadouts.get(name, ([], []))))
        cog = ShipPartsFinder(NS(db=db), wiki_client=wiki, start_refresh=False)
        return await cog._ports_for_vehicle({"id": 7, "name": "Reliant Tana", "name_full": "MISC Reliant Tana"}), wiki

    ports, wiki = asyncio.run(run())
    assert [p.name for p in ports] == ["hp_power"]
    assert [c.args[0] for c in wiki.get_vehicle_loadout.await_args_list] == ["Reliant Tana", "MISC Reliant Tana"]


def test_ports_for_vehicle_prefers_the_warm_db_reference_over_a_live_call():
    async def run():
        db = NS(get_ship_parts_reference=AsyncMock(return_value=[
            {"port_name": "hp_power", "port_type": "PowerPlant", "size_min": 1, "size_max": 1,
             "accepts_guns": 0, "port_tags": "AEGS_Avenger_Base"},
        ]))
        wiki = NS(get_vehicle_loadout=AsyncMock())
        cog = ShipPartsFinder(NS(db=db), wiki_client=wiki, start_refresh=False)
        return await cog._ports_for_vehicle({"id": 100, "name": "Avenger Stalker"}), wiki

    ports, wiki = asyncio.run(run())
    assert ports == [ShipPort(name="hp_power", port_type="PowerPlant", size_min=1, size_max=1,
                              tags=frozenset({"AEGS_Avenger_Base"}))]
    wiki.get_vehicle_loadout.assert_not_awaited()


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


def _detail(name, uuid="u1", price=1000.0, terminal_id=1, terminal_name="Platinum Bay - HUR-L5"):
    return {
        "uuid": uuid, "name": name, "type": "PowerPlant", "size": 1, "_uex_id": 1, "_detail_loaded": True,
        "_price_buy": price, "_id_terminal": terminal_id, "_terminal_name": terminal_name,
    }


def test_show_category_defers_before_the_slow_candidate_lookup():
    async def run():
        seen_defer_count = None

        async def slow_candidates(port, *, category=None, limit=None, origin_id=None):
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

        async def track(port, *, category=None, limit=None, origin_id=None):
            calls.append((port.name, category))
            return [_detail("VariPuck S3")]

        cog = NS(candidates_for_port=track)
        left = ShipPort(name="hardpoint_turret_left", port_type="Turret", size_min=3, size_max=3)
        nose = ShipPort(name="hardpoint_turret_nose", port_type="Turret", size_min=4, size_max=4)
        view = ship_parts_finder.PartsBrowserView(cog, {"id": 100, "name": "Avenger Stalker"}, (1, "Origin"),
                                                    {"Turrets": [left, nose]})
        view.category = "Turrets"
        await view.show_slot(_component_interaction(), nose)
        return view, calls

    view, calls = asyncio.run(run())
    assert calls == [("hardpoint_turret_nose", "Turrets")]
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


def test_list_heading_drops_a_slot_name_that_only_repeats_the_category():
    radar = ShipPort(name="hardpoint_radar", port_type="Radar", size_min=1, size_max=1)
    cooler = ShipPort(name="hardpoint_cooler_left", port_type="Cooler", size_min=1, size_max=1)
    assert ship_parts_finder._list_heading("Radar", radar) == "Radar (S1)"
    assert ship_parts_finder._list_heading("Coolers", cooler) == "Coolers · Cooler Left (S1)"


def test_part_select_options_carry_price_shop_and_distance():
    part = dict(_detail("PowerBolt"), _distance_gm=13.0)
    assert ship_parts_finder._part_option_description(part) == "1,000 aUEC · HUR-L5 (Platinum Bay) · 13.0 Gm"


def test_selected_candidate_is_visibly_marked_in_the_rendered_text():
    cog = NS()
    port = ShipPort(name="hardpoint_power_plant", port_type="PowerPlant", size_min=1, size_max=1)
    view = ship_parts_finder.PartsBrowserView(cog, {"id": 100, "name": "Avenger Stalker"}, (1, "Origin"),
                                                {"Power Plants": [port]})
    view.category = "Power Plants"
    view.selected_port = port
    a, b = _detail("PowerBolt", uuid="ua"), _detail("Atlas", uuid="ub")
    view._set_candidates([a, b])
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
    assert "Category: **Gun Mounts**" in summary
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
    view._set_candidates([_detail("Atlas", uuid="ua")])
    view.selected_candidate = view.candidates[0]
    summary = view._selection_summary()
    assert "Category: **Power Plants**" in summary
    assert "Slot:" not in summary  # single-port category never shows a redundant slot line
    assert "Part: **Atlas**" in summary
    assert summary in view.text()


# -- _attach_distances ------------------------------------------------------------------------

def test_attach_distances_sets_known_distances_and_none_for_unknown():
    async def run():
        uex = NS(get_terminal_distance=AsyncMock(side_effect=lambda origin, dest: {2: {"distance": 5.0}, 3: {"distance": 1.0}}.get(dest)))
        cog = ShipPartsFinder(NS(uex=uex), wiki_client=NS(), start_refresh=False)
        parts = [_detail("Far", terminal_id=2), _detail("Near", terminal_id=3), _detail("Unknown", terminal_id=4)]
        await cog._attach_distances(parts, origin_id=1)
        return parts

    result = asyncio.run(run())
    assert [d["_distance_gm"] for d in result] == [5.0, 1.0, None]


def test_attach_distances_treats_the_origin_terminal_itself_as_zero():
    async def run():
        uex = NS(get_terminal_distance=AsyncMock())
        cog = ShipPartsFinder(NS(uex=uex), wiki_client=NS(), start_refresh=False)
        here = _detail("Here", terminal_id=1)
        await cog._attach_distances([here], origin_id=1)
        return [here], uex

    result, uex = asyncio.run(run())
    assert result[0]["_distance_gm"] == 0.0
    uex.get_terminal_distance.assert_not_awaited()


def test_candidates_for_port_attaches_distances_only_when_origin_is_given():
    async def run():
        catalog = [{"id": 1, "uuid": "u1", "category": "Power Plants", "size": "1", "name": "X"}]
        uex = _uex(catalog, [_price(1, 100, 9)], distances={9: 2.5})
        wiki = _wiki({"u1": {"uuid": "u1", "name": "X", "size": 1}})
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
    assert ship_parts_finder._format_port_label(port) == "Left Wing Gun (S3)"


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


# -- ranked by key stat, paged (no part cut off, dropdown never past one page) -------------

def _qd(name, speed, terminal=1, price=1000.0):
    return {"uuid": name, "name": name, "type": "QuantumDrive", "size": 1, "_uex_id": 1, "_detail_loaded": True,
            "quantum_drive": {"standard_jump": {"drive_speed": speed, "drive_speed_formatted": f"{speed / 1e6:.1f} Mm/s"}},
            "_price_buy": price, "_id_terminal": terminal, "_terminal_name": "Platinum Bay - HUR-L5"}


def test_candidates_for_port_ranks_by_the_key_stat_not_distance():
    async def run():
        catalog = [{"id": i, "uuid": f"u{i}", "category": "Quantum Drives", "size": "1", "name": f"Q{i}"} for i in (1, 2, 3)]
        # Q1 is the closest shop but the slowest drive; Q3 the fastest but farthest.
        uex = _uex(catalog, [_price(1, 100, 11), _price(2, 100, 12), _price(3, 100, 13)],
                   distances={11: 5.0, 12: 10.0, 13: 40.0})
        speeds = {1: 188e6, 2: 259e6, 3: 629e6}
        wiki = _wiki({f"u{i}": {"uuid": f"u{i}", "name": f"Q{i}", "size": 1, "type": "QuantumDrive",
                                "quantum_drive": {"standard_jump": {"drive_speed": speeds[i]}}} for i in (1, 2, 3)})
        cog = ShipPartsFinder(NS(uex=uex), wiki_client=wiki, start_refresh=False)
        port = ShipPort(name="hp_qd", port_type="QuantumDrive", size_min=1, size_max=1)
        return await cog.candidates_for_port(port, origin_id=99)

    assert [c["name"] for c in asyncio.run(run())] == ["Q3", "Q2", "Q1"]


def test_equal_stats_fall_back_to_the_closer_shop():
    parts = [_qd("Far", 500e6, terminal=2), _qd("Near", 500e6, terminal=3), _qd("NoDetail", 0)]
    parts[0]["_distance_gm"], parts[1]["_distance_gm"] = 30.0, 5.0
    del parts[2]["quantum_drive"]
    assert [p["name"] for p in sorted(parts, key=ship_parts_finder._rank_key)] == ["Near", "Far", "NoDetail"]


def _paged_view(count):
    port = ShipPort(name="hp_qd", port_type="QuantumDrive", size_min=1, size_max=1)
    view = ship_parts_finder.PartsBrowserView(NS(), {"id": 100, "name": "Perseus"}, (1, "Origin"),
                                                {"Quantum Drives": [port]})
    view.category, view.selected_port = "Quantum Drives", port
    view._set_candidates([_qd(f"QD{i}", 600e6 - i * 1e6) for i in range(count)])
    return view


def _page_buttons(view):
    return [c for c in view.children if isinstance(c, ship_parts_finder._PageButton)]


def test_a_big_slot_is_paged_and_each_dropdown_holds_only_its_page():
    view = _paged_view(30)
    assert len(view.pages) > 1 and sum(len(p) for p in view.pages) == 30, "every part is on some page"
    select = next(c for c in view.children if isinstance(c, ship_parts_finder._PartSelect))
    assert len(select.options) == len(view.pages[0]) <= 6, "far below Discord's 25-option limit"
    prev, nxt = _page_buttons(view)
    assert prev.disabled and not nxt.disabled
    text = view.text()
    assert f"Page 1 of {len(view.pages)}" in text and "30 parts, best quantum speed first" in text
    assert len(text) < 2000


def test_turning_the_page_swaps_the_list_and_dropdown_and_keeps_the_pick():
    async def run():
        view = _paged_view(14)
        view.selected_candidate = view.pages[0][1]
        interaction = _component_interaction()
        await view.turn_page(interaction, +1)
        return view, interaction

    view, interaction = asyncio.run(run())
    assert view.page == 1
    select = next(c for c in view.children if isinstance(c, ship_parts_finder._PartSelect))
    assert [o.label for o in select.options] == [c["name"] for c in view.pages[1]]
    content = interaction.response.edit_message.await_args.kwargs["content"]
    assert "Page 2 of" in content and "Part: **QD1**" in content, "the pick from page 1 is still named"
    assert "✅" not in content, "it isn't marked on a page it isn't on"
    prev, nxt = _page_buttons(view)
    assert not prev.disabled


def test_a_single_page_has_no_page_buttons():
    view = _paged_view(3)
    assert len(view.pages) == 1 and _page_buttons(view) == []
    assert "Page 1 of" not in view.text()
