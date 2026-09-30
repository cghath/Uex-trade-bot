"""Labels that promised more than the code did (audit MSG-11 to MSG-14).

- MSG-11: a sell-shortfall reroute said it found a buyer "nearby" with no distance limit.
- MSG-12: /refinery-advisor called cached sell prices "live", and hid failed lookups.
- MSG-13: the ship-parts list kept each part's lock-in price forever; Refresh didn't re-price.
- MSG-14: README said no privileged intents were needed (the bot asked for Message Content),
  listed too few invite permissions, "not yet built" ideas that are built, and a /help that
  doesn't exist."""
from __future__ import annotations

import asyncio
import inspect
import re
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock

from discord.ext import commands

import bot.main as bot_main
from bot.cogs.route_progression import MAX_REROUTE_DISTANCE_GM, RouteProgression
from bot.cogs.ship_parts_finder import ShipPartsShoppingService
from bot.uex.backup_routes import find_backup_routes, reroute_buyer_ids
from bot.uex.client import cache_interval_text
from bot.uex.exceptions import UexApiError
from bot.uex.ship_part_display import list_price_text
from tests.test_refinery import _FakeInteraction as _RefineryInteraction
from tests.test_refinery import _METHODS, _cog, _raw, _refined
from tests.test_refinery import _make_db as _refinery_db
from tests.test_route_progression import (
    _create_thread_for_legs,
    _fake_thread_channel,
    _leg_input,
    _make_db,
    _seed_market_row,
    _seed_systems,
)


# -- MSG-11: the reroute's "nearby" -------------------------------------------------------

def _gold_buyer(terminal_id: int, price_sell: float, system: str = "Stanton") -> dict:
    return {"id_terminal": terminal_id, "terminal_name": f"Buyer {terminal_id}", "id_commodity": 1,
            "commodity_name": "Gold", "price_buy": 0, "price_sell": price_sell, "scu_buy": 0, "scu_sell": 60,
            "status_buy": None, "status_sell": 1, "star_system_name": system}


def test_reroute_candidates_are_profitable_buyers_in_the_same_system_best_paying_first():
    rows = [
        {**_gold_buyer(20, 0), "scu_sell": 0, "status_sell": 7},  # where the player is
        _gold_buyer(30, 150), _gold_buyer(31, 200), _gold_buyer(32, 90),  # 90 is below the 100 paid
        _gold_buyer(33, 300, system="Pyro"),
    ]
    assert reroute_buyer_ids(rows, terminal_id=20, anchor_commodity_id=1, anchor_buy_price=100) == [31, 30]
    unknown_home = [{**row, "star_system_name": None} if row["id_terminal"] == 20 else row for row in rows]
    assert reroute_buyer_ids(unknown_home, terminal_id=20, anchor_commodity_id=1, anchor_buy_price=100) == []


def test_backup_search_can_be_limited_to_given_destinations():
    rows = [{**_gold_buyer(20, 0), "price_buy": 100, "scu_buy": 60, "status_buy": 1},
            _gold_buyer(30, 150), _gold_buyer(31, 200)]
    common = dict(origin_terminal_id=20, destination_terminal_id=20, anchor_commodity_id=1, anchor_scu=10,
                  anchor_buy_price=100, ship_capacity_scu=10)
    assert find_backup_routes(rows, **common).other_destination.destination_id == 31
    assert find_backup_routes(rows, **common, destination_ids={30}).other_destination.destination_id == 30
    assert find_backup_routes(rows, **common, destination_ids=set()).other_destination is None


