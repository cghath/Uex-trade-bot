"""Pure logic for /ship-loadout: one recommended part for every slot on a ship, built on the
Ship Parts Finder's own candidates (each slot's sold, fitting, unlocked parts - see
ShipPartsFinder.candidates_for_port). No Discord, no I/O: the cog loads the candidates and the
stock parts' wiki details, and this decides what to recommend.

The owner's decisions (2026-10-01):
- Four profiles. Balanced ranks every slot by its category's key stat
  (ship_part_display.ranking_stat). Stealth ranks every component by lowest EM signature, a
  cooler's IR only breaking EM ties (EM matters more; coolers first went by IR). Tank ranks
  shields by HP (already their key stat) and every other component by its own durability.
  Guns rank by DPS in all three, except that between guns within 5% DPS of each other the
  higher alpha damage wins (DPS_BAND). Budget picks the most key stat per aUEC, only among parts
  that beat the stock part.
- A gun must hurt a heavy fighter (2026-10-03): since 4.7, armor ignores any projectile below its
  deflection threshold, and the live wiki (4.10.1) puts heavy fighters around 54 physical / 40
  energy per projectile. The S3 Mantis (32 physical a round) did nothing to any of them. A gun
  that gets through ranks ahead of one that doesn't (ARMOR_REFERENCE, beats_armor) - before the
  5% band, which research found is the right width as a tiebreak but the wrong tool for this.
  Budget buys a gun only when it beats stock head to head this way and loses no DPS.
- Scatterguns are never recommended (nobody uses them at the moment, the owner's call; their
  wiki DPS also counts every pellet of a shot, which put them first in S1-S3). A stock one is
  always worth replacing.
- A turret the game locks, gimbals and all, still has guns a player can change: the Idris-M's
  manned and remote turrets. Only the wiki's single-vehicle tree shows them
  (locked_turret_gun_ports), and the loadout adds them as gun slots.
- A point-defense (PDC) slot always keeps its stock turret, the M2C "Swarm" on every ship that
  has one: it shoots down incoming missiles and never runs out of ammo, which the turret rank
  (the gun size it holds) can't see - it had the Perseus swapping six for the Pepperbox.
- Missile racks keep the ship's own (2026-10-02): ranked by missile size, the one-missile rack
  won every slot size, swapping the Cutlass Black's 4x S2 racks for 1x S4 and most ships'
  multi-missile racks the same way. More smaller missiles or fewer bigger ones is the
  player's call, which the rack rank can't make. A rack slot that comes empty still gets a
  pick.
- Where the stock part is already the best pick, the slot says "keep stock" instead of
  suggesting a purchase.
- A gun hardpoint keeps whatever mount the ship comes with: under a stock gimbal, the pick is a
  gun for the gimbal's own gun slot. Every stock VariPuck gimbal holds a gun of its own size
  (S3 gimbal, S3 gun - checked on the live wiki), so the size is read from the mount's ports,
  never assumed.
- Power is shown, never warned about: the loadout's total power pips, what its power plants
  make. Comparing that with every part's maximum draw warned on nearly every stock ship, and no
  ship is meant to run every part at full power at once. It never changes a pick.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from bot.uex.ship_part_display import _number, format_port_label, ranking_stat, shop_text
from bot.uex.ship_parts import (
    GUNS_CATEGORY,
    ShipPort,
    _equipped_uuid,
    category_label,
    child_gun_ports,
)

BALANCED, STEALTH, TANK, BUDGET = "Balanced", "Stealth", "Tank", "Budget"
PROFILES = (BALANCED, STEALTH, TANK, BUDGET)
DEFAULT_PROFILE = BALANCED

# UEX category names (the shopping list's stored keys), for the categories a rule singles out.
SHIELDS_CATEGORY = "Shield Generators"
COOLERS_CATEGORY = "Coolers"
POWER_PLANTS_CATEGORY = "Power Plants"
MISSILE_RACKS_CATEGORY = "Missile Racks"

# Why a slot buys nothing. Shown to the player, so plain words.
STOCK_IS_BEST = "stock is already the best pick"
NOTHING_BEATS_STOCK = "nothing sold beats the stock part"
NOTHING_SOLD = "no shop sells a part that fits"
NO_STATS = "the wiki has no stats for the parts sold for this slot"
STOCK_UNKNOWN = "the stock part's stats couldn't be loaded to compare"
ONLY_SCATTERGUNS = "the only guns sold for it are scatterguns"
# Guns whose DPS is within this fraction of each other count as level on DPS, and the higher
# alpha damage wins between them (the owner's call, 2026-10-01). Revisit after the next game
# patch's weapon damage changes (ROADMAP.md).
DPS_BAND = 0.05
POINT_DEFENSE = "point defense: it shoots down incoming missiles and never runs out of ammo"
# Heavy-fighter armor, the owner's reference (2026-10-03): per-projectile deflection on the live
# wiki (4.10.1, armor.deflection) is 54 physical on the Hurricane, F8C, Scorpius and Vanguard
# Warden, and 29-40 energy across them. A projectile below its type's figure does nothing to
# full-health armor. Re-check after the combat (TTK) patch (ROADMAP.md).
ARMOR_REFERENCE = {"physical": 54.0, "energy": 40.0}
STOCK_RACKS = "missile racks keep the ship's own: more missiles or bigger ones is your call"

# What each profile favours, under the loadout's title and in /ship-loadout's profile choices.
# Player-facing, so the stat is named rather than called the "key stat" (a code term).
PROFILE_BLURBS = {
    BALANCED: "the best main stat in every slot: DPS, shield HP, quantum speed, power, cooling",
    STEALTH: "the lowest EM signature (IR breaks ties on coolers); guns still by DPS",
    TANK: "the most shield HP and the toughest components; guns still by DPS",
    BUDGET: "the cheapest real upgrades: the most main stat per aUEC, only where it beats stock",
}


def _path(detail: Any, *keys: str) -> Any:
    for key in keys:
        if not isinstance(detail, dict):
            return None
        detail = detail.get(key)
    return detail


def key_stat(detail: dict | None) -> float | None:
    """The category's key stat (ranking_stat's value), or None when there's nothing to rate."""
    stat = ranking_stat(detail) if isinstance(detail, dict) else None
    return stat[1] if stat else None


