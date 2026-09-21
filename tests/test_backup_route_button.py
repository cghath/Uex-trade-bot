"""The "Backup route" button on stock-limited route messages (bot.discord_ui.BackupRouteButton).

Driven through the real ranked-list send path (Trends._send_ranked_routes against a real Database)
and the button's real callback, the same way Discord would call it. The scenario is the one from
tests/test_ranked_routes_hedge.py: 21 SCU of Taranite at Origin 1 -> Destination 101 for a 100 SCU
ship, with Cobalt also trading between the same two terminals.
"""
from __future__ import annotations

import asyncio
import threading
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock

import discord

from bot import discord_ui
from bot.cogs.route_progression import RouteTrackingView
from tests.test_ranked_routes_hedge import _interaction, _make_cog, _route, _send


def _route_views(inter):
    """(embed, view) for every route message the command sent (the first embed is the intro)."""
    calls = [c for c in inter.followup.send.call_args_list if c.kwargs.get("embed") is not None][1:]
    return [(c.kwargs["embed"], c.kwargs.get("view")) for c in calls]


def _backup_button(view):
    matches = [c for c in (view.children if view else []) if getattr(c, "label", "") == "Backup route"]
    return matches[0] if matches else None


def _press(user_id=1):
    return NS(user=NS(id=user_id), response=NS(defer=AsyncMock(), send_message=AsyncMock()),
              followup=NS(send=AsyncMock()))


def _sent_text(press) -> str:
    call = press.followup.send.call_args
    embed = call.kwargs.get("embed")
    if embed is None:
        return str(call.args[0])
    return "\n".join([embed.title or "", embed.description or "", *(f"{f.name}\n{f.value}" for f in embed.fields),
                      embed.footer.text or ""])


def test_a_stock_limited_route_message_carries_a_backup_button_and_a_ship_limited_one_does_not(tmp_path):
    async def run():
        cog, _ = await _make_cog(tmp_path)
        inter = await _send(cog, [_route(commodity_id=1), _route(commodity_id=3, name="Laranite", scu=500)])
        by_title = {embed.title: view for embed, view in _route_views(inter)}
        stock_limited = next(view for title, view in by_title.items() if "Taranite" in title)
        ship_limited = next(view for title, view in by_title.items() if "Laranite" in title)
        assert _backup_button(stock_limited) is not None and _backup_button(stock_limited).row == 1
        assert _backup_button(ship_limited) is None, "a ship-limited route has nothing to back up"

    asyncio.run(run())


def test_the_backup_button_sits_below_the_track_button_when_tracking_is_loaded(tmp_path):
    async def run():
        cog, _ = await _make_cog(tmp_path)
        cog.bot.get_cog = lambda name: NS()  # any object: the Track button only calls it when pressed
        inter = await _send(cog, [_route()])
        ((_, view),) = _route_views(inter)
        assert isinstance(view, RouteTrackingView)
        rows = {child.label: child.row for child in view.children}
        assert rows == {"Track this route": 0, "Backup route": 1}

    asyncio.run(run())


def test_the_warning_points_at_the_button_only_when_the_message_has_one(tmp_path):
    async def run():
        cog, _ = await _make_cog(tmp_path)
        with_button = await _send(cog, [_route()])
        ((embed, view),) = _route_views(with_button)
        assert _backup_button(view) is not None
        assert "Press **Backup route** below" in "\n".join(f.value for f in embed.fields)

        from dataclasses import replace
        no_ids = await _send(cog, [replace(_route(), origin_terminal_id=None, destination_terminal_id=None)])
        ((embed, view),) = _route_views(no_ids)
        text = "\n".join(f.value for f in embed.fields)
        assert _backup_button(view) is None and "Backup route" not in text and "/mixed-routes" in text

    asyncio.run(run())


