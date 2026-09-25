"""Display helpers for /ship-parts-finder's comparison list - pure, no Discord or I/O.

Each candidate is a Star Citizen wiki /items/{uuid} detail dict (or a minimal
{name, size} stand-in when the wiki has no detail for it), plus the cog's own
underscore-prefixed keys: _price_buy, _terminal_name, _distance_gm.

Layout (picked by the owner from mockups against real data): one header naming the slot,
with any stat every option shares said once there; then per part a name line (grade,
class, maker), a price/shop/distance line, and a line of labeled stats that differ. The
previous version dumped raw wiki field names (max_health: 72000, decay_ratio: 0.25) on
every line, including values identical across every option.
"""
from __future__ import annotations

from typing import Any

from bot.uex.item_finder import split_place_and_vendor

# Discord rejects message content over 2000 characters; the browsing view's text also
# carries a header and selection summary around this list.
LIST_BUDGET_CHARS = 1400

Stat = tuple[str, str]


def _snake_case(camel: str) -> str:
    return "".join(["_" + c.lower() if c.isupper() else c for c in camel]).lstrip("_")


def _number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _sentence_case(text: str) -> str:
    return text[:1].upper() + text[1:].lower() if text else text


def _stat_block(detail: dict) -> tuple[str | None, dict]:
    for raw_key in (detail.get("type"), detail.get("sub_type")):
        if raw_key:
            key = _snake_case(str(raw_key))
            block = detail.get(key)
            if isinstance(block, dict):
                return key, block
    return None, {}


def _weapon_stats(weapon: dict) -> list[Stat]:
    stats: list[Stat] = []
    if weapon.get("type"):
        stats.append(("weapon_type", _sentence_case(str(weapon["type"]))))
    damage = weapon.get("damage") if isinstance(weapon.get("damage"), dict) else {}
    dps = damage.get("dps") if isinstance(damage.get("dps"), dict) else {}
    kinds = [kind for kind, value in dps.items() if (_number(value) or 0) > 0]
    if kinds:
        stats.append(("damage_type", " + ".join(kinds)))
    burst = _number(damage.get("burst"))
    if burst:
        stats.append(("dps", f"{burst:,.0f} DPS"))
    reach = _number(weapon.get("range"))
    if reach:
        stats.append(("range", f"{reach:,.0f} m range"))
    capacity = _number(weapon.get("capacity"))
    if capacity:
        stats.append(("ammo", f"{capacity:,.0f} rounds"))
    # Burst DPS alone hides how differently guns hit: the M6A Cannon does 615 per shot at
    # 100 rpm, the AD4B Gatling 84 per shot at 900 rpm.
    alpha = _number(damage.get("alpha_total"))
    if alpha:
        stats.append(("alpha", f"{alpha:,.0f} per shot"))
    rpm = _number(weapon.get("rpm"))
    if rpm:
        stats.append(("rpm", f"{rpm:,.0f} rpm"))
    ammunition = weapon.get("ammunition") if isinstance(weapon.get("ammunition"), dict) else {}
    speed = _number(ammunition.get("speed"))
    if speed:
        stats.append(("projectile_speed", f"{speed:,.0f} m/s"))
    return stats


