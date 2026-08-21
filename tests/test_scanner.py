"""Tests for the Undervalued Scanner's pure matching logic in bot/uex/scanner.py."""
from __future__ import annotations

from bot.uex.scanner import MIN_LISTINGS_FOR_FAIR_PRICE, build_fair_price_index, find_steals

KEY = (42, "UEC", "unit")


def _average_row(**overrides) -> dict:
    """A /marketplace_prices_averages_all row (numeric fields as JSON strings, per UEX's
    Marketplace quirk - see bot/uex/marketplace.py: parse_uex_number)."""
    row = {
        "id_item": 42,
        "item_name": "Laranite Raw",
        "operation": "sell",
        "quality_tier": 0,
        "currency": "UEC",
        "unit": "unit",
        "listings_count": 5,  # comfortably above MIN_LISTINGS_FOR_FAIR_PRICE by default
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
        "unit": "unit",
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
    assert fair_prices[KEY].price_avg_month == 900.0
    assert fair_prices[KEY].item_name == "Laranite Raw"


def test_build_fair_price_index_keeps_currencies_and_units_separate():
    """Regression test: an item priced in UEC and also (separately) in WIF, or per-unit
    vs per-crate, must never have their averages conflated - taking the "lowest" across
    unrelated currencies/units produced wildly wrong "steal" discounts in manual testing
    (a live scan flagged ~115 listings, most at 90-100% off, before this was keyed on
    currency/unit)."""
    rows = [
        _average_row(price_avg_month="1000", currency="UEC", unit="unit"),
        _average_row(price_avg_month="5", currency="WIF", unit="unit"),
        _average_row(price_avg_month="50", currency="UEC", unit="crate"),
    ]
    fair_prices = build_fair_price_index(rows)
    assert fair_prices[(42, "UEC", "unit")].price_avg_month == 1000.0
    assert fair_prices[(42, "WIF", "unit")].price_avg_month == 5.0
    assert fair_prices[(42, "UEC", "crate")].price_avg_month == 50.0


def test_build_fair_price_index_excludes_rows_below_min_listings():
    """Regression test: a real live scan flagged 'Novikov Backpack Halcyon' as 94% off -
    its averages row had listings_count=2, with the average almost certainly dominated
    by one outlier-priced listing. Thin-sample averages must not be trusted as a
    baseline, however low their price_avg_month is."""
    assert MIN_LISTINGS_FOR_FAIR_PRICE >= 2  # sanity-check the constant hasn't regressed to 1/0
    thin = _average_row(price_avg_month="1", listings_count=MIN_LISTINGS_FOR_FAIR_PRICE - 1)
    assert build_fair_price_index([thin]) == {}

    well_supported = _average_row(price_avg_month="1000", listings_count=MIN_LISTINGS_FOR_FAIR_PRICE)
    assert build_fair_price_index([well_supported])[KEY].price_avg_month == 1000.0


def test_build_fair_price_index_treats_missing_listings_count_as_untrustworthy():
    row = _average_row(price_avg_month="1")
    del row["listings_count"]
    assert build_fair_price_index([row]) == {}


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
    assert fair_prices[(1, "UEC", "unit")].price_avg_month == 2000.0
    assert fair_prices[(2, "UEC", "unit")].price_avg_month == 800.0


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


def test_find_steals_does_not_match_across_currencies():
    """Regression test for the currency-conflation bug (see
    test_build_fair_price_index_keeps_currencies_and_units_separate). Only a UEC-priced
    average exists for this item - a WIF-priced listing of the same item must not fall
    back to it, even though 5 WIF looks like a massive "discount" against 1000 UEC."""
    fair_prices = build_fair_price_index([_average_row(price_avg_month="1000", currency="UEC")])
    listings = [_listing(price="5", currency="WIF")]
    assert find_steals(listings, fair_prices, threshold=0.20) == []


def test_find_steals_does_not_match_across_units():
    """Only a per-crate average exists for this item - a listing priced per unit must
    not fall back to it, even though 1 looks like a massive "discount" against 50."""
    fair_prices = build_fair_price_index([_average_row(price_avg_month="50", unit="crate")])
    listings = [_listing(price="1", unit="unit")]
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
