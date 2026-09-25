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


def test_ship_parts_reference_stores_gun_compatibility_and_tags(tmp_path):
    async def run():
        db, _ = _db(tmp_path)
        await db.init()
        await db.replace_ship_parts_reference(100, "Avenger Titan", [
            {"name": "hp_nose", "port_type": "Turret", "size_min": 4, "size_max": 4,
             "accepts_guns": True, "port_tags": ["AEGS_Avenger_Base", "Nose_Tag"]},
            {"name": "hp_power", "port_type": "PowerPlant", "size_min": 1, "size_max": 1},
        ])
        return {row["port_name"]: row for row in await db.get_ship_parts_reference(100)}

    rows = asyncio.run(run())
    assert rows["hp_nose"]["accepts_guns"] == 1 and rows["hp_nose"]["port_tags"] == "AEGS_Avenger_Base Nose_Tag"
    assert rows["hp_power"]["accepts_guns"] == 0 and rows["hp_power"]["port_tags"] == ""


def test_an_existing_ship_parts_reference_table_gains_the_new_columns(tmp_path):
    import sqlite3

    async def run():
        db, path = _db(tmp_path)
        # The table as the previous release created it, with one collected row.
        con = sqlite3.connect(path)
        con.execute("""CREATE TABLE ship_parts_reference (
            id_vehicle INTEGER NOT NULL, vehicle_name TEXT NOT NULL, port_name TEXT NOT NULL,
            port_type TEXT NOT NULL, size_min INTEGER NOT NULL, size_max INTEGER NOT NULL,
            PRIMARY KEY (id_vehicle, port_name))""")
        con.execute("INSERT INTO ship_parts_reference VALUES (100, 'Avenger Titan', 'hp_nose', 'Turret', 4, 4)")
        con.commit()
        con.close()
        await db.init()
        return await db.get_ship_parts_reference(100)

    [row] = asyncio.run(run())
    # Until the next collector run rewrites it: no gun category, and no tags - which only
    # ever hides ship-specific parts, never offers one that doesn't fit.
    assert row["accepts_guns"] == 0 and row["port_tags"] == ""


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
