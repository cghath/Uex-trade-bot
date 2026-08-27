"""Pure helpers for personal inventory pricing and posting-time recommendations."""
from __future__ import annotations

import math
import re
import statistics
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable
from zoneinfo import ZoneInfo

from bot.uex.marketplace import parse_listing_quality, parse_uex_number, quality_to_tier

DEFAULT_MARKETPLACE_TIMEZONE = "America/New_York"
POSTING_WINDOW_HOURS = 4

# 'balanced' matches the schema default (no schema change needed for the common case).
# 'undercut'/'premium' let a user deliberately price off the recommended figure by a
# fixed spread rather than accepting the raw evidence-weighted number as-is.
PRICING_STRATEGY_MULTIPLIERS = {"balanced": 1.0, "undercut": 0.9, "premium": 1.1}


@dataclass(frozen=True)
class PostingWindow:
    start_hour: int
    end_hour: int
    scope: str
    confidence: str
    observation_count: int
    days_observed: int
    demand_events: float
    new_sell_listings: float

    @property
    def label(self) -> str:
        return f"{_format_hour(self.start_hour)}–{_format_hour(self.end_hour)}"


@dataclass(frozen=True)
class PriceRecommendation:
    price: int
    confidence: str
    evidence: tuple[str, ...]
    floor_applied: bool


def quality_label(quality: int) -> str:
    if quality <= 0:
        return "Q0 / standard"
    tier = quality_to_tier(quality)
    boundaries = {
        1: "Q1–499",
        2: "Q500–599",
        3: "Q600–699",
        4: "Q700–799",
        5: "Q800–899",
        6: "Q900–949",
        7: "Q950–1000",
    }
    return f"{quality} ({boundaries[tier]})"


def recommend_posting_window(
    rows: list[dict[str, Any]],
    *,
    id_item: int | None = None,
    timezone_name: str = DEFAULT_MARKETPLACE_TIMEZONE,
) -> PostingWindow | None:
    """Rank four-hour local-time windows from successive hourly trend snapshots.

    Positive changes in successful/open negotiations are demand evidence. A positive
    change in competing sell listings is treated as new competition. Item-specific timing
    is only used after at least seven local dates and 48 transitions; until then the much
    larger market-wide sample is used and clearly labelled as a fallback.
    """
    if not rows:
        return None

    if id_item is not None:
        item_rows = [row for row in rows if _integer(row.get("id_item")) == id_item]
        item_result = _rank_windows(item_rows, timezone_name=timezone_name, scope="item-specific")
        if (
            item_result
            and item_result.demand_events > 0
            and item_result.days_observed >= 7
            and item_result.observation_count >= 48
        ):
            return item_result

    market_result = _rank_windows(rows, timezone_name=timezone_name, scope="market-wide fallback")
    return market_result if market_result and market_result.demand_events > 0 else None


