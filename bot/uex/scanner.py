"""Pure matching logic for the Undervalued Scanner: comparing live Marketplace sell
listings against UEX's own precomputed price averages to find listings priced well
below their item's "fair" average - a steal for whoever buys it.

Kept dependency-free (no Discord, no I/O) like bot/uex/stock_alerts.py and
bot/uex/trends.py, so the matching rules are unit-testable against plain dicts - the
actual API calls and Discord delivery live in bot/cogs/scanner.py.

Scope: only sell-side listings (things for sale - a buyer's opportunity) are compared
against sell-side averages. A "steal" has no clean buy-side analogue (a buy listing
offering an unusually *high* price would be the mirror case - a good deal for a seller,
not what this feature is about), and averages are themselves operation-scoped rows (see
bot/uex/marketplace.py: parse_marketplace_average_rows) - mixing sides would compare a
sell price against a buy-side average or vice versa, never a meaningful comparison.
"""
from __future__ import annotations

from dataclasses import dataclass

from bot.uex.marketplace import parse_listing_quality, parse_uex_number

SELL_OPERATION = "sell"

# Verified against live data: a rarely-traded item's "30-day average" can be built from
# just 1-2 historical listings (see build_fair_price_index's docstring) - one outlier
# listing (a troll price, a typo, a "make me an offer" placeholder) then dominates the
# average outright, making it useless as a "fair price" baseline. Requiring a minimum
# sample size filters those out; 3 is small enough that a real, moderately-traded item
# still qualifies, but large enough that a single outlier can no longer single-handedly
# set the average.
MIN_LISTINGS_FOR_FAIR_PRICE = 3


@dataclass
class FairPrice:
    item_name: str
    price_avg_month: float


@dataclass
class StealEntry:
    listing_id: int
    item_name: str
    listing_title: str
    listing_price: float
    fair_price: float
    discount_pct: float
    currency: str
    seller: str
    quality: float | None


def _price_key(id_item: int, currency: str, unit: str) -> tuple[int, str, str]:
    return (id_item, currency.strip().upper(), unit.strip().lower())


def build_fair_price_index(average_rows: list[dict]) -> dict[tuple[int, str, str], FairPrice]:
    """From /marketplace_prices_averages_all rows, build {(id_item, currency, unit):
    FairPrice} using each item's 30-day rolling sell-side average as the "steal" baseline.

    Keyed on currency and unit, not just id_item: UEX returns a separate averages row per
    id_item x quality_tier x operation x currency x unit combination (see
    bot/uex/marketplace.py: parse_marketplace_average_rows) - an item can have both a
    UEC-priced row and a WIF/MGS-priced row, or a "per unit" row alongside a "per
    crate"/"per scu" row. Comparing a listing's price against an average from a different
    currency or unit isn't a real discount, just mismatched numbers that happen to differ
    by orders of magnitude - keying on all three keeps the comparison apples-to-apples.

    An item can still have several rows sharing one (currency, unit) pair (one per
    quality_tier), but a Marketplace listing itself almost never reports a comparable
    quality value (see parse_listing_quality's docstring - most listings never set one),
    so there's no reliable way to match a specific listing to a specific tier's average.
    Rather than guess, this takes the LOWEST price_avg_month across those tiers as the
    fair baseline: the cheapest quality tier still selling at that price is real,
    legitimate market data (not scanner noise), and comparing every listing against the
    most conservative baseline available means a listing has to undercut even the
    cheapest legitimate tier to be flagged - erring toward fewer false "steal" positives.

    A row whose listings_count is below MIN_LISTINGS_FOR_FAIR_PRICE is skipped entirely,
    not just deprioritized - see that constant's comment for why a thin sample size makes
    an average untrustworthy as a baseline, regardless of how it compares numerically to
    other tiers.
    """
    fair_prices: dict[tuple[int, str, str], FairPrice] = {}
    for row in average_rows:
        if (row.get("operation") or "").strip().lower() != SELL_OPERATION:
            continue
        id_item = parse_uex_number(row.get("id_item"))
        price = parse_uex_number(row.get("price_avg_month"))
        listings_count = parse_uex_number(row.get("listings_count"))
        if id_item is None or price is None or price <= 0:
            continue
        if listings_count is None or listings_count < MIN_LISTINGS_FOR_FAIR_PRICE:
            continue
        currency = row.get("currency") or "UEC"
        unit = row.get("unit") or "unit"
        key = _price_key(int(id_item), currency, unit)
        current = fair_prices.get(key)
        if current is None or price < current.price_avg_month:
            fair_prices[key] = FairPrice(item_name=row.get("item_name") or "Unknown item", price_avg_month=price)
    return fair_prices


def find_steals(
    listings: list[dict], fair_prices: dict[tuple[int, str, str], FairPrice], threshold: float
) -> list[StealEntry]:
    """Compare live sell listings against `fair_prices` (see build_fair_price_index),
    returning every listing priced at least `threshold` (e.g. 0.20 = 20%) below its
    item's fair price in the SAME currency and unit - sorted by discount, steepest
    first. A listing with no averages row for its exact (id_item, currency, unit) is
    skipped rather than guessed at.
    """
    steals: list[StealEntry] = []
    for listing in listings:
        if (listing.get("operation") or "").strip().lower() != SELL_OPERATION:
            continue
        id_item = listing.get("id_item")
        if id_item is None:
            continue

        currency = listing.get("currency") or "UEC"
        unit = listing.get("unit") or "unit"
        key = _price_key(id_item, currency, unit)
        fair = fair_prices.get(key)
        if fair is None:
            continue

        listing_price = parse_uex_number(listing.get("price"))
        if listing_price is None or listing_price <= 0:
            continue

        discount = (fair.price_avg_month - listing_price) / fair.price_avg_month
        if discount < threshold:
            continue

        listing_id = listing.get("id")
        if listing_id is None:
            continue

        steals.append(
            StealEntry(
                listing_id=listing_id,
                item_name=fair.item_name,
                listing_title=listing.get("title") or "Untitled listing",
                listing_price=listing_price,
                fair_price=fair.price_avg_month,
                discount_pct=round(discount * 100, 1),
                currency=currency,
                seller=listing.get("user_username") or listing.get("user_name") or "unknown seller",
                quality=parse_listing_quality(listing.get("quality")),
            )
        )

    steals.sort(key=lambda s: s.discount_pct, reverse=True)
    return steals
