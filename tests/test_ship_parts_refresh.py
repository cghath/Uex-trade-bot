"""/ship-parts-finder's ↻ Refresh button: the browsing view stops listening after 30 idle
minutes or a restart, and Refresh brings it back on the same message, ship, location and
category - without the player having to rerun the command."""
import asyncio
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock

import discord
from discord.ui.view import ViewStore

from bot.cogs import ship_parts_finder
from bot.cogs.ship_parts_finder import (
    EXPIRED_NOTE, REFRESH_HINT, LoadoutDoneButton, PartsBrowserView, RefreshBrowserButton, ShipPartsFinder,
    _RefreshStub,
    refresh_custom_id,
)
from bot.uex.exceptions import UexApiError
from bot.uex.ship_parts import ShipPort

VEHICLE = {"id": 100, "name": "Perseus"}
RADAR = ShipPort(name="hardpoint_radar", port_type="Radar", size_min=2, size_max=2)
LEFT = ShipPort(name="hardpoint_turret_left", port_type="Turret", size_min=3, size_max=3)
NOSE = ShipPort(name="hardpoint_turret_nose", port_type="Turret", size_min=4, size_max=4)
GROUPED = {"Radar": [RADAR], "Turrets": [LEFT, NOSE]}


def _detail(name):
    return {"uuid": name, "name": name, "type": "Radar", "size": 2, "_uex_id": 1, "_detail_loaded": True,
            "_price_buy": 1000.0, "_id_terminal": 5, "_terminal_name": "Omega Pro - New Babbage"}


def _refresh(view):
    return next(c for c in view.children if isinstance(c, _RefreshStub))


def _view(cog=None, grouped=GROUPED):
    return PartsBrowserView(cog or NS(), VEHICLE, (5, "Omega Pro"), grouped)


# ---- the button itself ----------------------------------------------------------------

def test_refresh_is_the_last_button_and_carries_ship_location_and_category():
    view = _view()
    assert view.children[-1] is _refresh(view)
    assert _refresh(view).custom_id == "ship-parts-browse:refresh:100:5:"
    view.category = "Radar"
    view._set_candidates([_detail("V801-12")])
    assert view.children[-1] is _refresh(view)
    assert _refresh(view).custom_id == "ship-parts-browse:refresh:100:5:Radar"
    assert REFRESH_HINT in view.text()


def test_refresh_custom_id_round_trips_through_the_registered_template():
    custom_id = refresh_custom_id(100, 5, "Quantum Drives")
    match = RefreshBrowserButton.__discord_ui_compiled_template__.fullmatch(custom_id)
    item = asyncio.run(RefreshBrowserButton.from_custom_id(NS(), NS(), match))
    assert (item.id_vehicle, item.id_terminal, item.category) == (100, 5, "Quantum Drives")
    blank = RefreshBrowserButton.__discord_ui_compiled_template__.fullmatch(refresh_custom_id(100, 5, None))
    assert asyncio.run(RefreshBrowserButton.from_custom_id(NS(), NS(), blank)).category is None


def test_a_browser_opened_without_a_location_refreshes_without_one():
    view = PartsBrowserView(NS(), VEHICLE, None, GROUPED)
    assert _refresh(view).custom_id == "ship-parts-browse:refresh:100::"
    match = RefreshBrowserButton.__discord_ui_compiled_template__.fullmatch(refresh_custom_id(100, None, "Radar"))
    item = asyncio.run(RefreshBrowserButton.from_custom_id(NS(), NS(), match))
    assert (item.id_vehicle, item.id_terminal, item.category) == (100, None, "Radar")
    assert refresh_custom_id(100, None, "x" * 120) == "ship-parts-browse:refresh:100::"


def test_an_overlong_category_is_dropped_rather_than_breaking_discords_100_char_limit():
    custom_id = refresh_custom_id(100, 5, "x" * 120)
    assert len(custom_id) <= 100 and custom_id.endswith(":")


def test_a_closing_browser_does_not_unregister_refresh_for_every_other_message():
    """discord.py drops a view's DynamicItem patterns bot-wide when that view closes, which
    is why the live view carries a non-dispatchable stub instead of the DynamicItem."""
    async def run():
        store = ViewStore(NS())
        store.add_dynamic_items(RefreshBrowserButton)
        view = _view()
        store.add_view(view, message_id=7)
        store.remove_view(view)  # what a timeout or stop() does
        return store

    store = asyncio.run(run())
    assert RefreshBrowserButton.__discord_ui_compiled_template__ in store._dynamic_items
    assert not _refresh(_view()).is_dispatchable()


