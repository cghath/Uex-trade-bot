"""In-game Item Finder: pure ranking/formatting logic (bot/uex/item_finder.py) and the
/ingame-item-finder command end to end."""
from __future__ import annotations

import asyncio
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock

from bot.cogs.item_finder import ItemFinder, MAX_RESULTS_SHOWN
from bot.uex.item_finder import ItemListing, format_item_listing_line, location_breadcrumb, rank_item_listings


def _row(id_terminal, terminal_name, price_buy, **overrides):
    base = dict(id_terminal=id_terminal, terminal_name=terminal_name, price_buy=price_buy)
    base.update(overrides)
    return base


# -- location_breadcrumb -------------------------------------------------------------

def test_location_breadcrumb_excludes_star_system():
    """Star system is deliberately left out here - it's the field/section header in the
    cog, not repeated on every line."""
    row = _row(
        10, "Dumper's Depot", 100,
        star_system_name="Stanton", planet_name="ArcCorp", city_name="Area18",
    )
    assert location_breadcrumb(row) == "ArcCorp → Area18 → Dumper's Depot"


def test_location_breadcrumb_skips_missing_levels():
    row = _row(10, "Some Outpost", 100, outpost_name=None, city_name=None)
    assert location_breadcrumb(row) == "Some Outpost"


def test_location_breadcrumb_falls_back_to_bare_terminal_name_with_no_location_data():
    row = _row(10, "Mystery Shop", 100)
    assert location_breadcrumb(row) == "Mystery Shop"


def test_location_breadcrumb_prefers_moon_over_orbit_and_outpost_over_space_station():
    row = _row(
        10, "Klescher Trading Post", 100,
        orbit_name="Crusader", moon_name="Aberdeen",
        space_station_name="Some Station", outpost_name="Klescher",
    )
    assert location_breadcrumb(row) == "Aberdeen → Klescher → Klescher Trading Post"


# -- rank_item_listings ----------------------------------------------------------------

def test_rank_item_listings_sorts_closest_first():
    listings = [
        _row(1, "Far Shop", 100),
        _row(2, "Near Shop", 120),
        _row(3, "Mid Shop", 110),
    ]
    ranked = rank_item_listings(listings, {1: 50.0, 2: 1.0, 3: 10.0})
    assert [r.id_terminal for r in ranked] == [2, 3, 1]


def test_rank_item_listings_unknown_distance_sorts_last_when_no_origin_system_given():
    """A terminal /terminals_distances couldn't price (a lookup failure, or a genuinely
    cross-system pair) must never be treated as distance 0 - that would wrongly bury a
    genuinely close option behind it."""
    listings = [_row(1, "Unknown Distance Shop", 100), _row(2, "Known Far Shop", 100)]
    ranked = rank_item_listings(listings, {1: None, 2: 999.0})
    assert [r.id_terminal for r in ranked] == [2, 1]


def test_rank_item_listings_same_system_unknown_distance_beats_cross_system_known_distance():
    """Real bug, caught live: /terminals_distances returns a bare `false` for SOME pairs
    even within the same star system (e.g. Admin - Seraphim -> Skutters - GrimHEX, both in
    Crusader orbit) - without this fallback tier, that genuinely nearby shop sorted dead
    last behind every cross-system shop UEX COULD measure, however far, and could be
    truncated off the display entirely."""
    listings = [
        _row(1, "Same System, Unknown Distance", 100, star_system_name="Stanton"),
        _row(2, "Cross System, Known Far", 100, star_system_name="Pyro"),
    ]
    ranked = rank_item_listings(listings, {1: None, 2: 80.0}, origin_star_system="Stanton")
    assert [r.id_terminal for r in ranked] == [1, 2]


def test_rank_item_listings_known_distance_still_wins_within_the_same_system_tier():
    """A same-system fallback tier only changes which TIER an unknown listing competes in -
    a same-system shop with a REAL known distance must still sort ahead of a same-system
    shop with an unknown one."""
    listings = [
        _row(1, "Same System, Unknown Distance", 100, star_system_name="Stanton"),
        _row(2, "Same System, Known Close", 100, star_system_name="Stanton"),
    ]
    ranked = rank_item_listings(listings, {1: None, 2: 5.0}, origin_star_system="Stanton")
    assert [r.id_terminal for r in ranked] == [2, 1]


def test_rank_item_listings_excludes_a_non_positive_or_missing_price():
    listings = [
        _row(1, "Zero Price", 0),
        _row(2, "Negative Price", -5),
        _row(3, "Missing Price", None),
        _row(4, "Real Listing", 50),
    ]
    ranked = rank_item_listings(listings, {1: 1.0, 2: 1.0, 3: 1.0, 4: 1.0})
    assert [r.id_terminal for r in ranked] == [4]


