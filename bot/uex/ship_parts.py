"""Pure logic for the Ship Parts Finder: bridges a ship's real hardpoint/
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
# LifeSupportGenerator was mapped too, then dropped on the owner's call: almost none are
# actually sold, and the wiki has no stats for them to compare anyway.
PORT_TYPE_TO_UEX_CATEGORY: dict[str, str] = {
    "PowerPlant": "Power Plants",
    "Cooler": "Coolers",
    "Shield": "Shield Generators",
    "QuantumDrive": "Quantum Drives",
    "Turret": "Turrets",
    "MissileLauncher": "Missile Racks",
    "Radar": "Radar",
}

# The wiki types every gun hardpoint as a "Turret" port, and UEX's "Turrets" category is
# gimbal/spinal mounts - so guns themselves were unreachable. A Turret port whose
# compatible_types include WeaponGun takes a gun directly too, and gets this second
# category, offered separately from its mounts (the owner's call).
GUNS_CATEGORY = "Guns"
MOUNTS_CATEGORY = "Turrets"

# UEX category names are the stored keys (existing saved entries use them); these are only
# what players see.
_CATEGORY_LABELS = {GUNS_CATEGORY: "Weapons", MOUNTS_CATEGORY: "Gun Mounts"}


def category_label(category: str) -> str:
    return _CATEGORY_LABELS.get(category, category)


@dataclass(frozen=True)
class ShipPort:
    name: str
    port_type: str
    size_min: int
    size_max: int
    accepts_guns: bool = False
    # The ship's own port_tags plus this port's; a part's required_tags must all be here.
    tags: frozenset[str] = frozenset()

    @property
    def uex_category(self) -> str | None:
        return PORT_TYPE_TO_UEX_CATEGORY.get(self.port_type)

    @property
    def categories(self) -> list[str]:
        """Every UEX category this port can be shopped under, guns before mounts."""
        base = self.uex_category
        if base is None:
            return []
        if base == MOUNTS_CATEGORY and self.accepts_guns:
            return [GUNS_CATEGORY, MOUNTS_CATEGORY]
        return [base]


def _accepts_guns(row: dict[str, Any]) -> bool:
    return any(
        isinstance(entry, dict) and entry.get("type") == "WeaponGun"
        for entry in row.get("compatible_types") or []
    )


def _tag_list(value: Any) -> list[str]:
    return [t for t in value if isinstance(t, str) and t] if isinstance(value, list) else []


def parse_ports(raw_ports: list[dict[str, Any]], vehicle_tags: list[str] | None = None) -> list[ShipPort]:
    """Wiki API `ports` rows -> ShipPort, keeping only ports whose type maps to a real UEX
    category (see PORT_TYPE_TO_UEX_CATEGORY) - unsupported/cosmetic port types and any row
    missing a usable name or integer size range are silently dropped, not surfaced as a
    broken slot. `vehicle_tags` is the ship's own port_tags (WikiApiClient.
    get_vehicle_loadout), merged into each port's tags."""
    base_tags = set(vehicle_tags or [])
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
        result.append(ShipPort(
            name=name, port_type=port_type, size_min=size_min, size_max=size_max,
            accepts_guns=port_type == "Turret" and _accepts_guns(row),
            tags=frozenset(base_tags | set(_tag_list(row.get("port_tags")))),
        ))
    return result


def tags_allow(part: dict[str, Any], port: ShipPort) -> bool:
    """A ship-specific part (wiki `required_tags`, e.g. the Reliant Toshima Turret's
    ['MISC_Reliant_Base']) only fits a port whose ship/port tags include every one of them
    - the game's own fit rule. Without it, an S4 Avenger nose was offered the Reliant's
    and the Buccaneer's own turrets just because their size matched. A part with no
    required_tags (or no wiki detail to say) isn't restricted."""
    return set(_tag_list(part.get("required_tags"))) <= port.tags


