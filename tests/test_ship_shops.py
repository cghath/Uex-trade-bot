"""Where to Buy a Ship: pure ranking/grouping/formatting logic (bot/uex/ship_shops.py) and
the /where-to-buy-ship command end to end. Fixtures are shaped like real
/vehicles_purchases_prices and /vehicles_rentals_prices rows pulled live."""
from __future__ import annotations

import asyncio
import itertools
import sqlite3
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock

import discord
import httpx
import pytest

from bot.cogs.help import CATEGORIES
from bot.cogs.ship_shops import FOOTER_TEXT, ShipShops, listed_ship_autocomplete
from bot.main import INITIAL_COGS
from bot.uex.client import _ENDPOINT_CACHE_TTL, UexClient
from bot.uex.exceptions import UexApiError, UexRateLimitError
from bot.uex.ship_shops import (
    NOT_RENTED_TEXT,
    NOT_SOLD_TEXT,
    PURCHASES_UNAVAILABLE_TEXT,
    RENTAL_RATE_NOTE,
    RENTALS_UNAVAILABLE_TEXT,
    UNKNOWN_SYSTEM_LABEL,
    ShipShopListing,
    build_ship_shop_sections,
    format_purchase_line,
    format_rental_line,
    group_by_star_system,
    rank_purchase_listings,
    rank_rental_listings,
    ship_autocomplete_names,
    ship_shop_description,
    terminal_ids_missing_star_system,
    vehicle_ids_with_listings,
)

LORVILLE_NEW_DEAL = "New Deal - Teasa Spaceport - Lorville"
LEVSKI_TEACHS = "Teach's Ship Shop - Levski"


def _buy(id_terminal, terminal_name, price_buy, star_system_name="Stanton", date_modified=1789103495, **extra):
    return dict(id_terminal=id_terminal, terminal_name=terminal_name, price_buy=price_buy,
                star_system_name=star_system_name, date_modified=date_modified, **extra)


def _rent(id_terminal, terminal_name, price_rent, star_system_name="Stanton", date_modified=1788133731, **extra):
    return dict(id_terminal=id_terminal, terminal_name=terminal_name, price_rent=price_rent,
                star_system_name=star_system_name, date_modified=date_modified, **extra)


def _listing(place, price, *, vendor="Vendor", system="Stanton", id_terminal=1, date_modified=1789103495):
    return ShipShopListing(
        id_terminal=id_terminal, terminal_name=f"{vendor} - {place}", place_label=place,
        vendor_label=vendor, star_system_name=system, price=price, date_modified=date_modified,
    )


# Real, live-pulled shapes (2026-09-25).
TITAN_BUYS = [
    _buy(791, LEVSKI_TEACHS, 1358280, star_system_name="Nyx", date_modified=1787927686),
    _buy(149, LORVILLE_NEW_DEAL, 1290370, date_modified=1789917431),
]
TITAN_RENTS = [
    _rent(151, "Traveler Rentals - Riker Memorial Spaceport - Area 18", 27166, date_modified=1790115329),
    _rent(150, "Traveler Rentals - August Dunlow Spaceport - Orison", 27166, date_modified=1788104686),
    _rent(789, "Teach's Rentals - Levski", 27166, star_system_name="Nyx"),
]
C2_BUYS = [
    _buy(112, "New Deal - Crusader Showroom - Orison", 18900000, date_modified=1788127888),
    _buy(148, "Astro Armada - Area 18", 18900000, date_modified=1787809945),
]


# -- rank_purchase_listings / rank_rental_listings ------------------------------------

def test_rank_purchase_listings_sorts_cheapest_first():
    ranked = rank_purchase_listings(TITAN_BUYS)
    assert [r.price for r in ranked] == [1290370, 1358280]
    assert ranked[0].place_label == "Lorville"
    assert ranked[0].vendor_label == "New Deal - Teasa Spaceport"
    assert ranked[0].star_system_name == "Stanton"
    assert ranked[1].place_label == "Levski" and ranked[1].star_system_name == "Nyx"