def test_rank_item_listings_excludes_a_row_with_no_terminal_id():
    listings = [dict(price_buy=100), _row(1, "Real Listing", 50)]
    ranked = rank_item_listings(listings, {1: 1.0})
    assert [r.id_terminal for r in ranked] == [1]


def test_rank_item_listings_carries_the_location_label_system_and_price_through():
    listings = [_row(1, "Dumper's Depot", 1234.5, star_system_name="Stanton")]
    ranked = rank_item_listings(listings, {1: 2.5})
    assert ranked[0].price_buy == 1234.5
    assert ranked[0].location_label == "Dumper's Depot"
    assert ranked[0].star_system_name == "Stanton"
    assert ranked[0].distance_gm == 2.5


# -- format_item_listing_line -----------------------------------------------------------

def test_format_item_listing_line_shows_distance_and_price():
    listing = ItemListing(
        id_terminal=1, terminal_name="X", location_label="X", star_system_name="Stanton",
        price_buy=1500, distance_gm=3.25,
    )
    line = format_item_listing_line(listing)
    assert "1,500 aUEC" in line
    assert "3.2 Gm away" in line


def test_format_item_listing_line_zero_distance_says_already_here():
    listing = ItemListing(
        id_terminal=1, terminal_name="X", location_label="X", star_system_name="Stanton",
        price_buy=100, distance_gm=0.0,
    )
    assert "you're already here" in format_item_listing_line(listing)


def test_format_item_listing_line_unknown_distance_with_no_origin_system_says_so_plainly():
    listing = ItemListing(
        id_terminal=1, terminal_name="X", location_label="X", star_system_name="Pyro",
        price_buy=100, distance_gm=None,
    )
    assert format_item_listing_line(listing).endswith("distance unknown")


def test_format_item_listing_line_unknown_distance_in_the_players_own_system_says_so():
    listing = ItemListing(
        id_terminal=1, terminal_name="X", location_label="X", star_system_name="Stanton",
        price_buy=100, distance_gm=None,
    )
    line = format_item_listing_line(listing, origin_star_system="Stanton")
    assert "same system, exact distance unknown" in line


def test_format_item_listing_line_unknown_distance_in_a_different_system_says_plain_unknown():
    listing = ItemListing(
        id_terminal=1, terminal_name="X", location_label="X", star_system_name="Pyro",
        price_buy=100, distance_gm=None,
    )
    line = format_item_listing_line(listing, origin_star_system="Stanton")
    assert line.endswith("distance unknown")
    assert "same system" not in line


# -- /ingame-item-finder command end to end ----------------------------------------------

class _FakeInteraction:
    def __init__(self) -> None:
        self.response = NS(defer=AsyncMock())
        self.followup = NS(send=AsyncMock())


def _cog(*, resolved_terminal, catalog, items_prices, distance_by_pair=None, origin_star_system=None):
    """distance_by_pair: {(origin, destination): {"distance": float}} - AsyncMock side
    effect keyed on the actual (origin, destination) args get_terminal_distance is called
    with, so a test can assert exactly which pairs were (or weren't) looked up live."""
    distance_by_pair = distance_by_pair or {}

    async def _get_terminal_distance(origin, destination):
        return distance_by_pair.get((origin, destination))

    cog = ItemFinder.__new__(ItemFinder)
    cog.bot = NS(
        db=NS(
            resolve_terminal_id_by_name=AsyncMock(return_value=resolved_terminal),
            get_terminal_star_system=AsyncMock(return_value=origin_star_system),
        ),
        uex=NS(
            get_item_catalog=AsyncMock(return_value=catalog),
            get_items_prices=AsyncMock(return_value=items_prices),
            get_terminal_distance=AsyncMock(side_effect=_get_terminal_distance),
        ),
    )
    return cog


