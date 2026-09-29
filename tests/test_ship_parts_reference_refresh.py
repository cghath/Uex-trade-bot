"""Audit REL-11: the ship-slot reference refresh re-read every ship from the wiki on every
start (~380 requests in 15 seconds, three times on one morning of deploys), and a ship the
wiki answered with no slots lost its saved ones. It now asks only about ships that are due,
spaced out, keeps saved slots on an empty answer, and stops early when the wiki is down."""
import asyncio
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock

from cryptography.fernet import Fernet

from bot.cogs import ship_parts_finder
from bot.cogs.ship_parts_finder import ShipPartsFinder
from bot.db.database import Database
from bot.uex.ship_parts import ShipPort
from bot.wiki_api import WikiApiError, WikiUnavailableError

VEHICLES = [{"id": 1, "name": "Perseus"}, {"id": 2, "name": "Cutlass Black"}, {"id": 3, "name": "Reliant Tana"}]
PORT = ShipPort(name="hardpoint_power_plant", port_type="PowerPlant", size_min=2, size_max=2)
SAVED = [{"name": "hardpoint_cooler", "port_type": "Cooler", "size_min": 2, "size_max": 2}]


async def _cog(tmp_path, *, wiki_ports, vehicles=VEHICLES):
    db = Database(tmp_path / "ref.sqlite", Fernet(Fernet.generate_key()))
    await db.init()
    uex = NS(get_vehicles=AsyncMock(return_value=vehicles))
    cog = ShipPartsFinder(NS(db=db, uex=uex), wiki_client=NS(), start_refresh=False)
    cog._wiki_ports = wiki_ports
    return cog, db


def _no_pause(monkeypatch):
    pauses = []

    async def pause(seconds):
        pauses.append(seconds)

    monkeypatch.setattr(ship_parts_finder.asyncio, "sleep", pause)
    return pauses


def _asked(wiki_ports):
    return [call.args[0]["name"] for call in wiki_ports.await_args_list]


def test_a_restart_only_asks_the_wiki_about_ships_that_are_due(tmp_path, monkeypatch):
    _no_pause(monkeypatch)

    async def run():
        wiki = AsyncMock(return_value=[PORT])
        cog, db = await _cog(tmp_path, wiki_ports=wiki)
        await cog.refresh_reference.coro(cog)          # first run: every ship
        first = _asked(wiki)
        wiki.reset_mock()
        await cog.refresh_reference.coro(cog)          # a restart soon after: nothing due
        return first, _asked(wiki), db

    first, second, db = asyncio.run(run())
    assert first == ["Perseus", "Cutlass Black", "Reliant Tana"]
    assert second == []


def test_a_ship_last_refreshed_over_a_day_ago_is_asked_about_again(tmp_path, monkeypatch):
    _no_pause(monkeypatch)

    async def run():
        wiki = AsyncMock(return_value=[PORT])
        cog, db = await _cog(tmp_path, wiki_ports=wiki)
        await cog.refresh_reference.coro(cog)
        async with db.connect() as conn:
            await conn.execute("UPDATE ship_parts_reference_status SET refreshed_at=datetime('now', '-25 hours') "
                               "WHERE id_vehicle=2")
            await conn.commit()
        wiki.reset_mock()
        await cog.refresh_reference.coro(cog)
        return _asked(wiki)

    assert asyncio.run(run()) == ["Cutlass Black"]


def test_requests_are_spaced_out(tmp_path, monkeypatch):
    pauses = _no_pause(monkeypatch)

    async def run():
        cog, _ = await _cog(tmp_path, wiki_ports=AsyncMock(return_value=[PORT]))
        await cog.refresh_reference.coro(cog)

    asyncio.run(run())
    assert pauses == [ship_parts_finder.WIKI_CRAWL_SPACING_SECONDS] * 2  # between 3 ships


def test_an_empty_wiki_answer_keeps_a_ships_saved_slots(tmp_path, monkeypatch):
    _no_pause(monkeypatch)

    async def run():
        cog, db = await _cog(tmp_path, wiki_ports=AsyncMock(return_value=[]), vehicles=VEHICLES[:1])
        await db.replace_ship_parts_reference(1, "Perseus", SAVED)
        await cog.refresh_reference.coro(cog)
        return await db.get_ship_parts_reference(1), await db.get_fresh_ship_parts_vehicles(24)

    rows, fresh = asyncio.run(run())
    assert [r["port_name"] for r in rows] == ["hardpoint_cooler"]
    assert fresh == {1}  # asked again tomorrow, not every hour


def test_a_ship_the_wiki_doesnt_have_is_not_asked_about_every_hour(tmp_path, monkeypatch):
    _no_pause(monkeypatch)

    async def run():
        wiki = AsyncMock(return_value=[])
        cog, db = await _cog(tmp_path, wiki_ports=wiki, vehicles=VEHICLES[:1])
        await cog.refresh_reference.coro(cog)
        await cog.refresh_reference.coro(cog)
        return wiki.await_count, await db.get_ship_parts_reference(1)

    count, rows = asyncio.run(run())
    assert count == 1 and rows == []


