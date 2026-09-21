"""Blueprint snapshot persistence (bot/db/database.py) against a real temporary SQLite file.

The guarantee that matters: a snapshot is replaced whole or not at all. Everything else here
(round trip, restart, no stale rows) is the ordinary path; the failure tests inject a bad row at the
two points a real write could die - mid-missions and mid-pool - and read the database back afterwards.
"""
from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from datetime import datetime
from pathlib import Path

from cryptography.fernet import Fernet
import pytest

from bot.db.database import Database
from bot.uex.blueprints import BlueprintRef, parse_missions

FIXTURES = Path(__file__).parent / "fixtures"
MISSIONS = parse_missions(json.loads((FIXTURES / "blueprint_missions_sample.json").read_text(encoding="utf-8")))
STAMP = datetime(2026, 9, 18, 12, 30, 5)


def _db(tmp_path: Path, name: str = "blueprints.sqlite3") -> Database:
    return Database(tmp_path / name, Fernet(Fernet.generate_key()))


def _run(coro):
    return asyncio.run(coro)


def _snapshot(db: Database):
    async def read():
        return (
            await db.get_blueprint_snapshot_state(),
            await db.get_blueprint_refs(),
            {m.uuid: m for m in await db.get_blueprint_missions([r.uuid for r in await db.get_blueprint_refs()])},
        )
    return _run(read())


def test_no_snapshot_reads_as_empty_not_as_an_error(tmp_path):
    db = _db(tmp_path)

    async def run():
        await db.init()
        return await db.get_blueprint_snapshot_state(), await db.get_blueprint_refs(), await db.get_blueprint_missions(["nope"])

    assert _run(run()) == (None, [], [])


def test_a_snapshot_round_trips_missions_pools_and_state(tmp_path):
    db = _db(tmp_path)

    async def run():
        await db.init()
        counts = await db.replace_blueprint_snapshot(MISSIONS, game_version="4.10.0-LIVE.1", synced_at=STAMP)
        return counts, await db.get_blueprint_snapshot_state()

    counts, state = _run(run())
    distinct = {ref.uuid for m in MISSIONS for ref in m.pool}
    assert counts == (len(MISSIONS), len(distinct))
    assert (state.game_version, state.synced_at, state.mission_count, state.blueprint_count) == (
        "4.10.0-LIVE.1", STAMP, len(MISSIONS), len(distinct))

    _, refs, stored = _snapshot(db)
    assert {r.uuid for r in refs} == distinct
    assert set(stored) == {m.uuid for m in MISSIONS}
    for original in MISSIONS:
        got = stored[original.uuid]
        assert got.title == original.title and got.giver == original.giver
        assert got.illegal == original.illegal and got.reputation == original.reputation
        assert got.rank_name == original.rank_name and got.star_systems == original.star_systems
        assert {r.uuid for r in got.pool} == {r.uuid for r in original.pool}, "the FULL pool comes back, not just the searched blueprint"


def test_a_blueprint_lookup_returns_every_mission_that_can_award_it(tmp_path):
    db = _db(tmp_path)
    shared = next(ref for ref in MISSIONS[0].pool if sum(ref in m.pool for m in MISSIONS) >= 1)
    expected = {m.uuid for m in MISSIONS if shared in m.pool}

    async def run():
        await db.init()
        await db.replace_blueprint_snapshot(MISSIONS, game_version="v", synced_at=STAMP)
        return await db.get_blueprint_missions([shared.uuid])

    assert {m.uuid for m in _run(run())} == expected


def test_replacing_a_snapshot_leaves_no_stale_rows_from_the_old_one(tmp_path):
    db = _db(tmp_path)
    keep = MISSIONS[:2]

    async def run():
        await db.init()
        await db.replace_blueprint_snapshot(MISSIONS, game_version="old", synced_at=STAMP)
        await db.replace_blueprint_snapshot(keep, game_version="new", synced_at=datetime(2026, 9, 20))
        return await db.get_blueprint_snapshot_state()

    state = _run(run())
    _, refs, stored = _snapshot(db)
    assert set(stored) == {m.uuid for m in keep} and state.game_version == "new" and state.mission_count == 2
    assert {r.uuid for r in refs} == {ref.uuid for m in keep for ref in m.pool}, "blueprints only the old snapshot had are gone"


@pytest.mark.parametrize(
    "corrupt",
    [
        pytest.param(lambda m: replace(m, title=None), id="a-mission-row-fails-mid-insert"),
        pytest.param(lambda m: replace(m, pool=(BlueprintRef("bad-uuid", None),)), id="a-pool-row-fails-after-the-missions-were-written"),
    ],
)
def test_a_failed_replace_leaves_the_previous_snapshot_completely_intact(tmp_path, corrupt):
    """The core guarantee. The bad row is the LAST one, so by the time it fails the DELETEs and most of
    the new rows have already executed - the rollback is what restores the old data."""
    db = _db(tmp_path)
    new_batch = [replace(m, title=m.title + " (new)") for m in MISSIONS[:3]] + [corrupt(MISSIONS[3])]

    async def run():
        await db.init()
        await db.replace_blueprint_snapshot(MISSIONS, game_version="good", synced_at=STAMP)

    _run(run())
    before = _snapshot(db)

    with pytest.raises(Exception):
        _run(db.replace_blueprint_snapshot(new_batch, game_version="bad", synced_at=datetime(2027, 1, 1)))

    assert _snapshot(db) == before
    state = before[0]
    assert state.game_version == "good" and state.mission_count == len(MISSIONS)
    assert not any(m.title.endswith("(new)") for m in before[2].values())


def test_replacing_with_nothing_is_refused_and_changes_nothing(tmp_path):
    db = _db(tmp_path)
    _run(db.init())
    _run(db.replace_blueprint_snapshot(MISSIONS, game_version="good", synced_at=STAMP))
    before = _snapshot(db)
    with pytest.raises(ValueError):
        _run(db.replace_blueprint_snapshot([], game_version="empty", synced_at=STAMP))
    assert _snapshot(db) == before


def test_a_snapshot_survives_a_restart_and_repeated_initialisation(tmp_path):
    first = _db(tmp_path)
    _run(first.init())
    _run(first.replace_blueprint_snapshot(MISSIONS, game_version="4.10.0", synced_at=STAMP))
    before = _snapshot(first)

    restarted = Database(tmp_path / "blueprints.sqlite3", Fernet(Fernet.generate_key()))
    _run(restarted.init())
    _run(restarted.init())
    assert _snapshot(restarted) == before


def test_users_never_see_each_others_data_because_the_snapshot_holds_no_user_rows(tmp_path):
    """Blueprint data is global reference data. This pins that no user-keyed column snuck into the
    tables - the future personal-inventory table is where user isolation will need its own tests."""
    db = _db(tmp_path)
    _run(db.init())

    async def columns():
        async with db.connect() as conn:
            found = {}
            for table in ("blueprint_snapshot_state", "blueprint_missions", "blueprint_pool_entries"):
                rows = await (await conn.execute(f"PRAGMA table_info({table})")).fetchall()
                found[table] = {row["name"] for row in rows}
            return found

    for table, names in _run(columns()).items():
        assert not any("user" in n or "guild" in n for n in names), table
