"""Pure matching logic for the Raw Materials Deal Scanner: comparing live Marketplace sell
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

Also scoped to a small allowlist of Marketplace categories (see
ALLOWED_MARKETPLACE_CATEGORY_IDS below) - most categories are player-crafted gear whose
real value depends on crafting material quality that UEX doesn't track anywhere in a
structured, comparable way, making price comparison for them fundamentally unreliable,
not just noisy.

Within the allowed raw-material categories, quality genuinely drives price (verified
against live data: a single commodity's 30-day average ranged from ~150K to over 200M
UEC across its quality_tier rows) - and unlike crafted gear, these listings reliably DO
report a real 0-1000 `quality` value (parse_listing_quality). So every match here is
tier-specific: a listing's own quality is converted to UEX's quality_tier bucket
(bot/uex/marketplace.py: quality_to_tier) and compared ONLY against that exact tier's average,
never a different tier substituted in because it happened to have more sample data.
"""
from __future__ import annotations

from dataclasses import dataclass

from bot.uex.inventory import _flag
from bot.uex.marketplace import parse_listing_quality, parse_uex_number, quality_to_tier

SELL_OPERATION = "sell"

# Verified against live data: a rarely-traded item/quality-tier combo's "30-day average"
# can be built from just 1-2 historical listings - one outlier listing (a troll price, a
# typo, a "make me an offer" placeholder) then dominates the average outright, making it
# useless as a "fair price" baseline. Requiring a minimum sample size filters those out;
# 3 is small enough that a real, moderately-traded item/tier still qualifies, but large
# enough that a single outlier can no longer single-handedly set the average.
MIN_LISTINGS_FOR_FAIR_PRICE = 3

# Verified against live data: most Marketplace categories (weapons, armor, ship
# components) are player-crafted, and the crafting material's quality meaningfully
# changes an item's real stats (damage %, armor mitigation, etc.) - but that quality is
# only ever communicated as free text in a listing's title/description ("Q970",
# "-44% dmg", "CRAFTED"), never a structured field UEX exposes. Neither
# /marketplace_listings' own `quality` field nor the averages' `quality_tier` capture
# it, and UEX's /items catalog doesn't even carry entries for most of these items to
# fall back on (checked directly - see the diagnostic history in PR discussion). So a
# "30-day average" for a craftable item is silently blended from whatever mix of
# crafted-and-boosted vs. stock listings happened to sell, making ANY price comparison
# for that category unreliable - not just noisy, structurally meaningless.
#
# Raw materials (id_category 36 "Commodities", 87 "Harvestables") don't have this
# problem: they're fungible, unmodified-by-players, and (unlike gear) reliably report a
# real quality value the averages' quality_tier can be matched against. Scanning is
# scoped to just these two categories rather than guessing at which other categories
# might also be safe - easy to extend later once a specific category is verified the
# same way.
ALLOWED_MARKETPLACE_CATEGORY_IDS = {36, 87}  # Commodities, Harvestables


@dataclass
class FairPrice:
    item_name: str
    price_avg_month: float


@dataclass
class StealEntry:
    listing_id: int
    id_item: int
    item_name: str
    listing_title: str
    listing_price: float
    fair_price: float
    discount_pct: float
    currency: str
    seller: str
    quality: float | None


def _price_key(id_item: int, quality_tier: int, currency: str, unit: str) -> tuple[int, int, str, str]:
    return (id_item, quality_tier, currency.strip().upper(), unit.strip().lower())


