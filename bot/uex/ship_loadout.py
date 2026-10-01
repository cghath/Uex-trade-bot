"""Pure logic for /ship-loadout: one recommended part for every slot on a ship, built on the
Ship Parts Finder's own candidates (each slot's sold, fitting, unlocked parts - see
ShipPartsFinder.candidates_for_port). No Discord, no I/O: the cog loads the candidates and the
stock parts' wiki details, and this decides what to recommend.

The owner's decisions (2026-10-01):
- Four profiles. Balanced ranks every slot by its category's key stat
  (ship_part_display.ranking_stat). Stealth ranks every component by lowest EM signature, a
  cooler's IR only breaking EM ties (EM matters more; coolers first went by IR). Tank ranks
  shields by HP (already their key stat) and every other component by its own durability.
  Guns rank by DPS in all three, alpha damage breaking DPS ties. Budget picks the most key stat
  per aUEC, only among parts that beat the stock part.
- Scatterguns are never recommended (nobody uses them at the moment, the owner's call; their
  wiki DPS also counts every pellet of a shot, which put them first in S1-S3). A stock one is
  always worth replacing.
- A point-defense (PDC) slot always keeps its stock turret, the M2C "Swarm" on every ship that
  has one: it shoots down incoming missiles and never runs out of ammo, which the turret rank
  (the gun size it holds) can't see - it had the Perseus swapping six for the Pepperbox.
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

# Why a slot buys nothing. Shown to the player, so plain words.
STOCK_IS_BEST = "stock is already the best pick"
NOTHING_BEATS_STOCK = "nothing sold beats the stock part"
NOTHING_SOLD = "no shop sells a part that fits"
NO_STATS = "the wiki has no stats for the parts sold for this slot"
STOCK_UNKNOWN = "the stock part's stats couldn't be loaded to compare"
ONLY_SCATTERGUNS = "the only guns sold for it are scatterguns"
POINT_DEFENSE = "point defense: it shoots down incoming missiles and never runs out of ammo"

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


def alpha_damage(detail: dict | None) -> float | None:
    """A gun's damage per shot, every pellet included (vehicle_weapon.damage.alpha_total)."""
    return _number(_path(detail, "vehicle_weapon", "damage", "alpha_total"))


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
    slot never ranks a scattergun, and any other gun beats a stock one."""
    rated = [c for c in candidates if key_stat(c) is not None]
    if category == GUNS_CATEGORY:
        rated = [c for c in rated if not is_scattergun(c)]
    if profile != BUDGET:
        return sorted(rated, key=lambda c: merit_key(c, category, profile) + shop_key(c))
    if category == GUNS_CATEGORY and is_scattergun(stock):
        floor = None
    else:
        floor = key_stat(stock)
        if stock is not None and floor is None:
            return []
    upgrades = [c for c in rated if value_per_auec(c) is not None and (floor is None or key_stat(c) > floor)]
    return sorted(upgrades, key=lambda c: (-value_per_auec(c),) + shop_key(c))


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
        if self.category == GUNS_CATEGORY and "gun" not in text.lower().split():
            text += " Gun"
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
    stock = group.stock
    rated = [c for c in candidates if key_stat(c) is not None]
    if not rated:
        return SlotPick(group, None, NO_STATS if candidates else NOTHING_SOLD)
    if group.category == GUNS_CATEGORY and all(is_scattergun(c) for c in rated):
        return SlotPick(group, None, ONLY_SCATTERGUNS)
    if profile == BUDGET:
        if group.stock_unknown or (stock is not None and key_stat(stock) is None):
            return SlotPick(group, None, STOCK_UNKNOWN)
        upgrades = rank_candidates(rated, group.category, profile, stock)
        if not upgrades:
            return SlotPick(group, None, NOTHING_BEATS_STOCK if stock is not None else NOTHING_SOLD)
        return SlotPick(group, upgrades[0])
    best = rank_candidates(rated, group.category, profile)[0]
    if stock is not None and (
        same_part(best, stock)
        or merit_key(stock, group.category, profile) <= merit_key(best, group.category, profile)
    ):
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
    its DPS and alpha damage together ('1,266 DPS / 84.4 alpha') for the same reason."""
    if profile == STEALTH and category == COOLERS_CATEGORY:
        em, ir = em_signature(detail), ir_signature(detail)
        if em is not None and ir is not None:
            return ("EM / IR", f"{stat_text('EM', em)} / {stat_text('IR', ir)}")
    stat = profile_stat(detail, category, profile)
    if stat is None:
        return None
    alpha = alpha_damage(detail) if category == GUNS_CATEGORY and stat[0] == "DPS" else None
    if alpha is not None:
        return ("DPS / alpha", f"{stat_text(*stat)} / {_amount(alpha)} alpha")
    return (stat[0], stat_text(*stat))