def em_signature(detail: dict | None) -> float | None:
    return _number(_path(detail, "emission", "em_max"))


def ir_signature(detail: dict | None) -> float | None:
    return _number(_path(detail, "emission", "ir"))


def component_health(detail: dict | None) -> float | None:
    return _number(_path(detail, "durability", "health"))


def shield_regen(detail: dict | None) -> float | None:
    return _number(_path(detail, "shield", "regen_rate"))


def tiebreak_key(detail: dict | None, category: str) -> tuple:
    """Between parts level on what the profile ranks by: a shield's faster regen, then the
    tougher component. One line's grades share their key stat - the 7SA Concord (A), 6SA
    Arbiter (B) and 5SA Rhada (C) all hold 3,000 shield HP on the live wiki (4.10.1) - and the
    shop used to decide between them, so Balanced could pick the C. The better grade regens
    faster and is tougher in every line checked. Only orders the candidates: a part that ties
    the stock part on the profile's own stat still isn't worth buying (pick_for_slot)."""
    regen = _highest_first(shield_regen(detail)) if category == SHIELDS_CATEGORY else ()
    return regen + _highest_first(component_health(detail))


def alpha_damage(detail: dict | None) -> float | None:
    """A gun's damage per shot, every pellet included (vehicle_weapon.damage.alpha_total)."""
    return _number(_path(detail, "vehicle_weapon", "damage", "alpha_total"))


def projectile_speed(detail: dict | None) -> float | None:
    """How fast a gun's rounds fly, in m/s (vehicle_weapon.ammunition.speed): shown beside DPS
    and alpha, the owner's call (2026-10-07) - a faster round is easier to land."""
    return _number(_path(detail, "vehicle_weapon", "ammunition", "speed"))


def beats_armor(detail: dict | None) -> bool | None:
    """Whether one projectile gets through heavy-fighter armor (ARMOR_REFERENCE): any damage
    type's share of a shot, per pellet, at or above that type's deflection. None when the wiki
    gives no per-type damage to judge by - such a gun isn't held back for it."""
    alpha = _path(detail, "vehicle_weapon", "damage", "alpha")
    if not isinstance(alpha, dict):
        return None
    modes = _path(detail, "vehicle_weapon", "modes")
    pellets = _number(modes[0].get("pellets_per_shot")) if isinstance(modes, list) and modes and isinstance(modes[0], dict) else None
    pellets = pellets if pellets and pellets > 0 else 1.0
    return any((_number(alpha.get(kind)) or 0.0) / pellets >= threshold for kind, threshold in ARMOR_REFERENCE.items())


def _gets_through(detail: dict | None) -> bool:
    return beats_armor(detail) is not False


def is_scattergun(detail: dict | None) -> bool:
    """A scattergun: the wiki's vehicle_weapon.type ('Laser Scattergun', 'Ballistic
    Scattergun', 'Plasma Scattergun' on the live wiki, 4.10.1), or its name."""
    if not isinstance(detail, dict):
        return False
    return any(isinstance(text, str) and "scattergun" in text.lower()
               for text in (_path(detail, "vehicle_weapon", "type"), detail.get("name")))


def is_point_defense(group: "SlotGroup") -> bool:
    """A PDC slot: its stock part is a PDCTurret (wiki sub_type), or the slot only takes
    parts tagged 'PDC' (the Perseus's hardpoint_pdc_* slots require it)."""
    stock = group.stock
    if isinstance(stock, dict) and str(stock.get("sub_type") or "").lower() == "pdcturret":
        return True
    return any(tag.lower() == "pdc" for tag in group.port.required_tags)


def _level_on_dps(a: float | None, b: float | None) -> bool:
    return a is not None and b is not None and abs(a - b) <= DPS_BAND * max(a, b)


def gun_at_least_as_good(a: dict | None, b: dict | None) -> bool:
    """Whether gun `a` is at least as good as gun `b`: a scattergun never is, against any other
    gun; one that gets through heavy-fighter armor is, against one that doesn't (beats_armor);
    within DPS_BAND of each other, the higher alpha damage wins (then DPS); otherwise the
    higher DPS. A missing figure counts as worse than any known one."""
    if is_scattergun(a) != is_scattergun(b):
        return not is_scattergun(a)
    if _gets_through(a) != _gets_through(b):
        return _gets_through(a)
    dps_a, dps_b = key_stat(a), key_stat(b)
    if _level_on_dps(dps_a, dps_b):
        return (_highest_first(alpha_damage(a)) + _highest_first(dps_a)
                <= _highest_first(alpha_damage(b)) + _highest_first(dps_b))
    return _highest_first(dps_a) <= _highest_first(dps_b)