async def _reroute(tmp_path, distances: dict[int, float | None]):
    """A sell-side shortfall at terminal 20 with buyers at 30 (pays most), 31, 32 and 33
    (in Pyro); `distances` is what UEX answers from 20 to each."""
    db = _make_db(tmp_path)
    await db.init()
    await _seed_market_row(db)
    await db.record_terminal_market_snapshot([
        {"id_commodity": 1, "id_terminal": 20, "commodity_name": "Gold", "terminal_name": "Elsewhere",
         "price_buy": 0, "price_sell": 0, "scu_buy": 0, "scu_sell": 0, "status_buy": None, "status_sell": 7},
        *({key: value for key, value in _gold_buyer(tid, price).items() if key != "star_system_name"}
          for tid, price in ((30, 200), (31, 150), (32, 180), (33, 300))),
    ])
    await _seed_systems(db, (20, "Elsewhere"), (30, "Buyer 30"), (31, "Buyer 31"), (32, "Buyer 32"))
    await _seed_systems(db, (33, "Buyer 33"), system="Pyro")
    legs = [
        _leg_input(id_terminal=10, id_commodity=1, quoted_price=100.0, quoted_scu=50.0, quoted_status=3),
        _leg_input(side="sell", id_terminal=20, id_commodity=1, terminal_name="Elsewhere",
                   display_label="Sell Gold at Elsewhere", quoted_price=90.0, quoted_scu=40.0, quoted_status=2),
        _leg_input(side="buy", id_terminal=50, id_commodity=3, terminal_name="Third Stop", commodity_name="Iron",
                   display_label="Buy Iron at Third Stop", quoted_price=10.0, quoted_scu=20.0, quoted_status=3),
    ]
    await _create_thread_for_legs(db, 1, legs)

    async def distance(origin, destination):
        gm = distances.get(destination)
        return False if gm is None else {"distance": gm}  # UEX answers a bare false when it can't

    uex = NS(get_terminal_distance=AsyncMock(side_effect=distance))
    cog = RouteProgression.__new__(RouteProgression)
    cog.bot = type("FakeBot", (), {"db": db, "uex": uex})()
    cog._active_legs = {1: legs}
    channel = _fake_thread_channel()
    await cog.handle_leg_outcome(channel, 1, 1, legs[1], outcome="missing")
    looked_up = {call.args[1] for call in uex.get_terminal_distance.await_args_list}
    return channel.send.call_args_list[0].args[0], looked_up


def test_a_reroute_only_suggests_a_buyer_within_reach(tmp_path):
    async def run():
        # 30 pays most but is across the system; 32's distance is unknown; 33 is a jump away.
        message, looked_up = await _reroute(tmp_path, {30: 40.0, 31: 10.0, 32: None})
        assert "Buyer 31" in message and "(10 Gm away)" in message, message
        assert looked_up == {30, 31, 32}, "a buyer in another star system is never even looked up"

    asyncio.run(run())


def test_a_reroute_with_every_buyer_too_far_says_so(tmp_path):
    async def run():
        message, _ = await _reroute(tmp_path, {30: 40.0, 31: MAX_REROUTE_DISTANCE_GM + 1, 32: 60.0})
        assert f"no other buyer within {MAX_REROUTE_DISTANCE_GM} Gm of **Elsewhere**" in message, message

    asyncio.run(run())


# -- MSG-12: /refinery-advisor --------------------------------------------------------------

def test_cache_intervals_read_as_plain_durations():
    assert [cache_interval_text(p) for p in ("commodities_prices", "items_prices_all", "refineries_yields",
                                             "marketplace_listings")] == ["30 min", "12h", "24h", "1 min"]


async def _refinery_embed(tmp_path, **failures):
    db = _refinery_db(tmp_path)
    await db.init()
    await db.record_refinery_yield_snapshot([
        {"id_commodity": 1, "id_terminal": 10, "commodity_name": "Quantainium (Raw)",
         "terminal_name": "Levski Refinery", "star_system_name": "Nyx", "value": 5},
    ])
    cog = _cog(db, commodities=[_raw(1, "Quantainium (Raw)", 100), _refined(100, "Quantainium")], methods=_METHODS,
               price_rows_by_commodity={"Quantainium": [{"terminal_name": "Levski", "price_sell": 9000.0,
                                                         "id_terminal": 10}]})
    for name in failures:
        setattr(cog.bot.uex, name, AsyncMock(side_effect=UexApiError("down")))
    interaction = _RefineryInteraction()
    await cog.refinery_advisor.callback(cog, interaction, ore_1="Quantainium (Raw)", ore_2=None, ore_3=None)
    return interaction.followup.send.call_args.kwargs["embed"]


def test_refinery_advisor_says_how_often_its_data_updates(tmp_path):
    embed = asyncio.run(_refinery_embed(tmp_path))
    assert "Refinery yield bonuses updated every 24h" in embed.footer.text
    assert "sell prices updated every 30 min" in embed.footer.text
    assert "live" not in embed.footer.text.lower()


def test_refinery_advisor_says_when_uex_didnt_answer(tmp_path):
    embed = asyncio.run(_refinery_embed(tmp_path, get_commodities_prices=1, get_refineries_methods=1,
                                        get_star_systems=1))
    fields = {field.name: field.value for field in embed.fields}
    assert "UEX didn't answer, so sell prices" in fields["Quantainium — best sell price"]
    assert "UEX didn't answer, so the refining methods" in fields["High-yield refining methods"]
    assert "ranked by yield bonus alone" in embed.footer.text