def test_rank_purchase_listings_breaks_an_exact_price_tie_the_same_way_regardless_of_input_order():
    """Real data: the C2 Hercules sells for an identical 18,900,000 at two showrooms. The
    order must be deterministic, not whatever order UEX happened to send."""
    orders = {
        tuple(r.id_terminal for r in rank_purchase_listings(list(permutation)))
        for permutation in itertools.permutations(C2_BUYS)
    }
    assert orders == {(148, 112)}, "Area 18 sorts before Orison on the place-name tie-break"


def test_rank_rental_listings_three_way_tie_is_deterministic():
    orders = {
        tuple(r.id_terminal for r in rank_rental_listings(list(permutation)))
        for permutation in itertools.permutations(TITAN_RENTS)
    }
    assert orders == {(151, 789, 150)}  # Area 18, Levski, Orison


@pytest.mark.parametrize("bad_price", [None, 0, -5, "", "not a number"])
def test_rank_purchase_listings_drops_a_missing_or_non_positive_price(bad_price):
    ranked = rank_purchase_listings([_buy(1, "A - Somewhere", bad_price), _buy(2, "B - Elsewhere", 100)])
    assert [r.id_terminal for r in ranked] == [2]


@pytest.mark.parametrize("bad_price", [None, 0, -1])
def test_rank_rental_listings_drops_a_missing_or_non_positive_price(bad_price):
    ranked = rank_rental_listings([_rent(1, "A - Somewhere", bad_price), _rent(2, "B - Elsewhere", 100)])
    assert [r.id_terminal for r in ranked] == [2]


def test_rank_rental_listings_reads_price_rent_not_price_buy():
    ranked = rank_rental_listings([dict(id_terminal=1, terminal_name="A - B", price_buy=999, price_rent=None)])
    assert ranked == []


def test_rank_purchase_listings_accepts_uex_numeric_strings():
    ranked = rank_purchase_listings([_buy(1, "A - B", "2010960")])
    assert ranked[0].price == 2010960


def test_rank_keeps_a_priced_row_with_no_terminal_id_or_date():
    ranked = rank_purchase_listings([dict(terminal_name="New Deal - Lorville", price_buy=100)])
    assert ranked[0].id_terminal is None
    assert ranked[0].date_modified is None
    assert ranked[0].place_label == "Lorville"


def test_rank_treats_a_blank_star_system_as_unknown():
    ranked = rank_rental_listings([_rent(1, "Vantage Rentals - Pyro Gateway (Nyx)", 100, star_system_name="")])
    assert ranked[0].star_system_name is None


def test_terminal_ids_missing_star_system_only_returns_rows_with_no_system():
    ids = terminal_ids_missing_star_system(
        [_buy(1, "A - B", 10), _buy(2, "C - D", 10, star_system_name=None)],
        [_rent(773, "Vantage Rentals - Pyro Gateway (Nyx)", 10, star_system_name=""),
         _rent(None, "No Id - Anywhere", 10, star_system_name=None)],
    )
    assert ids == {2, 773}


def test_rank_fills_a_missing_system_from_the_terminal_lookup_but_never_overrides_a_real_one():
    """Real rows (2026-09-25): MOTH at Pyro Gateway (Nyx), terminal 773, came back with no
    system; terminal_reference has it as Nyx."""
    ranked = rank_rental_listings(
        [_rent(773, "Vantage Rentals - Pyro Gateway (Nyx)", 10, star_system_name=None),
         _rent(559, "Vantage Rentals - Stanton Gateway (Pyro)", 20, star_system_name="Pyro"),
         _rent(999, "Vantage Rentals - Nowhere", 30, star_system_name=None)],
        {773: "Nyx", 559: "Stanton"},
    )
    assert [(r.id_terminal, r.star_system_name) for r in ranked] == [(773, "Nyx"), (559, "Pyro"), (999, None)]
    assert rank_purchase_listings([_buy(773, "A - B", 5, star_system_name=None)], {773: "Nyx"})[0].star_system_name == "Nyx"