def _rank_windows(
    rows: list[dict[str, Any]], *, timezone_name: str, scope: str
) -> PostingWindow | None:
    local_tz = ZoneInfo(timezone_name)
    grouped: dict[int, list[tuple[datetime, dict[str, Any]]]] = {}
    for row in rows:
        id_item = _integer(row.get("id_item"))
        observed_at = _parse_utc(row.get("recorded_hour"))
        if id_item is None or observed_at is None:
            continue
        grouped.setdefault(id_item, []).append((observed_at, row))

    metrics: dict[int, dict[str, Any]] = {}
    for observations in grouped.values():
        observations.sort(key=lambda pair: pair[0])
        for (previous_at, previous), (current_at, current) in zip(observations, observations[1:]):
            gap_hours = (current_at - previous_at).total_seconds() / 3600
            if not 0.5 <= gap_hours <= 2.5:
                continue
            local = current_at.astimezone(local_tz)
            bucket = (local.hour // POSTING_WINDOW_HOURS) * POSTING_WINDOW_HOURS
            slot = metrics.setdefault(
                bucket,
                {"transitions": 0, "dates": set(), "successful": 0.0, "open": 0.0, "competition": 0.0},
            )
            slot["transitions"] += 1
            slot["dates"].add(local.date())
            slot["successful"] += _positive_delta(previous, current, "negotiations_success")
            slot["open"] += _positive_delta(previous, current, "negotiations_open")
            slot["competition"] += _positive_delta(previous, current, "listings_count_sell")

    if not metrics:
        return None

    def rank_value(slot: dict[str, Any]) -> float:
        # Successful negotiations carry twice the weight of newly-opened ones. The
        # efficiency term favors demand that is not accompanied by a flood of competing
        # posts, while sqrt(volume) prevents a tiny overnight sample from winning on ratio
        # alone.
        demand = slot["successful"] + (0.5 * slot["open"])
        efficiency = demand / (slot["competition"] + 5.0)
        return efficiency * math.sqrt(max(demand, 0.0))

    start_hour, best = max(metrics.items(), key=lambda pair: (rank_value(pair[1]), pair[1]["transitions"]))
    all_dates = {
        date
        for slot in metrics.values()
        for date in slot["dates"]
    }
    days = len(all_dates)
    confidence = "High" if days >= 28 else "Medium" if days >= 14 else "Low"
    return PostingWindow(
        start_hour=start_hour,
        end_hour=(start_hour + POSTING_WINDOW_HOURS) % 24,
        scope=scope,
        confidence=confidence,
        observation_count=sum(slot["transitions"] for slot in metrics.values()),
        days_observed=days,
        demand_events=best["successful"] + (0.5 * best["open"]),
        new_sell_listings=best["competition"],
    )


def next_posting_time(
    window: PostingWindow,
    *,
    now: datetime | None = None,
    timezone_name: str = DEFAULT_MARKETPLACE_TIMEZONE,
) -> datetime:
    """Return the next time inside the recommended window, as an aware UTC datetime."""
    local_tz = ZoneInfo(timezone_name)
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    local_now = current.astimezone(local_tz)
    if window.start_hour <= local_now.hour < window.start_hour + POSTING_WINDOW_HOURS:
        return (current + timedelta(minutes=1)).astimezone(timezone.utc)

    candidate = local_now.replace(hour=window.start_hour, minute=0, second=0, microsecond=0)
    if candidate <= local_now:
        candidate += timedelta(days=1)
    return candidate.astimezone(timezone.utc)


def recommend_balanced_price(
    *,
    listings: list[dict[str, Any]],
    average_rows: list[dict[str, Any]],
    quality: int,
    unit: str,
    minimum_price: int,
    own_completed_unit_prices: Iterable[float] = (),
    strategy: str = "balanced",
) -> PriceRecommendation:
    """Recommend a robust balanced UEC sell price for one exact item/quality/unit.

    Sold-out rows are useful evidence about an asking price that cleared its available
    stock, but are deliberately weaker than the user's own known single-unit completed
    deals and are never described as verified transaction prices.

    ``strategy`` shifts the evidence-based price by a fixed spread ('undercut'/'premium',
    see PRICING_STRATEGY_MULTIPLIERS) before the manual floor is applied, so a deliberate
    undercut can never be pushed back out below the user's minimum. It has no effect when
    there's no evidence at all (the price is just the floor in that case).
    """
    target_tier = quality_to_tier(quality)
    target_unit = unit.strip().lower()
    active_sells: list[float] = []
    sold_out_sells: list[float] = []
    active_buys: list[float] = []

    for listing in listings:
        if (listing.get("currency") or "UEC").upper() != "UEC":
            continue
        if (listing.get("unit") or "unit").strip().lower() != target_unit:
            continue
        listing_quality = parse_listing_quality(listing.get("quality"))
        if listing_quality is None or quality_to_tier(listing_quality) != target_tier:
            continue
        price = parse_uex_number(listing.get("price"))
        if price is None or price <= 0:
            continue
        operation = (listing.get("operation") or "").lower()
        sold_out = _flag(listing.get("is_sold_out"))
        if operation == "sell" and sold_out:
            sold_out_sells.append(price)
        elif operation == "sell":
            active_sells.append(price)
        elif operation == "buy" and not sold_out:
            active_buys.append(price)

    weighted_signals: list[tuple[float, int, str]] = []
    if active_sells:
        competitive_asks = sorted(active_sells)[:3]
        weighted_signals.append((statistics.median(competitive_asks), 3, "current competing sell asks"))
    if sold_out_sells:
        weighted_signals.append((statistics.median(sold_out_sells), 2, "recent sold-out asking prices"))
    if active_buys:
        weighted_signals.append((max(active_buys), 1, "highest current buy offer"))

    for row in average_rows:
        tier = _integer(row.get("quality_tier"))
        if tier != target_tier or (row.get("operation") or "").lower() != "sell":
            continue
        if (row.get("currency") or "UEC").upper() != "UEC":
            continue
        if (row.get("unit") or "unit").strip().lower() != target_unit:
            continue
        seen_average_values: set[float] = set()
        for key, weight, label in (
            ("price_avg", 2, "current listing average"),
            ("price_avg_week", 2, "7-day listing average"),
            ("price_avg_month", 1, "30-day listing average"),
        ):
            value = parse_uex_number(row.get(key))
            normalized_value = round(value, 6) if value is not None else None
            if value is not None and value > 0 and normalized_value not in seen_average_values:
                weighted_signals.append((value, weight, label))
                seen_average_values.add(normalized_value)

    own_prices = [float(value) for value in own_completed_unit_prices if value and float(value) > 0]
    if own_prices:
        weighted_signals.append((statistics.median(own_prices), 4, "your completed single-unit deals"))

    if weighted_signals:
        raw_price = _weighted_median((value, weight) for value, weight, _ in weighted_signals)
        multiplier = PRICING_STRATEGY_MULTIPLIERS.get(strategy, 1.0)
        rounded = _round_market_price(raw_price * multiplier)
    else:
        rounded = minimum_price

    floor_applied = rounded < minimum_price
    price = max(rounded, minimum_price)
    distinct_evidence = tuple(dict.fromkeys(label for _, _, label in weighted_signals))
    confidence = "High" if len(distinct_evidence) >= 5 else "Medium" if len(distinct_evidence) >= 2 else "Low"
    return PriceRecommendation(
        price=price,
        confidence=confidence,
        evidence=distinct_evidence,
        floor_applied=floor_applied,
    )


def build_inventory_listing_payload(entry: dict[str, Any], *, quantity: int, price: int) -> dict[str, Any]:
    """Build the exact guarded UEX payload for a catalogued UEC sell listing."""
    item_name = str(entry["item_name"])
    quality = int(entry.get("quality") or 0)
    quality_text = quality_label(quality)
    title = _safe_title(f"{item_name} - {quality_text} - {quantity} available")
    description_parts = [
        f"Catalogued {item_name} from personal inventory.",
        f"Quantity available: {quantity} {entry.get('unit') or 'unit'}.",
        f"Quality: {quality_text}.",
    ]
    return {
        "id_category": int(entry["id_category"]),
        "id_item": int(entry["id_item"]),
        "operation": "sell",
        "type": "item",
        "language": "en_US",
        "unit": entry.get("unit") or "unit",
        "price": int(price),
        "currency": "UEC",
        "location": entry.get("location") or "Location not specified",
        "title": title,
        "description": "\n".join(description_parts),
        "in_stock": int(quantity),
        "availability": "immediate",
        "hours_expiration": 48,
        "is_hidden": 0,
        "is_tv_allowed": 0,
        "is_production": 1,
    }


def extract_listing_id(created: Any) -> int | None:
    """UEX names the POST result field id_listing (not id)."""
    if not isinstance(created, dict):
        return None
    return _integer(created.get("id_listing"))


def _weighted_median(values: Iterable[tuple[float, int]]) -> float:
    expanded = sorted((float(value), int(weight)) for value, weight in values if weight > 0)
    total = sum(weight for _, weight in expanded)
    midpoint = total / 2
    cumulative = 0
    for value, weight in expanded:
        cumulative += weight
        if cumulative >= midpoint:
            return value
    return expanded[-1][0]


def _round_market_price(value: float) -> int:
    increment = 10 if value < 1_000 else 50 if value < 10_000 else 100 if value < 100_000 else 1_000
    return max(increment, int(round(value / increment) * increment))


def _safe_title(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9 -]+", " ", value)
    return re.sub(r"\s+", " ", cleaned).strip()[:140]


def _positive_delta(previous: dict[str, Any], current: dict[str, Any], key: str) -> float:
    before = parse_uex_number(previous.get(key)) or 0.0
    after = parse_uex_number(current.get(key)) or 0.0
    return max(after - before, 0.0)


def _parse_utc(raw: Any) -> datetime | None:
    if isinstance(raw, datetime):
        value = raw
    elif raw:
        try:
            value = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
        except ValueError:
            return None
    else:
        return None
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def _integer(raw: Any) -> int | None:
    number = parse_uex_number(raw)
    return int(number) if number is not None else None


def _flag(raw: Any) -> bool:
    if isinstance(raw, str):
        return raw.strip().lower() in {"1", "true", "yes"}
    return bool(raw)


def _format_hour(hour: int) -> str:
    normalized = hour % 24
    suffix = "AM" if normalized < 12 else "PM"
    display = normalized % 12 or 12
    return f"{display} {suffix}"
