"""Pure aggregation helpers for /leaderboard.

Deliberately narrow scope, matching what was asked for: rank players by total SELL
revenue from their own verified UEX trade history (/user_trades via /uex-trades), never
the self-reported local /trade-log-add ledger, which is easy to fake. "Revenue" here is
gross sale proceeds (price/unit * scu), not profit - /user_trades rows aren't paired
buy/sell transactions, so there's no reliable way to compute a per-trade profit margin.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass
class LeaderboardEntry:
    user_id: int
    total_sell_revenue: float
    sell_trade_count: int


def sum_sell_revenue(trade_rows: list[dict[str, Any]]) -> tuple[float, int]:
    """Sum (price-per-unit * scu) across sell-side rows only, from one user's /user_trades data.

    UEX has been observed sending inconsistent casing for `operation` ("Buy"/"sell"), so the
    comparison is case-insensitive. Rows with a missing/zero price or scu contribute 0, not
    an error - partial/malformed rows shouldn't crash the whole leaderboard.
    """
    total = 0.0
    count = 0
    for row in trade_rows:
        operation = str(row.get("operation") or "").strip().lower()
        if operation != "sell":
            continue
        price = row.get("price") or 0
        scu = row.get("scu") or 0
        total += price * scu
        count += 1
    return round(total, 2), count


def rank_leaderboard(entries: list[LeaderboardEntry], limit: int = 10) -> list[LeaderboardEntry]:
    ranked = sorted(entries, key=lambda e: e.total_sell_revenue, reverse=True)
    return ranked[:limit]