# -- group_by_star_system --------------------------------------------------------------

def test_group_by_star_system_orders_groups_by_their_cheapest_listing():
    ranked = rank_rental_listings([
        _rent(1, "Traveler Rentals - Cargo Center - Baijini Point", 52920, star_system_name="Stanton"),
        _rent(2, "Vantage Rentals - Nyx Gateway (Pyro)", 50274, star_system_name="Pyro"),
        _rent(3, "Traveler Rentals - Pyro Gateway (Nyx)", 52920, star_system_name="Nyx"),
        _rent(4, "Vantage Rentals - Lorville", 50274, star_system_name="Stanton"),
    ])
    grouped = group_by_star_system(ranked)
    assert list(grouped) == ["Stanton", "Pyro", "Nyx"]  # Lorville (Stanton) wins the 50,274 tie on place name
    assert [listing.id_terminal for listing in grouped["Stanton"]] == [4, 1]


def test_group_by_star_system_puts_rows_with_no_system_in_one_unknown_group_last():
    """Seen live: some rental rows (e.g. 'Vantage Rentals - Pyro Gateway (Nyx)') come back
    with star_system_name = None. They must still be shown, never dropped or guessed."""
    ranked = rank_rental_listings([
        _rent(1, "Vantage Rentals - Pyro Gateway (Nyx)", 10, star_system_name=None),
        _rent(2, "Vantage Rentals - Lorville", 20, star_system_name="Stanton"),
    ])
    grouped = group_by_star_system(ranked)
    assert list(grouped) == ["Stanton", UNKNOWN_SYSTEM_LABEL]
    assert grouped[UNKNOWN_SYSTEM_LABEL][0].id_terminal == 1


# -- line formatting -------------------------------------------------------------------

def test_format_purchase_line_shows_place_vendor_price_system_and_report_time():
    line = format_purchase_line(rank_purchase_listings([TITAN_BUYS[1]])[0])
    assert line == (
        "**Lorville** (New Deal - Teasa Spaceport) — 1,290,370 aUEC · Stanton · updated <t:1789917431:R>"
    )


def test_format_purchase_line_with_unknown_system_and_no_date():
    line = format_purchase_line(_listing("X", 100, system=None, date_modified=None))
    assert line.endswith(f"· {UNKNOWN_SYSTEM_LABEL}")
    assert "updated" not in line


def test_format_rental_line_is_per_day_and_omits_the_system():
    line = format_rental_line(rank_rental_listings([TITAN_RENTS[2]])[0])
    assert line == "**Levski** (Teach's Rentals) — 27,166 aUEC / day · updated <t:1788133731:R>"


def test_format_lines_with_no_vendor_omit_the_parenthetical():
    listing = ShipShopListing(None, "Mystery", "Mystery", None, "Stanton", 5, None)
    assert "(" not in format_purchase_line(listing)
    assert "(" not in format_rental_line(listing)


# -- sections / description ------------------------------------------------------------

def test_build_sections_buy_then_one_rent_section_per_system():
    sections = build_ship_shop_sections(rank_purchase_listings(TITAN_BUYS), rank_rental_listings(TITAN_RENTS))
    assert [name for name, _ in sections] == ["Buy", "Rent — Stanton", "Rent — Nyx"]
    assert len(sections[0][1]) == 2
    assert len(sections[1][1]) == 2


def test_build_sections_no_buy_rows_says_not_sold_and_no_rent_rows_says_not_rentable():
    assert build_ship_shop_sections([], rank_rental_listings(TITAN_RENTS))[0] == ("Buy", [NOT_SOLD_TEXT])
    assert build_ship_shop_sections(rank_purchase_listings(C2_BUYS), [])[-1] == ("Rent", [NOT_RENTED_TEXT])


def test_build_sections_a_failed_fetch_says_it_could_not_load_not_that_nothing_exists():
    sections = build_ship_shop_sections([], [], purchases_failed=True, rentals_failed=True)
    assert sections == [("Buy", [PURCHASES_UNAVAILABLE_TEXT]), ("Rent", [RENTALS_UNAVAILABLE_TEXT])]


