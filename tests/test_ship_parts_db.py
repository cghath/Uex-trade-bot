import asyncio
import sqlite3
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


def test_an_old_category_keyed_entries_table_is_migrated_to_the_port_name_key(tmp_path):
    # The exact table shape commit 370e232 created, before port_name joined the key.
    # CREATE TABLE IF NOT EXISTS alone left it in place and every lock-in failed with
    # "table ship_parts_shopping_entries has no column named port_name".
    path = tmp_path / "old.sqlite"
    with sqlite3.connect(path) as conn:
        conn.execute(
            """CREATE TABLE ship_parts_shopping_entries (
                user_id INTEGER NOT NULL, guild_id INTEGER NOT NULL, id_vehicle INTEGER NOT NULL,
                vehicle_name TEXT NOT NULL, category TEXT NOT NULL, id_item INTEGER NOT NULL,
                item_name TEXT NOT NULL, id_terminal INTEGER, terminal_name TEXT, price_buy REAL,
                locked_at TEXT NOT NULL,
                PRIMARY KEY (user_id, guild_id, id_vehicle, category)
            )"""
        )
        conn.execute(
            "INSERT INTO ship_parts_shopping_entries VALUES (1, 10, 100, 'Avenger Stalker', 'Power Plants', "
            "500, 'PowerBolt', 139, 'Platinum Bay - HUR-L5', 19998.0, '2026-09-23 00:00:00')"
        )

    async def run():
        db = Database(path, Fernet(Fernet.generate_key()))
        await db.init()
        # The pre-existing part survives, under a placeholder slot, not dropped.
        [old] = await db.get_ship_parts_entries(1, 10)
        assert (old["item_name"], old["port_name"]) == ("PowerBolt", "unknown_slot")
        # Locking a real slot now works, and doesn't collide with the placeholder row.
        await db.set_ship_parts_entry(
            1, 10, 100, "Avenger Stalker", "Power Plants", "hp_power", 501, "Atlas", 114,
            "Dumper's Depot - Area 18", 21000.0, "2026-09-25 00:00:00",
        )
        assert {e["port_name"] for e in await db.get_ship_parts_entries(1, 10)} == {"unknown_slot", "hp_power"}
        await db.remove_ship_parts_entry(1, 10, 100, "Power Plants", "unknown_slot")
        assert [e["item_name"] for e in await db.get_ship_parts_entries(1, 10)] == ["Atlas"]
        # Running init() again on the migrated database is a no-op.
        await Database(path, db._fernet).init()
        assert [e["item_name"] for e in await db.get_ship_parts_entries(1, 10)] == ["Atlas"]
    asyncio.run(run())


def test_ship_parts_entries_lock_one_slot_per_ship_per_port_and_survive_restart(tmp_path):
    async def run():
        db, path = _db(tmp_path)
        await db.init()
        await db.set_ship_parts_entry(
            1, 10, 100, "Avenger Stalker", "Power Plants", "hp_power", 500, "PowerBolt", 139,
            "Platinum Bay - HUR-L5", 19998.0, "2026-09-23 00:00:00",
        )
        entries = await db.get_ship_parts_entries(1, 10)
        assert len(entries) == 1 and entries[0]["item_name"] == "PowerBolt"

        # Re-locking the same port replaces it, not a second row.
        await db.set_ship_parts_entry(
            1, 10, 100, "Avenger Stalker", "Power Plants", "hp_power", 501, "Atlas", 114,
            "Dumper's Depot - Area 18", 21000.0, "2026-09-23 01:00:00",
        )
        entries = await db.get_ship_parts_entries(1, 10)
        assert len(entries) == 1 and entries[0]["item_name"] == "Atlas"

        # A second, distinct port in the SAME category is a second entry, not collapsed.
        await db.set_ship_parts_entry(
            1, 10, 100, "Avenger Stalker", "Turrets", "hp_turret_left", 600, "VariPuck S3", 115,
            "Dumper's Depot - GrimHEX", 5000.0, "2026-09-23 02:00:00",
        )
        await db.set_ship_parts_entry(
            1, 10, 100, "Avenger Stalker", "Turrets", "hp_turret_nose", 601, "VariPuck S4", 115,
            "Dumper's Depot - GrimHEX", 6000.0, "2026-09-23 02:00:00",
        )
        entries = await db.get_ship_parts_entries(1, 10)
        assert len(entries) == 3
        assert {e["port_name"] for e in entries if e["category"] == "Turrets"} == {"hp_turret_left", "hp_turret_nose"}

        # Scoped per user/guild.
        assert await db.get_ship_parts_entries(2, 10) == []
        assert await db.get_ship_parts_entries(1, 20) == []

        await db.set_ship_parts_thread(1, 10, 700, 800)

        db2 = Database(path, db._fernet)
        await db2.init()
        entries = await db2.get_ship_parts_entries(1, 10)
        assert len(entries) == 3
        assert (await db2.get_ship_parts_thread(1, 10))["message_id"] == 800
        assert (await db2.get_ship_parts_thread_owner(700))["user_id"] == 1

        # Removing one slot leaves the other two untouched.
        await db2.remove_ship_parts_entry(1, 10, 100, "Turrets", "hp_turret_left")
        entries = await db2.get_ship_parts_entries(1, 10)
        assert len(entries) == 2
        assert {e["port_name"] for e in entries} == {"hp_power", "hp_turret_nose"}

        # Removing a slot that isn't there is a harmless no-op, not an error.
        await db2.remove_ship_parts_entry(1, 10, 100, "Turrets", "hp_turret_left")
        assert len(await db2.get_ship_parts_entries(1, 10)) == 2

        await db2.clear_ship_parts_entries(1, 10)
        assert await db2.get_ship_parts_entries(1, 10) == []
        await db2.delete_ship_parts_thread(1, 10)
        assert await db2.get_ship_parts_thread(1, 10) is None
    asyncio.run(run())
