"""Pure helpers for commodity restock (stock) alerts - kept dependency-free for easy testing,
same pattern as bot/uex/marketplace.py and bot/uex/trading.py.

The core idea: /commodities_prices reports, per terminal, whether a commodity can currently
be bought there (price_buy > 0) and how much is in stock (scu_buy). A restock alert watches
for that availability flipping from false to true at any terminal selling the commodity, so
someone doesn't have to keep re-running /best-route hoping the usual "Out of Stock" has
cleared.
"""
from __future__ import annotations

from typing import Any


def compute_terminal_availability(price_rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Distill /commodities_prices rows down to per-terminal buy-side availability.

    A terminal is considered "available" only if it actually sells the commodity at all
    (price_buy > 0 - some terminals report price_buy: 0 meaning they don't buy-side trade it)
    AND currently has real stock (scu_buy > 0). Rows missing id_terminal are skipped - there's
    nothing to track state against without a stable terminal identity.
    """
    result = []
    for row in price_rows:
        id_terminal = row.get("id_terminal")
        if id_terminal is None:
            continue
        price_buy = row.get("price_buy") or 0
        scu_buy = row.get("scu_buy") or 0
        result.append(
            {
                "id_terminal": id_terminal,
                "terminal_name": row.get("terminal_name", "Unknown"),
                "price_buy": price_buy,
                "scu_buy": scu_buy,
                "is_available": price_buy > 0 and scu_buy > 0,
            }
        )
    return result


def detect_restocks(
    current: list[dict[str, Any]], previous_state: dict[int, dict[str, Any]]
) -> tuple[list[dict[str, Any]], dict[int, dict[str, Any]]]:
    """Compare this poll's terminal availability against last-known state to find genuine
    empty->available transitions worth notifying about.

    A terminal with no prior state at all (first time this alert has ever seen it) is treated
    as a transition too when it's currently available - if you just set up the watch and the
    commodity happens to already be in stock somewhere, you'd want to know immediately rather
    than wait for it to go out of stock and restock again. A terminal that's already known to
    be available, and still is, does NOT re-fire - that's the whole point of tracking state.

    Returns (to_notify, new_state) where new_state is the full updated per-terminal state map
    (not just the changed entries) - the caller persists this back via
    upsert_stock_alert_terminal_state for every terminal in `current`.
    """
    to_notify: list[dict[str, Any]] = []
    new_state: dict[int, dict[str, Any]] = {}

    for terminal in current:
        id_terminal = terminal["id_terminal"]
        was = previous_state.get(id_terminal)
        was_available = was["was_available"] if was is not None else False

        if terminal["is_available"] and not was_available:
            to_notify.append(terminal)

        new_state[id_terminal] = {"was_available": terminal["is_available"], "last_seen_scu": terminal["scu_buy"]}

    return to_notify, new_state


def format_cargo_fit_note(scu_available: float, ship_cargo_scu: float | None) -> str:
    """Describe how much of a restock a given ship's hold could actually take, for the
    notification message. With no known ship, points the user at how to set one instead of
    just omitting the detail silently.
    """
    if ship_cargo_scu is None or ship_cargo_scu <= 0:
        return "set /set-default-ship (or pass one to /stock-alert-add) to see how much of this would fill your hold"
    if scu_available >= ship_cargo_scu:
        return f"fills your full {ship_cargo_scu:,.0f} SCU hold"
    return f"fills {scu_available:,.0f} of your {ship_cargo_scu:,.0f} SCU hold"