def test_description_carries_the_one_day_rate_note_only_when_rentals_are_shown():
    assert RENTAL_RATE_NOTE in ship_shop_description(has_rentals=True)
    assert RENTAL_RATE_NOTE not in ship_shop_description(has_rentals=False)


# -- autocomplete helpers --------------------------------------------------------------

VEHICLES = [
    {"id": 27, "name": "Avenger Titan", "name_full": "Aegis Avenger Titan"},
    {"id": 35, "name": "C2 Hercules Starlifter", "name_full": "Crusader C2 Hercules Starlifter"},
    {"id": 50, "name": "Corsair", "name_full": "Drake Corsair"},
    {"id": 52, "name": "Cutlass Black", "name_full": "Drake Cutlass Black"},
    {"id": 109, "name": "Idris-P", "name_full": "Aegis Idris-P"},
]


def test_vehicle_ids_with_listings_unions_both_lists_and_skips_bad_ids():
    ids = vehicle_ids_with_listings(
        [{"id_vehicle": 52}, {"id_vehicle": "27"}, {"id_vehicle": None}],
        [{"id_vehicle": 50}, {}],
    )
    assert ids == {27, 50, 52}


def test_ship_autocomplete_names_only_offers_listed_ships_matching_name_or_name_full():
    assert ship_autocomplete_names(VEHICLES, {27, 52}, "") == ["Avenger Titan", "Cutlass Black"]
    assert ship_autocomplete_names(VEHICLES, {27, 50, 52}, "drake") == ["Corsair", "Cutlass Black"]
    assert ship_autocomplete_names(VEHICLES, {27, 50, 52}, "idris") == []


def test_ship_autocomplete_names_dedupes_and_caps():
    vehicles = [{"id": i, "name": f"Ship {i % 30}"} for i in range(60)]
    names = ship_autocomplete_names(vehicles, set(range(60)), "ship", limit=25)
    assert len(names) == 25 and len(set(names)) == 25


def test_ship_autocomplete_names_dedupes_a_real_duplicate_name_below_the_cap():
    """Real /vehicles has 'Sabre Raven EX' at both id 290 and 291."""
    vehicles = [{"id": 290, "name": "Sabre Raven EX"}, {"id": 291, "name": "Sabre Raven EX"}]
    assert ship_autocomplete_names(vehicles, {290, 291}, "") == ["Sabre Raven EX"]


# -- registration ----------------------------------------------------------------------

def test_cog_is_registered_and_categorized_in_help():
    assert "bot.cogs.ship_shops" in INITIAL_COGS
    assert any("where-to-buy-ship" in names for _, _, names in CATEGORIES)


# -- /where-to-buy-ship end to end -----------------------------------------------------

class _FakeInteraction:
    def __init__(self) -> None:
        self.response = NS(defer=AsyncMock())
        self.followup = NS(send=AsyncMock())


def _result(value):
    return AsyncMock(side_effect=value) if isinstance(value, BaseException) else AsyncMock(return_value=value)


def _cog(*, purchases=(), rentals=(), vehicles=VEHICLES, db=None):
    # No db by default: a cog test whose rows all carry a star system must never touch it.
    cog = ShipShops.__new__(ShipShops)
    cog.bot = NS(uex=NS(
        get_vehicles=_result(vehicles),
        get_vehicle_purchase_prices=_result(purchases if isinstance(purchases, BaseException) else list(purchases)),
        get_vehicle_rental_prices=_result(rentals if isinstance(rentals, BaseException) else list(rentals)),
    ))
    if db is not None:
        cog.bot.db = db
    return cog


def _run(cog, ship):
    interaction = _FakeInteraction()
    asyncio.run(cog.where_to_buy_ship.callback(cog, interaction, ship=ship))
    return interaction


def _field(embed, name):
    return next(f for f in embed.fields if f.name == name)


