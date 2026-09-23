"""In-game Item Finder: pure ranking/formatting logic (bot/uex/item_finder.py) and the
/ingame-item-finder command end to end."""
from __future__ import annotations

import asyncio
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock

from bot.cogs.item_finder import ItemFinder, MAX_RESULTS_SHOWN, sold_item_name_autocomplete
from bot.uex.item_finder import ItemListing, format_item_listing_line, rank_item_listings, split_place_and_vendor


def _row(id_terminal, terminal_name, price_buy, **overrides):
    base = dict(id_terminal=id_terminal, terminal_name=terminal_name, price_buy=price_buy)
    base.update(overrides)
    return base


def _listing(id_terminal, place_label, price_buy, *, vendor_label=None, star_system_name="Stanton",
             distance_gm=1.0):
    return ItemListing(
        id_terminal=id_terminal, terminal_name=f"{vendor_label or 'V'} - {place_label}",
        place_label=place_label, vendor_label=vendor_label, star_system_name=star_system_name,
        price_buy=price_buy, distance_gm=distance_gm,
    )


# -- split_place_and_vendor -----------------------------------------------------------

def test_split_place_and_vendor_splits_on_the_vendor_dash_place_convention():
    row = _row(10, "Skutters - GrimHEX", 100)
    assert split_place_and_vendor(row) == ("GrimHEX", "Skutters")


def test_split_place_and_vendor_splits_on_the_last_separator_only():
    row = _row(10, "Guns - Ammo - Checkmate", 100)
    assert split_place_and_vendor(row) == ("Checkmate", "Guns - Ammo")


def test_split_place_and_vendor_falls_back_to_structured_field_with_no_separator():
    """Real exception found in live data: 'Equipment Contested Zone Checkmate' has no
    ' - ' separator at all - falls back to the structured location field instead of
    showing the whole raw terminal name as the place."""
    row = _row(10, "Equipment Contested Zone Checkmate", 100, space_station_name="Checkmate Station")
    assert split_place_and_vendor(row) == ("Checkmate Station", None)


def test_split_place_and_vendor_fallback_prefers_city_over_outpost_over_space_station():
    row = _row(
        10, "No Separator Here", 100,
        city_name="Area18", outpost_name="Some Outpost", space_station_name="Some Station",
    )
    assert split_place_and_vendor(row) == ("Area18", None)


def test_split_place_and_vendor_falls_back_to_bare_terminal_name_with_no_location_data():
    row = _row(10, "Mystery Shop", 100)
    assert split_place_and_vendor(row) == ("Mystery Shop", None)


def test_split_place_and_vendor_strips_whitespace_around_both_parts():
    row = _row(10, "Guns  -  Orbituary", 100)
    assert split_place_and_vendor(row) == ("Orbituary", "Guns")


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


def test_rank_item_listings_carries_the_place_vendor_system_and_price_through():
    listings = [_row(1, "Skutters - GrimHEX", 1234.5, star_system_name="Stanton")]
    ranked = rank_item_listings(listings, {1: 2.5})
    assert ranked[0].price_buy == 1234.5
    assert ranked[0].place_label == "GrimHEX"
    assert ranked[0].vendor_label == "Skutters"
    assert ranked[0].star_system_name == "Stanton"
    assert ranked[0].distance_gm == 2.5


# -- format_item_listing_line -----------------------------------------------------------

def test_format_item_listing_line_shows_place_vendor_price_and_distance():
    listing = _listing(1, "GrimHEX", 1500, vendor_label="Skutters", distance_gm=3.25)
    line = format_item_listing_line(listing)
    assert "**GrimHEX**" in line
    assert "(Skutters)" in line
    assert "1,500 aUEC" in line
    assert "3.2 Gm" in line


def test_format_item_listing_line_zero_distance_says_here():
    listing = _listing(1, "X", 100, distance_gm=0.0)
    assert "here" in format_item_listing_line(listing)


def test_format_item_listing_line_unknown_distance_with_no_origin_system_says_so_plainly():
    listing = _listing(1, "X", 100, star_system_name="Pyro", distance_gm=None)
    line = format_item_listing_line(listing)
    assert line.endswith("unknown")
    assert "same system" not in line