def build_fair_price_index(average_rows: list[dict]) -> dict[tuple[int, int, str, str], FairPrice]:
    """From /marketplace_prices_averages_all rows, build {(id_item, quality_tier,
    currency, unit): FairPrice} using each item/tier's 30-day rolling sell-side average
    as the "steal" baseline.

    Keyed on quality_tier as well as currency and unit: verified against live data that
    quality_tier genuinely drives price for raw materials (one commodity's 30-day
    average ranged from ~150K to 200M+ UEC across its tiers), so a listing must be
    compared against ITS OWN tier's average, never a different one. Currency/unit
    matter for the same "apples to apples" reason - UEX returns a separate row per
    id_item x quality_tier x operation x currency x unit combination (see
    bot/uex/marketplace.py: parse_marketplace_average_rows), and an item can have a
    UEC-priced row alongside a WIF/MGS one, or a "per unit" row alongside a "per
    crate"/"per scu" one.

    A row whose listings_count is below MIN_LISTINGS_FOR_FAIR_PRICE is skipped
    entirely - see that constant's comment for why a thin sample size makes an average
    untrustworthy as a baseline. This is now safe to do per-tier (rather than falling
    back to some other tier that happens to have more data) precisely because
    find_steals matches on the listing's own tier - see that function's docstring for
    why substituting a different tier's average produced real false positives in
    manual testing.
    """
    fair_prices: dict[tuple[int, int, str, str], FairPrice] = {}
    for row in average_rows:
        if (row.get("operation") or "").strip().lower() != SELL_OPERATION:
            continue
        id_item = parse_uex_number(row.get("id_item"))
        quality_tier = parse_uex_number(row.get("quality_tier"))
        price = parse_uex_number(row.get("price_avg_month"))
        listings_count = parse_uex_number(row.get("listings_count"))
        if id_item is None or quality_tier is None or price is None or price <= 0:
            continue
        if listings_count is None or listings_count < MIN_LISTINGS_FOR_FAIR_PRICE:
            continue
        currency = row.get("currency") or "UEC"
        unit = row.get("unit") or "unit"
        key = _price_key(int(id_item), int(quality_tier), currency, unit)
        current = fair_prices.get(key)
        if current is None or price < current.price_avg_month:
            fair_prices[key] = FairPrice(item_name=row.get("item_name") or "Unknown item", price_avg_month=price)
    return fair_prices


def find_steals(
    listings: list[dict], fair_prices: dict[tuple[int, int, str, str], FairPrice], threshold: float
) -> list[StealEntry]:
    """Compare live sell listings against `fair_prices` (see build_fair_price_index),
    returning every listing priced at least `threshold` (e.g. 0.20 = 20%) below its
    OWN quality tier's fair price in the same currency and unit - sorted by discount,
    steepest first.

    A listing with no reported quality is skipped, not compared against some other
    tier's average - confirmed against a real flagged listing ("322-776 Savrilium
    bulk", quality 322 -> tier 1) that a prior version of this function wrongly flagged
    at 93% off: the only average row that passed MIN_LISTINGS_FOR_FAIR_PRICE for that
    item/currency/unit happened to be quality_tier 5 (a materially more expensive
    tier), so the listing was compared against a price band it was never actually
    part of. Matching strictly on the listing's own tier - and skipping when that
    exact tier has no averages row, rather than substituting a different one - is what
    fixes that. Also skipped: any listing outside ALLOWED_MARKETPLACE_CATEGORY_IDS
    (see that constant for why), including one missing id_category entirely, since an
    unknown category can't be verified safe.
    """
    steals: list[StealEntry] = []
    for listing in listings:
        if (listing.get("operation") or "").strip().lower() != SELL_OPERATION:
            continue
        if listing.get("id_category") not in ALLOWED_MARKETPLACE_CATEGORY_IDS:
            continue
        id_item = listing.get("id_item")
        if id_item is None:
            continue

        # A sold-out or zero-remaining-stock listing isn't an opportunity - nothing is
        # actually purchasable at the flagged price, no matter how deep the discount.
        if _flag(listing.get("is_sold_out")):
            continue
        in_stock = parse_uex_number(listing.get("in_stock"))
        if in_stock is not None and in_stock <= 0:
            continue

        quality = parse_listing_quality(listing.get("quality"))
        if quality is None:
            continue
        quality_tier = quality_to_tier(quality)

        currency = listing.get("currency") or "UEC"
        unit = listing.get("unit") or "unit"
        key = _price_key(id_item, quality_tier, currency, unit)
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
                id_item=int(id_item),
                item_name=fair.item_name,
                listing_title=listing.get("title") or "Untitled listing",
                listing_price=listing_price,
                fair_price=fair.price_avg_month,
                discount_pct=round(discount * 100, 1),
                currency=currency,
                seller=listing.get("user_username") or listing.get("user_name") or "unknown seller",
                quality=quality,
            )
        )

    steals.sort(key=lambda s: s.discount_pct, reverse=True)
    return steals