def _rank_guns(guns: list[dict]) -> list[dict]:
    """Guns best first: every gun that gets through heavy-fighter armor (beats_armor) ahead of
    every one that doesn't, and within each, the highest-DPS gun left leads and every gun within
    DPS_BAND of it is ordered by alpha damage (then DPS, then shop) ahead of the rest, which
    are ranked the same way in turn."""
    through = [c for c in guns if _gets_through(c)]
    return _rank_by_band(through) + _rank_by_band([c for c in guns if not _gets_through(c)])


def _rank_by_band(guns: list[dict]) -> list[dict]:
    remaining = sorted(guns, key=lambda c: _highest_first(key_stat(c)) + shop_key(c))
    ranked: list[dict] = []
    while remaining:
        lead = key_stat(remaining[0])
        level = [c for c in remaining if _level_on_dps(key_stat(c), lead)] or remaining[:1]
        level.sort(key=lambda c: _highest_first(alpha_damage(c)) + _highest_first(key_stat(c)) + shop_key(c))
        ranked += level
        remaining = [c for c in remaining if not any(c is chosen for chosen in level)]
    return ranked


def _lowest_first(value: float | None) -> tuple:
    # A part missing the figure sorts after every part that has it, whichever way the
    # figure itself is ordered: an unknown signature is never the stealthiest.
    return (value is None, value if value is not None else 0.0)


def _highest_first(value: float | None) -> tuple:
    return (value is None, -value if value is not None else 0.0)


def merit_key(detail: dict | None, category: str, profile: str) -> tuple:
    """How good a part is for `profile` in a `category` slot: lower sorts first. Comparable
    between a candidate and the stock part, so it also decides "keep stock". Shop distance and
    price aren't in it - those only break ties between parts (shop_key)."""
    best = _highest_first(key_stat(detail))
    if profile not in PROFILES:
        raise ValueError(f"unknown loadout profile: {profile!r}")
    if category == GUNS_CATEGORY:
        # A scattergun after every other gun, so a stock one is always replaced. Then DPS, with
        # alpha damage breaking DPS ties (the AD4B and Revenant Gatling: both 1,266 DPS).
        return (is_scattergun(detail),) + best + _highest_first(alpha_damage(detail))
    if profile in (BALANCED, BUDGET):
        return best
    if profile == STEALTH:
        em = _lowest_first(em_signature(detail))
        # EM leads everywhere. A cooler is the ship's main IR source, so its IR breaks EM ties.
        if category == COOLERS_CATEGORY:
            return em + _lowest_first(ir_signature(detail)) + best
        return em + best
    if category == SHIELDS_CATEGORY:
        return best
    return _highest_first(component_health(detail)) + best


def shop_key(candidate: dict) -> tuple:
    """Ties between equally good parts: the nearest shop with a location (a part whose
    distance is unknown after every known one), else the cheapest - the order
    /ship-parts-finder's list breaks ties in. Without a location no candidate carries
    `_distance_gm`, so price alone decides."""
    distance = _number(candidate.get("_distance_gm"))
    return (distance is None, distance if distance is not None else 0.0, _number(candidate.get("_price_buy")) or 0.0)


def value_per_auec(candidate: dict) -> float | None:
    """Budget's measure: key stat per aUEC at the part's cheapest shop."""
    stat, price = key_stat(candidate), _number(candidate.get("_price_buy"))
    if stat is None or price is None or price <= 0:
        return None
    return stat / price


def same_part(a: dict | None, b: dict | None) -> bool:
    """Whether two details are the same part: by wiki uuid, else by name and size. The name
    alone isn't enough - the S3 and S4 'Revenant Gatling' share one. UEX's uuid is never
    compared: it often differs from the wiki's for the very same part."""
    if not isinstance(a, dict) or not isinstance(b, dict):
        return False
    if a.get("uuid") and a.get("uuid") == b.get("uuid"):
        return True
    name_a, name_b = (str(d.get("name") or "").strip().lower() for d in (a, b))
    return bool(name_a) and name_a == name_b and _number(a.get("size")) == _number(b.get("size"))


def rank_candidates(candidates: list[dict], category: str, profile: str, stock: dict | None = None) -> list[dict]:
    """The slot's candidates, best first for `profile`. A part with no key stat (no wiki
    detail) is left out: there's nothing to judge it by. Budget keeps only parts whose key stat
    beats `stock`'s (every rated part, for an empty slot) and ranks those by key stat per aUEC;
    a stock part with no key stat of its own leaves Budget nothing it can call an upgrade. A gun
    slot never ranks a scattergun, and any other gun beats a stock one; otherwise Budget's gun
    must also beat stock head to head (gun_at_least_as_good) with at least stock's DPS."""
    rated = [c for c in candidates if key_stat(c) is not None]
    if category == GUNS_CATEGORY:
        rated = [c for c in rated if not is_scattergun(c)]
        if profile != BUDGET:
            return _rank_guns(rated)
    if profile != BUDGET:
        return sorted(rated, key=lambda c: merit_key(c, category, profile) + tiebreak_key(c, category) + shop_key(c))
    if category == GUNS_CATEGORY and is_scattergun(stock):
        floor = None
    else:
        floor = key_stat(stock)
        if stock is not None and floor is None:
            return []
    if category == GUNS_CATEGORY and floor is not None:
        # A gun upgrade beats stock head to head (armor gate, band) and loses no DPS: DPS alone
        # bought the YellowJacket, whose rounds bounce off even a Gladius, over a stock
        # Deadbolt I (the owner's call, 2026-10-03; audit LOGIC-2).
        upgrades = [c for c in rated if value_per_auec(c) is not None and key_stat(c) >= floor
                    and gun_at_least_as_good(c, stock) and not gun_at_least_as_good(stock, c)]
        return sorted(upgrades, key=lambda c: (-value_per_auec(c),) + tiebreak_key(c, category) + shop_key(c))
    upgrades = [c for c in rated if value_per_auec(c) is not None and (floor is None or key_stat(c) > floor)]
    return sorted(upgrades, key=lambda c: (-value_per_auec(c),) + tiebreak_key(c, category) + shop_key(c))


