"""Pure helpers for Where to Buy a Ship (/where-to-buy-ship): every in-game terminal UEX
reports selling or renting one ship, cheapest first. Dependency-free like the rest of
bot/uex/, so it's easy to unit test against synthetic /vehicles_purchases_prices and
/vehicles_rentals_prices rows.

No distance sorting, deliberately: ships are sold at only 7 terminals in-game (4 vendor
brands), so "which is closest" isn't a question players need answered here - price and
system are.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable, Mapping

from bot.uex.item_finder import split_place_and_vendor

UNKNOWN_SYSTEM_LABEL = "Unknown system"

# price_rent is the 1-day rate: UEX's own terminal rent tabs label it "UEC / Day", and
# Cornerstone's per-duration listings match it at 1 Day only (3/7/30-day prices differ,
# and not by one fixed discount - it varies per ship), so no multi-day price is derived.
RENTAL_RATE_NOTE = "Rental prices are the 1-day rate; longer rentals are discounted in-game."

PURCHASES_UNAVAILABLE_TEXT = "Couldn't load purchase locations from UEX just now - try again in a moment."
RENTALS_UNAVAILABLE_TEXT = "Couldn't load rental locations from UEX just now - try again in a moment."
NOT_SOLD_TEXT = "Not sold at any in-game shop UEX tracks."
NOT_RENTED_TEXT = "Not available to rent at any terminal UEX tracks."


@dataclass(frozen=True)
class ShipShopListing:
    id_terminal: int | None
    terminal_name: str
    place_label: str
    vendor_label: str | None
    star_system_name: str | None
    price: float
    # Unix timestamp of the datarunner report behind `price` (UEX's price_buy/price_rent is
    # "last", i.e. the most recent report, which can be days old). None when UEX sent none.
    date_modified: int | None


def _positive_float(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


def _optional_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _positive_timestamp(value: Any) -> int | None:
    number = _positive_float(value)
    return int(number) if number is not None else None


def terminal_ids_missing_star_system(*row_lists: Iterable[dict[str, Any]]) -> set[int]:
    """id_terminal of every row UEX sent with no star_system_name. Seen live on real rental
    rows (id_star_system 0, e.g. MOTH at 'Vantage Rentals - Pyro Gateway (Nyx)') even though
    other rows at the same terminal carry the system - the caller looks these up in
    terminal_reference and passes the result back in as `system_by_terminal`."""
    ids: set[int] = set()
    for rows in row_lists:
        for row in rows:
            id_terminal = _optional_int(row.get("id_terminal"))
            if not row.get("star_system_name") and id_terminal is not None:
                ids.add(id_terminal)
    return ids


def _rank(
    rows: Iterable[dict[str, Any]], price_field: str, system_by_terminal: Mapping[int, str] | None,
) -> list[ShipShopListing]:
    systems = system_by_terminal or {}
    result = []
    for row in rows:
        price = _positive_float(row.get(price_field))
        if price is None:
            continue
        place_label, vendor_label = split_place_and_vendor(row)
        id_terminal = _optional_int(row.get("id_terminal"))
        result.append(ShipShopListing(
            id_terminal=id_terminal,
            terminal_name=str(row.get("terminal_name") or "Unknown"),
            place_label=place_label,
            vendor_label=vendor_label,
            # The row's own system always wins; the lookup only fills in a missing one.
            star_system_name=row.get("star_system_name") or systems.get(id_terminal) or None,
            price=price,
            date_modified=_positive_timestamp(row.get("date_modified")),
        ))

    def sort_key(listing: ShipShopListing) -> tuple:
        # Cheapest first; the rest only exists so equal prices (e.g. the C2 Hercules at an
        # identical 18,900,000 in two showrooms) always come out in the same order.
        return (
            listing.price,
            listing.place_label.casefold(),
            (listing.vendor_label or "").casefold(),
            listing.terminal_name.casefold(),
            listing.id_terminal if listing.id_terminal is not None else -1,
        )

    result.sort(key=sort_key)
    return result


def rank_purchase_listings(
    rows: Iterable[dict[str, Any]], system_by_terminal: Mapping[int, str] | None = None,
) -> list[ShipShopListing]:
    """/vehicles_purchases_prices rows -> cheapest first. A row with a missing or
    non-positive price_buy is dropped, never shown as 0 or guessed at. A row with no
    star_system_name takes its terminal's system from `system_by_terminal`, if present
    (see terminal_ids_missing_star_system)."""
    return _rank(rows, "price_buy", system_by_terminal)


def rank_rental_listings(
    rows: Iterable[dict[str, Any]], system_by_terminal: Mapping[int, str] | None = None,
) -> list[ShipShopListing]:
    """/vehicles_rentals_prices rows -> cheapest first, same rules as purchases."""
    return _rank(rows, "price_rent", system_by_terminal)


def group_by_star_system(listings: Iterable[ShipShopListing]) -> dict[str, list[ShipShopListing]]:
    """Star system -> listings, preserving the input order within each group, and ordering
    groups by their first (i.e. cheapest, for ranked input) listing. Rows with no star
    system even after the terminal_reference fallback (see
    terminal_ids_missing_star_system) go in one UNKNOWN_SYSTEM_LABEL group, always last,
    rather than being dropped or guessed into a system."""
    grouped: dict[str, list[ShipShopListing]] = {}
    unknown: list[ShipShopListing] = []
    for listing in listings:
        if listing.star_system_name:
            grouped.setdefault(listing.star_system_name, []).append(listing)
        else:
            unknown.append(listing)
    if unknown:
        grouped[UNKNOWN_SYSTEM_LABEL] = unknown
    return grouped


def vehicle_ids_with_listings(*row_lists: Iterable[dict[str, Any]]) -> set[int]:
    """Every id_vehicle appearing in any of the given *_all rows. Matched on ids, not
    names - the *_all endpoints' vehicle_name isn't guaranteed to match /vehicles' name."""
    ids: set[int] = set()
    for rows in row_lists:
        for row in rows:
            id_vehicle = _optional_int(row.get("id_vehicle"))
            if id_vehicle is not None:
                ids.add(id_vehicle)
    return ids


