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


def build_fair_price_index(average_rows: list[dict]) -> dict[int, FairPrice]:
    """From /marketplace_prices_averages_all rows, build {id_item: FairPrice} using each
    item's 30-day rolling sell-side average as the "steal" baseline.

    An item can have several rows (one per quality_tier/currency/unit combo), but a
    Marketplace listing itself almost never reports a comparable quality value (see
    parse_listing_quality's docstring - most listings never set one), so there's no
    reliable way to match a specific listing to a specific tier's average. Rather than
    guess, this takes the LOWEST price_avg_month across an item's tiers as the fair
    baseline: the cheapest quality tier still selling at that price is real, legitimate
    market data (not scanner noise), and comparing every listing against the most
    conservative baseline available means a listing has to undercut even the cheapest
    legitimate tier to be flagged - erring toward fewer false "steal" positives, not more.
    """
    fair_prices: dict[int, FairPrice] = {}
    for row in average_rows:
        if (row.get("operation") or "").strip().lower() != SELL_OPERATION:
            continue
        id_item = parse_uex_number(row.get("id_item"))
        price = parse_uex_number(row.get("price_avg_month"))
        if id_item is None or price is None or price <= 0:
            continue
        id_item = int(id_item)
        current = fair_prices.get(id_item)
        if current is None or price < current.price_avg_month:
            fair_prices[id_item] = FairPrice(item_name=row.get("item_name") or "Unknown item", price_avg_month=price)
    return fair_prices


def find_steals(listings: list[dict], fair_prices: dict[int, FairPrice], threshold: float) -> list[StealEntry]:
    """Compare live sell listings against `fair_prices` (see build_fair_price_index),
    returning every listing priced at least `threshold` (e.g. 0.20 = 20%) below its
    item's fair price - sorted by discount, steepest first. A listing whose id_item
    isn't in `fair_prices` (no averages data for that item yet) is skipped rather than
    guessed at.
    """
    steals: list[StealEntry] = []
    for listing in listings:
        if (listing.get("operation") or "").strip().lower() != SELL_OPERATION:
            continue
        id_item = listing.get("id_item")
        if id_item is None or id_item not in fair_prices:
            continue

        listing_price = parse_uex_number(listing.get("price"))
        fair = fair_prices[id_item]
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
                currency=listing.get("currency") or "UEC",
                seller=listing.get("user_username") or listing.get("user_name") or "unknown seller",
                quality=parse_listing_quality(listing.get("quality")),
            )
        )

    steals.sort(key=lambda s: s.discount_pct, reverse=True)
    return steals