@dataclass(frozen=True)
class LoadoutSlot:
    """One slot the loadout fills."""
    # What a part has to fit. For a gun under a kept stock mount, the mount's own gun slot.
    port: ShipPort
    # The UEX category it's shopped under - also the shopping-list entry's category key.
    category: str
    # The port name a shopping-list entry is saved under: the browser's own name for the slot,
    # so locking in from the loadout replaces what the browser saved there and vice versa.
    entry_port_name: str
    # The stock part's wiki detail; None for an empty slot.
    stock: dict | None = None
    # The slot has a stock part, but its detail couldn't be loaded.
    stock_unknown: bool = False


def _stock_identity(slot: LoadoutSlot) -> tuple | None:
    if slot.stock_unknown:
        return ("unknown",)
    stock = slot.stock
    if not isinstance(stock, dict):
        return None
    return (stock.get("uuid") or "", str(stock.get("name") or "").strip().lower(), _number(stock.get("size")))


def fit_key(slot: LoadoutSlot) -> tuple:
    """Slots with the same key get the same candidates (same category, size and tags), so the
    cog loads them once."""
    port = slot.port
    return (slot.category, port.port_type, port.size_min, port.size_max, port.tags, port.required_tags)


@dataclass(frozen=True)
class SlotGroup:
    """Identical slots with the same stock part, shown as one line ("2x S3 Wing Gun")."""
    slots: tuple[LoadoutSlot, ...]

    @property
    def count(self) -> int:
        return len(self.slots)

    @property
    def category(self) -> str:
        return self.slots[0].category

    @property
    def port(self) -> ShipPort:
        return self.slots[0].port

    @property
    def stock(self) -> dict | None:
        return self.slots[0].stock

    @property
    def stock_unknown(self) -> bool:
        return self.slots[0].stock_unknown

    @property
    def fit_key(self) -> tuple:
        return fit_key(self.slots[0])

    @property
    def label(self) -> str:
        """'2x S3 Wing Gun', 'S1 Power Plant': the words every slot's name shares (so left
        and right drop out), after the count and size."""
        port = self.port
        size = f"S{port.size_min}" if port.size_min == port.size_max else f"S{port.size_min}-{port.size_max}"
        names = [format_port_label(slot.entry_port_name).split() for slot in self.slots]
        common = [word for word in names[0] if all(word in other for other in names[1:])]
        # A turret's gun slot reads 'Turret Remote Top · Gimbal Left': keep the separator only
        # between words that are still there.
        text = " ".join(word for i, word in enumerate(common) if word != "·" or (i and common[i - 1] != "·"))
        text = text.strip(" ·") or category_label(self.category)
        # The Idris-M's remote turrets share only 'Turret · Turret' ('Front Left Turret · Turret
        # Left'): one word said twice across the separator reads as once.
        text = re.sub(r"\b(\w+) · \1\b", r"\1", text)
        if self.category == GUNS_CATEGORY and not any(word.endswith("gun") for word in text.lower().split()):
            text += " Gun"  # a Nose Railgun slot is not a Nose Railgun Gun
        return f"{self.count}x {size} {text}" if self.count > 1 else f"{size} {text}"


def group_slots(slots: list[LoadoutSlot]) -> list[SlotGroup]:
    """Group identical slots holding the same stock part, in first-seen order. Two slots that
    fit the same parts but hold different stock (the Gladius nose's Mantis vs its wings'
    Panthers) stay apart: their "keep stock" and "was X stock" can differ."""
    grouped: dict[tuple, list[LoadoutSlot]] = {}
    for slot in slots:
        grouped.setdefault(fit_key(slot) + (_stock_identity(slot),), []).append(slot)
    return [SlotGroup(tuple(group)) for group in grouped.values()]


def slot_category(port: ShipPort) -> str | None:
    """The one category the loadout fills a port from: guns for a gun hardpoint (its mount is
    kept, never replaced - ShipPort.categories lists guns before mounts), else the port's own
    category. None for a slot the game locks."""
    categories = port.categories
    return categories[0] if categories else None


def is_gun_mount(item: dict | None) -> bool:
    """Whether a stock item is a gun mount: wiki type 'Turret', like the VariPuck S3 Gimbal
    Mount (sub_type 'GunTurret'), whose gun sits in the mount's own port. A fixed gun is type
    'WeaponGun'."""
    return isinstance(item, dict) and item.get("type") == "Turret"


def loadout_gun_ports(port: ShipPort, stock_item: dict | None) -> list[ShipPort]:
    """The gun slot(s) a gun hardpoint's recommendation has to fit. Under a stock mount (its
    wiki item detail, with `ports`): the mount's own gun slots, sized from those ports - a
    gun slot the game locks is left out, as the browser does. Under a fixed gun, or an empty
    slot: the hardpoint itself, at its own size."""
    if is_gun_mount(stock_item):
        return child_gun_ports(port, stock_item)
    return [port]


def gun_entry_port_name(hardpoint: ShipPort, gun_port: ShipPort, gun_ports: int) -> str:
    """The shopping-list port name for a gun: the hardpoint's own name, which is where the
    browser saves a gun picked for it, unless the mount holds several guns - then each gun's
    own '<hardpoint>/<gun port>' name, so they don't overwrite one another."""
    return hardpoint.name if gun_port is hardpoint or gun_ports == 1 else gun_port.name