def test_pressing_the_button_answers_privately_with_a_route_that_keeps_the_original_commodity(tmp_path):
    async def run():
        cog, _ = await _make_cog(tmp_path)
        inter = await _send(cog, [_route()])
        ((_, view),) = _route_views(inter)
        press = _press(user_id=1)
        await _backup_button(view).callback(press)
        press.response.defer.assert_awaited_once_with(ephemeral=True, thinking=True)
        assert press.followup.send.call_args.kwargs["ephemeral"] is True
        text = _sent_text(press)
        assert "Same trip, fuller hold" in text and "Taranite" in text and "(yours)" in text
        assert "Cobalt" in text and "79 SCU" in text
        assert "assumes you're carrying 21 SCU of Taranite" in text

    asyncio.run(run())


def test_when_nothing_beats_the_plan_it_says_so_and_tells_the_player_to_continue(tmp_path):
    async def run():
        cog, db = await _make_cog(tmp_path, seed_hedge=False)
        # Only the original commodity trades here: nothing to add and nowhere better to send it.
        await db.record_terminal_market_snapshot([
            {"id_commodity": 1, "id_terminal": 1, "commodity_name": "Taranite", "terminal_name": "Origin 1",
             "price_buy": 100, "price_sell": 0, "scu_buy": 21, "scu_sell": 0, "status_buy": 1, "status_sell": None},
            {"id_commodity": 1, "id_terminal": 101, "commodity_name": "Taranite", "terminal_name": "Destination 101",
             "price_buy": 0, "price_sell": 200, "scu_buy": 0, "scu_sell": 21, "status_buy": None, "status_sell": 1},
        ])
        inter = await _send(cog, [_route()])
        ((_, view),) = _route_views(inter)
        press = _press()
        await _backup_button(view).callback(press)
        text = _sent_text(press)
        assert "Nothing I can find beats your current plan" in text
        assert "continue to **Destination 101** as planned" in text

    asyncio.run(run())


def test_with_no_market_data_for_the_route_it_says_so_instead_of_calling_it_unprofitable(tmp_path):
    async def run():
        cog, _ = await _make_cog(tmp_path, seed_hedge=False)  # the collected snapshot has nothing at all
        inter = await _send(cog, [_route()])
        ((_, view),) = _route_views(inter)
        press = _press()
        await _backup_button(view).callback(press)
        text = _sent_text(press)
        assert "I don't have a current Taranite price at Destination 101" in text
        assert "no longer looks" not in text and "Nothing I can find beats" not in text

    asyncio.run(run())


def test_a_player_who_bought_the_whole_stock_still_gets_a_backup_that_keeps_it(tmp_path):
    """After buying everything the origin lists 0 stock; the commodity they now hold must still be
    kept in the plan (the bug this guards: needing stock on record dropped it)."""
    async def run():
        cog, db = await _make_cog(tmp_path)
        inter = await _send(cog, [_route()])
        ((_, view),) = _route_views(inter)
        await db.record_terminal_market_snapshot([  # the collector's next pass: Origin 1 is out of Taranite
            {"id_commodity": 1, "id_terminal": 1, "commodity_name": "Taranite", "terminal_name": "Origin 1",
             "price_buy": 100, "price_sell": 0, "scu_buy": 0, "scu_sell": 0, "status_buy": 1, "status_sell": None},
        ])
        press = _press()
        await _backup_button(view).callback(press)
        text = _sent_text(press)
        assert "Same trip, fuller hold" in text and "(yours)" in text and "Cobalt" in text, text

    asyncio.run(run())


def test_only_the_player_who_ran_the_command_can_use_the_button(tmp_path):
    async def run():
        cog, db = await _make_cog(tmp_path)
        inter = await _send(cog, [_route()])
        ((_, view),) = _route_views(inter)
        loads_before = db.get_mixed_route_market_rows.await_count
        stranger = _press(user_id=999)
        await _backup_button(view).callback(stranger)
        stranger.response.defer.assert_not_awaited()
        assert stranger.response.send_message.call_args.kwargs["ephemeral"] is True
        assert "belongs to whoever ran the command" in stranger.response.send_message.call_args.args[0]
        assert db.get_mixed_route_market_rows.await_count == loads_before, "a refused press does no work"

    asyncio.run(run())