def pick_fitting_variant(variants: list[dict[str, Any]], port: ShipPort) -> dict[str, Any] | None:
    """Of every wiki item sharing one shop name, the one to show for this port: an
    unrestricted one first, else one whose required_tags this ship has, else None. The
    detail UEX's uuid leads to can be a ship-specific variant of a generic shop part -
    'VariPuck S4 Gimbal Mount' led to the Polaris-only one, which both failed the tag
    check for every other ship and carried the Polaris variant's own stats."""
    fitting = [v for v in variants if tags_allow(v, port)]
    unrestricted = [v for v in fitting if not _tag_list(v.get("required_tags"))]
    return (unrestricted or fitting or [None])[0]


def group_ports_by_category(ports: list[ShipPort]) -> dict[str, list[ShipPort]]:
    """Group a ship's real (mapped) ports by UEX category name, for a category select menu
    - preserves first-seen order. A gun hardpoint appears under both Guns and Turrets."""
    grouped: dict[str, list[ShipPort]] = {}
    for port in ports:
        for category in port.categories:
            grouped.setdefault(category, []).append(port)
    return grouped


# UEX's catalog `size` can't be trusted to decide what fits a slot. Checked every sold part
# against the wiki's own size: missile racks are systematically wrong (every MSD rack listed
# as 6, including the MSD-322 that is really an S3 rack - 18 of 19 sold racks at "6"), and
# every other category but quantum drives has real disagreements too - 7 of 86 guns (AD4B
# Ballistic Gatling listed S1, really S4), 6 of 41 shields (GUARD listed S1, its 72,000 HP
# is S3-class), 4 of 41 power plants, and 9 of 19 gun mounts (several with no UEX size at
# all, so never offered). The wiki's size decides; UEX's is only a fallback for a part the
# wiki has no size for - except missile racks, where UEX's is known wrong, so a rack the
# wiki can't size is left out rather than offered for a slot it might not fit.
WIKI_SIZE_ONLY_CATEGORIES = frozenset({"Missile Racks"})


def _positive_size(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        size = int(value)
    except (TypeError, ValueError):
        return None
    # The wiki reports 0 for a handful of parts it has no real size for (IonWave, Vogel).
    return size if size > 0 else None


def part_fits_port(port: ShipPort, category: str, *, wiki_size: Any = None, uex_size: Any = None) -> bool:
    """Whether a part fits `port`: the wiki's size when it has one, else UEX's (except for
    WIKI_SIZE_ONLY_CATEGORIES). A part with no usable size either way never fits."""
    size = _positive_size(wiki_size)
    if size is None and category not in WIKI_SIZE_ONLY_CATEGORIES:
        size = _positive_size(uex_size)
    return size is not None and port.size_min <= size <= port.size_max


def candidate_items_for_port(
    catalog: list[dict[str, Any]], port: ShipPort, category: str | None = None,
) -> list[dict[str, Any]]:
    """Every catalog item in `category` (default: the port's own mapped category), if this
    port can take that category at all. Size isn't checked here - UEX's catalog size is
    unreliable (see WIKI_SIZE_ONLY_CATEGORIES), so the caller checks each part with
    part_fits_port once its wiki detail is loaded."""
    category = category or port.uex_category
    if category is None or category not in port.categories:
        return []
    return [row for row in catalog if row.get("category") == category]


def cheapest_listing_by_item(price_rows: list[dict[str, Any]]) -> dict[int, dict[str, Any]]:
    """id_item -> its cheapest real shop row from UexClient.get_items_prices_all(). UEX's
    own prices, not the copy embedded in the wiki's item detail - that copy was missing
    for many parts UEX really lists (Stronghold, 7CA 'Nargun', Durango, ...), which showed
    as "price unknown" even though several shops sell them."""
    cheapest: dict[int, dict[str, Any]] = {}
    for row in price_rows:
        try:
            id_item = int(row.get("id_item"))
            price = float(row.get("price_buy"))
        except (TypeError, ValueError):
            continue
        if price <= 0:
            continue
        current = cheapest.get(id_item)
        if current is None or price < float(current["price_buy"]):
            cheapest[id_item] = row
    return cheapest
