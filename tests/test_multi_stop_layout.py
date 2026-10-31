"""/multi-stop-route's layout text (bot/uex/route_presentation.py: multi_stop_blocks).

The owner picked it from real-data mockups on 2026-10-04: a summary, then a section per leg
with that leg's warnings under it, leg profit on every leg, no refuel/repair lines, and a
short footer. Synthetic routes, no Discord."""
from __future__ import annotations

from bot.uex.data_health import TerminalDataHealth
from bot.uex.mixed_routes import MixedCargoItem
from bot.uex.multi_stop_routes import MultiStopLeg, MultiStopRoute
from bot.uex.practical_routes import terminal_limit_notes, terminal_practical_notes
from bot.uex.price_outliers import index_commodity_prices
from bot.uex.route_confidence import RouteConfidence
from bot.uex.route_presentation import (
    MultiStopLegFacts,
    cargo_item_notes,
    cargo_item_warnings,
    format_gm,
    multi_stop_blocks,
    multi_stop_footer,
    system_crossing,
)

STATUS = {"buy": {1: dict(name_short="High")}, "sell": {2: dict(name_short="Low")}}
# Every confirmed risk flag present and false, so no "Cargo risk" line unless a test sets one.
SAFE = dict(is_illegal=0, is_explosive=0, is_volatile_time=0, is_volatile_qt=0, is_buggy=0, is_harvestable=0,
            is_fuel=0, is_inert=0)


def _station(name, system, **extra):
    return dict(SAFE, terminal_name=name, star_system_name=system, scu_buy=500, scu_sell=500,
                status_buy=1, status_sell=2, **extra)


def _item(id_commodity, name, quantity, buy, sell, source, destination, limits=("cargo space",)):
    return MixedCargoItem(id_commodity, name, quantity, buy, sell, 500, quantity * buy, quantity * (sell - buy),
                          source, destination, limiting_factors=limits)


ARC = _station("ArcCorp 045", "Stanton", id_terminal=1, max_container_size=24)
GATEWAY = _station("Stanton Gateway (Pyro)", "Pyro", id_terminal=2, is_refuel=1, is_repair=1, is_cargo_center=1,
                   max_container_size=32, has_loading_dock=1)
SEER = _station("Seer's Canyon", "Pyro", id_terminal=3, is_refuel=1, is_repair=1, max_container_size=16,
                has_loading_dock=1)
TRESSLER = _station("Port Tressler", "Stanton", id_terminal=4, has_loading_dock=1, max_container_size=32)


def _route():
    legs = (
        MultiStopLeg(1, "ArcCorp 045", 2, "Stanton Gateway (Pyro)",
                     (_item(10, "Quartz", 607, 3705, 5841, ARC, GATEWAY, ("stock",)),
                      _item(11, "Copper", 89, 400, 1375, ARC, GATEWAY)),
                     2_284_535, 3_667_762, 1_383_227, True),
        MultiStopLeg(2, "Stanton Gateway (Pyro)", 3, "Seer's Canyon",
                     (_item(12, "Recycled Material Composite", 84, 7587, 8500, GATEWAY, SEER, ("demand",)),),
                     637_308, 714_000, 76_692, True),
        MultiStopLeg(3, "Seer's Canyon", 4, "Port Tressler",
                     (_item(13, "Construction Materials", 696, 10023, 13000, SEER, TRESSLER),),
                     6_976_008, 9_048_000, 2_071_992, False),
    )
    return MultiStopRoute(legs, 5_515_989, 9_048_000, 3_532_011)


FACTS = [MultiStopLegFacts(68.0, None, None), MultiStopLegFacts(92.0, None, None), MultiStopLegFacts(160.0, None, None)]


def _blocks(route=None, facts=FACTS, **overrides):
    options = dict(index=5, ship_name="C2 Hercules Starlifter", space_only=False, leg_facts=facts,
                   confidence=RouteConfidence(68, "Medium"), footer="Prices can change before you arrive",
                   status_lookup=STATUS)
    return multi_stop_blocks(route or _route(), **{**options, **overrides})


