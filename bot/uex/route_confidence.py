"""Explainable route-confidence scoring, separate from profit and UEX route score."""
from __future__ import annotations

from dataclasses import dataclass

from bot.uex.data_health import TerminalDataHealth


@dataclass(frozen=True)
class RouteConfidence:
    score: int
    label: str


def coalesce_report_count(primary: int | None, fallback: int | None) -> int | None:
    """Prefer the primary UEX report count without treating a valid zero as missing."""
    return primary if primary is not None else fallback


# A single Recommendation Outcome Tracking report saying "matched" would otherwise look
# like 100% confidence from one data point - no adjustment is applied below this sample
# size, matching the same minimum-corroboration-count reasoning as
# bot/uex/supply_demand.py's MIN_STATE_CHANGES.
MIN_REPORTS_FOR_TRACK_RECORD = 3


def track_record_modifier(matched: int, total: int) -> int:
    """A bounded +/-10 adjustment to compute_route_confidence's score from real player-
    reported outcomes (route_progression_legs.outcome), on top of - not instead of - the
    existing evidence-quality scoring. 0 (no adjustment) below MIN_REPORTS_FOR_TRACK_RECORD
    or with no reports at all, so the vast majority of routes with no tracking history are
    completely unaffected."""
    if total < MIN_REPORTS_FOR_TRACK_RECORD:
        return 0
    match_rate = matched / total
    return round(20 * (match_rate - 0.5))


def compute_route_confidence(
    *,
    origin_health: TerminalDataHealth | None,
    destination_health: TerminalDataHealth | None,
    origin_report_count: int | None,
    destination_report_count: int | None,
    volatility_origin: float | None,
    volatility_destination: float | None,
    origin_available: bool,
    destination_available: bool,
    track_record_modifier: int = 0,
) -> RouteConfidence:
    """Score evidence quality, not profitability, on a bounded 0-100 scale.

    track_record_modifier is computed by the caller (see the module-level function of the
    same name) from real Recommendation Outcome Tracking reports - kept as a plain int
    parameter here rather than raw matched/total counts so this function stays free of any
    opinion about sample-size thresholds; that judgment call lives in one place.
    """
    health_weight = {"fresh": 1.0, "recent": 0.8, "limited": 0.5, "unknown": 0.4, "stale": 0.0}
    health_scores = [
        health_weight.get(health.status, 0.0) if health else 0.4
        for health in (origin_health, destination_health)
    ]
    freshness = 35 * (sum(health_scores) / 2)

    reports = max(0, int(origin_report_count or 0)) + max(0, int(destination_report_count or 0))
    report_depth = 25 * min(reports / 10, 1.0)

    availability = 12.5 * int(origin_available) + 12.5 * int(destination_available)

    samples = [max(0.0, float(v)) for v in (volatility_origin, volatility_destination) if v is not None]
    volatility = 7.5 if not samples else 15 * max(0.0, 1.0 - min(sum(samples) / len(samples), 1.0))

    score = round(freshness + report_depth + availability + volatility) + track_record_modifier
    label = "High" if score >= 75 else "Medium" if score >= 50 else "Low"
    return RouteConfidence(score=max(0, min(score, 100)), label=label)
