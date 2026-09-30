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
    # A bare gun hardpoint. Rare at ship level, but it's what a turret's own gun slots are
    # (child_gun_ports).
    "WeaponGun": "Guns",
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
    # The wiki's own flag (from the game data) for whether a player can swap what's in the
    # port. The Perseus's remote turrets are False: the housing is fixed, only the guns in
    # it change.
    editable: bool = True
    # The port's own requirement on the part: every one must be in the part's `tags`. A
    # Perseus remote turret slot requires 'RSI_Perseus_Remote_Turret_Top'; a PDC slot 'PDC'.
    required_tags: frozenset[str] = frozenset()
    # The stock item's wiki uuid - for a turret, where its own gun slots come from.
    equipped_uuid: str | None = None
    # Whether a gun mount (UEX "Turrets") can go here: always for a turret port, and for a
    # gun slot whose compatible_types list Turret too (a gimbal).
    accepts_mounts: bool | None = None

    @property
    def uex_category(self) -> str | None:
        return PORT_TYPE_TO_UEX_CATEGORY.get(self.port_type)

    @property
    def categories(self) -> list[str]:
        """Every UEX category this port can be shopped under, guns before mounts. A weapon
        port the game doesn't let the player change is shopped under neither."""
        base = self.uex_category
        if base is None:
            return []
        if self.port_type not in ("Turret", "WeaponGun"):
            return [base]
        if not self.editable:
            return []
        mounts = self.accepts_mounts if self.accepts_mounts is not None else self.port_type == "Turret"
        categories = [GUNS_CATEGORY] if self.accepts_guns or self.port_type == "WeaponGun" else []
        return categories + ([MOUNTS_CATEGORY] if mounts else [])

    @property
    def needs_child_gun_ports(self) -> bool:
        """A turret whose guns sit in its own ports, not in the ship's slot (the Perseus's
        remote turrets): child_gun_ports finds them from the stock item's detail."""
        return self.port_type == "Turret" and not self.accepts_guns and bool(self.equipped_uuid)


def _compatible(row: dict[str, Any], port_type: str) -> bool:
    return any(
        isinstance(entry, dict) and entry.get("type") == port_type
        for entry in row.get("compatible_types") or []
    )


def _accepts_guns(row: dict[str, Any]) -> bool:
    return _compatible(row, "WeaponGun")


def _sizes(row: dict[str, Any]) -> tuple[int, int] | None:
    sizes = row.get("sizes") or {}
    low, high = sizes.get("min"), sizes.get("max")
    if not isinstance(low, int) or not isinstance(high, int) or isinstance(low, bool) or isinstance(high, bool):
        return None
    return low, high


def _equipped_uuid(row: dict[str, Any]) -> str | None:
    equipped = row.get("equipped_item") if isinstance(row.get("equipped_item"), dict) else {}
    uuid = row.get("equipped_item_uuid") or equipped.get("uuid")
    return uuid if isinstance(uuid, str) and uuid else None


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
        sizes = _sizes(row)
        if not name or sizes is None:
            continue
        result.append(ShipPort(
            name=name, port_type=port_type, size_min=sizes[0], size_max=sizes[1],
            accepts_guns=port_type == "WeaponGun" or (port_type == "Turret" and _accepts_guns(row)),
            tags=frozenset(base_tags | set(_tag_list(row.get("port_tags")))),
            editable=row.get("editable") is not False,
            required_tags=frozenset(_tag_list(row.get("required_tags"))),
            equipped_uuid=_equipped_uuid(row),
        ))
    return result


def child_gun_ports(parent: ShipPort, equipped: dict[str, Any] | None) -> list[ShipPort]:
    """The gun slots inside a turret, from its stock item's wiki detail (`ports`): e.g. the
    Perseus's top remote turret holds two S3 guns ('hardpoint_gimbal_left'/'_right'), which
    the ship's own slot never shows. Named '<turret port>/<gun port>' so a saved entry
    stays tied to that exact slot. A gun slot the game locks (the Perseus PDCs' own gun)
    is left out, like any other locked weapon port."""
    if not isinstance(equipped, dict):
        return []
    item_tags = set(_tag_list(equipped.get("tags")))
    result: list[ShipPort] = []
    for row in equipped.get("ports") or []:
        if not isinstance(row, dict) or not row.get("name"):
            continue
        if row.get("type") != "WeaponGun" and not _accepts_guns(row):
            continue
        sizes = _sizes(row)
        if sizes is None or row.get("editable") is False:
            continue
        result.append(ShipPort(
            name=f"{parent.name}/{row['name']}", port_type="WeaponGun", size_min=sizes[0], size_max=sizes[1],
            accepts_guns=True, tags=frozenset(parent.tags | item_tags | set(_tag_list(row.get("port_tags")))),
            required_tags=frozenset(_tag_list(row.get("required_tags"))),
            accepts_mounts=_compatible(row, "Turret"),
        ))
    return result


def tags_allow(part: dict[str, Any], port: ShipPort) -> bool:
    """A ship-specific part (wiki `required_tags`, e.g. the Reliant Toshima Turret's
    ['MISC_Reliant_Base']) only fits a port whose ship/port tags include every one of them
    - the game's own fit rule. Without it, an S4 Avenger nose was offered the Reliant's
    and the Buccaneer's own turrets just because their size matched. A part with no
    required_tags (or no wiki detail to say) isn't restricted.

    It's checked the other way too: a port with its own required_tags only takes a part
    carrying all of them in its `tags` (a PDC slot needs 'PDC'), so an ordinary gimbal isn't
    offered for it. A part with no wiki detail can't show those tags, so it doesn't fit such
    a port."""
    if not set(_tag_list(part.get("required_tags"))) <= port.tags:
        return False
    return port.required_tags <= set(_tag_list(part.get("tags")))


def pick_fitting_variant(variants: list[dict[str, Any]], port: ShipPort) -> dict[str, Any] | None:
    """Of every wiki item sharing one shop name, the one to show for this port: an
    unrestricted one first, else one whose required_tags this ship has, else None. The
    detail UEX's uuid leads to can be a ship-specific variant of a generic shop part -
    'VariPuck S4 Gimbal Mount' led to the Polaris-only one, which both failed the tag
    check for every other ship and carried the Polaris variant's own stats."""
    fitting = [v for v in variants if tags_allow(v, port)]
    unrestricted = [v for v in fitting if not _tag_list(v.get("required_tags"))]
    # Of the rest, the plain one: 12 wiki items are named "VariPuck S3 Gimbal Mount", and
    # the first unrestricted one listed is 'Mount_Gimbal_S3_AllSizes' (holds S1-S13), not
    # the plain 'Mount_Gimbal_S3' the shop sells. The shortest class name is the base one.
    pool = unrestricted or fitting
    pool.sort(key=lambda v: len(str(v.get("class_name") or "")))
    return pool[0] if pool else None


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