def test_a_route_is_a_summary_a_section_per_leg_and_the_small_print():
    header, *legs, footer = _blocks()
    assert header.splitlines() == [
        "## #5 · ArcCorp 045 → Stanton Gateway (Pyro) → Seer's Canyon → Port Tressler",
        "Profit **3,532,011 aUEC** · ROI **64.0%**",
        "Investment **5,515,989** · Revenue **9,048,000 aUEC**",
        "Distance **320 Gm** total · Confidence **Medium (68/100)**",
        "-# 3-leg chain for C2 Hercules Starlifter · ranked by profit (ROI as a tie-breaker)",
    ]
    assert [leg.splitlines()[0] for leg in legs] == [
        "### Leg 1 · ArcCorp 045 → Stanton Gateway (Pyro)",
        "### Leg 2 · Stanton Gateway (Pyro) → Seer's Canyon",
        "### Leg 3 · Seer's Canyon → Port Tressler",
    ]
    assert footer == "-# Prices can change before you arrive"


def test_each_cargo_line_has_its_limit_and_market_status_in_small_print_under_it():
    leg_one = _blocks()[1].splitlines()
    assert leg_one[1:5] == [
        "**Quartz** · 607 SCU · +2,136/SCU · **+1,296,552**",
        "-# Limited by stock · market status: origin High, destination Low",
        "**Copper** · 89 SCU · +975/SCU · **+86,775**",
        "-# Limited by cargo space · market status: origin High, destination Low",
    ]


def test_every_leg_shows_its_own_profit_even_with_one_commodity():
    money = [next(line for line in leg.splitlines() if line.startswith("-# Investment")) for leg in _blocks()[1:-1]]
    assert money == [
        "-# Investment 2,284,535 · revenue 3,667,762 · leg profit 1,383,227 · 68 Gm",
        "-# Investment 637,308 · revenue 714,000 · leg profit 76,692 · 92 Gm",
        "-# Investment 6,976,008 · revenue 9,048,000 · leg profit 2,071,992 · 160 Gm",
    ]


def test_warnings_sit_under_their_own_leg_at_full_size():
    _, leg_one, leg_two, leg_three, _ = _blocks()
    assert "⚠️ Crosses systems: `Stanton` → `Pyro`" in leg_one.splitlines()
    assert "⚠️ ArcCorp 045: maximum container size 24 SCU" in leg_one.splitlines()
    assert "⚠️ Seer's Canyon: maximum container size 16 SCU" in leg_two.splitlines()
    assert "Crosses systems" not in leg_two, "Stanton Gateway (Pyro) and Seer's Canyon are both in Pyro"
    assert "⚠️ Crosses systems: `Pyro` → `Stanton`" in leg_three.splitlines()
    for block in (leg_one, leg_two, leg_three):
        assert all(not line.startswith("-# ⚠️") for line in block.splitlines()), "a warning is never small print"


def test_a_stations_own_facts_are_said_once_by_name_and_refuel_and_repair_are_left_out():
    blocks = _blocks()
    text = "\n".join(blocks)
    # Seer's Canyon ends leg 2 and starts leg 3; the old list said its container size twice.
    assert text.count("Seer's Canyon: maximum container size 16 SCU") == 1
    assert "Seer's Canyon: maximum container size 16 SCU" in blocks[2]
    assert text.count("Stanton Gateway (Pyro) has a cargo center") == 1
    assert blocks[1].splitlines()[-1] == "-# Stanton Gateway (Pyro) has a cargo center", "small print, last"
    assert "refuel" not in text and "repair" not in text
    assert "Origin" not in text and "Destination" not in text, "stations are named, not 'Origin'/'Destination'"


