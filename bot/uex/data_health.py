"""Terminal price-data freshness and coverage classification."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

# The data-health collector (bot/cogs/intelligence.py) runs hourly. This is deliberately
# several times that interval, not a tight 1h cutoff - a couple of missed cycles (a
# restart, a slow poll) shouldn't itself flip a terminal to "unknown"; only collection
# that has genuinely stopped should.
LOCAL_COLLECTION_STALE_HOURS = 6.0


def _parse_last_seen(value: Any) -> datetime | None:
    if not value:
        return None
    text = str(value)
    try:
        parsed = datetime.strptime(text, "%Y-%m-%d %H:%M:%S")
    except ValueError:
        try:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


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


def classify_terminal_health(row: dict[str, Any], *, now: datetime | None = None) -> TerminalDataHealth:
    """Classify UEX data-monitor state from its explicit age and TTL fields.

    ``has_recent_reports`` only means that pending, unconsolidated report ids exist. It is
    retained for diagnostics, but it must not influence freshness. Coverage describes how
    many known prices were updated within the TTL window.

    ``row["last_seen"]`` (when present) is this row's own local collection timestamp -
    distinct from every other field above, which UEX computed relative to ITS OWN clock at
    collection time and which this bot just stores verbatim. If the collector that writes
    this row stops running (a crash, a bug, a long outage), the last successfully stored
    row keeps whatever age/TTL numbers UEX reported back then forever - so a terminal that
    hasn't actually been re-checked in days could still classify as "fresh" purely because
    it looked fresh the one time it was last collected. `now` is only ever overridden in
    tests; production always uses the real current time.
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

    last_seen = _parse_last_seen(row.get("last_seen"))
    if last_seen is not None and status in ("fresh", "recent"):
        elapsed_hours = ((now or datetime.now(timezone.utc)) - last_seen).total_seconds() / 3600
        if elapsed_hours > LOCAL_COLLECTION_STALE_HOURS:
            # UEX's own numbers said "fresh" as of whenever they were captured, but our own
            # collection of this row has itself gone stale enough that we can no longer
            # trust that classification - "unknown" (not "stale") since this is doubt about
            # OUR data, not a claim that UEX's underlying prices expired.
            status = "unknown"

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
