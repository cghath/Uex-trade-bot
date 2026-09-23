import asyncio
from cryptography.fernet import Fernet
from bot.db.database import Database


def _db(tmp_path):
    path = tmp_path / "ship_parts.sqlite"
    return Database(path, Fernet(Fernet.generate_key())), path


def test_ship_parts_reference_is_replaced_wholesale_per_ship(tmp_path):
    async def run():
        db, _ = _db(tmp_path)
        await db.init()
        ports_a = [{"name": "hp_power", "port_type": "PowerPlant", "size_min": 1, "size_max": 1}]
        await db.replace_ship_parts_reference(100, "Avenger Stalker", ports_a)
        assert [row["port_name"] for row in await db.get_ship_parts_reference(100)] == ["hp_power"]

        # A second run for the same ship must replace, not accumulate.
        ports_b = [
            {"name": "hp_power", "port_type": "PowerPlant", "size_min": 1, "size_max": 1},
            {"name": "hp_cooler", "port_type": "Cooler", "size_min": 1, "size_max": 1},
        ]
        await db.replace_ship_parts_reference(100, "Avenger Stalker", ports_b)
        rows = await db.get_ship_parts_reference(100)
        assert sorted(row["port_name"] for row in rows) == ["hp_cooler", "hp_power"]

        # A different ship's rows are untouched.
        await db.replace_ship_parts_reference(200, "Cutlass Black", [
            {"name": "hp_shield", "port_type": "Shield", "size_min": 2, "size_max": 2},
        ])
        assert len(await db.get_ship_parts_reference(100)) == 2
        assert len(await db.get_ship_parts_reference(200)) == 1
    asyncio.run(run())


def test_ship_parts_entries_lock_one_slot_per_ship_per_category_and_survive_restart(tmp_path):
    async def run():
        db, path = _db(tmp_path)
        await db.init()
        await db.set_ship_parts_entry(
            1, 10, 100, "Avenger Stalker", "Power Plants", 500, "PowerBolt", 139,
            "Platinum Bay - HUR-L5", 19998.0, "2026-09-23 00:00:00",
        )
        entries = await db.get_ship_parts_entries(1, 10)
        assert len(entries) == 1 and entries[0]["item_name"] == "PowerBolt"

        # Re-locking the same slot replaces it, not a second row.
        await db.set_ship_parts_entry(
            1, 10, 100, "Avenger Stalker", "Power Plants", 501, "Atlas", 114,
            "Dumper's Depot - Area 18", 21000.0, "2026-09-23 01:00:00",
        )
        entries = await db.get_ship_parts_entries(1, 10)
        assert len(entries) == 1 and entries[0]["item_name"] == "Atlas"

        # Scoped per user/guild.
        assert await db.get_ship_parts_entries(2, 10) == []
        assert await db.get_ship_parts_entries(1, 20) == []

        await db.set_ship_parts_thread(1, 10, 700, 800)

        db2 = Database(path, db._fernet)
        await db2.init()
        entries = await db2.get_ship_parts_entries(1, 10)
        assert len(entries) == 1
        assert (await db2.get_ship_parts_thread(1, 10))["message_id"] == 800
        assert (await db2.get_ship_parts_thread_owner(700))["user_id"] == 1

        await db2.clear_ship_parts_entries(1, 10)
        assert await db2.get_ship_parts_entries(1, 10) == []
        await db2.delete_ship_parts_thread(1, 10)
        assert await db2.get_ship_parts_thread(1, 10) is None
    asyncio.run(run())