def stock_uuids_by_port(raw_ports: Any, prefix: str = "") -> dict[str, str]:
    """Port path -> the stock item's wiki uuid, from the wiki's single-vehicle endpoint
    (GET /vehicles/{uuid}): the only one that nests what's inside a stock mount. The list
    endpoint the finder reads ports from has `ports: null` under each hardpoint, and the
    mount's own item detail has its gun slot empty. Paths join port names with '/' the way
    child_gun_ports names its slots, so 'hardpoint_gun_nose/hardpoint_class_2' is the Gladius
    nose gimbal's Mantis.

    A port the tree lists with nothing in it maps to "" - a genuinely empty slot. A path
    missing altogether (its mount's row carried no `ports`) is unknown, not empty: the cog
    must not read that as "nothing fitted" and drop Budget's beats-stock guard."""
    result: dict[str, str] = {}
    for row in raw_ports if isinstance(raw_ports, list) else []:
        if not isinstance(row, dict) or not row.get("name"):
            continue
        path = f"{prefix}{row['name']}"
        result[path] = _equipped_uuid(row) or ""
        result.update(stock_uuids_by_port(row.get("ports"), prefix=f"{path}/"))
    return result


# Port types a turret comes in: 'TurretBase' is a manned turret (the Idris-M's), which the
# finder doesn't list at all, since UEX sells none.
_TURRET_PORT_TYPES = frozenset({"Turret", "TurretBase"})


def locked_turret_gun_ports(raw_ports: Any, *, max_depth: int = 3) -> list[ShipPort]:
    """Gun slots a player can change inside a turret, read from the wiki's single-vehicle tree
    (GET /vehicles/{uuid}, the raw ports stock_uuids_by_port reads): every unlocked WeaponGun
    port below a top-level turret, down to `max_depth` levels (the cog's MAX_MOUNT_DEPTH). The Idris-M's manned turrets
    hold two locked VariPuck S5 gimbals each, with an unlocked gun inside: the finder's own
    slots never reach those guns, since the gimbal slot is locked (child_gun_ports skips it).
    Named by their full path, the shopping list's key for them. Every turret's guns are
    returned; the caller drops a turret whose guns or mount the loadout already has (the
    Perseus's remote turrets, through child_gun_ports). A PDC is left out: it keeps its stock
    turret (is_point_defense)."""
    result: list[ShipPort] = []

    def walk(rows: Any, path: str, depth: int) -> None:
        for row in rows if isinstance(rows, list) else []:
            if not isinstance(row, dict) or not row.get("name"):
                continue
            child = f"{path}/{row['name']}"
            sizes = row.get("sizes") or {}
            low, high = sizes.get("min"), sizes.get("max")
            if row.get("type") == "WeaponGun" and row.get("editable") is not False:
                if isinstance(low, int) and isinstance(high, int) and not isinstance(low, bool):
                    result.append(ShipPort(
                        name=child, port_type="WeaponGun", size_min=low, size_max=high, accepts_guns=True,
                        tags=frozenset(t for t in row.get("port_tags") or [] if isinstance(t, str)),
                        required_tags=frozenset(t for t in row.get("required_tags") or [] if isinstance(t, str)),
                        accepts_mounts=False,
                    ))
            elif depth < max_depth:
                walk(row.get("ports"), child, depth + 1)

    for row in raw_ports if isinstance(raw_ports, list) else []:
        if not isinstance(row, dict) or row.get("type") not in _TURRET_PORT_TYPES or not row.get("name"):
            continue
        item = row.get("equipped_item") if isinstance(row.get("equipped_item"), dict) else {}
        if str(item.get("sub_type") or "").lower() == "pdcturret":
            continue
        walk(row.get("ports"), row["name"], 1)
    return result


@dataclass(frozen=True)
class SlotPick:
    """One line of the loadout: the part to buy for every slot in `group`, or None to keep
    what's there, with `reason` saying why."""
    group: SlotGroup
    part: dict | None = None
    reason: str | None = None

    @property
    def keeps_stock(self) -> bool:
        return self.part is None and (self.group.stock is not None or self.group.stock_unknown)


def pick_for_slot(group: SlotGroup, candidates: list[dict], profile: str) -> SlotPick:
    """The pick for one group of slots from its candidates (any order). A part is only
    recommended over the stock one when it's better for `profile` - an equal or worse best
    pick, or the stock part itself, is "keep stock". Budget only ever buys an upgrade, so a
    stock part it can't rate makes it keep stock rather than risk a downgrade; the other
    profiles recommend their best pick, since its own stats are known."""
    if profile not in PROFILES:
        raise ValueError(f"unknown loadout profile: {profile!r}")
    if is_point_defense(group) and (group.stock is not None or group.stock_unknown):
        return SlotPick(group, None, POINT_DEFENSE)
    if group.category == MISSILE_RACKS_CATEGORY and (group.stock is not None or group.stock_unknown):
        return SlotPick(group, None, STOCK_RACKS)
    stock = group.stock
    rated = [c for c in candidates if key_stat(c) is not None]
    if not rated:
        return SlotPick(group, None, NO_STATS if candidates else NOTHING_SOLD)
    if group.category == GUNS_CATEGORY and all(is_scattergun(c) for c in rated):
        return SlotPick(group, None, ONLY_SCATTERGUNS)
    if profile == BUDGET:
        if group.stock_unknown or (stock is not None and key_stat(stock) is None):
            return SlotPick(group, None, STOCK_UNKNOWN)
        # A shop copy of the fitted part (another uuid, slightly different stats) is never an
        # "upgrade" to buy - the other profiles call it stock too (audit LOGIC-3).
        upgrades = rank_candidates([c for c in rated if not same_part(c, stock)], group.category, profile, stock)
        if not upgrades:
            return SlotPick(group, None, NOTHING_BEATS_STOCK if stock is not None else NOTHING_SOLD)
        return SlotPick(group, upgrades[0])
    if group.category == GUNS_CATEGORY and stock is not None:
        # Only the guns that beat the stock gun head to head. The 5% band isn't transitive, so
        # the top sold gun can lose to stock on DPS while another sold gun beats stock on alpha
        # within the band - which "keep stock" used to hide (audit LOGIC-1).
        beats_stock = [c for c in rated if not same_part(c, stock)
                       and gun_at_least_as_good(c, stock) and not gun_at_least_as_good(stock, c)]
        ranked = rank_candidates(beats_stock, group.category, profile)
        return SlotPick(group, ranked[0]) if ranked else SlotPick(group, None, STOCK_IS_BEST)
    best = rank_candidates(rated, group.category, profile)[0]
    stock_holds = merit_key(stock, group.category, profile) <= merit_key(best, group.category, profile)
    if stock is not None and (same_part(best, stock) or stock_holds):
        return SlotPick(group, None, STOCK_IS_BEST)
    return SlotPick(group, best)


