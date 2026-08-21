"""Pure input-wrangling for the per-user want-to-sell list (/items-to-sell).

Kept dependency-free (no Discord, no I/O) like bot/uex's helper modules, so the slot
pairing/validation rules are unit-testable against plain tuples.
"""
from __future__ import annotations

from dataclasses import dataclass

# /items-to-sell exposes this many (item, price) slot pairs. 10 pairs = 20 slash command
# options, safely under Discord's 25-options-per-command cap.
MAX_SELL_LIST_ITEMS_PER_ADD = 10


@dataclass
class SellListEntry:
    item_name: str
    asking_price: float


def pair_sell_list_inputs(
    slots: list[tuple[str | None, float | None]],
) -> tuple[list[SellListEntry], list[str]]:
    """Validate /items-to-sell's ten independent (item_N, price_N) option pairs into clean
    entries. Returns (entries, errors) with entries in slot order; the caller saves nothing
    unless errors is empty, and reports every problem at once - filling ten optional slots
    only to fix mistakes one resubmit at a time would be miserable.

    Rules: an untouched pair (both empty) is skipped; an item needs its price and a price
    needs its item (same slot number); prices must be above zero; the same item can't be
    listed twice in one submission (case-insensitive, matching the DB's NOCASE uniqueness -
    two slots would otherwise just overwrite each other and which price "won" would be
    silent luck of slot order).
    """
    entries: list[SellListEntry] = []
    errors: list[str] = []
    first_slot_by_name: dict[str, int] = {}

    for slot_number, (raw_item, price) in enumerate(slots, start=1):
        item = (raw_item or "").strip()
        if not item and price is None:
            continue
        if not item:
            errors.append(f"price{slot_number} was given without item{slot_number}.")
            continue
        if price is None:
            errors.append(f"'{item}' (item{slot_number}) has no price{slot_number} - every item needs an asking price.")
            continue
        if price <= 0:
            errors.append(f"'{item}' (item{slot_number}): the asking price must be above 0 aUEC.")
            continue

        key = item.lower()
        if key in first_slot_by_name:
            errors.append(
                f"'{item}' appears twice (item{first_slot_by_name[key]} and item{slot_number}) - list each item once."
            )
            continue
        first_slot_by_name[key] = slot_number
        entries.append(SellListEntry(item_name=item, asking_price=float(price)))

    if not entries and not errors:
        # Unreachable through Discord itself (item1/price1 are required options), but keeps
        # the function total for direct callers.
        errors.append("Fill in at least item1 and price1.")
    return entries, errors
