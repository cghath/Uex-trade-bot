"""Tests for the Undervalued Scanner's pure matching logic in bot/uex/scanner.py."""
from __future__ import annotations

from bot.uex.scanner import build_fair_price_index, find_steals


def _average_row(**overrides) -> dict:
    """A /marketplace_prices_averages_all row (numeric fields as JSON strings, per UEX's
    Marketplace quirk - see bot/uex/marketplace.py: parse_uex_number)."""
    row = {
        "id_item": 42,
        "item_name": "Laranite Raw",
        "operation": "sell",
        "quality_tier": 0,
        "price_avg": "1000",
        "price_avg_week": "1000",
        "price_avg_month": "1000",
    }
    row.update(overrides)
    return row


def _listing(**overrides) -> dict:
    """A /marketplace_listings row."""
    row = {
        "id": 1,
        "id_item": 42,
        "operation": "sell",
        "title": "Selling Laranite",
        "price": "1000",
        "currency": "UEC",
        "user_username": "trader_joe",
        "quality": None,
    }
    row.update(overrides)
    return row


# --- build_fair_price_index ---


def test_build_fair_price_index_picks_lowest_tier_average():
    rows = [
        _average_row(price_avg_month="1200", quality_tier=5),
        _average_row(price_avg_month="900", quality_tier=0),
        _average_row(price_avg_month="1500", quality_tier=7),
    ]
    fair_prices = build_fair_price_index(rows)
    assert fair_prices[42].price_avg_month == 900.0
    assert fair_prices[42].item_name == "Laranite Raw"


def test_build_fair_price_index_ignores_buy_side_rows():
    rows = [_average_row(operation="buy", price_avg_month="1")]
    assert build_fair_price_index(rows) == {}


def test_build_fair_price_index_skips_unparsable_or_zero_prices():
    rows = [
        _average_row(price_avg_month=None),
        _average_row(price_avg_month="0"),
        _average_row(price_avg_month="not-a-number"),
    ]
    assert build_fair_price_index(rows) == {}


def test_build_fair_price_index_keeps_items_separate():
    rows = [
        _average_row(id_item=1, item_name="Gold", price_avg_month="2000"),
        _average_row(id_item=2, item_name="Laranite Raw", price_avg_month="800"),
    ]
    fair_prices = build_fair_price_index(rows)
    assert fair_prices[1].price_avg_month == 2000.0
    assert fair_prices[2].price_avg_month == 800.0


# --- find_steals ---


def test_find_steals_flags_listing_at_or_above_threshold():
    fair_prices = build_fair_price_index([_average_row(price_avg_month="1000")])
    listings = [_listing(price="800")]  # 20% off
    (steal,) = find_steals(listings, fair_prices, threshold=0.20)
    assert steal.listing_id == 1
    assert steal.item_name == "Laranite Raw"
    assert steal.listing_price == 800.0
    assert steal.fair_price == 1000.0
    assert steal.discount_pct == 20.0


def test_find_steals_excludes_listing_below_threshold():
    fair_prices = build_fair_price_index([_average_row(price_avg_month="1000")])
    listings = [_listing(price="850")]  # 15% off, threshold is 20%
    assert find_steals(listings, fair_prices, threshold=0.20) == []


def test_find_steals_ignores_buy_side_listings():
    fair_prices = build_fair_price_index([_average_row(price_avg_month="1000")])
    listings = [_listing(operation="buy", price="100")]
    assert find_steals(listings, fair_prices, threshold=0.20) == []


def test_find_steals_skips_listings_with_no_averages_data():
    fair_prices = build_fair_price_index([_average_row(id_item=42, price_avg_month="1000")])
    listings = [_listing(id_item=999, price="1")]
    assert find_steals(listings, fair_prices, threshold=0.20) == []


def test_find_steals_skips_listings_missing_price_or_id():
    fair_prices = build_fair_price_index([_average_row(price_avg_month="1000")])
    listings = [_listing(price=None), _listing(price="0"), _listing(id=None, price="1")]
    assert find_steals(listings, fair_prices, threshold=0.20) == []


def test_find_steals_sorted_by_discount_descending():
    fair_prices = build_fair_price_index([_average_row(price_avg_month="1000")])
    listings = [
        _listing(id=1, price="850"),  # 15% off
        _listing(id=2, price="500"),  # 50% off
        _listing(id=3, price="700"),  # 30% off
    ]
    steals = find_steals(listings, fair_prices, threshold=0.0)
    assert [s.listing_id for s in steals] == [2, 3, 1]


def test_find_steals_carries_quality_and_seller_through():
    fair_prices = build_fair_price_index([_average_row(price_avg_month="1000")])
    listings = [_listing(price="500", quality="75", user_username="ace_trader")]
    (steal,) = find_steals(listings, fair_prices, threshold=0.20)
    assert steal.quality == 75.0
    assert steal.seller == "ace_trader"
