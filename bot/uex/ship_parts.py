"""Pure logic for the in-design Ship Parts Finder: bridges a ship's real hardpoint/
component slots (sourced from the Star Citizen Wiki API - UEX has no such data itself; its
own `id_vehicle` FK on `/items` is populated almost exclusively for cosmetic Liveries,
confirmed empirically against live catalog data - see PROJECT_CONTEXT.md) to UEX's own item
catalog for candidate enumeration and real-availability filtering. Kept dependency-free and
free of both bot.wiki_api and bot.uex.client for easy testing against synthetic fixtures.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

# Wiki port `type` -> UEX `/items` `category` name. Confirmed live via UUID cross-reference
# for PowerPlant/Cooler/Shield/QuantumDrive (one real component per category, 4/4 exact uuid
# matches between the two APIs); the rest needed a name-based fallback for candidate
# resolution since the two APIs' uuids for the same real component don't always agree (e.g.
# "MSD-322 Missile Rack" exists on both sides under the identical name but different uuids)
# - not a problem here, since candidates are found by category+size, not by uuid lookup.
# Armor and WeaponDefensive/CountermeasureLauncher ports both resolve to UEX's catch-all
# "Miscellaneous" category - too broad to filter reliably by category+size alone, so both
# are deliberately left out of this mapping and out of scope for v1 (confirmed with the
# user rather than a unilateral guess). FlightController and the cosmetic paint port ('')
# are excluded too - neither is a real "buy this part" concept a player shops for.
PORT_TYPE_TO_UEX_CATEGORY: dict[str, str] = {
    "PowerPlant": "Power Plants",
    "Cooler": "Coolers",
    "Shield": "Shield Generators",
    "QuantumDrive": "Quantum Drives",
    "Turret": "Turrets",
    "MissileLauncher": "Missile Racks",
    "Radar": "Radar",
    "LifeSupportGenerator": "Life Support Generator",
}


@dataclass(frozen=True)
class ShipPort:
    name: str
    port_type: str
    size_min: int
    size_max: int

    @property
    def uex_category(self) -> str | None:
        return PORT_TYPE_TO_UEX_CATEGORY.get(self.port_type)


def parse_ports(raw_ports: list[dict[str, Any]]) -> list[ShipPort]:
    """Wiki API `ports` rows -> ShipPort, keeping only ports whose type maps to a real UEX
    category (see PORT_TYPE_TO_UEX_CATEGORY) - unsupported/cosmetic port types and any row
    missing a usable name or integer size range are silently dropped, not surfaced as a
    broken slot."""
    result: list[ShipPort] = []
    for row in raw_ports:
        port_type = row.get("type")
        if port_type not in PORT_TYPE_TO_UEX_CATEGORY:
            continue
        name = row.get("name")
        if not name:
            continue
        sizes = row.get("sizes") or {}
        size_min, size_max = sizes.get("min"), sizes.get("max")
        if not isinstance(size_min, int) or not isinstance(size_max, int):
            continue
        if isinstance(size_min, bool) or isinstance(size_max, bool):
            continue
        result.append(ShipPort(name=name, port_type=port_type, size_min=size_min, size_max=size_max))
    return result


def group_ports_by_category(ports: list[ShipPort]) -> dict[str, list[ShipPort]]:
    """Group a ship's real (mapped) ports by their UEX category name, for a category select
    menu - preserves first-seen order."""
    grouped: dict[str, list[ShipPort]] = {}
    for port in ports:
        category = port.uex_category
        if category is None:
            continue
        grouped.setdefault(category, []).append(port)
    return grouped


def candidate_items_for_port(catalog: list[dict[str, Any]], port: ShipPort) -> list[dict[str, Any]]:
    """Every catalog item that could physically fit this port: same UEX category, and a
    numeric `size` within [size_min, size_max]. UEX's own `size` field is a numeric string
    ('1'..'4') with some blank/zero/null rows for items with no size at all - those never
    match a real port, since a port always has a positive minimum size."""
    category = port.uex_category
    if category is None:
        return []
    matches = []
    for row in catalog:
        if row.get("category") != category:
            continue
        try:
            size = int(row.get("size"))
        except (TypeError, ValueError):
            continue
        if port.size_min <= size <= port.size_max:
            matches.append(row)
    return matches


def sold_item_ids(price_rows: list[dict[str, Any]]) -> set[int]:
    """Distinct id_item values with at least one real UEX shop listing, from
    UexClient.get_items_prices_all() - mirrors /ingame-item-finder's own
    sold_item_name_autocomplete filter, since most of the catalog isn't actually for sale
    (confirmed empirically there too)."""
    result: set[int] = set()
    for row in price_rows:
        try:
            result.add(int(row.get("id_item")))
        except (TypeError, ValueError):
            continue
    return result


def filter_to_sold_items(candidates: list[dict[str, Any]], sold_ids: set[int]) -> list[dict[str, Any]]:
    """Drop any candidate UEX has no real shop listing for right now - a candidate list is
    only useful if every entry in it is something a player can actually go buy, not a
    catalogued item that only guarantees a dead end."""
    result = []
    for row in candidates:
        try:
            id_item = int(row.get("id"))
        except (TypeError, ValueError):
            continue
        if id_item in sold_ids:
            result.append(row)
    return result