def test_stale_data_risky_cargo_and_price_outliers_are_full_size_warnings_on_their_leg():
    route = _route()
    risky = dict(GATEWAY, is_illegal=1)
    leg_two = route.legs[1]
    illegal = _item(12, "Recycled Material Composite", 84, 7587, 8500, risky, SEER, ("demand",))
    route = MultiStopRoute((route.legs[0], MultiStopLeg(2, leg_two.origin_name, 3, leg_two.destination_name,
                                                        (illegal,), 637_308, 714_000, 76_692, True),
                            route.legs[2]), route.investment, route.revenue, route.profit)
    stale = TerminalDataHealth("Seer's Canyon", "stale", 5, 3, 0, 0, False)
    facts = [FACTS[0], MultiStopLegFacts(92.0, None, stale), MultiStopLegFacts(160.0, stale, None)]
    others = [dict(id_commodity=13, id_terminal=terminal, price_buy=10_000, price_sell=0, scu_buy=10)
              for terminal in (20, 21, 22)]
    outliers = index_commodity_prices([*others, dict(id_commodity=13, id_terminal=3, price_buy=1_000,
                                                     price_sell=0, scu_buy=10)])
    route_three = route.legs[2]
    cheap = _item(13, "Construction Materials", 696, 1_000, 13000, SEER, TRESSLER)
    route = MultiStopRoute((*route.legs[:2], MultiStopLeg(3, route_three.origin_name, 4, route_three.destination_name,
                                                          (cheap,), 696_000, 9_048_000, 8_352_000, True)),
                           route.investment, route.revenue, route.profit)

    blocks = _blocks(route, facts, price_outlier_index=outliers)
    leg_two, leg_three = blocks[2].splitlines(), blocks[3].splitlines()
    assert "⚠️ Recycled Material Composite: Cargo risk: restricted in some jurisdictions" in leg_two, leg_two
    assert "⚠️ Seer's Canyon: stale terminal data (5d old)" in leg_two, "said where the route first stops there"
    assert not any("stale" in line for line in leg_three), "and not again"
    assert any(line.startswith("⚠️ Construction Materials origin buy price 1,000 is 10.0x below") for line in leg_three)


def test_distance_unavailable_for_a_leg_is_said_in_the_leg_and_the_total_is_marked_partial():
    facts = [FACTS[0], MultiStopLegFacts(None, None, None), FACTS[2]]
    header, _, leg_two, *_ = _blocks(facts=facts)
    assert "Distance **~228 Gm** (some legs' distance unavailable)" in header
    assert "-# Investment 637,308 · revenue 714,000 · leg profit 76,692 · distance unavailable" in leg_two.splitlines()


def test_space_only_is_named_in_the_summary():
    assert _blocks(space_only=True)[0].splitlines()[-1].endswith(" · space stations only")


def test_the_footer_says_only_what_a_player_can_act_on():
    assert multi_stop_footer(is_exact=True, budget=None, filters_note=None, capital_access_only=False) == (
        "Prices can change before you arrive")
    assert multi_stop_footer(is_exact=False, budget=250000, filters_note="Filters: auto-load-only (saved)",
                             capital_access_only=True) == (
        "Prices can change before you arrive · the cargo split is approximate · starting budget 250,000 aUEC"
        " · Filters: auto-load-only (saved) · Capital-ship access confirmed: XL hangar or external cargo"
        " loading dock at every stop")


def test_format_gm_drops_a_whole_number_decimal():
    assert (format_gm(17.0), format_gm(1250.0), format_gm(17.5)) == ("17 Gm", "1,250 Gm", "17.5 Gm")


def test_system_crossing_needs_two_known_different_systems():
    assert system_crossing("Stanton", "Pyro") == ("Stanton", "Pyro")
    assert system_crossing("Pyro", "Pyro") is None
    assert system_crossing(None, "Pyro") is None and system_crossing(" ", "Pyro") is None


def test_the_shared_warning_lines_other_commands_show_are_unchanged():
    """cargo_item_warnings and terminal_practical_notes are now built from the pieces the
    layout uses; every other route command still gets the same lines."""
    item = _route().legs[1].cargo[0]
    assert cargo_item_warnings(item, status_lookup=STATUS, prefix="Leg 2 ") == [
        "Leg 2 Recycled Material Composite: limited by demand (destination will take ~500 SCU)",
        "Leg 2 Recycled Material Composite market status: origin High · destination Low",
    ]
    assert cargo_item_notes(item, status_lookup=STATUS).market_status == ("origin High", "destination Low")
    assert terminal_practical_notes("Destination", SEER) == [
        "⚠️ Destination: maximum container size 16 SCU", "Destination services: refuel, repair"]
    assert terminal_limit_notes("Seer's Canyon", SEER) == ["⚠️ Seer's Canyon: maximum container size 16 SCU"]
