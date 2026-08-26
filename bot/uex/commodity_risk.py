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