def _assert_no_mentions_anywhere(interaction):
    for call in interaction.followup.send.call_args_list:
        mentions = call.kwargs["allowed_mentions"]
        assert mentions.everyone is False and mentions.users is False and mentions.roles is False


def test_defer_is_awaited_before_the_first_uex_call():
    interaction = _FakeInteraction()
    defer_count_at_first_call = []

    async def _vehicles():
        defer_count_at_first_call.append(interaction.response.defer.await_count)
        return VEHICLES

    cog = _cog(purchases=TITAN_BUYS, rentals=TITAN_RENTS)
    cog.bot.uex.get_vehicles = AsyncMock(side_effect=_vehicles)
    asyncio.run(cog.where_to_buy_ship.callback(cog, interaction, ship="Avenger Titan"))

    assert defer_count_at_first_call == [1]
    interaction.response.defer.assert_awaited_once_with()  # public, like /ingame-item-finder


def test_both_sections_render_buy_then_rent_per_system():
    cog = _cog(purchases=TITAN_BUYS, rentals=TITAN_RENTS)
    interaction = _run(cog, "Avenger Titan")

    cog.bot.uex.get_vehicle_purchase_prices.assert_awaited_once_with(27)
    cog.bot.uex.get_vehicle_rental_prices.assert_awaited_once_with(27)
    embed = interaction.followup.send.call_args.kwargs["embed"]
    assert embed.title == "Avenger Titan — Where to Buy"
    assert RENTAL_RATE_NOTE in embed.description
    assert embed.footer.text == FOOTER_TEXT
    assert [f.name for f in embed.fields] == ["Buy", "Rent — Stanton", "Rent — Nyx"]
    buy_lines = _field(embed, "Buy").value.splitlines()
    assert buy_lines[0].startswith("**Lorville** (New Deal - Teasa Spaceport) — 1,290,370 aUEC · Stanton")
    assert buy_lines[1].startswith("**Levski** (Teach's Ship Shop) — 1,358,280 aUEC · Nyx")
    assert "27,166 aUEC / day" in _field(embed, "Rent — Nyx").value
    _assert_no_mentions_anywhere(interaction)


def test_a_name_full_query_in_any_case_shows_the_ships_canonical_name():
    cog = _cog(purchases=TITAN_BUYS, rentals=TITAN_RENTS)
    interaction = _run(cog, "aegis avenger titan")
    cog.bot.uex.get_vehicle_purchase_prices.assert_awaited_once_with(27)
    assert interaction.followup.send.call_args.kwargs["embed"].title == "Avenger Titan — Where to Buy"

    interaction = _run(_cog(purchases=[], rentals=[]), "AEGIS IDRIS-P")
    assert interaction.followup.send.call_args.kwargs["content"] == (
        "UEX has no in-game purchase or rental location on record for **Idris-P**."
    )


def test_a_rental_row_with_no_system_joins_its_real_systems_section():
    """Real MOTH data: 'Vantage Rentals - Pyro Gateway (Nyx)' (terminal 773) came back with
    no star system, and ended up in its own 'Unknown system' section right next to
    'Rent — Nyx'. terminal_reference knows it's Nyx."""
    db = NS(get_terminal_star_system=AsyncMock(side_effect=lambda id_terminal: {773: "Nyx"}.get(id_terminal)))
    rentals = [
        _rent(789, "Teach's Rentals - Levski", 27166, star_system_name="Nyx"),
        _rent(773, "Vantage Rentals - Pyro Gateway (Nyx)", 27500, star_system_name=None),
        _rent(999, "Vantage Rentals - Nowhere Known", 28000, star_system_name=None),
    ]
    interaction = _run(_cog(purchases=TITAN_BUYS, rentals=rentals, db=db), "Avenger Titan")

    embed = interaction.followup.send.call_args.kwargs["embed"]
    assert [f.name for f in embed.fields] == ["Buy", "Rent — Nyx", f"Rent — {UNKNOWN_SYSTEM_LABEL}"]
    assert "Pyro Gateway (Nyx)" in _field(embed, "Rent — Nyx").value
    assert "Nowhere Known" in _field(embed, f"Rent — {UNKNOWN_SYSTEM_LABEL}").value
    assert sorted(call.args[0] for call in db.get_terminal_star_system.await_args_list) == [773, 999]


