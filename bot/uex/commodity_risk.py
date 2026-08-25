"""User-facing operational risk labels from UEX commodity-reference flags."""
from __future__ import annotations

from typing import Any


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
    labels = commodity_risk_labels(commodity)
    return f"⚠️ Cargo risk: {' · '.join(labels)}" if labels else None
