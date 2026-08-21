"""Tests for parse_marketplace_average_rows against synthetic /marketplace_prices_averages
rows, including the Marketplace API's string-typed-numbers quirk (see parse_uex_number)."""
from __future__ import annotations

from bot.uex.marketplace import parse_marketplace_average_rows


def _row(**overrides) -> dict:
    """A realistic /marketplace_prices_averages row; prices as JSON strings on purpose,
    since that's how Marketplace endpoints have actually been observed to send them."""
    row = {
        "id": 1,
        "id_item": 42,
        "item_name": "Laranite Raw",
        "quality_tier": 6,
        "operation": "sell",
        "currency": "UEC",
        "unit": "scu",
        "listings_count": 5,
        "price_avg": "1180",
        "price_avg_week": "1200.5",
        "price_avg_month": "1234",
        "game_version": "4.3",
        "date_added": 1755000000,
        "date_modified": 1755600000,
    }
    row.update(overrides)
    return row


def test_string_prices_are_coerced_to_floats():
    entries = parse_marketplace_average_rows([_row()])
    assert len(entries) == 1
    entry = entries[0]
    assert entry.price_avg == 1180.0
    assert entry.price_avg_week == 1200.5
    assert entry.price_avg_month == 1234.0
    assert entry.item_name == "Laranite Raw"
    assert entry.currency == "UEC"
    assert entry.unit == "scu"
    assert entry.listings_count == 5


def test_row_with_no_parsable_prices_is_dropped():
    rows = [
        _row(price_avg=None, price_avg_week=None, price_avg_month=None),
        _row(price_avg="garbage", price_avg_week=None, price_avg_month=None),
        _row(),
    ]
    entries = parse_marketplace_average_rows(rows)
    assert len(entries) == 1


def test_partial_prices_are_kept_with_missing_as_none():
    entries = parse_marketplace_average_rows([_row(price_avg_week=None, price_avg_month=None)])
    assert len(entries) == 1
    assert entries[0].price_avg == 1180.0
    assert entries[0].price_avg_week is None
    assert entries[0].price_avg_month is None


def test_quality_tier_zero_is_a_real_tier_not_unset():
    entries = parse_marketplace_average_rows([_row(quality_tier=0)])
    assert entries[0].quality_tier == 0


def test_string_quality_tier_and_listings_count_are_coerced():
    entries = parse_marketplace_average_rows([_row(quality_tier="7", listings_count="12")])
    assert entries[0].quality_tier == 7
    assert entries[0].listings_count == 12


def test_missing_listings_count_defaults_to_zero():
    entries = parse_marketplace_average_rows([_row(listings_count=None)])
    assert entries[0].listings_count == 0


def test_sorted_sell_first_then_tier_ascending_then_currency():
    rows = [
        _row(operation="buy", quality_tier=1),
        _row(operation="sell", quality_tier=7),
        _row(operation="sell", quality_tier=2, currency="WIF"),
        _row(operation="sell", quality_tier=2, currency="UEC"),
        _row(operation="sell", quality_tier=None),
    ]
    entries = parse_marketplace_average_rows(rows)
    order = [(e.operation, e.quality_tier, e.currency) for e in entries]
    assert order == [
        ("sell", 2, "UEC"),
        ("sell", 2, "WIF"),
        ("sell", 7, "UEC"),
        ("sell", None, "UEC"),  # untiered rows sort after every real tier
        ("buy", 1, "UEC"),
    ]


def test_unknown_operation_sorts_last_instead_of_crashing():
    rows = [_row(operation="TRADE"), _row(operation="buy")]
    entries = parse_marketplace_average_rows(rows)
    assert [e.operation for e in entries] == ["buy", "trade"]


def test_defaults_for_missing_currency_and_unit():
    entries = parse_marketplace_average_rows([_row(currency=None, unit=None)])
    assert entries[0].currency == "UEC"
    assert entries[0].unit == "unit"
