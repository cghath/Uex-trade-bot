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
) -> RouteConfidence:
    """Score evidence quality, not profitability, on a bounded 0-100 scale."""
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

    score = round(freshness + report_depth + availability + volatility)
    label = "High" if score >= 75 else "Medium" if score >= 50 else "Low"
    return RouteConfidence(score=max(0, min(score, 100)), label=label)
