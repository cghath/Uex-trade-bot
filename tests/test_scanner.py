"""Tests for the Raw Materials Deal Scanner's pure matching logic in bot/uex/scanner.py."""
from __future__ import annotations

from bot.uex.marketplace import quality_to_tier
from bot.uex.scanner import (
    ALLOWED_MARKETPLACE_CATEGORY_IDS,
    MIN_LISTINGS_FOR_FAIR_PRICE,
    build_fair_price_index,
    find_steals,
)

COMMODITIES_CATEGORY_ID = 36  # a real id from ALLOWED_MARKETPLACE_CATEGORY_IDS
QUALITY = 850  # -> tier 5, per bot/uex/marketplace.py: quality_to_tier
TIER = quality_to_tier(QUALITY)
KEY = (42, TIER, "UEC", "unit")


def _average_row(**overrides) -> dict:
    """A /marketplace_prices_averages_all row (numeric fields as JSON strings, per UEX's
    Marketplace quirk - see bot/uex/marketplace.py: parse_uex_number)."""
    row = {
        "id_item": 42,
        "item_name": "Laranite Raw",
        "operation": "sell",
        "quality_tier": TIER,
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
        "id_category": COMMODITIES_CATEGORY_ID,
        "operation": "sell",
        "title": "Selling Laranite",
        "price": "1000",
        "currency": "UEC",
        "unit": "unit",
        "user_username": "trader_joe",
        "quality": QUALITY,
    }
    row.update(overrides)
    return row


# --- build_fair_price_index ---


def test_build_fair_price_index_keys_by_exact_quality_tier():
    fair_prices = build_fair_price_index([_average_row(price_avg_month="900")])
    assert fair_prices[KEY].price_avg_month == 900.0
    assert fair_prices[KEY].item_name == "Laranite Raw"


def test_build_fair_price_index_keeps_quality_tiers_separate():
    """Regression test: a real live scan flagged '322-776 Savrilium bulk' (quality 322,
    tier 1) at 93% off against a price of 6,402,439 UEC - but that price came from
    quality_tier 5's average, the only tier with enough listings to pass the
    sample-size filter, not tier 1's. Tiers must never be conflated - each is priced
    completely differently (verified live: one commodity's 30-day average ranged from
    ~150K to 200M+ UEC across its tiers)."""
    rows = [
        _average_row(price_avg_month="1000", quality_tier=1),
        _average_row(price_avg_month="6402439", quality_tier=5),
    ]
    fair_prices = build_fair_price_index(rows)
    assert fair_prices[(42, 1, "UEC", "unit")].price_avg_month == 1000.0
    assert fair_prices[(42, 5, "UEC", "unit")].price_avg_month == 6402439.0


def test_build_fair_price_index_keeps_currencies_and_units_separate():
    """Regression test: an item priced in UEC and also (separately) in WIF, or per-unit
    vs per-crate, must never have their averages conflated - taking the "lowest" across
    unrelated currencies/units produced wildly wrong "steal" discounts in manual
    testing."""
    rows = [
        _average_row(price_avg_month="1000", currency="UEC", unit="unit"),
        _average_row(price_avg_month="5", currency="WIF", unit="unit"),
        _average_row(price_avg_month="50", currency="UEC", unit="crate"),
    ]
    fair_prices = build_fair_price_index(rows)
    assert fair_prices[(42, TIER, "UEC", "unit")].price_avg_month == 1000.0
    assert fair_prices[(42, TIER, "WIF", "unit")].price_avg_month == 5.0
    assert fair_prices[(42, TIER, "UEC", "crate")].price_avg_month == 50.0


def test_build_fair_price_index_excludes_rows_below_min_listings():
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


def test_build_fair_price_index_skips_rows_missing_quality_tier():
    row = _average_row(price_avg_month="1")
    del row["quality_tier"]
    assert build_fair_price_index([row]) == {}


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
    assert fair_prices[(1, TIER, "UEC", "unit")].price_avg_month == 2000.0
    assert fair_prices[(2, TIER, "UEC", "unit")].price_avg_month == 800.0


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
    assert steal.quality == QUALITY