def test_the_cog_registers_and_unregisters_the_refresh_handler():
    calls = []
    bot = NS(add_view=lambda view: None,
             add_dynamic_items=lambda *items: calls.append(("add", items)),
             remove_dynamic_items=lambda *items: calls.append(("remove", items)))
    cog = ShipPartsFinder(bot, wiki_client=NS(aclose=AsyncMock()), start_refresh=False)
    asyncio.run(cog.cog_load())
    cog.cog_unload()
    assert calls == [("add", (RefreshBrowserButton, LoadoutDoneButton)),
                     ("remove", (RefreshBrowserButton, LoadoutDoneButton))]


def test_the_button_hands_the_click_to_the_cog():
    refresh = AsyncMock()
    interaction = NS(client=NS(get_cog=lambda name: NS(refresh_browser=refresh) if name == "ShipPartsFinder" else None))
    asyncio.run(RefreshBrowserButton(100, 5, "Radar").callback(interaction))
    refresh.assert_awaited_once_with(interaction, 100, 5, "Radar")


# ---- going idle ---------------------------------------------------------------------

def test_an_idle_browser_greys_out_everything_but_refresh_and_says_so():
    async def run():
        cog = NS(_browsers={})
        view = _view(cog)
        view.category = "Radar"
        view._set_candidates([_detail("V801-12")])
        view.message = NS(id=7, edit=AsyncMock())
        cog._browsers[7] = view
        await view.on_timeout()
        return cog, view

    cog, view = asyncio.run(run())
    content = view.message.edit.await_args.kwargs["content"]
    assert EXPIRED_NOTE in content and REFRESH_HINT not in content
    assert all(c.disabled for c in view.children if not isinstance(c, _RefreshStub))
    assert not _refresh(view).disabled
    assert cog._browsers == {}


def test_a_browser_already_replaced_by_a_refresh_leaves_the_message_alone():
    async def run():
        cog = NS(_browsers={})
        old, new = _view(cog), _view(cog)
        old.message = NS(id=7, edit=AsyncMock())
        cog._browsers[7] = new
        await old.on_timeout()
        return cog, old, new

    cog, old, new = asyncio.run(run())
    old.message.edit.assert_not_awaited()
    assert cog._browsers[7] is new


def test_an_idle_note_that_cant_be_posted_is_logged_not_raised():
    async def run():
        cog = NS(_browsers={})
        view = _view(cog)
        response = NS(status=403, reason="Forbidden")
        view.message = NS(id=7, edit=AsyncMock(side_effect=discord.HTTPException(response, "archived")))
        cog._browsers[7] = view
        await view.on_timeout()

    asyncio.run(run())


# ---- refreshing -------------------------------------------------------------------------

def _cog(*, candidates=None, vehicles=None):
    uex = NS(get_vehicles=AsyncMock(return_value=[VEHICLE] if vehicles is None else vehicles))
    cog = ShipPartsFinder(NS(uex=uex, db=NS()), wiki_client=NS(), start_refresh=False)
    cog._ports_for_vehicle = AsyncMock(return_value=[RADAR, LEFT, NOSE])
    cog.turret_gun_lookups_unanswered = lambda ports: 0
    cog.candidates_for_port = candidates or AsyncMock(return_value=[_detail("V801-12"), _detail("Vigilance")])
    return cog


def _click(message_id=7):
    return NS(
        message=NS(id=message_id),
        response=NS(defer=AsyncMock()),
        followup=NS(send=AsyncMock()),
        edit_original_response=AsyncMock(),
    )


def test_refresh_rebuilds_the_same_message_back_on_the_same_category():
    async def run():
        cog = _cog()
        old = _view(cog)
        old.message = NS(id=7)
        cog._browsers[7] = old
        interaction = _click()
        await cog.refresh_browser(interaction, 100, 5, "Radar")
        return cog, old, interaction

    cog, old, interaction = asyncio.run(run())
    interaction.response.defer.assert_awaited_once()
    new = interaction.edit_original_response.await_args.kwargs["view"]
    assert new is cog._browsers[7] and new is not old and old.is_finished()
    assert new.category == "Radar" and [c["name"] for c in new.candidates] == ["V801-12", "Vigilance"]
    assert "V801-12" in interaction.edit_original_response.await_args.kwargs["content"]
    assert next(o for o in new.category_select.options if o.default).value == "Radar"
    port, kwargs = cog.candidates_for_port.await_args.args[0], cog.candidates_for_port.await_args.kwargs
    assert port is RADAR and kwargs["origin_id"] == 5


