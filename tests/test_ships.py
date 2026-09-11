"""Tests for bot/uex/ships.py's route cargo math."""
from __future__ import annotations

from bot.uex.ships import estimate_route_cargo


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