def test_a_failed_terminal_system_lookup_still_sends_the_row_under_unknown_system():
    db = NS(get_terminal_star_system=AsyncMock(side_effect=sqlite3.OperationalError("database is locked")))
    rentals = [_rent(773, "Vantage Rentals - Pyro Gateway (Nyx)", 27500, star_system_name=None)]
    interaction = _run(_cog(purchases=TITAN_BUYS, rentals=rentals, db=db), "Avenger Titan")
    embed = interaction.followup.send.call_args.kwargs["embed"]
    assert "Pyro Gateway (Nyx)" in _field(embed, f"Rent — {UNKNOWN_SYSTEM_LABEL}").value


def test_buy_only_ship_says_it_is_not_rentable_and_drops_the_rental_note():
    cog = _cog(purchases=C2_BUYS, rentals=[])
    interaction = _run(cog, "C2 Hercules Starlifter")
    cog.bot.uex.get_vehicle_purchase_prices.assert_awaited_once_with(35)
    embed = interaction.followup.send.call_args.kwargs["embed"]
    assert embed.title == "C2 Hercules Starlifter — Where to Buy"
    assert [f.name for f in embed.fields] == ["Buy", "Rent"]
    assert _field(embed, "Rent").value == NOT_RENTED_TEXT
    assert RENTAL_RATE_NOTE not in embed.description
    assert len(_field(embed, "Buy").value.splitlines()) == 2


def test_rent_only_ship_says_it_is_not_sold_at_any_tracked_shop():
    interaction = _run(_cog(purchases=[], rentals=TITAN_RENTS), "Avenger Titan")
    embed = interaction.followup.send.call_args.kwargs["embed"]
    assert _field(embed, "Buy").value == NOT_SOLD_TEXT
    assert "Rent — Stanton" in [f.name for f in embed.fields]


def test_neither_buy_nor_rent_rows_says_so_plainly_with_no_embed():
    interaction = _run(_cog(purchases=[], rentals=[]), "Idris-P")
    kwargs = interaction.followup.send.call_args.kwargs
    assert "embed" not in kwargs
    assert kwargs["content"] == "UEX has no in-game purchase or rental location on record for **Idris-P**."


def test_rows_that_all_have_bad_prices_count_as_nothing_on_record():
    interaction = _run(_cog(purchases=[_buy(1, "A - B", 0)], rentals=[_rent(2, "C - D", None)]), "Idris-P")
    assert "no in-game purchase or rental location on record" in interaction.followup.send.call_args.kwargs["content"]


def test_purchase_fetch_failing_still_shows_rentals_and_discloses_the_gap():
    interaction = _run(_cog(purchases=UexApiError("down"), rentals=TITAN_RENTS), "Avenger Titan")
    embed = interaction.followup.send.call_args.kwargs["embed"]
    assert _field(embed, "Buy").value == PURCHASES_UNAVAILABLE_TEXT
    assert "27,166 aUEC / day" in _field(embed, "Rent — Stanton").value


def test_rental_fetch_failing_still_shows_purchases_and_discloses_the_gap():
    interaction = _run(_cog(purchases=TITAN_BUYS, rentals=UexApiError("down")), "Avenger Titan")
    embed = interaction.followup.send.call_args.kwargs["embed"]
    assert "1,290,370 aUEC" in _field(embed, "Buy").value
    assert _field(embed, "Rent").value == RENTALS_UNAVAILABLE_TEXT
    assert RENTAL_RATE_NOTE not in embed.description