def ship_autocomplete_names(
    vehicles: Iterable[dict[str, Any]], listed_ids: set[int], current: str, *, limit: int = 25,
) -> list[str]:
    """Names of ships with at least one buy or rent row (listed_ids), matching `current`
    as a case-insensitive substring of name or name_full, deduplicated, at most `limit`."""
    current_lower = current.strip().lower()
    seen: set[str] = set()
    names: list[str] = []
    for vehicle in vehicles:
        if _optional_int(vehicle.get("id")) not in listed_ids:
            continue
        name = vehicle.get("name")
        if not name or name in seen:
            continue
        name_full = vehicle.get("name_full") or ""
        if current_lower not in name.lower() and current_lower not in name_full.lower():
            continue
        seen.add(name)
        names.append(name)
        if len(names) >= limit:
            break
    return names


def _vendor_part(listing: ShipShopListing) -> str:
    return f" ({listing.vendor_label})" if listing.vendor_label else ""


def _updated_part(listing: ShipShopListing) -> str:
    # Discord renders <t:...:R> as a live relative time ("2 weeks ago") in the viewer's
    # own client, so it stays accurate however long the message sits in a channel.
    return f" · updated <t:{listing.date_modified}:R>" if listing.date_modified else ""


def format_purchase_line(listing: ShipShopListing) -> str:
    """'**Place** (Vendor) — Price aUEC · System · updated <t:..:R>'. Plain proportional
    text, same as /ingame-item-finder's lines (a monospace table broke live twice there).
    Buys aren't grouped by system, so the system goes on each line instead."""
    system = listing.star_system_name or UNKNOWN_SYSTEM_LABEL
    return (
        f"**{listing.place_label}**{_vendor_part(listing)} — {listing.price:,.0f} aUEC · "
        f"{system}{_updated_part(listing)}"
    )


def format_rental_line(listing: ShipShopListing) -> str:
    """'**Place** (Vendor) — Price aUEC / day · updated <t:..:R>'. No system on the line -
    rentals are shown grouped under a per-system heading. The '1-day rate' caveat
    (RENTAL_RATE_NOTE) is shown once for the whole message, not repeated per line."""
    return f"**{listing.place_label}**{_vendor_part(listing)} — {listing.price:,.0f} aUEC / day{_updated_part(listing)}"


def build_ship_shop_sections(
    purchases: list[ShipShopListing], rentals: list[ShipShopListing], *,
    purchases_failed: bool = False, rentals_failed: bool = False,
) -> list[tuple[str, list[str]]]:
    """(heading, lines) per displayed section: one 'Buy' section, then one
    'Rent — <system>' section per star system. A section whose fetch failed, or that has
    no rows, still appears with a one-line explanation, so a missing section never reads
    as "checked, nothing there" when it really means "couldn't check"."""
    if purchases_failed:
        buy_lines = [PURCHASES_UNAVAILABLE_TEXT]
    elif purchases:
        buy_lines = [format_purchase_line(listing) for listing in purchases]
    else:
        buy_lines = [NOT_SOLD_TEXT]
    sections: list[tuple[str, list[str]]] = [("Buy", buy_lines)]

    if rentals_failed:
        sections.append(("Rent", [RENTALS_UNAVAILABLE_TEXT]))
    elif rentals:
        for system, listings in group_by_star_system(rentals).items():
            sections.append((f"Rent — {system}", [format_rental_line(listing) for listing in listings]))
    else:
        sections.append(("Rent", [NOT_RENTED_TEXT]))
    return sections


def ship_shop_description(*, has_rentals: bool) -> str:
    description = "In-game prices in aUEC, cheapest first."
    if has_rentals:
        description += f"\n{RENTAL_RATE_NOTE}"
    return description
