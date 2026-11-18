"""Tests for bot/uex/ships.py: ship-name lookup (typos included) and route cargo math."""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock

from bot.cogs.ships import ship_name_autocomplete
from bot.uex.ships import estimate_route_cargo, resolve_ship, ship_name_suggestions

# UEX's /vehicles names (2026-11-18), the real list the typo tier was calibrated against.
VEHICLES = json.loads((Path(__file__).parent / "fixtures" / "vehicles.json").read_text(encoding="utf-8"))


def _name(query: str) -> str | None:
    vehicle = resolve_ship(VEHICLES, query)
    return vehicle["name"] if vehicle else None


# -- resolve_ship --------------------------------------------------------------------------------------------------

def test_an_exact_name_or_full_name_resolves():
    assert _name("tiburon") == "Tiburon"
    assert _name("Aegis Tiburon") == "Tiburon"


def test_a_unique_part_of_a_name_resolves_and_a_shared_one_does_not():
    assert _name("tibur") == "Tiburon"
    assert _name("cutlass") is None  # Black, Blue, Red, Steel...


def test_a_misspelled_name_resolves_to_its_ship():
    # Asked for 2026-11-18: "Tiberon" found nothing.
    assert _name("tiberon") == "Tiburon"
    assert _name("aegis tiberon") == "Tiburon"
    assert _name("cutlas black") == "Cutlass Black"
    assert _name("carrak") == "Carrack"


def test_a_typo_as_close_to_several_ships_is_declined():
    assert _name("constelation") is None  # every Constellation is as close


def test_a_word_close_to_no_ship_is_declined():
    assert _name("zorbix") is None
    assert _name("") is None


def test_no_typo_of_a_real_name_resolves_to_a_different_ship():
    # The calibration's rule, on a slice of it: each real name with its middle letter dropped and its first vowel
    # changed resolves to that ship or to nothing - never to another one. A "typo" that is itself a real name or part
    # of one ("e1 spirit" from A1 Spirit, "adin" from Odin, part of Paladin) is the exact and part-of-a-name lookups'
    # business, not a typo's, so it's left out, as the calibration left it out.
    names = {v["name"].strip().lower(): v["name"] for v in VEHICLES}
    spellings = [s.strip().lower() for v in VEHICLES for s in (v["name"], v.get("name_full") or "") if s.strip()]
    wrong, tried = [], 0
    for lower, name in names.items():
        middle = len(lower) // 2
        vowel = next((i for i, c in enumerate(lower) if c in "aeiou"), None)
        typos = [lower[:middle] + lower[middle + 1:]]
        if vowel is not None:
            typos.append(lower[:vowel] + ("a" if lower[vowel] != "a" else "e") + lower[vowel + 1:])
        for typo in typos:
            if any(typo in spelling for spelling in spellings):
                continue
            tried += 1
            got = _name(typo)
            if got is not None and got.lower() != lower:
                wrong.append((typo, name, got))
    assert tried > 400 and wrong == []


# -- the autocomplete --------------------------------------------------------------------------------------------

def test_suggestions_are_the_names_containing_what_was_typed():
    assert ship_name_suggestions(VEHICLES, "cutl")[:3] == ["Cutlass Black", "Cutlass Black Best In Show Edition",
                                                          "Cutlass Blue"]


def test_a_misspelling_still_offers_the_ship():
    assert ship_name_suggestions(VEHICLES, "tiber")[0] == "Tiburon"
    assert all(name.startswith("Constellation") for name in ship_name_suggestions(VEHICLES, "constelation")[:4])


def test_nothing_typed_offers_the_first_names_and_the_list_is_capped():
    assert len(ship_name_suggestions(VEHICLES, "")) == 25
    assert len(ship_name_suggestions(VEHICLES, "a", limit=5)) == 5


def test_the_autocomplete_offers_a_misspelled_ship():
    interaction = NS(client=NS(uex=NS(get_vehicles=AsyncMock(return_value=VEHICLES))))
    choices = asyncio.run(ship_name_autocomplete(interaction, "tiber"))
    assert choices[0].name == "Tiburon" and choices[0].value == "Tiburon"


# -- estimate_route_cargo ------------------------------------------------------------------------------------------