def test_format_item_listing_line_unknown_distance_in_the_players_own_system_says_so():
    listing = _listing(1, "X", 100, star_system_name="Stanton", distance_gm=None)
    line = format_item_listing_line(listing, origin_star_system="Stanton")
    assert "same system" in line


def test_format_item_listing_line_unknown_distance_in_a_different_system_says_plain_unknown():
    listing = _listing(1, "X", 100, star_system_name="Pyro", distance_gm=None)
    line = format_item_listing_line(listing, origin_star_system="Stanton")
    assert line.endswith("unknown")
    assert "same system" not in line


def test_format_item_listing_line_no_vendor_omits_the_parenthetical():
    listing = _listing(1, "Mystery Shop", 100, vendor_label=None)
    line = format_item_listing_line(listing)
    assert "**Mystery Shop**" in line
    assert "(" not in line


def test_format_item_listing_line_never_truncates_a_long_place_or_vendor():
    """Real bug, caught live: a fixed-width table column either truncated two DIFFERENT
    real places down to identical displayed text, or - once widened to fix that - got wide
    enough that Discord wrapped it inside an embed field and broke column alignment
    anyway. Plain text has neither failure mode: a long name just appears in full."""
    listing = _listing(
        1, "People's Service Station Lambda", 100, vendor_label="Weapons and Armor",
    )
    line = format_item_listing_line(listing)
    assert "People's Service Station Lambda" in line
    assert "Weapons and Armor" in line
    assert "…" not in line


def test_format_item_listing_line_two_different_long_places_stay_distinguishable():
    listing_a = _listing(1, "People's Service Station Alpha", 100, vendor_label="Weapons and Armor")
    listing_b = _listing(2, "People's Service Station Lambda", 100, vendor_label="Weapons and Armor")
    line_a = format_item_listing_line(listing_a)
    line_b = format_item_listing_line(listing_b)
    assert line_a != line_b
    assert "Alpha" in line_a
    assert "Lambda" in line_b


# -- sold_item_name_autocomplete ---------------------------------------------------------

def _price_row(item_name):
    return {"item_name": item_name}


def test_sold_item_name_autocomplete_filters_by_current_input():
    async def run():
        rows = [_price_row("Omnisky III Cannon"), _price_row("Karna Rifle"), _price_row("Arclight Pistol")]
        interaction = NS(client=NS(uex=NS(get_items_prices_all=AsyncMock(return_value=rows))))

        choices = await sold_item_name_autocomplete(interaction, "arc")

        assert {c.value for c in choices} == {"Arclight Pistol"}

    asyncio.run(run())


def test_sold_item_name_autocomplete_never_suggests_an_item_with_no_price_row():
    """The whole point of this autocomplete: an item that's only in the catalog, with no
    real shop listing (cosmetics, ship paint, and similar - confirmed live that ~5,000 of
    7,769 catalog items are like this), must never appear as a suggestion - unlike
    Marketplace's traded_item_autocomplete, there is no fallback to the full catalog here."""
    async def run():
        rows = [_price_row("Omnisky III Cannon")]
        interaction = NS(client=NS(uex=NS(get_items_prices_all=AsyncMock(return_value=rows))))

        choices = await sold_item_name_autocomplete(interaction, "livery")

        assert choices == []

    asyncio.run(run())


def test_sold_item_name_autocomplete_dedupes_the_same_item_across_many_terminals():
    async def run():
        rows = [_price_row("Omnisky III Cannon") for _ in range(50)]
        interaction = NS(client=NS(uex=NS(get_items_prices_all=AsyncMock(return_value=rows))))

        choices = await sold_item_name_autocomplete(interaction, "omnisky")

        assert len(choices) == 1

    asyncio.run(run())


def test_sold_item_name_autocomplete_caps_at_discords_25_choice_limit():
    async def run():
        rows = [_price_row(f"Test Item {i}") for i in range(40)]
        interaction = NS(client=NS(uex=NS(get_items_prices_all=AsyncMock(return_value=rows))))

        choices = await sold_item_name_autocomplete(interaction, "test")

        assert len(choices) == 25

    asyncio.run(run())


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
    assert "1,300 aUEC" in lines[0] and "2.0 Gm" in lines[0]
    assert "1,200 aUEC" in lines[1] and "50.0 Gm" in lines[1]


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
    assert "GrimHEX" in stanton_field.value
    assert "Skutters" in stanton_field.value
    assert "same system" in stanton_field.value