def test_ingame_item_finder_happy_path_sorts_closest_first_grouped_by_system():
    async def run():
        cog = _cog(
            resolved_terminal=(1, "Area18 TDD"),
            catalog=[{"id": 5, "name": "P4-AR"}],
            items_prices=[
                _row(10, "Far Armory", 1200, item_name="P4-AR", star_system_name="Stanton"),
                _row(20, "Near Armory", 1300, item_name="P4-AR", star_system_name="Stanton"),
            ],
            distance_by_pair={(1, 10): {"distance": 50.0}, (1, 20): {"distance": 2.0}},
            origin_star_system="Stanton",
        )
        interaction = _FakeInteraction()

        await cog.ingame_item_finder.callback(cog, interaction, item="P4-AR", location="Area18 TDD")
        return interaction

    interaction = asyncio.run(run())
    interaction.response.defer.assert_awaited_once()
    embed = interaction.followup.send.call_args.kwargs["embed"]
    assert "P4-AR" in embed.title
    stanton_field = next(f for f in embed.fields if f.name == "Stanton")
    lines = stanton_field.value.splitlines()
    assert lines[0].startswith("**Near Armory**"), "closest terminal must be listed first"
    assert "1,300 aUEC" in lines[0] and "2.0 Gm away" in lines[0]
    assert "1,200 aUEC" in lines[1] and "50.0 Gm away" in lines[1]


def test_ingame_item_finder_groups_results_into_one_field_per_star_system():
    async def run():
        cog = _cog(
            resolved_terminal=(1, "Area18 TDD"),
            catalog=[{"id": 5, "name": "P4-AR"}],
            items_prices=[
                _row(10, "Stanton Shop", 1200, item_name="P4-AR", star_system_name="Stanton"),
                _row(20, "Pyro Shop", 1300, item_name="P4-AR", star_system_name="Pyro"),
            ],
            distance_by_pair={(1, 20): {"distance": 80.0}},
            origin_star_system="Stanton",
        )
        interaction = _FakeInteraction()

        await cog.ingame_item_finder.callback(cog, interaction, item="P4-AR", location="Area18 TDD")
        return interaction

    interaction = asyncio.run(run())
    embed = interaction.followup.send.call_args.kwargs["embed"]
    field_names = [f.name for f in embed.fields]
    assert field_names == ["Stanton", "Pyro"], "the origin's own system must be the first group"
    stanton_field = next(f for f in embed.fields if f.name == "Stanton")
    pyro_field = next(f for f in embed.fields if f.name == "Pyro")
    assert "Stanton Shop" in stanton_field.value
    assert "Pyro Shop" in pyro_field.value


def test_ingame_item_finder_real_bug_grim_hex_same_system_unknown_distance_still_shown():
    """Regression for the exact live incident: a same-system shop (Skutters - GrimHEX) UEX
    couldn't compute a distance for was missing from the results entirely, buried behind
    every cross-system shop and truncated off past MAX_RESULTS_SHOWN."""
    async def run():
        items_prices = [
            _row(143, "Skutters - GrimHEX", 4500, item_name="Boomtube Rocket", star_system_name="Stanton"),
            *[
                _row(200 + i, f"Pyro Shop {i}", 4219, item_name="Boomtube Rocket", star_system_name="Pyro")
                for i in range(MAX_RESULTS_SHOWN)
            ],
        ]
        distance_by_pair = {(259, 200 + i): {"distance": 50.0 + i} for i in range(MAX_RESULTS_SHOWN)}
        # (259, 143) deliberately absent -> UEX's real observed `false` response, so the
        # fake get_terminal_distance returns None for it, same as production.
        cog = _cog(
            resolved_terminal=(259, "Admin - Seraphim"), catalog=[{"id": 5, "name": "Boomtube Rocket"}],
            items_prices=items_prices, distance_by_pair=distance_by_pair, origin_star_system="Stanton",
        )
        interaction = _FakeInteraction()

        await cog.ingame_item_finder.callback(cog, interaction, item="Boomtube Rocket", location="Admin - Seraphim")
        return interaction

    interaction = asyncio.run(run())
    embed = interaction.followup.send.call_args.kwargs["embed"]
    stanton_field = next(f for f in embed.fields if f.name == "Stanton")
    assert "Skutters - GrimHEX" in stanton_field.value
    assert "same system, exact distance unknown" in stanton_field.value


def test_ingame_item_finder_unknown_location_says_so_and_makes_no_other_calls():
    async def run():
        cog = _cog(resolved_terminal=None, catalog=[], items_prices=[])
        interaction = _FakeInteraction()

        await cog.ingame_item_finder.callback(cog, interaction, item="P4-AR", location="Nowhere")
        return cog, interaction

    cog, interaction = asyncio.run(run())
    message = interaction.followup.send.call_args.args[0]
    assert "Nowhere" in message
    cog.bot.uex.get_item_catalog.assert_not_awaited()