@pytest.mark.parametrize("purchases, rentals, buy_text, rent_text", [
    (UexApiError("down"), [], PURCHASES_UNAVAILABLE_TEXT, NOT_RENTED_TEXT),
    ([], UexApiError("down"), NOT_SOLD_TEXT, RENTALS_UNAVAILABLE_TEXT),
], ids=["purchases-failed", "rentals-failed"])
def test_one_fetch_failing_and_the_other_empty_does_not_claim_nothing_is_on_record(
    purchases, rentals, buy_text, rent_text,
):
    interaction = _run(_cog(purchases=purchases, rentals=rentals), "Idris-P")
    kwargs = interaction.followup.send.call_args.kwargs
    assert "content" not in kwargs, "must not send the 'nothing on record' message"
    assert _field(kwargs["embed"], "Buy").value == buy_text
    assert _field(kwargs["embed"], "Rent").value == rent_text


def test_both_fetches_failing_sends_the_standard_uex_error():
    interaction = _run(_cog(purchases=UexRateLimitError("slow down"), rentals=UexApiError("down")), "Avenger Titan")
    kwargs = interaction.followup.send.call_args.kwargs
    assert "embed" not in kwargs
    assert "rate-limiting" in kwargs["content"]


def test_an_unexpected_non_uex_exception_is_not_swallowed():
    with pytest.raises(RuntimeError):
        _run(_cog(purchases=RuntimeError("bug"), rentals=TITAN_RENTS), "Avenger Titan")


def test_vehicle_list_failing_sends_the_uex_error_and_fetches_no_prices():
    cog = _cog(vehicles=UexApiError("down"))
    interaction = _run(cog, "Avenger Titan")
    assert "didn't return a valid response" in interaction.followup.send.call_args.kwargs["content"]
    cog.bot.uex.get_vehicle_purchase_prices.assert_not_awaited()


def test_unresolvable_ship_asks_to_pick_from_autocomplete_and_never_pings():
    cog = _cog()
    interaction = _run(cog, "@everyone")
    kwargs = interaction.followup.send.call_args.kwargs
    assert "@everyone" in kwargs["content"] and "autocomplete" in kwargs["content"]
    _assert_no_mentions_anywhere(interaction)
    cog.bot.uex.get_vehicle_purchase_prices.assert_not_awaited()
    cog.bot.uex.get_vehicle_rental_prices.assert_not_awaited()


def test_an_ambiguous_partial_name_is_not_guessed():
    cog = _cog()
    interaction = _run(cog, "a")  # substring of several ships
    assert "autocomplete" in interaction.followup.send.call_args.kwargs["content"]
    cog.bot.uex.get_vehicle_purchase_prices.assert_not_awaited()


def test_oversized_result_falls_back_to_plain_text_with_every_row_and_the_rental_note():
    rentals = [
        _rent(1000 + i, f"Traveler Rentals - Cargo Center - Extremely Long Station Name Number {i:02d}", 50000 + i,
              star_system_name=("Stanton", "Pyro", "Nyx")[i % 3])
        for i in range(60)
    ]
    interaction = _run(_cog(purchases=TITAN_BUYS, rentals=rentals), "Avenger Titan")

    calls = interaction.followup.send.call_args_list
    assert len(calls) > 1
    assert all("embed" not in call.kwargs for call in calls)
    contents = [call.kwargs["content"] for call in calls]
    assert all(len(content) <= 2000 for content in contents)
    text = "\n".join(contents)
    for i in range(60):
        assert f"Extremely Long Station Name Number {i:02d}" in text
    assert "1,290,370 aUEC" in text
    assert RENTAL_RATE_NOTE in text
    assert FOOTER_TEXT in text
    assert "**Rent — Pyro**" in text
    _assert_no_mentions_anywhere(interaction)


def test_a_normal_embed_stays_within_discords_total_size_limit():
    interaction = _run(_cog(purchases=TITAN_BUYS, rentals=TITAN_RENTS), "Avenger Titan")
    embed = interaction.followup.send.call_args.kwargs["embed"]
    assert isinstance(embed, discord.Embed) and len(embed) <= 6000


# -- listed_ship_autocomplete ----------------------------------------------------------

