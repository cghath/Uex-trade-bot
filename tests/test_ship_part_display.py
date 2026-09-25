"""/ship-parts-finder's comparison-list text (bot/uex/ship_part_display.py), against stat
shapes taken from real wiki item details."""
from bot.uex.ship_part_display import (
    format_part_block,
    format_part_list,
    format_port_label,
    part_stats,
    shared_stats,
    shop_text,
)


def _shield(name, hp, regen, full, decay=0.25, **extra):
    return {"name": name, "size": 1, "grade": "C", "class": "Civilian", "manufacturer": {"name": "Seal Corporation"},
            "type": "Shield", "shield": {"max_health": hp, "regen_rate": regen, "regen_time": full, "decay_ratio": decay},
            "_price_buy": 7228.0, "_terminal_name": "Platinum Bay - CRU-L4", "_distance_gm": 27.0, **extra}


# -- part_stats: one labeled line per category, never raw field names ----------------------

def test_shield_stats_are_labeled():
    texts = [text for _, text in part_stats(_shield("INK", 2160, 389, 5.55))]
    assert texts == ["2,160 HP", "regens 389/s", "full in 5.5s", "decay 25%"]


def test_weapon_stats_come_from_the_vehicle_weapon_block():
    detail = {"vehicle_weapon": {"type": "BALLISTIC GATLING", "range": 2799, "capacity": 5220,
                                 "damage": {"burst": 1266.4, "dps": {"physical": 1266.4, "energy": 0}}}}
    texts = [text for _, text in part_stats(detail)]
    assert texts == ["Ballistic gatling", "physical", "1,266 DPS", "2,799 m range", "5,220 rounds"]


def test_quantum_drive_stats_skip_the_raw_jump_range_sentinel():
    detail = {"type": "QuantumDrive", "quantum_drive": {
        "jump_range": 3.402823e+38, "jump_range_formatted": "Unlimited", "fuel_consumption_scu_per_gm": 0.005,
        "standard_jump": {"drive_speed_formatted": "188.3 Mm/s", "spool_up_time": 5.3, "cooldown_time": 12.5},
        "travel_time_10gm": {"formatted": "1:09"},
    }}
    texts = [text for _, text in part_stats(detail)]
    assert texts == ["188.3 Mm/s", "10 Gm in 1:09", "spools in 5.3s", "12.5s cooldown", "0.005 SCU/Gm fuel",
                     "unlimited jump range"]
    assert not any("e+38" in text for text in texts)


def test_mount_rack_power_and_cooler_stats():
    mount = {"type": "Turret", "turret": {"mounts": 1, "min_size": 4, "max_size": 4, "yaw_axis": {"speed": 80}}}
    rack = {"type": "MissileLauncher", "sub_type": "MissileRack", "missile_rack": {"missile_count": 2, "missile_size": 2}}
    power = {"type": "PowerPlant", "power_plant": {"power_segment_generation": 14}}
    cooler = {"type": "Cooler", "cooler": {"coolant_segment_generation": 23}}
    assert [t for _, t in part_stats(mount)] == ["Holds 1× S4 gun", "turns 80°/s"]
    assert [t for _, t in part_stats(rack)] == ["Holds 2× S2 missiles"]
    assert [t for _, t in part_stats(power)] == ["14 power segments"]
    assert [t for _, t in part_stats(cooler)] == ["23 cooling segments"]


def test_unknown_shape_gets_no_stats_rather_than_a_field_dump():
    assert part_stats({"type": "Mystery", "mystery": {"a": 1}}) == []
    assert part_stats({"name": "No detail"}) == []


# -- shared stats lift into the header ---------------------------------------------------

def test_shared_stats_are_said_once_in_the_header_not_on_every_part():
    parts = [_shield("INK", 2160, 389, 5.55), _shield("Bulwark", 2160, 410, 5.3)]
    assert shared_stats(parts) == ["S1", "2,160 HP", "decay 25%"]
    lines = format_part_list(parts)
    assert lines[0] == "All options: S1 · 2,160 HP · decay 25%"
    body = "\n".join(lines[1:])
    assert "2,160 HP" not in body and "decay" not in body and "S1" not in body
    assert "regens 389/s" in body and "regens 410/s" in body


def test_a_fixed_slot_size_is_not_repeated_under_all_options():
    parts = [_shield("INK", 2160, 389, 5.55), _shield("Trenta", 6400, 1216, 5.3)]
    lines = format_part_list(parts, slot_size=1)
    assert lines[0] == "All options: decay 25%", "the heading already says (S1)"


def test_a_single_part_shares_nothing():
    assert shared_stats([_shield("INK", 2160, 389, 5.55)]) == []


# -- format_part_block / format_part_list ------------------------------------------------

def test_part_block_is_name_then_price_shop_distance_then_stats():
    lines = format_part_block(_shield("INK", 2160, 389, 5.55), shared=["S1"]).split("\n")
    assert lines == [
        "**INK** · Grade C · Civilian · Seal Corporation",
        "7,228 aUEC · CRU-L4 (Platinum Bay) · 27.0 Gm",
        "2,160 HP · regens 389/s · full in 5.5s · decay 25%",
    ]


def test_part_block_marks_the_selected_part_and_says_how_to_save_it():
    block = format_part_block(_shield("INK", 2160, 389, 5.55), shared=[], selected=True)
    assert block.startswith("✅ **INK**")
    assert block.endswith('Selected - press "Lock in selected part" to save it.')


def test_part_block_without_price_or_distance_says_so():
    block = format_part_block({"name": "Ecouter"}, shared=[])
    assert block.split("\n")[1] == "no shop price on record · distance unknown"


def test_part_list_stops_at_the_budget_in_order_and_counts_the_rest():
    parts = [_shield(f"Part{i}", 1000 + i, 300 + i, 5.0) for i in range(10)]
    lines = format_part_list(parts, budget=400)
    shown = [block.split("\n")[0] for block in lines if block.startswith("**Part")]
    assert shown == [f"**Part{i}** · Grade C · Civilian · Seal Corporation" for i in range(len(shown))], \
        "closest-first order kept, never skipping ahead to a shorter part"
    assert 0 < len(shown) < 10
    assert lines[-1] == f"+ {10 - len(shown)} more in the dropdown below."


def test_part_list_always_shows_the_selected_part_even_past_the_budget():
    parts = [_shield(f"Part{i}", 1000 + i, 300 + i, 5.0) for i in range(10)]
    lines = format_part_list(parts, selected=parts[9], budget=400)
    assert any(line.startswith("✅ **Part9**") for line in lines)


# -- labels ---------------------------------------------------------------------------------

def test_port_labels_are_cleaned_up():
    assert format_port_label("hardpoint_weapon_gun_class1_left_wing", 3, 3) == "Left Wing Gun (S3)"
    assert format_port_label("hardpoint_weapon_class2_nose", 4, 4) == "Nose (S4)"
    assert format_port_label("hardpoint_missilerack_right_wing", 3, 3) == "Right Wing Missile Rack (S3)"
    assert format_port_label("hardpoint_turret", 2, 4) == "Turret (S2-4)"
    assert format_port_label("hardpoint_radar") == "Radar"


def test_shop_text_is_place_then_vendor():
    assert shop_text("Platinum Bay - CRU-L4") == "CRU-L4 (Platinum Bay)"
    assert shop_text(None) is None


def test_shop_text_doesnt_stack_parentheses():
    assert shop_text("Ship Weapons - Pyro Gateway (Stanton)") == "Ship Weapons at Pyro Gateway (Stanton)"
