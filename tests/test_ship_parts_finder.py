"""Ship Parts Finder cog (in design): the shopping-thread service/view layer
(bot/cogs/ship_parts_finder.py), mirroring bot/cogs/blueprint_planner.py's own test
harness, plus the candidate-resolution pipeline end to end against fake UEX/wiki clients."""
import asyncio
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock

from cryptography.fernet import Fernet

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
    assert "**PowerBolt**" in line and "(S1)" in line and "Grade C" in line and "Lightning Power Ltd." in line
    assert "18,701 aUEC @ Dumper's Depot - Area 18" in line
    assert "power_segment_generation: 14" in line


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
        interaction, 100, "Avenger Stalker", "Power Plants",
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
            _interaction(channel, interaction_id=301), 100, "Avenger Stalker", "Coolers",
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
            _interaction(channel, interaction_id=301), 100, "Avenger Stalker", "Power Plants",
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
            {"vehicle_name": "Avenger Stalker", "category": "Power Plants", "item_name": "PowerBolt",
             "price_buy": 19998.0, "terminal_name": "Platinum Bay - HUR-L5"},
            {"vehicle_name": "Cutlass Black", "category": "Shield Generators", "item_name": "Shimmer",
             "price_buy": None, "terminal_name": None},
        ]))
        return await ShipPartsShoppingService(NS(db=db)).render(1, 10)

    pages = asyncio.run(run())
    text = "\n".join(pages)
    assert "**Avenger Stalker**" in text and "**Cutlass Black**" in text
    assert "PowerBolt" in text and "19,998 aUEC" in text
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