def test_new_slots_still_replace_the_saved_ones(tmp_path, monkeypatch):
    _no_pause(monkeypatch)

    async def run():
        cog, db = await _cog(tmp_path, wiki_ports=AsyncMock(return_value=[PORT]), vehicles=VEHICLES[:1])
        await db.replace_ship_parts_reference(1, "Perseus", SAVED)
        await cog.refresh_reference.coro(cog)
        return await db.get_ship_parts_reference(1)

    assert [r["port_name"] for r in asyncio.run(run())] == ["hardpoint_power_plant"]


def test_the_crawl_stops_when_the_wiki_is_down_and_leaves_the_rest_due(tmp_path, monkeypatch):
    _no_pause(monkeypatch)
    many = [{"id": i, "name": f"Ship {i}"} for i in range(1, 11)]

    async def run():
        wiki = AsyncMock(side_effect=WikiUnavailableError("down"))
        cog, db = await _cog(tmp_path, wiki_ports=wiki, vehicles=many)
        await cog.refresh_reference.coro(cog)
        return wiki.await_count, await db.get_fresh_ship_parts_vehicles(24)

    count, fresh = asyncio.run(run())
    assert count == ship_parts_finder.WIKI_CRAWL_MAX_OUTAGES
    assert fresh == set()  # nothing marked, so the next hourly check tries again


def test_a_definite_wiki_error_keeps_saved_slots_and_waits_a_day_without_stopping_the_crawl(tmp_path, monkeypatch):
    _no_pause(monkeypatch)

    async def run():
        async def ports(vehicle):
            if vehicle["name"] == "Cutlass Black":
                raise WikiApiError("identity mismatch")
            return [PORT]

        wiki = AsyncMock(side_effect=ports)
        cog, db = await _cog(tmp_path, wiki_ports=wiki)
        await db.replace_ship_parts_reference(2, "Cutlass Black", SAVED)
        await cog.refresh_reference.coro(cog)
        await cog.refresh_reference.coro(cog)  # an hour later: nothing due
        return wiki.await_count, await db.get_fresh_ship_parts_vehicles(24), await db.get_ship_parts_reference(2)

    count, fresh, rows = asyncio.run(run())
    assert count == 3 and fresh == {1, 2, 3}
    assert [r["port_name"] for r in rows] == ["hardpoint_cooler"]


def test_an_unexpected_failure_leaves_the_ship_due_for_the_next_check(tmp_path, monkeypatch):
    _no_pause(monkeypatch)

    async def run():
        cog, db = await _cog(tmp_path, wiki_ports=AsyncMock(return_value=[PORT]), vehicles=VEHICLES[:2])
        real = db.replace_ship_parts_reference

        async def flaky(id_vehicle, name, ports):
            if id_vehicle == 1:
                raise RuntimeError("database is locked")
            await real(id_vehicle, name, ports)

        db.replace_ship_parts_reference = flaky
        await cog.refresh_reference.coro(cog)
        return await db.get_fresh_ship_parts_vehicles(24)

    assert asyncio.run(run()) == {2}


def test_the_outage_count_resets_after_a_ship_the_wiki_answers(tmp_path, monkeypatch):
    _no_pause(monkeypatch)
    limit = ship_parts_finder.WIKI_CRAWL_MAX_OUTAGES
    ships = [{"id": i, "name": f"Ship {i}"} for i in range(1, 2 * limit + 2)]
    answered = {limit}  # one success just before the limit would be reached

    async def run():
        async def ports(vehicle):
            if vehicle["id"] in answered:
                return [PORT]
            raise WikiUnavailableError("flaky")

        wiki = AsyncMock(side_effect=ports)
        cog, _ = await _cog(tmp_path, wiki_ports=wiki, vehicles=ships)
        await cog.refresh_reference.coro(cog)
        return wiki.await_count

    # limit-1 failures, a success, then `limit` failures in a row stop it.
    assert asyncio.run(run()) == 2 * limit


def test_ships_without_a_usable_id_or_name_are_skipped(tmp_path, monkeypatch):
    _no_pause(monkeypatch)

    async def run():
        wiki = AsyncMock(return_value=[PORT])
        cog, _ = await _cog(tmp_path, wiki_ports=wiki,
                            vehicles=[{"id": None, "name": "Ghost"}, {"id": 4, "name": ""}, {"id": 5, "name": "Arrow"}])
        await cog.refresh_reference.coro(cog)
        return _asked(wiki)

    assert asyncio.run(run()) == ["Arrow"]


def test_the_refresh_checks_hourly_and_refreshes_daily():
    assert ship_parts_finder.REFERENCE_CHECK_HOURS == 1
    assert ship_parts_finder.REFERENCE_REFRESH_HOURS == 24
    assert ShipPartsFinder.refresh_reference.hours == 1
