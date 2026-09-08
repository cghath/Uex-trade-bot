"""Time-weighted supply and demand reliability from change-only observations."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from bot.uex.data_health import TerminalDataHealth


MIN_HISTORY_HOURS = 24
# A single recorded observation (state_changes == 0) is one point in time extrapolated
# forward to observed_until, with zero corroboration that the state actually persisted -
# requiring at least one real recorded change is the minimum bar for "this has genuinely
# been watched," not just "time has passed since the collector wrote one row."
MIN_STATE_CHANGES = 1
SELL_SIDE_NO_DEMAND_CODE = 7


def has_sell_side_demand(scu_wanted: Any, status_sell: Any) -> bool:
    """Return whether a sell-side market is confirmed to be accepting cargo.

    UEX status 7 means maximum terminal inventory (no demand), while 0/None are not
    applicable or unknown. Demand is a hard route limit, so unknown values fail closed.
    """
    try:
        wanted = float(scu_wanted)
        status = int(float(status_sell)) if status_sell is not None else None
    except (TypeError, ValueError):
        return False
    return wanted > 0 and status not in (None, 0, SELL_SIDE_NO_DEMAND_CODE)


@dataclass(frozen=True)
class TerminalMarketHistory:
    observed_hours: float
    supply_available_pct: float
    demand_available_pct: float
    state_changes: int
    last_change_at: datetime

    @property
    def enough_history(self) -> bool:
        return self.observed_hours >= MIN_HISTORY_HOURS and self.state_changes >= MIN_STATE_CHANGES


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
        if (row.get("price_sell") or 0) > 0 and has_sell_side_demand(
            row.get("scu_sell"), row.get("status_sell")
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


# Evidence-Level Labels: what a route's stock/demand figure actually rests on, distinct
# from the blended 0-100 RouteConfidence score (bot/uex/route_confidence.py) - that score
# answers "how much should I trust this route overall," this answers "where did THIS
# specific number come from." Four tiers, in descending order of directness:
#   "current"  - a live reported figure, and the terminal's data is fresh/recent
#   "aging"    - a live reported figure, but the terminal's data is limited/stale/unknown
#                (a real number, just not a fresh one - not the same as having none)
#   "inferred" - no live figure at all, but enough collected history (>= MIN_HISTORY_HOURS
#                AND >= MIN_STATE_CHANGES real recorded transitions, not just one stale
#                point extrapolated forward) to estimate how often this terminal has had
#                supply/demand historically
#   "unknown"  - no live figure AND no usable history - genuinely no information, which
#                must never be displayed as if it meant "confirmed zero"
EVIDENCE_TIERS = ("current", "aging", "inferred", "unknown")


@dataclass(frozen=True)
class EvidenceLevel:
    tier: str
    quantity_scu: float | None = None
    historical_availability_pct: float | None = None
    observed_hours: float | None = None


def classify_supply_evidence(
    *,
    scu: float | None,
    health: TerminalDataHealth | None,
    history: TerminalMarketHistory | None,
    side: str,
    status_sell: Any = None,
) -> EvidenceLevel:
    """side is 'supply' (origin/buy) or 'demand' (destination/sell) - selects which of
    history's two percentages describes this side.

    status_sell is only consulted when side == 'demand'. UEX status code 7 ("Maximum
    Inventory, No Demand") means the terminal is CONFIRMED to have zero real demand even
    when scu itself reports a real positive number (the same buy/sell status inversion
    has_sell_side_demand already exists for) - without this, a route could show e.g.
    "Demand: 500 SCU (verify before departure)" in the same embed that separately shows
    "sell side: Maximum Inventory (No Demand)", directly contradicting itself. Any other
    status (including unknown/None) never overrides a live scu figure - only code 7 is an
    authoritative zero-demand signal, not merely a missing one, so a genuine live report
    with no status information is still trusted as reported.
    """
    effective_scu = scu
    if side == "demand" and scu is not None:
        try:
            status_code = int(float(status_sell)) if status_sell is not None else None
        except (TypeError, ValueError):
            status_code = None
        if status_code == SELL_SIDE_NO_DEMAND_CODE:
            effective_scu = 0.0
    if effective_scu is not None:
        status = health.status if health is not None else "unknown"
        tier = "current" if status in ("fresh", "recent") else "aging"
        return EvidenceLevel(tier=tier, quantity_scu=float(effective_scu))
    if history is not None and history.enough_history:
        pct = history.demand_available_pct if side == "demand" else history.supply_available_pct
        return EvidenceLevel(tier="inferred", historical_availability_pct=pct, observed_hours=history.observed_hours)
    return EvidenceLevel(tier="unknown")