def _shield_stats(block: dict) -> list[Stat]:
    stats: list[Stat] = []
    health = _number(block.get("max_health"))
    if health:
        stats.append(("hp", f"{health:,.0f} HP"))
    regen = _number(block.get("regen_rate"))
    if regen:
        stats.append(("regen", f"regens {regen:,.0f}/s"))
    # regen_time is time-to-full from empty: checked against real rows, max_health /
    # regen_rate matches it every time (72,000 / 14,400 = 5.0, 100,000 / 19,000 = 5.26).
    full = _number(block.get("regen_time"))
    if full:
        stats.append(("full", f"full in {full:.1f}s"))
    decay = _number(block.get("decay_ratio"))
    if decay is not None:
        stats.append(("decay", f"decay {decay:.0%}"))
    reserve = block.get("reserve_pool") if isinstance(block.get("reserve_pool"), dict) else {}
    reserve_regen = _number(reserve.get("regen_rate"))
    if reserve_regen:
        stats.append(("reserve", f"reserve {reserve_regen:,.0f}/s"))
    # regen_delay.damage: seconds after taking a hit before regen starts again.
    delay = block.get("regen_delay") if isinstance(block.get("regen_delay"), dict) else {}
    after_hit = _number(delay.get("damage"))
    if after_hit:
        stats.append(("regen_delay", f"regen after {after_hit:.1f}s"))
    return stats


def _quantum_stats(block: dict) -> list[Stat]:
    stats: list[Stat] = []
    jump = block.get("standard_jump") if isinstance(block.get("standard_jump"), dict) else {}
    if jump.get("drive_speed_formatted"):
        stats.append(("speed", str(jump["drive_speed_formatted"])))
    travel = block.get("travel_time_10gm") if isinstance(block.get("travel_time_10gm"), dict) else {}
    if travel.get("formatted"):
        stats.append(("travel", f"10 Gm in {travel['formatted']}"))
    spool = _number(jump.get("spool_up_time"))
    if spool:
        stats.append(("spool", f"spools in {spool:.1f}s"))
    cooldown = _number(jump.get("cooldown_time"))
    if cooldown:
        stats.append(("cooldown", f"{cooldown:.1f}s cooldown"))
    # Fuel use (fuel_consumption_scu_per_gm) is left out on the owner's call: it's 0.005
    # SCU/Gm on every S1 drive, so it only lengthened every line.
    # jump_range's raw value is float32's max used as a "no limit" sentinel; the
    # _formatted sibling already reads "Unlimited".
    if block.get("jump_range_formatted"):
        stats.append(("jump_range", f"{str(block['jump_range_formatted']).lower()} jump range"))
    return stats


def _mount_stats(block: dict) -> list[Stat]:
    stats: list[Stat] = []
    mounts = _number(block.get("mounts"))
    low, high = _number(block.get("min_size")), _number(block.get("max_size"))
    if mounts and low:
        size = f"S{low:.0f}" if not high or high == low else f"S{low:.0f}-S{high:.0f}"
        noun = "gun" if mounts == 1 else "guns"
        stats.append(("holds", f"Holds {mounts:.0f}× {size} {noun}"))
    yaw = block.get("yaw_axis") if isinstance(block.get("yaw_axis"), dict) else {}
    turn = _number(yaw.get("speed"))
    if turn:
        stats.append(("turn", f"turns {turn:.0f}°/s"))
    return stats


def _rack_stats(block: dict) -> list[Stat]:
    count, size = _number(block.get("missile_count")), _number(block.get("missile_size"))
    if count and size:
        noun = "missile" if count == 1 else "missiles"
        return [("holds", f"Holds {count:.0f}× S{size:.0f} {noun}")]
    return []


def _radar_stats(block: dict) -> list[Stat]:
    stats: list[Stat] = []
    assist = block.get("aim_assist") if isinstance(block.get("aim_assist"), dict) else {}
    low, high = _number(assist.get("distance_min_assignment")), _number(assist.get("distance_max_assignment"))
    if low and high:
        # Observer-Go's wiki data has these the other way round (585 / 569).
        low, high = sorted((low, high))
        stats.append(("aim_assist", f"aim assist {low:,.0f}–{high:,.0f} m"))
    sensitivity = block.get("sensitivity") if isinstance(block.get("sensitivity"), dict) else {}
    values = {_number(sensitivity.get(k)) for k in ("infrared", "electromagnetic", "cross_section")}
    if len(values) == 1 and None not in values:
        stats.append(("sensitivity", f"sensitivity {values.pop():g}"))
    cooldown = _number(block.get("cooldown"))
    if cooldown:
        stats.append(("radar_cooldown", f"{cooldown:g}s cooldown"))
    return stats