# -- MSG-13: ship-parts list prices ---------------------------------------------------------

def test_a_list_price_shows_today_and_what_it_was_at_lock_in():
    shop = "Platinum Bay - HUR-L5"
    assert list_price_text(19998, shop, (21000, shop), prices_loaded=True) == \
        "21,000 aUEC · HUR-L5 (Platinum Bay) (was 19,998 aUEC at lock-in)"
    assert list_price_text(21000, shop, (21000, shop), prices_loaded=True) == "21,000 aUEC · HUR-L5 (Platinum Bay)"
    assert list_price_text(5000, shop, None, prices_loaded=True) == "no shop sells it right now (was 5,000 aUEC at lock-in)"
    assert list_price_text(19998, shop, None, prices_loaded=False) == "19,998 aUEC · HUR-L5 (Platinum Bay) (at lock-in)"
    assert list_price_text(None, None, None, prices_loaded=False) == "no shop price on record"


def _list_service(prices) -> ShipPartsShoppingService:
    entry = dict(vehicle_name="Avenger Stalker", category="Power Plants", port_name="hardpoint_power_plant")
    db = NS(
        get_ship_parts_entries=AsyncMock(return_value=[
            dict(entry, id_item=1, item_name="PowerBolt", price_buy=19998.0, terminal_name="Platinum Bay - HUR-L5"),
            dict(entry, category="Coolers", port_name="hardpoint_cooler", id_item=2, item_name="Frost-Star",
                 price_buy=5000.0, terminal_name="Platinum Bay - HUR-L5"),
        ]),
        get_terminal_references_by_ids=AsyncMock(return_value={7: {"terminal_name": "Cousin Crow's - Orison"}}),
    )
    return ShipPartsShoppingService(NS(db=db, uex=NS(get_items_prices_all=prices)))


def test_the_list_is_repriced_every_time_it_is_drawn():
    rows = [{"id_item": 1, "price_buy": 21000, "id_terminal": 7, "terminal_name": "Cousin Crow's"}]
    text = "\n".join(asyncio.run(_list_service(AsyncMock(return_value=rows)).render(1, 10)))
    assert "Each part's cheapest shop right now" in text and "updated every 12h" in text
    assert "PowerBolt** — 21,000 aUEC · Orison (Cousin Crow's) (was 19,998 aUEC at lock-in)" in text
    assert "Frost-Star** — no shop sells it right now (was 5,000 aUEC at lock-in)" in text


def test_the_list_falls_back_to_lock_in_prices_when_uex_doesnt_answer():
    text = "\n".join(asyncio.run(_list_service(AsyncMock(side_effect=UexApiError("down"))).render(1, 10)))
    assert "UEX's prices didn't load" in text
    assert "PowerBolt** — 19,998 aUEC · HUR-L5 (Platinum Bay) (at lock-in)" in text


# -- MSG-14: README and intents ---------------------------------------------------------------

def test_the_bot_asks_for_no_privileged_intents(tmp_path):
    async def run():
        bot = bot_main.UexBot(NS(database_path=tmp_path / "uexbot.sqlite3", uex_app_token="token",
                                 uex_secret_key=None))
        try:
            intents = bot.intents
            assert not (intents.message_content or intents.members or intents.presences)
        finally:
            await bot.uex.aclose()

    asyncio.run(run())


def _all_commands() -> set[str]:
    names = set()
    for path in bot_main.INITIAL_COGS:
        module = __import__(path, fromlist=["_"])
        for _, cls in inspect.getmembers(module, inspect.isclass):
            if issubclass(cls, commands.Cog) and cls.__module__ == module.__name__:
                names |= {command.name for command in cls.__cog_app_commands__}
    return names


def test_the_readme_layout_lists_exactly_the_real_commands():
    readme = Path("README.md").read_text(encoding="utf-8")
    start = readme.index("```\nbot/")
    block = readme[start:readme.index("```", start + 3)]
    listed = set(re.findall(r"(?<![\w/])/([a-z][a-z0-9-]*)(?![\w/])", block))
    real = _all_commands()
    assert listed - real == set(), "README lists commands that don't exist"
    assert real - listed == set(), "README's project layout is missing commands"
    assert "`/help`" not in readme and "/intro" in readme