def _autocomplete(*, vehicles=VEHICLES, purchases_all=(), rentals_all=(), current=""):
    interaction = NS(client=NS(uex=NS(
        get_vehicles=_result(vehicles),
        get_vehicle_purchase_prices_all=_result(
            purchases_all if isinstance(purchases_all, BaseException) else list(purchases_all)),
        get_vehicle_rental_prices_all=_result(
            rentals_all if isinstance(rentals_all, BaseException) else list(rentals_all)),
    )))
    return asyncio.run(listed_ship_autocomplete(interaction, current))


def test_autocomplete_only_suggests_ships_with_a_buy_or_rent_row():
    choices = _autocomplete(purchases_all=[{"id_vehicle": 27}], rentals_all=[{"id_vehicle": 52}])
    assert [c.value for c in choices] == ["Avenger Titan", "Cutlass Black"]


def test_autocomplete_matches_on_ids_not_the_all_endpoints_short_names():
    choices = _autocomplete(purchases_all=[{"id_vehicle": 50, "vehicle_name": "Something Else Entirely"}])
    assert [c.value for c in choices] == ["Corsair"]


def test_autocomplete_filters_by_the_typed_text():
    choices = _autocomplete(purchases_all=[{"id_vehicle": 27}, {"id_vehicle": 52}], current="cut")
    assert [c.value for c in choices] == ["Cutlass Black"]


def test_autocomplete_falls_back_to_whichever_all_endpoint_succeeded():
    choices = _autocomplete(purchases_all=UexApiError("down"), rentals_all=[{"id_vehicle": 52}])
    assert [c.value for c in choices] == ["Cutlass Black"]
    choices = _autocomplete(purchases_all=[{"id_vehicle": 27}], rentals_all=UexApiError("down"))
    assert [c.value for c in choices] == ["Avenger Titan"]


def test_autocomplete_returns_nothing_when_both_all_endpoints_fail():
    assert _autocomplete(purchases_all=UexApiError("down"), rentals_all=UexApiError("down")) == []


def test_autocomplete_returns_nothing_when_the_vehicle_list_fails():
    assert _autocomplete(vehicles=UexApiError("down"), purchases_all=[{"id_vehicle": 27}]) == []


def test_autocomplete_caps_at_discords_25_choice_limit():
    vehicles = [{"id": i, "name": f"Ship {i}"} for i in range(40)]
    choices = _autocomplete(vehicles=vehicles, purchases_all=[{"id_vehicle": i} for i in range(40)])
    assert len(choices) == 25


# -- UexClient's four ship-shop methods, through a real client over MockTransport -------

def test_uex_client_ship_shop_methods_hit_the_right_endpoint_with_the_right_params():
    """Every cog/autocomplete test above stubs these methods, so a misspelled endpoint or
    param name would pass them all - this goes through the real UexClient instead."""
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        endpoint = request.url.path.rsplit("/", 1)[-1]
        return httpx.Response(200, json={"status": "ok", "data": [{"endpoint": endpoint}]})

    async def run():
        client = UexClient(app_token="test", base_url="https://uex.test")
        await client._client.aclose()
        client._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        try:
            return [
                await client.get_vehicle_purchase_prices(52),
                await client.get_vehicle_rental_prices(52),
                await client.get_vehicle_purchase_prices_all(),
                await client.get_vehicle_rental_prices_all(),
            ]
        finally:
            await client.aclose()

    results = asyncio.run(run())

    expected = [
        ("vehicles_purchases_prices", {"id_vehicle": "52"}),
        ("vehicles_rentals_prices", {"id_vehicle": "52"}),
        ("vehicles_purchases_prices_all", {}),
        ("vehicles_rentals_prices_all", {}),
    ]
    assert [(r.url.path, dict(r.url.params)) for r in requests] == [(f"/{e}", p) for e, p in expected]
    assert results == [[{"endpoint": endpoint}] for endpoint, _ in expected]
    for endpoint, _ in expected:
        assert _ENDPOINT_CACHE_TTL[endpoint] == 12 * 3600, "UEX documents a 12h cache TTL for this endpoint"
