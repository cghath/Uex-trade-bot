"""Terminal price-data freshness and coverage classification."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class TerminalDataHealth:
    terminal_name: str
    status: str
    last_update_days: float | None
    last_update_days_limit: float | None
    last_update_days_percentage: float | None
    coverage_percentage: int | None
    has_recent_reports: bool

    @property
    def warning(self) -> bool:
        return self.status in {"limited", "stale", "unknown"}


def classify_terminal_health(row: dict[str, Any]) -> TerminalDataHealth:
    """Classify UEX data-monitor state from its explicit age and TTL fields.

    ``has_recent_reports`` only means that pending, unconsolidated report ids exist. It is
    retained for diagnostics, but it must not influence freshness. Coverage describes how
    many known prices were updated within the TTL window.
    """
    has_recent = bool(row.get("has_recent_reports"))
    coverage_raw = row.get("prices_updated_percentage")
    coverage = int(coverage_raw) if coverage_raw is not None else None
    age_raw = row.get("last_update_days")
    age = float(age_raw) if age_raw is not None else None
    age_limit_raw = row.get("last_update_days_limit")
    age_limit = float(age_limit_raw) if age_limit_raw is not None else None
    ttl_remaining_raw = row.get("last_update_days_percentage")
    ttl_remaining = float(ttl_remaining_raw) if ttl_remaining_raw is not None else None

    ttl_known = ttl_remaining is not None or (age is not None and age_limit is not None)
    if ttl_remaining is not None:
        expired = ttl_remaining <= 0
        is_recent = ttl_remaining <= 50
    elif age is not None and age_limit is not None:
        expired = age >= age_limit
        is_recent = age >= age_limit * 0.5
    else:
        expired = False
        is_recent = False

    if not ttl_known:
        status = "unknown"
    elif expired:
        status = "stale"
    elif coverage is not None and coverage < 50:
        status = "limited"
    elif is_recent:
        status = "recent"
    else:
        status = "fresh"

    return TerminalDataHealth(
        terminal_name=str(row.get("terminal_name") or "Unknown terminal"),
        status=status,
        last_update_days=age,
        last_update_days_limit=age_limit,
        last_update_days_percentage=ttl_remaining,
        coverage_percentage=coverage,
        has_recent_reports=has_recent,
    )


def format_health_note(health: TerminalDataHealth | None) -> str | None:
    """Return a compact Discord-friendly note, only calling attention to weak data."""
    if health is None or not health.warning:
        return None
    if health.status == "unknown":
        age = f"; last update {health.last_update_days:g}d ago" if health.last_update_days is not None else ""
        return f"⚠️ terminal freshness unavailable (TTL metadata missing{age})"
    if health.status == "limited":
        coverage = f"{health.coverage_percentage}% coverage" if health.coverage_percentage is not None else "limited coverage"
        return f"⚠️ terminal price data has {coverage}"
    age = f"{health.last_update_days:g}d old" if health.last_update_days is not None else "age unknown"
    return f"⚠️ stale terminal data ({age})"
