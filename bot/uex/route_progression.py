"""Pure logic for Recommendation Outcome Tracking (Phase 1, local-only): translating a
player-reported leg outcome into the row Database.record_terminal_market_snapshot expects.
No Discord, no I/O - see bot/cogs/route_progression.py for the thread/button/modal glue.
"""
from __future__ import annotations

import math
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


def is_reportable_amount(value: float | None, *, allow_negative: bool = False) -> bool:
    """True if a player-typed numeric report is safe to write into shared market state:
    finite (rejects inf/nan, both valid float() parses) and, unless allow_negative,
    non-negative. None (not provided) is always considered valid here - whether an
    omitted value is acceptable is the caller's concern, not this range check's."""
    if value is None:
        return True
    return math.isfinite(value) and (allow_negative or value >= 0)


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
    market_scu: float | None = None,
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

    market_scu is the terminal's real quoted stock/demand at recommendation time (e.g.
    MixedCargoItem.available_scu for /mixed-routes and /multi-stop-route), kept separate
    from quoted_scu, which for those two commands is the PLANNED cargo allocation for this
    ship/budget - capped by capacity, not by what the terminal actually has. A 'matched'
    report only confirms the planned transaction went through as quoted; it is not a
    player-confirmed count of the terminal's total remaining stock, so this function
    prefers market_scu (real observed availability) over quoted_scu (allocation) whenever
    both were given. Callers whose quoted_scu already IS the real market figure directly
    (/best-route, /top-routes) simply never pass market_scu, and behavior is unchanged.

    actual_price/actual_scu are defense-in-depth validated here (finite, non-negative) even
    though the Discord-facing modal should already have rejected anything else before
    calling this - a second boundary check at the point a row is actually built for
    shared storage, not just at the outermost UI input.
    """
    if side not in SIDES:
        raise ValueError(f"side must be one of {SIDES}, got {side!r}")
    if outcome not in OUTCOMES:
        raise ValueError(f"outcome must be one of {OUTCOMES}, got {outcome!r}")
    if not is_reportable_amount(actual_price):
        raise ValueError(f"actual_price must be finite and non-negative, got {actual_price!r}")
    if not is_reportable_amount(actual_scu):
        raise ValueError(f"actual_scu must be finite and non-negative, got {actual_scu!r}")

    price_key = "price_buy" if side == "buy" else "price_sell"
    scu_key = "scu_buy" if side == "buy" else "scu_sell"
    status_key = "status_buy" if side == "buy" else "status_sell"
    empty_code = BUY_SIDE_EMPTY_CODE if side == "buy" else SELL_SIDE_NO_DEMAND_CODE

    if outcome == "matched":
        if quoted_price is None and quoted_scu is None:
            return None
        price = quoted_price
        scu = market_scu if market_scu is not None else quoted_scu
        status = quoted_status
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


# How long a confirmed-empty (commodity, terminal, side) is suppressed from route
# recommendations before it's eligible to show up again - a deliberate best-guess
# tunable, not observed game data (no CIG-documented restock rate exists to anchor this
# to; see ROADMAP.md's "Discuss: bot user reporting importance" entry for the empirical
# groundwork this number came from - a median ~18-28h restock gap measured from this
# bot's own collected terminal_market_observations history landed the decision here,
# splitting the difference against a much shorter, unverifiable community-claimed figure
# that didn't hold up when its own cited source was checked).
SUPPRESSION_HOURS = 3


def update_confirms_depletion(update_row: dict[str, Any] | None, *, side: str) -> bool:
    """True when a terminal_state_update_for_outcome row represents a CONFIRMED EMPTY
    state for this side - scu=0 paired with that side's own empty status code (missing,
    or a 'more' outcome drained to nothing with precision='exact') - the only case where
    recommending this exact (commodity, terminal, side) again immediately would be
    pointless. False for 'matched' (state unchanged, no depletion signal) and a positive
    'less' partial (some stock/demand remains, just less than quoted) - both still
    produce a real update_row, so this checks the row's own values rather than the
    outcome/precision the caller used to build it, keeping the two functions' branching
    in exactly one place."""
    if side not in SIDES:
        raise ValueError(f"side must be one of {SIDES}, got {side!r}")
    if update_row is None:
        return False
    scu_key = "scu_buy" if side == "buy" else "scu_sell"
    status_key = "status_buy" if side == "buy" else "status_sell"
    empty_code = BUY_SIDE_EMPTY_CODE if side == "buy" else SELL_SIDE_NO_DEMAND_CODE
    return update_row.get(scu_key) == 0.0 and update_row.get(status_key) == empty_code


def describe_leg_outcome(
    *,
    outcome: str,
    actual_price: float | None = None,
    actual_scu: float | None = None,
    precision: str | None = None,
) -> str:
    """A short, player-facing line describing what was just reported for a leg - shown
    appended under the original 'Quoted: ...' line once a LegOutcomeView commits, so the
    leg's message keeps showing what actually happened rather than just going to disabled
    buttons with no visible result. Pure text formatting only - no I/O, no Discord types."""
    if outcome not in OUTCOMES:
        raise ValueError(f"outcome must be one of {OUTCOMES}, got {outcome!r}")
    if outcome == "matched":
        return "**Reported:** Matched the quote."
    if outcome == "missing":
        return "**Reported:** Nothing was there."
    scu_text = f"{actual_scu:,.0f} SCU" if actual_scu is not None else "an unspecified amount"
    price_text = f" at {actual_price:,.2f} aUEC/unit" if actual_price is not None else ""
    if outcome == "less":
        return f"**Reported:** Less than quoted - {scu_text}{price_text}."
    # "more"
    if precision not in PRECISIONS:
        raise ValueError(f"precision must be one of {PRECISIONS} for outcome='more', got {precision!r}")
    if precision == "floor":
        return f"**Reported:** More than quoted - at least {scu_text}{price_text} (you were capped; more may have been there)."
    return f"**Reported:** More than quoted - {scu_text}{price_text} (terminal was drained)."