def purchases(picks: list[SlotPick]) -> list[tuple[LoadoutSlot, dict]]:
    """Every (slot, part) to buy - one per slot, so "2x S3 Wing Gun" is two entries. Kept stock
    is never in it: that's what "Add all to shopping list" saves."""
    return [(slot, pick.part) for pick in picks if pick.part is not None for slot in pick.group.slots]


def total_cost(picks: list[SlotPick]) -> float:
    """What every purchase costs at each part's cheapest shop, a part per slot."""
    return sum((_number(part.get("_price_buy")) or 0.0) for _, part in purchases(picks))


def profile_stat(detail: dict | None, category: str, profile: str) -> tuple[str, float] | None:
    """(label, value) of the figure `profile` chose this part by, for showing it beside the
    stock part's: EM for Stealth (a cooler with no EM figure: its IR, which decided it then),
    component HP for Tank, else the key stat. A part missing that figure shows its key stat
    instead, which is what decided it then."""
    if not isinstance(detail, dict):
        return None
    if category != GUNS_CATEGORY:
        if profile == STEALTH:
            if em_signature(detail) is not None:
                return ("EM", em_signature(detail))
            if category == COOLERS_CATEGORY and ir_signature(detail) is not None:
                return ("IR", ir_signature(detail))
        if profile == TANK and category != SHIELDS_CATEGORY and component_health(detail) is not None:
            return ("component HP", component_health(detail))
    return ranking_stat(detail)


def _amount(value: float) -> str:
    value = round(value, 1)
    return f"{value:,.0f}" if value == int(value) else f"{value:,.1f}"


def stat_text(label: str, value: float) -> str:
    """'1,266 DPS', 'EM 7,430', '189.3 Mm/s'. The mount and rack ranks
    (ship_part_display._mount_rank/_rack_rank: size x 100 + count) read as what they hold."""
    if label == "quantum speed":
        return f"{value / 1_000_000:,.1f} Mm/s"
    if label == "gun size held":
        return f"holds {int(value) % 100}x S{int(value) // 100}"
    if label == "missile size":
        return f"{int(value) % 100}x S{int(value) // 100} missiles"
    if label in ("EM", "IR"):
        return f"{label} {value:,.0f}"
    whole = {"DPS": "DPS", "shield HP": "shield HP", "component HP": "component HP", "aim assist range": "m aim assist"}
    if label in whole:
        return f"{value:,.0f} {whole[label]}"
    # A power plant's segments are the pips players assign in-game: called that here, as in the
    # loadout's total power line, so one message never names the same unit two ways.
    segments = {"power generation": "power pips", "cooling": "cooling segments"}
    return f"{_amount(value)} {segments.get(label, label)}"


def shown_stat(detail: dict | None, category: str, profile: str) -> tuple[str, str] | None:
    """(kind, text) of the figure a line shows for a part: profile_stat's, as stat_text. A
    Stealth cooler shows its EM and IR together ('EM 1,490 / IR 7,130'): EM decides, but coolers
    often tie on it (the Bracer and Ultra-Flow are both EM 1,490), and then IR does. A gun shows
    its DPS and alpha damage together ('1,266 DPS / 84.4 alpha') for the same reason, and its
    projectile speed after them ('1,266 DPS / 84.4 alpha / 1,332 m/s') - shown, never ranked by."""
    if profile == STEALTH and category == COOLERS_CATEGORY:
        em, ir = em_signature(detail), ir_signature(detail)
        if em is not None and ir is not None:
            return ("EM / IR", f"{stat_text('EM', em)} / {stat_text('IR', ir)}")
    stat = profile_stat(detail, category, profile)
    if stat is None:
        return None
    if category != GUNS_CATEGORY or stat[0] != "DPS":
        return (stat[0], stat_text(*stat))
    kinds, texts = ["DPS"], [stat_text(*stat)]
    alpha, speed = alpha_damage(detail), projectile_speed(detail)
    if alpha is not None:
        kinds.append("alpha")
        texts.append(f"{_amount(alpha)} alpha")
    if speed is not None:
        kinds.append("speed")
        texts.append(f"{speed:,.0f} m/s")
    return (" / ".join(kinds), " / ".join(texts))


