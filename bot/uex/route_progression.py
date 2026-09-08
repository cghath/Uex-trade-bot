"""Pure logic for Recommendation Outcome Tracking (Phase 1, local-only): translating a
player-reported leg outcome into the row Database.record_terminal_market_snapshot expects.
No Discord, no I/O - see bot/cogs/route_progression.py for the thread/button/modal glue.
"""
from __future__ import annotations

from typing import Any

from bot.uex.supply_demand import SELL_SIDE_NO_DEMAND_CODE

# Buy-side status 1 ("Out of Stock (Empty)") and sell-side status SELL_SIDE_NO_DEMAND_CODE
# ("Maximum Inventory, No Demand") are the only two status codes this module ever writes -
# UEX's 1-7 bands mean opposite things on the buy vs sell side (see CLAUDE.local.md), and
# guessing an intermediate band from a raw SCU count without knowing a terminal's real
# capacity would repeat a mistake this codebase has already been burned by once.
BUY_SIDE_EMPTY_CODE = 1

SIDES = ("buy", "sell")
OUTCOMES = ("matched", "less", "more", "missing")
PRECISIONS = ("exact", "floor")


def terminal_state_update_for_outcome(
    *,
    id_commodity: int,
    id_terminal: int,
    commodity_name: str,
    terminal_name: str,
    side: str,
    outcome: str,
    quoted_price: float | None,
    quoted_scu: float | None,
    quoted_status: int | None,
    actual_price: float | None = None,
    actual_scu: float | None = None,
    precision: str | None = None,
) -> dict[str, Any] | None:
    """Return a row for Database.record_terminal_market_snapshot(rows, source='player_report'),
    or None when the outcome carries nothing safe to write back as current terminal state.

    Two cases deliberately return None rather than a best-effort guess:

    - A 'more' outcome with precision='floor' (the player's cargo hold, or the terminal's
      own demand, capped them before the true stock/demand was known). The only fact
      confirmed is a lower bound - "at least this much was there" - which must never be
      written into scu_buy/scu_sell as if it were the real figure. That floor still lives
      in route_progression_legs for later aggregate analysis; it just never overwrites
      terminal_market_state.
    - A 'less' outcome with no actual_scu given, or a 'matched' outcome with no quoted
      figures to re-confirm. There's nothing informative to write, and writing NULLs here
      would silently wipe out whatever this terminal/commodity pair already had.

    A 'more' outcome with precision='exact' means the player took everything there was, so
    the confirmed post-leg state is drained (scu=0), not "equal to how much they took" -
    actual_scu describes the transaction, not what's left afterward.
    """
    if side not in SIDES:
        raise ValueError(f"side must be one of {SIDES}, got {side!r}")
    if outcome not in OUTCOMES:
        raise ValueError(f"outcome must be one of {OUTCOMES}, got {outcome!r}")

    price_key = "price_buy" if side == "buy" else "price_sell"
    scu_key = "scu_buy" if side == "buy" else "scu_sell"
    status_key = "status_buy" if side == "buy" else "status_sell"
    empty_code = BUY_SIDE_EMPTY_CODE if side == "buy" else SELL_SIDE_NO_DEMAND_CODE

    if outcome == "matched":
        if quoted_price is None and quoted_scu is None:
            return None
        price, scu, status = quoted_price, quoted_scu, quoted_status
    elif outcome == "missing":
        price, scu, status = None, 0.0, empty_code
    elif outcome == "less":
        if actual_scu is None:
            return None
        price = actual_price if actual_price is not None else quoted_price
        scu = actual_scu
        status = empty_code if actual_scu <= 0 else None
    else:  # "more"
        if precision not in PRECISIONS:
            raise ValueError(f"precision must be one of {PRECISIONS} for outcome='more', got {precision!r}")
        if precision == "floor":
            return None
        price = actual_price if actual_price is not None else quoted_price
        scu, status = 0.0, empty_code

    return {
        "id_commodity": id_commodity,
        "id_terminal": id_terminal,
        "commodity_name": commodity_name,
        "terminal_name": terminal_name,
        price_key: price,
        scu_key: scu,
        status_key: status,
    }