def test_ingame_item_finder_unknown_item_says_so():
    async def run():
        cog = _cog(
            resolved_terminal=(1, "Area18 TDD"), catalog=[{"id": 5, "name": "P4-AR"}], items_prices=[],
        )
        interaction = _FakeInteraction()

        await cog.ingame_item_finder.callback(cog, interaction, item="Nonexistent Gun", location="Area18 TDD")
        return interaction

    interaction = asyncio.run(run())
    message = interaction.followup.send.call_args.args[0]
    assert "Nonexistent Gun" in message
    assert "embed" not in interaction.followup.send.call_args.kwargs


def test_ingame_item_finder_no_listings_says_so():
    async def run():
        cog = _cog(
            resolved_terminal=(1, "Area18 TDD"), catalog=[{"id": 5, "name": "P4-AR"}], items_prices=[],
        )
        interaction = _FakeInteraction()

        await cog.ingame_item_finder.callback(cog, interaction, item="P4-AR", location="Area18 TDD")
        return interaction

    interaction = asyncio.run(run())
    message = interaction.followup.send.call_args.args[0]
    assert "No shop currently lists" in message


def test_ingame_item_finder_origin_terminal_itself_never_gets_a_live_distance_call():
    """The player's own location, if it happens to also sell the item, is trivially 0
    distance away - no need to burn a live /terminals_distances call confirming that."""
    async def run():
        cog = _cog(
            resolved_terminal=(1, "Area18 TDD"),
            catalog=[{"id": 5, "name": "P4-AR"}],
            items_prices=[_row(1, "Area18 TDD", 1200, item_name="P4-AR", star_system_name="Stanton")],
            origin_star_system="Stanton",
        )
        interaction = _FakeInteraction()

        await cog.ingame_item_finder.callback(cog, interaction, item="P4-AR", location="Area18 TDD")
        return cog, interaction

    cog, interaction = asyncio.run(run())
    cog.bot.uex.get_terminal_distance.assert_not_awaited()
    embed = interaction.followup.send.call_args.kwargs["embed"]
    stanton_field = next(f for f in embed.fields if f.name == "Stanton")
    assert "you're already here" in stanton_field.value


def test_ingame_item_finder_a_cross_system_distance_lookup_failure_still_shows_the_listing():
    """A single failed /terminals_distances call for a genuinely cross-system pair must
    not crash the whole command or silently drop that shop - it shows with 'distance
    unknown' (no same-system fallback applies, since it isn't the origin's system) and
    sorts after every successfully-measured option in its own system group."""
    async def run():
        cog = _cog(
            resolved_terminal=(1, "Area18 TDD"),
            catalog=[{"id": 5, "name": "P4-AR"}],
            items_prices=[
                _row(10, "Broken Distance Shop", 1200, item_name="P4-AR", star_system_name="Pyro"),
                _row(11, "Normal Pyro Shop", 1300, item_name="P4-AR", star_system_name="Pyro"),
            ],
            distance_by_pair={(1, 11): {"distance": 5.0}},  # (1, 10) deliberately absent -> None
            origin_star_system="Stanton",
        )
        interaction = _FakeInteraction()

        await cog.ingame_item_finder.callback(cog, interaction, item="P4-AR", location="Area18 TDD")
        return interaction

    interaction = asyncio.run(run())
    embed = interaction.followup.send.call_args.kwargs["embed"]
    pyro_field = next(f for f in embed.fields if f.name == "Pyro")
    lines = pyro_field.value.splitlines()
    assert "Normal Pyro Shop" in lines[0], "the successfully-measured shop must sort first"
    assert "distance unknown" in lines[1]


def test_ingame_item_finder_truncates_and_discloses_omitted_count():
    async def run():
        items_prices = [
            _row(i, f"Shop {i}", 100 + i, item_name="P4-AR", star_system_name="Stanton")
            for i in range(1, MAX_RESULTS_SHOWN + 6)
        ]
        distance_by_pair = {(1, i): {"distance": float(i)} for i in range(1, MAX_RESULTS_SHOWN + 6)}
        cog = _cog(
            resolved_terminal=(1, "Area18 TDD"), catalog=[{"id": 5, "name": "P4-AR"}],
            items_prices=items_prices, distance_by_pair=distance_by_pair, origin_star_system="Stanton",
        )
        interaction = _FakeInteraction()

        await cog.ingame_item_finder.callback(cog, interaction, item="P4-AR", location="Area18 TDD")
        return interaction

    interaction = asyncio.run(run())
    embed = interaction.followup.send.call_args.kwargs["embed"]
    stanton_field = next(f for f in embed.fields if f.name == "Stanton")
    assert len(stanton_field.value.splitlines()) == MAX_RESULTS_SHOWN
    assert "5 more shop(s) omitted" in embed.footer.text