def test_a_failing_search_tells_the_player_and_leaves_the_button_usable(tmp_path):
    async def run():
        cog, db = await _make_cog(tmp_path)
        inter = await _send(cog, [_route()])
        ((_, view),) = _route_views(inter)
        button = _backup_button(view)
        db.get_mixed_route_market_rows = AsyncMock(side_effect=RuntimeError("database is locked"))
        press = _press()
        await button.callback(press)
        assert "couldn't work out a backup route" in _sent_text(press)
        assert press.followup.send.call_args.kwargs["ephemeral"] is True

        db.get_mixed_route_market_rows = AsyncMock(return_value=[])
        again = _press()
        await button.callback(again)
        text = _sent_text(again)
        assert "Backup route:" in text and "couldn't work out" not in text, \
            "the failed press must not leave the button stuck 'working'"

    asyncio.run(run())


def test_an_overlapping_press_is_told_to_wait_instead_of_running_twice(tmp_path):
    async def run():
        cog, db = await _make_cog(tmp_path)
        inter = await _send(cog, [_route()])
        ((_, view),) = _route_views(inter)
        button = _backup_button(view)
        button._working = True
        loads_before = db.get_mixed_route_market_rows.await_count
        press = _press()
        await button.callback(press)
        assert "Still working" in press.response.send_message.call_args.args[0]
        assert db.get_mixed_route_market_rows.await_count == loads_before

    asyncio.run(run())


def test_the_search_runs_off_the_event_loop_thread(tmp_path, monkeypatch):
    """Deterministic (not timing-based): record which thread the search runs on. An earlier
    heartbeat-style version of this kind of test passed even with the blocking bug present."""
    async def run():
        cog, _ = await _make_cog(tmp_path)
        inter = await _send(cog, [_route()])
        ((_, view),) = _route_views(inter)
        seen = {}
        real = discord_ui.run_backup_search

        def spy(rows, context):
            seen["thread"] = threading.current_thread()
            return real(rows, context)

        monkeypatch.setattr(discord_ui, "run_backup_search", spy)
        await _backup_button(view).callback(_press())
        assert seen["thread"] is not threading.main_thread()

    asyncio.run(run())


def test_the_button_uses_the_players_own_ship_budget_and_filters(tmp_path):
    async def run():
        cog, db = await _make_cog(tmp_path)
        refs = {tid: {"is_auto_load": 1, "star_system_name": "Stanton"} for tid in (1, 101)}
        db.get_terminal_references_by_ids = AsyncMock(return_value=refs)
        inter = await _send(cog, [_route()], budget=2300.0, auto_load_only=True, system="Stanton")
        ((_, view),) = _route_views(inter)
        context = _backup_button(view).context
        assert context.ship_capacity_scu == 100 and context.ship_name == "Ship"
        assert context.budget == 2300.0 and context.auto_load_only is True and context.system == "Stanton"
        assert (context.origin_terminal_id, context.destination_terminal_id) == (1, 101)
        assert (context.anchor_commodity_id, context.anchor_scu, context.anchor_buy_price) == (1, 21, 100.0)

    asyncio.run(run())


def test_a_hedge_lookup_failure_does_not_take_the_backup_button_away(tmp_path):
    async def run():
        cog, db = await _make_cog(tmp_path)
        db.get_mixed_route_market_rows = AsyncMock(side_effect=RuntimeError("database is locked"))
        inter = await _send(cog, [_route()])
        ((embed, view),) = _route_views(inter)
        assert _backup_button(view) is not None
        assert "Hedge:" not in "\n".join(f.value for f in embed.fields)

    asyncio.run(run())


def test_the_top_routes_command_attaches_the_button_through_its_real_callback(tmp_path):
    async def run():
        cog, _ = await _make_cog(tmp_path)
        cog._top_scored_routes = [_route()]
        cog._top_scored_routes_updated_at = None
        cog._top_scored_routes_lock = asyncio.Lock()
        inter = _interaction()
        await cog.top_routes.callback(cog, inter, ship="Ship")
        ((_, view),) = _route_views(inter)
        assert isinstance(_backup_button(view), discord.ui.Button)

    asyncio.run(run())