def test_ingame_item_finder_shows_vendor_for_two_shops_at_the_same_place():
    """The whole reason for a separate vendor label: two different shops at the SAME
    place (e.g. two gun stores both at Checkmate Station) must stay distinguishable, not
    collapse into two identical-looking lines."""
    async def run():
        cog = _cog(
            resolved_terminal=(1, "Area18 TDD"),
            catalog=[{"id": 5, "name": "P4-AR"}],
            items_prices=[
                _row(437, "Guns - Checkmate", 1200, item_name="P4-AR", star_system_name="Stanton"),
                _row(438, "Sharp Shooters - Checkmate", 1300, item_name="P4-AR", star_system_name="Stanton"),
            ],
            distance_by_pair={(1, 437): {"distance": 10.0}, (1, 438): {"distance": 10.0}},
            origin_star_system="Stanton",
        )
        interaction = _FakeInteraction()

        await cog.ingame_item_finder.callback(cog, interaction, item="P4-AR", location="Area18 TDD")
        return interaction

    interaction = asyncio.run(run())
    embed = interaction.followup.send.call_args.kwargs["embed"]
    stanton_field = next(f for f in embed.fields if f.name == "Stanton")
    lines = stanton_field.value.splitlines()
    assert len(lines) == 2
    assert all("Checkmate" in line for line in lines), "both lines share the same place"
    assert "Guns" in lines[0] and "Sharp Shooters" not in lines[0]
    assert "Sharp Shooters" in lines[1] and "Guns" not in lines[1]


def test_ingame_item_finder_long_similar_place_names_stay_distinguishable():
    """End-to-end regression for the live-reported truncation-collision bug: four
    real-shaped Nyx terminals sharing a long common prefix ('People's Service Station
    Alpha/Delta/Theta/Lambda') must each still show their own distinguishing suffix."""
    async def run():
        cog = _cog(
            resolved_terminal=(1, "Aparelli - New Babbage"),
            catalog=[{"id": 5, "name": "Scalpel Sniper Rifle"}],
            items_prices=[
                _row(
                    100 + i, f"Weapons and Armor - People's Service Station {suffix}", 9519,
                    item_name="Scalpel Sniper Rifle", star_system_name="Nyx",
                )
                for i, suffix in enumerate(["Alpha", "Delta", "Theta", "Lambda"])
            ],
            distance_by_pair={(1, 100 + i): {"distance": 100.0 + i} for i in range(4)},
        )
        interaction = _FakeInteraction()

        await cog.ingame_item_finder.callback(
            cog, interaction, item="Scalpel Sniper Rifle", location="Aparelli - New Babbage",
        )
        return interaction

    interaction = asyncio.run(run())
    embed = interaction.followup.send.call_args.kwargs["embed"]
    nyx_field = next(f for f in embed.fields if f.name == "Nyx")
    lines = nyx_field.value.splitlines()
    assert len(lines) == 4
    assert len(set(lines)) == 4, "four different real places must render as four distinct lines"
    for suffix in ["Alpha", "Delta", "Theta", "Lambda"]:
        assert any(suffix in line for line in lines), f"{suffix} must still be visible, not truncated away"


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
    assert "here" in stanton_field.value


def test_ingame_item_finder_a_cross_system_distance_lookup_failure_still_shows_the_listing():
    """A single failed /terminals_distances call for a genuinely cross-system pair must
    not crash the whole command or silently drop that shop - it shows with 'unknown'
    distance (no same-system fallback applies, since it isn't the origin's system) and
    sorts after every successfully-measured option in its own system group."""
    async def run():
        cog = _cog(
            resolved_terminal=(1, "Area18 TDD"),
            catalog=[{"id": 5, "name": "P4-AR"}],
            items_prices=[
                _row(10, "Broken Shop", 1200, item_name="P4-AR", star_system_name="Pyro"),
                _row(11, "Normal Shop", 1300, item_name="P4-AR", star_system_name="Pyro"),
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
    assert "Normal Shop" in lines[0], "the successfully-measured shop must sort first"
    assert lines[1].endswith("unknown")


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