def test_find_steals_excludes_listing_below_threshold():
    fair_prices = build_fair_price_index([_average_row(price_avg_month="1000")])
    listings = [_listing(price="850")]  # 15% off, threshold is 20%
    assert find_steals(listings, fair_prices, threshold=0.20) == []


def test_find_steals_ignores_buy_side_listings():
    fair_prices = build_fair_price_index([_average_row(price_avg_month="1000")])
    listings = [_listing(operation="buy", price="100")]
    assert find_steals(listings, fair_prices, threshold=0.20) == []


def test_find_steals_does_not_match_across_quality_tiers():
    """Regression test for the exact real-world false positive described in
    build_fair_price_index's test_build_fair_price_index_keeps_quality_tiers_separate:
    a listing at quality 322 (tier 1) must never be compared against tier 5's average,
    even though tier 5 is the only one available and the 'discount' would look huge."""
    fair_prices = build_fair_price_index([_average_row(price_avg_month="6402439", quality_tier=5)])
    listings = [_listing(price="450000", quality=322)]  # tier 1 - no matching average exists
    assert find_steals(listings, fair_prices, threshold=0.20) == []


def test_find_steals_skips_listings_with_no_reported_quality():
    """A listing with no quality value can't be matched to a specific tier, so it's
    skipped rather than guessed at - even if an average happens to exist at some tier."""
    fair_prices = build_fair_price_index([_average_row(price_avg_month="1000")])
    listing = _listing(price="500", quality=None)
    assert find_steals([listing], fair_prices, threshold=0.20) == []


def test_find_steals_does_not_match_across_currencies():
    """Only a UEC-priced average exists for this item/tier - a WIF-priced listing of
    the same item/tier must not fall back to it, even though 5 WIF looks like a
    massive "discount" against 1000 UEC."""
    fair_prices = build_fair_price_index([_average_row(price_avg_month="1000", currency="UEC")])
    listings = [_listing(price="5", currency="WIF")]
    assert find_steals(listings, fair_prices, threshold=0.20) == []


def test_find_steals_does_not_match_across_units():
    """Only a per-crate average exists for this item/tier - a listing priced per unit
    must not fall back to it, even though 1 looks like a massive "discount" against 50."""
    fair_prices = build_fair_price_index([_average_row(price_avg_month="50", unit="crate")])
    listings = [_listing(price="1", unit="unit")]
    assert find_steals(listings, fair_prices, threshold=0.20) == []


def test_find_steals_excludes_listings_outside_allowed_categories():
    """Crafted gear (weapons, armor, ship components) has real stat variance driven by
    crafting material quality that UEX never exposes as structured data - only free
    text ("Q970", "-44% dmg") a seller happened to type. Only a small allowlist of
    raw-material categories (verified safe - see ALLOWED_MARKETPLACE_CATEGORY_IDS) is
    ever considered."""
    assert COMMODITIES_CATEGORY_ID in ALLOWED_MARKETPLACE_CATEGORY_IDS  # sanity-check the fixture
    disallowed_category_id = max(ALLOWED_MARKETPLACE_CATEGORY_IDS) + 1000  # guaranteed not allowlisted
    fair_prices = build_fair_price_index([_average_row(price_avg_month="1000")])
    listings = [_listing(price="500", id_category=disallowed_category_id)]  # 50% off, would otherwise flag
    assert find_steals(listings, fair_prices, threshold=0.20) == []


def test_find_steals_excludes_listings_with_missing_category():
    """A listing with no id_category at all can't be verified safe, so it's excluded
    the same as an explicitly disallowed one - not given the benefit of the doubt."""
    fair_prices = build_fair_price_index([_average_row(price_avg_month="1000")])
    listing = _listing(price="500")
    del listing["id_category"]
    assert find_steals([listing], fair_prices, threshold=0.20) == []


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
    listings = [_listing(price="500", quality=QUALITY, user_username="ace_trader")]
    (steal,) = find_steals(listings, fair_prices, threshold=0.20)
    assert steal.quality == QUALITY
    assert steal.seller == "ace_trader"