def _signature_stats(detail: dict, *, ir: bool = False) -> list[Stat]:
    """EM (and optionally IR) signature from the wiki's `emission` block: how visible the
    part makes the ship. 0 is shown, since a zero-signature part is worth seeing."""
    emission = detail.get("emission") if isinstance(detail.get("emission"), dict) else {}
    stats: list[Stat] = []
    if ir and _number(emission.get("ir")) is not None:
        stats.append(("ir", f"IR {_number(emission.get('ir')):,.0f}"))
    if _number(emission.get("em_max")) is not None:
        stats.append(("em", f"EM {_number(emission.get('em_max')):,.0f}"))
    return stats


def _component_hp(detail: dict) -> list[Stat]:
    durability = detail.get("durability") if isinstance(detail.get("durability"), dict) else {}
    health = _number(durability.get("health"))
    return [("component_hp", f"{health:,.0f} HP")] if health else []


def part_stats(detail: dict) -> list[Stat]:
    """(stable key, labeled text) pairs for one candidate, in display order. The key lets
    a stat identical across every option be lifted into the header once. A category or
    field shape this doesn't know gets no stats rather than a raw field dump.

    Which extras each category gets was picked by the owner from mockups: weapons add per
    shot/rpm/projectile speed; radar, power plants, coolers, shields and quantum drives
    add signature (coolers IR too, the main IR source); radar, power plants and coolers
    add the component's own HP. Gun mounts and missile racks stay as they were."""
    if isinstance(detail.get("vehicle_weapon"), dict):
        return _weapon_stats(detail["vehicle_weapon"])
    key, block = _stat_block(detail)
    if key == "shield":
        # Mockup order: the shield's own numbers, then EM, then reserve and regen delay.
        stats = _shield_stats(block)
        later = [s for s in stats if s[0] in ("reserve", "regen_delay")]
        return [s for s in stats if s not in later] + _signature_stats(detail) + later
    if key == "quantum_drive":
        return _quantum_stats(block) + _signature_stats(detail)
    if key == "radar":
        return _radar_stats(block) + _signature_stats(detail) + _component_hp(detail)
    if key == "turret":
        return _mount_stats(block)
    if key == "missile_rack":
        return _rack_stats(block)
    power = _number(block.get("power_segment_generation"))
    if key == "power_plant" and power:
        return [("power", f"{power:.0f} power segments")] + _signature_stats(detail) + _component_hp(detail)
    cooling = _number(block.get("coolant_segment_generation"))
    if key == "cooler" and cooling:
        return ([("cooling", f"{cooling:.0f} cooling segments")] + _signature_stats(detail, ir=True)
                + _component_hp(detail))
    return []


def _size_text(detail: dict) -> str | None:
    size = _number(detail.get("size"))
    return f"S{size:.0f}" if size else None


def shared_stats(details: list[dict]) -> list[str]:
    """Texts every candidate has identically (size first) - said once in the header.
    Nothing is shared among fewer than two candidates."""
    if len(details) < 2:
        return []
    shared: list[str] = []
    sizes = {_size_text(d) for d in details}
    if len(sizes) == 1 and None not in sizes:
        shared.append(sizes.pop())
    per_part = [part_stats(d) for d in details]
    for key, text in per_part[0]:
        if all((key, text) in stats for stats in per_part[1:]):
            shared.append(text)
    return shared