def test_estimate_route_cargo_computes_investment_from_price_origin():
    cargo = estimate_route_cargo(
        per_unit_profit=50.0, origin_scu_available=100, destination_scu_wanted=100,
        ship_cargo_scu=20, price_origin=100.0,
    )
    assert cargo.max_scu == 20
    assert cargo.investment == 2000.0
    assert cargo.run_profit == 1000.0


def test_estimate_route_cargo_investment_is_none_without_a_price():
    """price_origin is optional (backward-compat default) - a caller that doesn't pass it
    still gets a valid estimate, just without an investment figure."""
    cargo = estimate_route_cargo(
        per_unit_profit=50.0, origin_scu_available=100, destination_scu_wanted=100,
        ship_cargo_scu=20,
    )
    assert cargo.investment is None
    assert cargo.max_scu == 20


def test_estimate_route_cargo_investment_scales_with_the_stock_limited_quantity():
    """Investment reflects however much cargo is actually haulable (min of ship/stock),
    not the ship's full capacity."""
    cargo = estimate_route_cargo(
        per_unit_profit=50.0, origin_scu_available=5, destination_scu_wanted=100,
        ship_cargo_scu=20, price_origin=100.0,
    )
    assert cargo.max_scu == 5
    assert cargo.limited_by == "stock"
    assert cargo.investment == 500.0


def test_estimate_route_cargo_returns_none_with_no_stock_or_ship_data():
    assert estimate_route_cargo(
        per_unit_profit=50.0, origin_scu_available=None, destination_scu_wanted=None,
        ship_cargo_scu=None, price_origin=100.0,
    ) is None


def test_estimate_route_cargo_caps_by_budget_when_it_is_the_tightest_constraint():
    """Budget of 1,000 at 100 aUEC/unit affords 10 SCU - tighter than the 20 SCU ship
    hold and the 100 SCU of real stock, so it must be the one that binds."""
    cargo = estimate_route_cargo(
        per_unit_profit=50.0, origin_scu_available=100, destination_scu_wanted=100,
        ship_cargo_scu=20, price_origin=100.0, budget=1000.0,
    )
    assert cargo.max_scu == 10
    assert cargo.limited_by == "budget"
    assert cargo.investment == 1000.0
    assert cargo.run_profit == 500.0


def test_estimate_route_cargo_ignores_budget_when_it_is_not_the_tightest_constraint():
    cargo = estimate_route_cargo(
        per_unit_profit=50.0, origin_scu_available=100, destination_scu_wanted=100,
        ship_cargo_scu=20, price_origin=100.0, budget=1_000_000.0,
    )
    assert cargo.max_scu == 20
    assert cargo.limited_by == "ship"


def test_estimate_route_cargo_ignores_budget_without_a_known_price():
    """Nothing to divide the budget by without price_origin - matches the existing
    backward-compat behavior for a caller that omits price_origin entirely."""
    cargo = estimate_route_cargo(
        per_unit_profit=50.0, origin_scu_available=100, destination_scu_wanted=100,
        ship_cargo_scu=20, budget=1000.0,
    )
    assert cargo.max_scu == 20
    assert cargo.limited_by == "ship"


def test_estimate_route_cargo_a_budget_tie_with_ship_credits_ship():
    cargo = estimate_route_cargo(
        per_unit_profit=50.0, origin_scu_available=100, destination_scu_wanted=100,
        ship_cargo_scu=10, price_origin=100.0, budget=1000.0,
    )
    assert cargo.max_scu == 10
    assert cargo.limited_by == "ship"


def test_estimate_route_cargo_a_budget_tie_with_stock_credits_budget():
    """Between the two tied constraints, budget is more actionable than real-world stock
    (a player can bring more capital; nothing makes more stock exist)."""
    cargo = estimate_route_cargo(
        per_unit_profit=50.0, origin_scu_available=10, destination_scu_wanted=100,
        ship_cargo_scu=50, price_origin=100.0, budget=1000.0,
    )
    assert cargo.max_scu == 10
    assert cargo.limited_by == "budget"


def test_estimate_route_cargo_budget_alone_with_no_ship_or_stock_data():
    cargo = estimate_route_cargo(
        per_unit_profit=50.0, origin_scu_available=None, destination_scu_wanted=None,
        ship_cargo_scu=None, price_origin=100.0, budget=1000.0,
    )
    assert cargo.max_scu == 10
    assert cargo.limited_by == "budget"