def test_refresh_without_a_location_rebuilds_without_one():
    async def run():
        cog = _cog()
        interaction = _click()
        await cog.refresh_browser(interaction, 100, None, "Radar")
        return cog, interaction

    cog, interaction = asyncio.run(run())
    new = interaction.edit_original_response.await_args.kwargs["view"]
    assert new.origin_terminal is None and new.category == "Radar"
    assert cog.candidates_for_port.await_args.kwargs["origin_id"] is None
    assert _refresh(new).custom_id == "ship-parts-browse:refresh:100::Radar"


def test_refresh_defers_before_its_slow_lookups():
    async def run():
        seen = {}
        cog = _cog()

        async def get_vehicles():
            seen["deferred"] = interaction.response.defer.await_count
            return [VEHICLE]

        cog.bot.uex.get_vehicles = get_vehicles
        interaction = _click()
        await cog.refresh_browser(interaction, 100, 5, None)
        return seen

    assert asyncio.run(run())["deferred"] == 1


def test_refresh_after_a_restart_works_with_no_live_view_to_replace():
    async def run():
        cog = _cog()
        interaction = _click()
        await cog.refresh_browser(interaction, 100, 5, None)
        return cog, interaction

    cog, interaction = asyncio.run(run())
    new = interaction.edit_original_response.await_args.kwargs["view"]
    assert cog._browsers[7] is new and new.category is None and new.message.id == 7


def test_refresh_on_a_multi_slot_category_asks_for_the_slot_again():
    async def run():
        cog = _cog(candidates=AsyncMock(side_effect=AssertionError("no slot picked yet")))
        interaction = _click()
        await cog.refresh_browser(interaction, 100, 5, "Turrets")
        return interaction

    new = asyncio.run(run()).edit_original_response.await_args.kwargs["view"]
    assert new.category == "Turrets"
    assert any(isinstance(c, ship_parts_finder._SlotSelect) for c in new.children)
    assert _refresh(new).custom_id.endswith(":Turrets")


def test_refresh_that_cant_reload_its_category_reopens_the_browser_with_a_note():
    async def run():
        cog = _cog(candidates=AsyncMock(side_effect=UexApiError("UEX is down")))
        interaction = _click()
        await cog.refresh_browser(interaction, 100, 5, "Radar")
        return interaction

    interaction = asyncio.run(run())
    new = interaction.edit_original_response.await_args.kwargs["view"]
    assert new.category is None
    assert "Couldn't reload Radar options" in interaction.edit_original_response.await_args.kwargs["content"]


def test_refresh_for_a_ship_uex_no_longer_lists_says_to_run_the_command_again():
    async def run():
        cog = _cog(vehicles=[])
        interaction = _click()
        await cog.refresh_browser(interaction, 100, 5, "Radar")
        return interaction

    interaction = asyncio.run(run())
    interaction.edit_original_response.assert_not_awaited()
    message = interaction.followup.send.await_args.args[0]
    assert "/ship-parts-finder" in message and interaction.followup.send.await_args.kwargs["ephemeral"] is True


def test_refresh_when_uex_is_down_explains_instead_of_hanging():
    async def run():
        cog = _cog()
        cog.bot.uex.get_vehicles = AsyncMock(side_effect=UexApiError("down"))
        interaction = _click()
        await cog.refresh_browser(interaction, 100, 5, "Radar")
        return interaction

    interaction = asyncio.run(run())
    interaction.edit_original_response.assert_not_awaited()
    interaction.followup.send.assert_awaited_once()


def test_an_unexpected_refresh_failure_still_answers_the_player():
    async def run():
        cog = _cog()
        cog._ports_for_vehicle = AsyncMock(side_effect=RuntimeError("database is locked"))
        interaction = _click()
        await cog.refresh_browser(interaction, 100, 5, "Radar")
        return interaction

    interaction = asyncio.run(run())
    message = interaction.followup.send.await_args.args[0]
    assert "/ship-parts-finder" in message and "saved" in message


def test_the_browser_waits_30_idle_minutes_and_says_so():
    assert _view().timeout == 30 * 60
    assert "30 minutes idle" in EXPIRED_NOTE
