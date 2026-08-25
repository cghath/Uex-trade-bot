"""Terminal price-data freshness and coverage classification."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class TerminalDataHealth:
    terminal_name: str
    status: str
    last_update_days: float | None
    coverage_percentage: int | None
    has_recent_reports: bool

    @property
    def warning(self) -> bool:
        return self.status in {"limited", "stale"}


def classify_terminal_health(row: dict[str, Any]) -> TerminalDataHealth:
    """Classify UEX data-monitor state without confusing coverage with freshness.

    ``has_recent_reports`` is UEX's own TTL-aware freshness decision. Coverage describes
    how many of the terminal's known prices were updated, and can be high even when the
    newest report is old, so it only distinguishes healthy from limited recent data.
    """
    has_recent = bool(row.get("has_recent_reports"))
    coverage_raw = row.get("prices_updated_percentage")
    coverage = int(coverage_raw) if coverage_raw is not None else None
    age_raw = row.get("last_update_days")
    age = float(age_raw) if age_raw is not None else None

    if not has_recent:
        status = "stale"
    elif coverage is not None and coverage < 50:
        status = "limited"
    elif age is not None and age > 1:
        status = "recent"
    else:
        status = "fresh"

    return TerminalDataHealth(
        terminal_name=str(row.get("terminal_name") or "Unknown terminal"),
        status=status,
        last_update_days=age,
        coverage_percentage=coverage,
        has_recent_reports=has_recent,
    )


def format_health_note(health: TerminalDataHealth | None) -> str | None:
    """Return a compact Discord-friendly note, only calling attention to weak data."""
    if health is None or not health.warning:
        return None
    if health.status == "limited":
        coverage = f"{health.coverage_percentage}% coverage" if health.coverage_percentage is not None else "limited coverage"
        return f"⚠️ recent reports, but {coverage}"
    age = f"{health.last_update_days:g}d old" if health.last_update_days is not None else "age unknown"
    return f"⚠️ stale terminal data ({age})"