def stat_vs_stock(part: dict, stock: dict | None, category: str, profile: str) -> str:
    """'1,266 DPS (was 547 DPS stock)': the deciding figure, and the stock part's beside it
    when it has the same one."""
    shown = shown_stat(part, category, profile)
    if shown is None:
        return ""
    was = shown_stat(stock, category, profile)
    if was is not None and was[0] == shown[0]:
        return f"{shown[1]} (was {was[1]} stock)"
    return shown[1]


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

    def line(self) -> str | None:
        """'⚡ **16** power pips in total, from the power plant.' None for a ship with no power
        plant. A missing figure is never guessed: the line says the total is a lower bound, or
        unknown."""
        if not self.plants:
            return None
        source = "the power plant" if self.plants == 1 else f"the {self.plants} power plants"
        if not self.unknown:
            return f"⚡ **{_amount(self.pips)}** power pips in total, from {source}."
        if self.unknown == self.plants:
            return f"⚡ Total power pips unknown: the wiki has no output figure for {source}."
        return (f"⚡ At least **{_amount(self.pips)}** power pips in total: the wiki has no output figure "
                f"for {self.unknown} of {source}.")


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


def pick_line(pick: SlotPick, profile: str, *, reason: str | None = None) -> str:
    """One line of the loadout message. A purchase: '**2x S3 Wing Gun** → **Mantis GT-220
    Gatling** · 853 DPS (was 547 DPS stock) · 24,045 aUEC each · Area18 (Centermass) · 3.2 Gm'.
    Nothing to buy: '**S4 Nose Gun** · keep stock **Revenant Gatling** (1,266 DPS) - stock is
    already the best pick'. `reason` replaces pick.reason, for a cause only the caller knows
    (the wiki not answering for the slot's parts)."""
    group = pick.group
    head = f"**{group.label}**"
    why = reason or pick.reason
    if pick.part is None:
        if not pick.keeps_stock:
            return f"{head} · nothing to recommend - {why}"
        name = group.stock.get("name") if isinstance(group.stock, dict) else None
        text = f"keep stock **{name}**" if name else "keep stock"
        # A PDC's turret rank (the gun size it holds) isn't why it's kept, so it isn't shown.
        shown = shown_stat(group.stock, group.category, profile) if why != POINT_DEFENSE else None
        if shown is not None:
            text += f" ({shown[1]})"
        return f"{head} · {text} - {why}"
    part = pick.part
    bits = [f"{head} → **{part.get('name') or 'Unknown'}**"]
    stat = stat_vs_stock(part, group.stock, group.category, profile)
    if group.stock_unknown:
        # Bought without a comparison: the other profiles still recommend their best pick,
        # whose own stats are known, but the line says the stock part couldn't be checked.
        stat = f"{stat} (stock part unknown)" if stat else "stock part unknown"
    if stat:
        bits.append(stat)
    price = _number(part.get("_price_buy"))
    if price:
        bits.append(f"{price:,.0f} aUEC" + (" each" if group.count > 1 else ""))
    shop = shop_text(part.get("_terminal_name"))
    if shop:
        bits.append(shop)
    distance = _number(part.get("_distance_gm"))
    if distance is not None:
        bits.append(f"{distance:.1f} Gm")
    return " · ".join(bits)


def total_line(picks: list[SlotPick]) -> str:
    """'**Total: 520,274 aUEC** for 9 parts', or that nothing needs buying."""
    bought = purchases(picks)
    if not bought:
        return "**Nothing to buy** - every slot keeps what it has."
    count = f"{len(bought)} parts" if len(bought) != 1 else "1 part"
    return f"**Total: {total_cost(picks):,.0f} aUEC** for {count}"


def paginate_lines(lines: list[str], budget: int) -> list[list[str]]:
    """Whole lines in pages of at most `budget` characters (newlines counted), never cutting
    one: a big ship's loadout runs past one Discord message. A single line longer than the
    budget gets a page to itself. Always at least one (possibly empty) page."""
    pages: list[list[str]] = [[]]
    used = 0
    for line in lines:
        cost = len(line) + 1
        if pages[-1] and used + cost > budget:
            pages.append([])
            used = 0
        pages[-1].append(line)
        used += cost
    return pages
