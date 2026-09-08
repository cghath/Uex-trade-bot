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
