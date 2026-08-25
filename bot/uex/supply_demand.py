"""Time-weighted supply and demand reliability from change-only observations."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any


MIN_HISTORY_HOURS = 24
SELL_SIDE_NO_DEMAND_CODE = 7


@dataclass(frozen=True)
class TerminalMarketHistory:
    observed_hours: float
    supply_available_pct: float
    demand_available_pct: float
    state_changes: int
    last_change_at: datetime

    @property
    def enough_history(self) -> bool:
        return self.observed_hours >= MIN_HISTORY_HOURS


def _timestamp(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def analyze_terminal_market_history(
    observations: list[dict[str, Any]], *, observed_until: str
) -> TerminalMarketHistory | None:
    """Reconstruct duration in each state from rows written only when state changed."""
    if not observations:
        return None
    rows = sorted(observations, key=lambda row: _timestamp(str(row["observed_at"])))
    end = _timestamp(observed_until)
    start = _timestamp(str(rows[0]["observed_at"]))
    if end <= start:
        return None

    supply_seconds = 0.0
    demand_seconds = 0.0
    for index, row in enumerate(rows):
        interval_start = _timestamp(str(row["observed_at"]))
        interval_end = (
            _timestamp(str(rows[index + 1]["observed_at"]))
            if index + 1 < len(rows)
            else end
        )
        seconds = max(0.0, (interval_end - interval_start).total_seconds())
        if (row.get("price_buy") or 0) > 0 and (row.get("scu_buy") or 0) > 0:
            supply_seconds += seconds
        if (
            (row.get("price_sell") or 0) > 0
            and (row.get("scu_sell") or 0) > 0
            and row.get("status_sell") not in (None, 0, SELL_SIDE_NO_DEMAND_CODE)
        ):
            demand_seconds += seconds

    total_seconds = (end - start).total_seconds()
    return TerminalMarketHistory(
        observed_hours=round(total_seconds / 3600, 1),
        supply_available_pct=round(100 * supply_seconds / total_seconds, 1),
        demand_available_pct=round(100 * demand_seconds / total_seconds, 1),
        state_changes=max(0, len(rows) - 1),
        last_change_at=_timestamp(str(rows[-1]["observed_at"])),
    )
