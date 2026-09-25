"""/ship-parts-finder's comparison-list text (bot/uex/ship_part_display.py), against stat
shapes taken from real wiki item details."""
from bot.uex.ship_part_display import (
    format_part_block,
    format_part_page,
    format_port_label,
    list_shared,
    paginate_parts,
    ranked_by_label,
    ranking_stat,
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
    # Fuel use left out on the owner's call: 0.005 SCU/Gm on every S1 drive.
    assert texts == ["188.3 Mm/s", "10 Gm in 1:09", "spools in 5.3s", "12.5s cooldown", "unlimited jump range"]
    assert not any("e+38" in text for text in texts)


# -- the extras the owner picked per category from mockups ---------------------------------

def test_weapon_extras_are_per_shot_fire_rate_and_projectile_speed():
    detail = {"vehicle_weapon": {"type": "Laser Cannon", "range": 2799, "rpm": 100,
                                 "damage": {"burst": 1026.0, "alpha_total": 615.3, "dps": {"energy": 1026.0}},
                                 "ammunition": {"speed": 1152}}}
    assert [t for _, t in part_stats(detail)] == [
        "Laser cannon", "energy", "1,026 DPS", "2,799 m range", "615 per shot", "100 rpm", "1,152 m/s"]


def test_shield_extras_are_signature_reserve_and_regen_delay():
    detail = _shield("Cloak", 2244, 404, 5.5, emission={"em_max": 250, "ir": 0})
    detail["shield"].update(reserve_pool={"regen_rate": 1010}, regen_delay={"damage": 3.85, "downed": 11})
    texts = [t for _, t in part_stats(detail)]
    assert texts[-3:] == ["EM 250", "reserve 1,010/s", "regen after 3.9s"]
    assert "IR 0" not in texts, "IR is only shown for coolers"


def test_signature_and_component_hp_extras_per_category():
    sig = {"emission": {"em_max": 5250, "ir": 4000}, "durability": {"health": 220}}
    power = dict(sig, type="PowerPlant", power_plant={"power_segment_generation": 14})
    cooler = dict(sig, type="Cooler", cooler={"coolant_segment_generation": 26})
    radar = dict(sig, type="Radar", radar={"cooldown": 2.5, "aim_assist": {}})
    qd = dict(sig, type="QuantumDrive", quantum_drive={"standard_jump": {"drive_speed_formatted": "259.1 Mm/s"}})
    assert [t for _, t in part_stats(power)] == ["14 power segments", "EM 5,250", "220 HP"]
    assert [t for _, t in part_stats(cooler)] == ["26 cooling segments", "IR 4,000", "EM 5,250", "220 HP"]
    assert [t for _, t in part_stats(radar)] == ["2.5s cooldown", "EM 5,250", "220 HP"]
    assert [t for _, t in part_stats(qd)] == ["259.1 Mm/s", "EM 5,250"], "no component HP for quantum drives"


def test_a_zero_signature_is_shown_but_a_missing_one_isnt():
    power = {"type": "PowerPlant", "power_plant": {"power_segment_generation": 14}, "emission": {"em_max": 0}}
    assert [t for _, t in part_stats(power)] == ["14 power segments", "EM 0"]
    power["emission"] = None
    assert [t for _, t in part_stats(power)] == ["14 power segments"]


def test_mounts_and_racks_get_no_extras():
    extras = {"emission": {"em_max": 100}, "durability": {"health": 1650}}
    mount = dict(extras, type="Turret", turret={"mounts": 1, "min_size": 4, "max_size": 4})
    rack = dict(extras, type="MissileLauncher", sub_type="MissileRack", missile_rack={"missile_count": 2, "missile_size": 2})
    assert [t for _, t in part_stats(mount)] == ["Holds 1× S4 gun"]
    assert [t for _, t in part_stats(rack)] == ["Holds 2× S2 missiles"]


def test_an_identical_radar_cooldown_moves_to_the_header():
    radars = [{"name": n, "size": 1, "type": "Radar", "radar": {"cooldown": 2.5}, "emission": {"em_max": em}}
              for n, em in (("Fleming", 1760), ("Capston", 1600))]
    assert shared_stats(radars) == ["S1", "2.5s cooldown"]


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
    header, shared = list_shared(parts)
    assert header == ["S1", "2,160 HP", "decay 25%"]
    body = "\n".join(format_part_page(parts, shared=shared))
    assert "2,160 HP" not in body and "decay" not in body and "S1" not in body
    assert "regens 389/s" in body and "regens 410/s" in body


def test_a_fixed_slot_size_is_not_repeated_under_all_options():
    parts = [_shield("INK", 2160, 389, 5.55), _shield("Trenta", 6400, 1216, 5.3)]
    header, shared = list_shared(parts, slot_size=1)
    assert header == ["decay 25%"], "the heading already says (S1)"
    assert "S1" in shared, "so a part of that size doesn't repeat it either"


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


def test_pages_keep_order_and_never_drop_a_part():
    parts = [_shield(f"Part{i}", 1000 + i, 300 + i, 5.0) for i in range(20)]
    pages = paginate_parts(parts, shared=[])
    assert [p for page in pages for p in page] == parts, "every part on some page, in ranked order"
    assert all(len(page) <= 6 for page in pages)
    assert len(pages) == 4


def test_a_page_stops_early_when_the_next_part_would_not_fit():
    parts = [_shield(f"Part{i}", 1000 + i, 300 + i, 5.0) for i in range(6)]
    block = len(format_part_block(parts[0], shared=[])) + 2
    pages = paginate_parts(parts, shared=[], budget=block * 3 + 20)
    assert [len(page) for page in pages] == [2, 2, 2], "room for the Selected note is kept free"


def test_selecting_a_part_never_reflows_pages():
    parts = [_shield(f"Part{i}", 1000 + i, 300 + i, 5.0) for i in range(12)]
    pages = paginate_parts(parts, shared=[], budget=700)
    for page in pages:
        for part in page:
            text = "\n".join(format_part_page(page, shared=[], selected=part))
            assert len(text) <= 700


def test_only_the_selected_part_on_a_page_is_marked():
    parts = [_shield("INK", 2160, 389, 5.55), _shield("WEB", 2160, 410, 5.3)]
    text = "\n".join(format_part_page(parts, shared=[], selected=parts[1]))
    assert "✅ **WEB**" in text and "✅ **INK**" not in text
    assert "✅" not in "\n".join(format_part_page(parts, shared=[], selected={"name": "elsewhere"}))


# -- ranking stat (the owner's "quant speed, power generation, etc") -------------------------

def test_each_category_is_ranked_by_its_key_stat():
    qd = {"type": "QuantumDrive", "quantum_drive": {"standard_jump": {"drive_speed": 629300000}}}
    power = {"type": "PowerPlant", "power_plant": {"power_segment_generation": 16}}
    cooler = {"type": "Cooler", "cooler": {"coolant_segment_generation": 32}}
    shield = _shield("INK", 2160, 389, 5.55)
    gun = {"vehicle_weapon": {"damage": {"burst": 1266.4}}}
    radar = {"type": "Radar", "radar": {"aim_assist": {"distance_min_assignment": 585, "distance_max_assignment": 569}}}
    mount = {"type": "Turret", "turret": {"mounts": 2, "max_size": 3}}
    rack = {"type": "MissileLauncher", "sub_type": "MissileRack", "missile_rack": {"missile_count": 4, "missile_size": 1}}
    assert ranking_stat(qd) == ("quantum speed", 629300000)
    assert ranking_stat(power) == ("power generation", 16)
    assert ranking_stat(cooler) == ("cooling", 32)
    assert ranking_stat(shield) == ("shield HP", 2160)
    assert ranking_stat(gun) == ("DPS", 1266.4)
    assert ranking_stat(radar) == ("aim assist range", 585)
    assert ranking_stat(mount) == ("gun size held", 302)
    assert ranking_stat(rack) == ("missile size", 104)


def test_a_part_without_detail_has_no_ranking_stat():
    assert ranking_stat({"name": "No detail"}) is None
    assert ranked_by_label([{"name": "No detail"}, _shield("INK", 2160, 389, 5.55)]) == "shield HP"
    assert ranked_by_label([{"name": "No detail"}]) is None


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


def test_a_turret_gun_slot_label_names_both_parts():
    assert format_port_label("hardpoint_turret_remote_top/hardpoint_gimbal_left", 3, 3) == \
        "Turret Remote Top · Gimbal Left (S3)"