def power_generation(part: dict | None) -> float | None:
    value = _number(_path(part, "power_plant", "power_segment_generation"))
    return value if value is not None else _number(_path(part, "resource_network", "generation", "power"))


@dataclass(frozen=True)
class PowerTotal:
    """A loadout's power pips: what its power plants make between them."""
    pips: float
    plants: int
    # Plants with no output figure (the wiki has none, or the stock plant's stats didn't load):
    # the total leaves them out, so it's a lower bound.
    unknown: int = 0

    def text(self) -> str | None:
        """'⚡ 16 power pips', for the loadout's summary line. None for a ship with no power
        plant. A missing figure is never guessed: the text says the total is a lower bound, or
        unknown."""
        if not self.plants:
            return None
        source = "the power plant" if self.plants == 1 else f"the {self.plants} power plants"
        if not self.unknown:
            return f"⚡ {_amount(self.pips)} power pips"
        if self.unknown == self.plants:
            return f"⚡ power pips unknown: no output figure for {source}"
        return f"⚡ at least {_amount(self.pips)} power pips: no output figure for {self.unknown} of {source}"


def _installed(picks: list[SlotPick]) -> list[tuple[str, dict | None]]:
    """(category, part) for every slot as the loadout leaves it: the part bought, else the
    stock part kept (None when its stats couldn't be loaded). An empty slot nothing is bought
    for adds nothing."""
    installed: list[tuple[str, dict | None]] = []
    for pick in picks:
        group = pick.group
        if pick.part is not None:
            part = pick.part
        elif group.stock_unknown:
            part = None
        elif group.stock is not None:
            part = group.stock
        else:
            continue
        installed.extend([(group.category, part)] * group.count)
    return installed


def power_total(picks: list[SlotPick]) -> PowerTotal:
    """The loadout's power pips as it leaves the ship: each power plant's
    power_segment_generation, the bought plant's else the stock one's. Only output is totalled,
    never the parts' maximum draw: no ship is meant to run every part at full power at once,
    and draw against output warned on nearly every stock ship (the owner's call, 2026-10-01)."""
    pips, plants, unknown = 0.0, 0, 0
    for category, part in _installed(picks):
        if category != POWER_PLANTS_CATEGORY:
            continue
        plants += 1
        made = power_generation(part)
        unknown += made is None
        pips += made or 0.0
    return PowerTotal(pips, plants, unknown)


# The loadout's message, layout C (the owner's pick from real-data mockups, 2026-10-07): a header
# with the total, then "Upgrades" - a part per slot with its stat before -> after and, under it,
# its shop - then "Keeping stock", each reason said once over the slots it covers.

_STAT_PIECE = re.compile(r"^([\d,.]+) (.+)$")
_SIGNATURE_PIECE = re.compile(r"^(EM|IR) ([\d,.]+)$")


def _stat_pieces(text: str) -> list[tuple[str, str, bool]]:
    """'684 DPS / 410.2 alpha' -> [('684', 'DPS', False), ('410.2', 'alpha', False)]; a
    signature reads label first ('EM 250' -> ('250', 'EM', True)); anything else ('holds 2x S3')
    is one value with no unit."""
    pieces = []
    for piece in text.split(" / "):
        if match := _SIGNATURE_PIECE.match(piece):
            pieces.append((match.group(2), match.group(1), True))
        elif match := _STAT_PIECE.match(piece):
            pieces.append((match.group(1), match.group(2), False))
        else:
            pieces.append((piece, "", False))
    return pieces


def stat_change(part: dict, stock: dict | None, category: str, profile: str) -> str:
    """'`547 → 930` DPS', '1,266 DPS · `63.3 → 84.4` alpha', 'EM `1,240 → 250`': the
    figure the profile chose the part by, from the stock part's to this one's. Only the part's own
    figure when the stock part has no comparable one. Says when the armor gate decided, so a
    lower-DPS pick doesn't read as a mistake."""
    shown = shown_stat(part, category, profile)
    if shown is None:
        return ""
    was = shown_stat(stock, category, profile)
    if was is None or was[0].split(" / ")[0] != shown[0].split(" / ")[0]:
        text = shown[1]
    else:
        # Figure by figure: one the stock part lacks (a gun with no speed on the wiki) is
        # shown alone, the rest still compared. One that didn't change is said once
        # ('1,266 DPS'), not '`1,266 → 1,266`'.
        old = {(unit, label_first): before for before, unit, label_first in _stat_pieces(was[1])}
        pieces = []
        for after, unit, label_first in _stat_pieces(shown[1]):
            before = old.get((unit, label_first))
            if before is None or before == after:
                pieces.append((f"{unit} {after}" if label_first else f"{after} {unit}").strip())
            else:
                pieces.append((f"{unit} `{before} → {after}`" if label_first else f"`{before} → {after}` {unit}").strip())
        text = " · ".join(pieces)
    if category == GUNS_CATEGORY and beats_armor(part) and beats_armor(stock) is False:
        text += " — stock can't get through heavy-fighter armor"
    return text


def upgrade_entry(pick: SlotPick, profile: str) -> str:
    """One purchase under "Upgrades":
    '**2x S3 Wing Gun** → **M5A Cannon** · 75,145 each' / '`546 → 684` DPS · `43.7 → 410.2` alpha'
    / '-# Orison (Ship Weapons - Crusader Showroom) · 3.2 Gm'. Its shop sits under it, not in a
    list of its own (the owner's call)."""
    group, part = pick.group, pick.part
    head = f"**{group.label}** → **{part.get('name') or 'Unknown'}**"
    price = _number(part.get("_price_buy"))
    if price:
        head += f" · {price:,.0f}" + (" each" if group.count > 1 else "")
    lines = [head]
    change = stat_change(part, group.stock, group.category, profile)
    if group.stock_unknown:
        # Bought without a comparison: the other profiles still recommend their best pick,
        # whose own stats are known, but the line says the stock part couldn't be checked.
        change = f"{change} · stock part unknown" if change else "stock part unknown"
    if change:
        lines.append(change)
    distance = _number(part.get("_distance_gm"))
    where = [bit for bit in (shop_text(part.get("_terminal_name")),
                             f"{distance:.1f} Gm" if distance is not None else None) if bit]
    if where:
        lines.append("-# " + " · ".join(where))
    return "\n".join(lines)


