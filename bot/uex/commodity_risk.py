"""User-facing operational risk labels from UEX commodity-reference flags."""
from __future__ import annotations

from typing import Any

RISK_FLAG_KEYS = (
    "is_illegal",
    "is_explosive",
    "is_volatile_qt",
    "is_volatile_time",
    "is_buggy",
)


# What each saved risk tolerance (/set-trading-preferences) leaves out of route suggestions.
# "high", or no preference at all, leaves nothing out.
RISK_TOLERANCE_EXCLUDES: dict[str, tuple[str, ...]] = {
    "low": RISK_FLAG_KEYS,
    "medium": ("is_illegal", "is_buggy"),
}
RISK_TOLERANCE_SKIPS = {
    "low": "illegal, explosive, volatile and buggy goods",
    "medium": "illegal and buggy goods",
}


def outside_risk_tolerance(commodity: dict[str, Any] | None, tolerance: str | None) -> bool:
    """Whether a commodity's flags put it outside the player's saved risk tolerance. A
    commodity with no collected flags isn't left out: routes already warn that its risk is
    unknown, which is the more useful answer than hiding it."""
    excluded = RISK_TOLERANCE_EXCLUDES.get(tolerance or "", ())
    return bool(commodity) and any(commodity.get(key) for key in excluded)


def within_risk_tolerance(rows: list[dict[str, Any]], tolerance: str | None) -> list[dict[str, Any]]:
    """The market rows whose commodity is within the tolerance. Each row carries its own
    commodity's flags, as Database.get_mixed_route_market_rows returns them."""
    if tolerance not in RISK_TOLERANCE_EXCLUDES:
        return rows
    return [row for row in rows if not outside_risk_tolerance(row, tolerance)]


def has_commodity_risk_metadata(commodity: dict[str, Any] | None) -> bool:
    """Return whether the collected reference row explicitly supplies its risk flags."""
    return bool(commodity) and all(commodity.get(key) is not None for key in RISK_FLAG_KEYS)


def commodity_risk_labels(commodity: dict[str, Any] | None) -> list[str]:
    if not commodity:
        return []
    labels: list[str] = []
    if commodity.get("is_illegal"):
        labels.append("restricted in some jurisdictions")
    if commodity.get("is_explosive"):
        labels.append("explosion risk")
    if commodity.get("is_volatile_qt"):
        labels.append("volatile during quantum travel")
    if commodity.get("is_volatile_time"):
        labels.append("becomes unstable over time")
    if commodity.get("is_buggy"):
        labels.append("recent gameplay bugs reported")
    return labels


def format_commodity_risk(commodity: dict[str, Any] | None) -> str | None:
    if not has_commodity_risk_metadata(commodity):
        return "⚠️ Cargo risk metadata unavailable; verify restrictions before departure"
    labels = commodity_risk_labels(commodity)
    return f"⚠️ Cargo risk: {' · '.join(labels)}" if labels else None
