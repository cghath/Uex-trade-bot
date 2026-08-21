"""Resolve raw status_buy/status_sell codes (from /commodities_prices, /commodities_routes)
into readable labels ("High Supply", "Out of Stock") using /commodities_status's code
definitions. Kept pure/dependency-free for easy unit testing.

UEX's own numeric codes are small ints (observed 0-7); 0 (or any code missing from the
definitions) means "not applicable/no data" for that side - e.g. a terminal that doesn't
buy a commodity at all reports status_buy: 0 even though it has a real status_sell.
"""
from __future__ import annotations

from typing import Any

StatusLookup = dict[str, dict[int, dict[str, Any]]]


def build_status_lookup(status_data: dict[str, list[dict[str, Any]]]) -> StatusLookup:
    """status_data is the {"buy": [...], "sell": [...]} shape from get_commodities_status().
    Returns {"buy": {code: row}, "sell": {code: row}} for O(1) lookups."""
    lookup: StatusLookup = {"buy": {}, "sell": {}}
    for side in ("buy", "sell"):
        for row in status_data.get(side) or []:
            code = row.get("code")
            if code is not None:
                lookup[side][code] = row
    return lookup


def resolve_status_label(lookup: StatusLookup, side: str, code: int | float | None, *, short: bool = True) -> str | None:
    """side is 'buy' or 'sell'. Returns a readable label, or None if there's nothing to show
    (code is 0/None, or the code isn't in UEX's current definitions)."""
    if not code:
        return None
    row = lookup.get(side, {}).get(int(code))
    if row is None:
        return None
    if short:
        return row.get("name_short") or row.get("name")
    return row.get("name") or row.get("name_short")