# How "Keeping stock" heads a reason that needs more than the reason itself.
_REASON_HEADINGS = {
    STOCK_RACKS: "**Missile racks** · more missiles or bigger ones is your call",
    POINT_DEFENSE: "**Point defense** · shoots down incoming missiles, never runs out of ammo",
}


def _kept_line(pick: SlotPick, profile: str, reason: str) -> tuple[tuple, str, int, str]:
    """(what makes two slots the same line, size, count, the rest) for one slot group nothing
    is bought for."""
    group = pick.group
    if not pick.keeps_stock:
        what, stat = "empty slot", None
    else:
        what = (group.stock.get("name") if isinstance(group.stock, dict) else None) or "stock part"
        # A PDC's turret rank (the gun size it holds) isn't why it's kept, so it isn't shown. A
        # kept rack shows what it holds ('4x S2 missiles'), not the profile's figure (Tank's HP).
        shown = None if reason == POINT_DEFENSE else shown_stat(
            group.stock, group.category, BALANCED if reason == STOCK_RACKS else profile)
        stat = shown[1] if shown else None
    count_label = group.label
    size, words = "", count_label
    match = re.match(r"^(?:\d+x )?(S\d+(?:-\d+)?) (.*)$", count_label)
    if match:
        size, words = match.group(1), match.group(2)
    return (reason, what, stat, size), size, group.count, words


def kept_sections(picks: list[SlotPick], profile: str, reasons: list[str | None] | None = None) -> list[str]:
    """"Keeping stock": one section per reason, said once over the slots it covers, each slot
    '-# S4 Nose Gun · Revenant Gatling · 1,266 DPS'. Slots with the same stock part, figure and
    reason are one line - the Polaris's four torpedo racks read '4x S10 Torpedo'. `reasons`
    overrides a pick's reason (one only the caller knows), position for position."""
    sections: dict[str, list[dict]] = {}
    for index, pick in enumerate(picks):
        if pick.part is not None:
            continue
        reason = (reasons[index] if reasons and reasons[index] else None) or pick.reason or ""
        key, size, count, words = _kept_line(pick, profile, reason)
        lines = sections.setdefault(reason, [])
        for line in lines:
            if line["key"] == key:
                line["count"] += count
                line["words"].append(words.split())
                break
        else:
            lines.append({"key": key, "size": size, "count": count, "words": [words.split()]})
    out = []
    for reason, lines in sections.items():
        heading = _REASON_HEADINGS.get(reason) or f"**{reason[:1].upper()}{reason[1:]}**"
        body = [heading]
        for line in lines:
            # The words every merged slot's name shares, so left and right drop out.
            common = [w for w in line["words"][0] if all(w in other for other in line["words"][1:])]
            label = " ".join(common) or " ".join(line["words"][0])
            label = f"{line['size']} {label}".strip()
            if line["count"] > 1:
                label = f"{line['count']}x {label}"
            _, what, stat, _ = line["key"]
            body.append("-# " + " · ".join(bit for bit in (label, what, stat) if bit))
        out.append("\n".join(body))
    return out


def summary_line(picks: list[SlotPick]) -> str:
    """'**520,274 aUEC** for 9 parts · ⚡ 20 power pips', or that nothing needs buying."""
    bought = purchases(picks)
    if bought:
        text = f"**{total_cost(picks):,.0f} aUEC** for {len(bought)} part{'s' if len(bought) != 1 else ''}"
    else:
        text = "**Nothing to buy**: every slot keeps what it has"
    power = power_total(picks).text()
    return f"{text} · {power}" if power else text


def loadout_blocks(header: str, entries: list[str], kept: list[str]) -> tuple[str, ...]:
    """One page's blocks: the header, its upgrades, its kept-stock sections."""
    blocks = [header]
    if entries:
        blocks.append("\n".join(["### Upgrades", *entries]))
    if kept:
        blocks.append("\n".join(["### Keeping stock", *kept]))
    return tuple(blocks)


def paginate_loadout(header: str, entries: list[str], kept: list[str], budget: int) -> list[tuple[str, ...]]:
    """Pages of blocks, each at most `budget` characters with the blocks joined by blank lines,
    the header on every page, no entry or section ever cut: a big ship's loadout can run past
    one message. Always at least one page."""
    pages: list[tuple[str, ...]] = []
    page_entries: list[str] = []
    page_kept: list[str] = []
    for kind, unit in [("entry", e) for e in entries] + [("kept", k) for k in kept]:
        trial = (page_entries + [unit], page_kept) if kind == "entry" else (page_entries, page_kept + [unit])
        if (page_entries or page_kept) and len("\n\n".join(loadout_blocks(header, *trial))) > budget:
            pages.append(loadout_blocks(header, page_entries, page_kept))
            page_entries, page_kept = [], []
            trial = ([unit], []) if kind == "entry" else ([], [unit])
        page_entries, page_kept = trial
    pages.append(loadout_blocks(header, page_entries, page_kept))
    return pages