def format_port_label(port_name: str, size_min: int | None = None, size_max: int | None = None) -> str:
    """'hardpoint_weapon_gun_class1_left_wing' -> 'Left Wing Gun (S3)'. A mechanical
    cleanup of the wiki's raw port name, not a per-ship curated label."""
    words = [w for w in port_name.lower().split("_") if w and w != "hardpoint"]
    words = [w for w in words if not (w.startswith("class") and w[5:].isdigit())]
    trailing = []
    for word, label in (("missilerack", "missile rack"), ("gun", "gun")):
        if word in words and len(words) > 1:
            words.remove(word)
            trailing.append(label)
    if "weapon" in words and len(words) > 1:
        words.remove("weapon")
    label = " ".join(words + trailing).title() or port_name
    if size_min is None:
        return label
    size = f"S{size_min}" if size_min == size_max else f"S{size_min}-{size_max}"
    return f"{label} ({size})"


def shop_text(terminal_name: str | None) -> str | None:
    """'Platinum Bay - CRU-L4' -> 'CRU-L4 (Platinum Bay)', the same Place (Vendor) shape
    /ingame-item-finder uses. A place already ending in its own parentheses ('Pyro Gateway
    (Stanton)') reads 'Ship Weapons at Pyro Gateway (Stanton)' instead of stacking two."""
    if not terminal_name:
        return None
    place, vendor = split_place_and_vendor({"terminal_name": terminal_name})
    if not vendor:
        return place
    if place.rstrip().endswith(")"):
        return f"{vendor} at {place}"
    return f"{place} ({vendor})"


def format_part_block(detail: dict, *, shared: list[str], selected: bool = False) -> str:
    name = detail.get("name") or "Unknown"
    marker = "✅ " if selected else ""
    identity = []
    size = _size_text(detail)
    if size and size not in shared:
        identity.append(size)
    if detail.get("grade"):
        identity.append(f"Grade {detail['grade']}")
    if detail.get("class"):
        identity.append(str(detail["class"]))
    maker = detail.get("manufacturer")
    if isinstance(maker, dict) and maker.get("name"):
        identity.append(str(maker["name"]))
    first = f"{marker}**{name}**" + (f" · {' · '.join(identity)}" if identity else "")

    price = _number(detail.get("_price_buy"))
    shop = shop_text(detail.get("_terminal_name"))
    second = [f"{price:,.0f} aUEC" if price else "no shop price on record"]
    if shop:
        second.append(shop)
    distance = _number(detail.get("_distance_gm"))
    second.append(f"{distance:.1f} Gm" if distance is not None else "distance unknown")

    lines = [first, " · ".join(second)]
    own = [text for _, text in part_stats(detail) if text not in shared]
    if own:
        lines.append(" · ".join(own))
    if selected:
        lines.append('Selected - press "Lock in selected part" to save it.')
    return "\n".join(lines)


def format_part_list(details: list[dict], *, selected: dict | None = None,
                     budget: int = LIST_BUDGET_CHARS, slot_size: int | None = None) -> list[str]:
    """One block per part, stopping before the budget (whole parts only, never a part cut
    mid-line) and saying how many more are in the dropdown. The selected part is always
    shown, even when it's past the budget. `slot_size` is a fixed-size slot's size, which
    the heading already says - a part of exactly that size doesn't repeat it."""
    shared = shared_stats(details)
    header_shared = shared
    if slot_size is not None:
        known = f"S{slot_size}"
        header_shared = [text for text in shared if text != known]
        shared = header_shared + [known]
    lines = [f"All options: {' · '.join(header_shared)}"] if header_shared else []
    used = sum(len(line) + 1 for line in lines)
    shown = 0
    full = False
    for detail in details:
        block = format_part_block(detail, shared=shared, selected=detail is selected)
        # Once one part doesn't fit, stop there rather than skipping ahead to a shorter
        # one - the list is closest-first, and skipping would scramble that order.
        full = full or (shown > 0 and used + len(block) + 2 > budget)
        if full and detail is not selected:
            continue
        lines.extend(["", block])
        used += len(block) + 2
        shown += 1
    hidden = len(details) - shown
    if hidden:
        lines.extend(["", f"+ {hidden} more in the dropdown below."])
    return lines
