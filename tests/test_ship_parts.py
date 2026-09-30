"""Pure bridging logic for the in-design Ship Parts Finder (bot/uex/ship_parts.py)."""
from __future__ import annotations

from bot.uex.ship_parts import (
    GUNS_CATEGORY,
    MOUNTS_CATEGORY,
    PORT_TYPE_TO_UEX_CATEGORY,
    WIKI_SIZE_ONLY_CATEGORIES,
    ShipPort,
    candidate_items_for_port,
    child_gun_ports,
    category_label,
    cheapest_listing_by_item,
    group_ports_by_category,
    parse_ports,
    part_fits_port,
    pick_fitting_variant,
    tags_allow,
)


def _wiki_port(port_type: str, name: str = "hardpoint_x", size_min: int = 1, size_max: int = 1) -> dict:
    return {"name": name, "type": port_type, "sizes": {"min": size_min, "max": size_max}}


def _catalog_row(id_, category: str, size, name: str = "Item") -> dict:
    return {"id": id_, "category": category, "size": size, "name": name}


# -- parse_ports ------------------------------------------------------------------------

def test_parse_ports_keeps_only_mapped_types():
    raw = [_wiki_port("PowerPlant"), _wiki_port("Paints"), _wiki_port("")]
    ports = parse_ports(raw)
    assert [p.port_type for p in ports] == ["PowerPlant"]


def test_parse_ports_drops_a_port_missing_a_name_or_usable_size():
    raw = [
        {"type": "PowerPlant", "sizes": {"min": 1, "max": 1}},  # no name
        {"name": "x", "type": "Cooler", "sizes": {"min": 1}},  # no max
        {"name": "x", "type": "Shield", "sizes": {"min": "1", "max": "1"}},  # string sizes
        {"name": "x", "type": "QuantumDrive", "sizes": {"min": True, "max": 1}},  # bool, not int
    ]
    assert parse_ports(raw) == []


def test_parse_ports_reads_real_size_range():
    ports = parse_ports([_wiki_port("Turret", name="hp_nose", size_min=3, size_max=4)])
    assert ports == [ShipPort(name="hp_nose", port_type="Turret", size_min=3, size_max=4)]


def test_every_mapped_category_name_is_a_non_empty_string():
    # Guards against a typo silently mapping a port type to "" or None.
    assert all(isinstance(v, str) and v for v in PORT_TYPE_TO_UEX_CATEGORY.values())


# -- ShipPort.uex_category ---------------------------------------------------------------

def test_uex_category_is_none_for_an_unmapped_type():
    port = ShipPort(name="x", port_type="Armor", size_min=1, size_max=1)
    assert port.uex_category is None


def test_uex_category_matches_the_mapping_table():
    port = ShipPort(name="x", port_type="PowerPlant", size_min=1, size_max=1)
    assert port.uex_category == "Power Plants"


# -- group_ports_by_category --------------------------------------------------------------

def test_group_ports_by_category_preserves_first_seen_order():
    ports = [
        ShipPort("a", "Cooler", 1, 1),
        ShipPort("b", "PowerPlant", 1, 1),
        ShipPort("c", "Cooler", 1, 1),
    ]
    grouped = group_ports_by_category(ports)
    assert list(grouped.keys()) == ["Coolers", "Power Plants"]
    assert [p.name for p in grouped["Coolers"]] == ["a", "c"]


def test_group_ports_by_category_excludes_unmapped_ports():
    ports = [ShipPort("a", "Armor", 1, 1), ShipPort("b", "PowerPlant", 1, 1)]
    assert list(group_ports_by_category(ports).keys()) == ["Power Plants"]


# -- candidate_items_for_port -------------------------------------------------------------

def test_candidate_items_for_port_filters_by_category_only():
    # Size is left to part_fits_port - UEX's catalog size is unreliable.
    port = ShipPort(name="x", port_type="PowerPlant", size_min=2, size_max=3)
    catalog = [
        _catalog_row(1, "Power Plants", "1"),
        _catalog_row(2, "Power Plants", None),
        _catalog_row(3, "Coolers", "2"),  # wrong category
    ]
    assert {row["id"] for row in candidate_items_for_port(catalog, port)} == {1, 2}


def test_a_gun_hardpoint_is_shopped_for_guns_and_mounts_separately():
    gun_port = ShipPort(name="hp_nose", port_type="Turret", size_min=4, size_max=4, accepts_guns=True)
    turret = ShipPort(name="hp_turret", port_type="Turret", size_min=4, size_max=4)
    catalog = [_catalog_row(1, GUNS_CATEGORY, "4"), _catalog_row(2, MOUNTS_CATEGORY, "4")]
    assert gun_port.categories == [GUNS_CATEGORY, MOUNTS_CATEGORY]
    assert [r["id"] for r in candidate_items_for_port(catalog, gun_port, GUNS_CATEGORY)] == [1]
    assert [r["id"] for r in candidate_items_for_port(catalog, gun_port, MOUNTS_CATEGORY)] == [2]
    assert candidate_items_for_port(catalog, turret, GUNS_CATEGORY) == [], "a turret that takes no gun directly"
    grouped = group_ports_by_category([gun_port, turret])
    assert [p.name for p in grouped[GUNS_CATEGORY]] == ["hp_nose"]
    assert [p.name for p in grouped[MOUNTS_CATEGORY]] == ["hp_nose", "hp_turret"]


def test_parse_ports_reads_gun_compatibility_and_merges_ship_tags():
    raw = [dict(_wiki_port("Turret", name="hp_nose", size_min=4, size_max=4),
                compatible_types=[{"type": "Turret"}, {"type": "WeaponGun"}], port_tags=["Nose_Tag"])]
    [port] = parse_ports(raw, ["AEGS_Avenger_Base"])
    assert port.accepts_guns is True
    assert port.tags == frozenset({"AEGS_Avenger_Base", "Nose_Tag"})


def test_category_labels_are_what_players_see():
    assert category_label(GUNS_CATEGORY) == "Weapons"
    assert category_label(MOUNTS_CATEGORY) == "Gun Mounts"
    assert category_label("Coolers") == "Coolers"


# -- part_fits_port / tags_allow / pick_fitting_variant --------------------------------------

def test_part_fits_port_prefers_the_wikis_size_over_uexs():
    port = ShipPort(name="x", port_type="Shield", size_min=1, size_max=1)
    # Real case: GUARD is listed S1 by UEX, S3 (72,000 HP) by the wiki.
    assert not part_fits_port(port, "Shield Generators", wiki_size=3, uex_size="1")
    assert part_fits_port(port, "Shield Generators", wiki_size=1, uex_size="3")


def test_part_fits_port_falls_back_to_uex_when_the_wiki_has_no_size():
    port = ShipPort(name="x", port_type="Shield", size_min=1, size_max=2)
    assert part_fits_port(port, "Shield Generators", wiki_size=None, uex_size="2")
    # The wiki reports 0 for a few parts it has no real size for (IonWave, Vogel).
    assert part_fits_port(port, "Shield Generators", wiki_size=0, uex_size="1")
    assert not part_fits_port(port, "Shield Generators", wiki_size=None, uex_size="")


def test_part_fits_port_never_trusts_uex_size_for_missile_racks():
    port = ShipPort(name="x", port_type="MissileLauncher", size_min=6, size_max=6)
    assert "Missile Racks" in WIKI_SIZE_ONLY_CATEGORIES
    assert not part_fits_port(port, "Missile Racks", wiki_size=None, uex_size="6")
    assert part_fits_port(port, "Missile Racks", wiki_size=6, uex_size="6")


def test_tags_allow_blocks_another_ships_own_part():
    avenger = ShipPort(name="x", port_type="Turret", size_min=4, size_max=4, tags=frozenset({"AEGS_Avenger_Base"}))
    reliant = ShipPort(name="x", port_type="Turret", size_min=4, size_max=4, tags=frozenset({"MISC_Reliant_Base"}))
    toshima = {"name": "Reliant Toshima Turret", "required_tags": ["MISC_Reliant_Base"]}
    assert not tags_allow(toshima, avenger)
    assert tags_allow(toshima, reliant)
    assert tags_allow({"name": "Generic", "required_tags": []}, avenger)
    assert tags_allow({"name": "No detail"}, avenger)


def test_pick_fitting_variant_prefers_the_unrestricted_one():
    port = ShipPort(name="x", port_type="Turret", size_min=4, size_max=4, tags=frozenset({"RSI_Polaris"}))
    variants = [
        {"uuid": "polaris", "required_tags": ["RSI_Polaris"]},
        {"uuid": "generic", "required_tags": []},
        {"uuid": "m80", "required_tags": ["ORIG_m80_Base"]},
    ]
    assert pick_fitting_variant(variants, port)["uuid"] == "generic"
    assert pick_fitting_variant(variants[:1], port)["uuid"] == "polaris"
    assert pick_fitting_variant(variants[2:], port) is None


# -- cheapest_listing_by_item ---------------------------------------------------------------

def test_cheapest_listing_by_item_keeps_the_lowest_real_price_per_item():
    rows = [
        {"id_item": 1, "price_buy": 22051, "id_terminal": 10},
        {"id_item": 1, "price_buy": 18701, "id_terminal": 11},
        {"id_item": 2, "price_buy": 0, "id_terminal": 12},  # no real price
        {"id_item": "bad", "price_buy": 5},
    ]
    cheapest = cheapest_listing_by_item(rows)
    assert set(cheapest) == {1}
    assert cheapest[1]["id_terminal"] == 11


def test_candidate_items_for_port_returns_empty_for_an_unmapped_port_type():
    port = ShipPort(name="x", port_type="Armor", size_min=1, size_max=1)
    catalog = [_catalog_row(1, "Miscellaneous", "1")]
    assert candidate_items_for_port(catalog, port) == []


# -- turret gun slots, locked ports, and two-way tag checks (the Perseus) -------------------

PERSEUS_TOP_TURRET = {
    "name": "Remote Turret", "class_name": "RSI_Perseus_Remote_Turret_Top_S3",
    "tags": ["RSI_Perseus_Remote_Turret_Top"], "required_tags": ["RSI_Perseus_Remote_Turret_Top"],
    "ports": [
        {"name": "hardpoint_gimbal_left", "type": "WeaponGun", "sizes": {"min": 3, "max": 3}, "editable": True,
         "compatible_types": [{"type": "WeaponGun"}, {"type": "Turret"}], "required_tags": []},
        {"name": "hardpoint_gimbal_right", "type": "WeaponGun", "sizes": {"min": 3, "max": 3}, "editable": True,
         "compatible_types": [{"type": "WeaponGun"}, {"type": "Turret"}], "required_tags": []},
        {"name": "hardpoint_locked", "type": "WeaponGun", "sizes": {"min": 1, "max": 1}, "editable": False,
         "compatible_types": [{"type": "WeaponGun"}]},
        {"name": "hardpoint_camera", "type": "Misc", "sizes": {"min": 1, "max": 1}},
    ],
}


def _perseus_top_slot():
    [port] = parse_ports([{
        "name": "hardpoint_turret_remote_top", "type": "Turret", "sizes": {"min": 3, "max": 3}, "editable": False,
        "compatible_types": [{"type": "Turret", "sub_types": ["TopTurret"]}],
        "required_tags": ["RSI_Perseus_Remote_Turret_Top"], "equipped_item_uuid": "turret-uuid",
    }], ["rsi_perseus"])
    return port


def test_a_locked_turret_isnt_offered_but_its_own_gun_slots_are():
    port = _perseus_top_slot()
    assert port.editable is False and port.equipped_uuid == "turret-uuid"
    assert port.required_tags == frozenset({"RSI_Perseus_Remote_Turret_Top"})
    assert port.categories == [], "the housing can't be swapped, so no Gun Mounts for it"
    assert port.needs_child_gun_ports
    guns = child_gun_ports(port, PERSEUS_TOP_TURRET)
    assert [g.name for g in guns] == ["hardpoint_turret_remote_top/hardpoint_gimbal_left",
                                     "hardpoint_turret_remote_top/hardpoint_gimbal_right"], "locked gun slot left out"
    assert all((g.size_min, g.size_max) == (3, 3) for g in guns)
    assert guns[0].categories == [GUNS_CATEGORY, MOUNTS_CATEGORY], "a gimbal can go in that gun slot too"
    assert "rsi_perseus" in guns[0].tags and "RSI_Perseus_Remote_Turret_Top" in guns[0].tags


def test_no_gun_slots_without_the_turrets_detail():
    assert child_gun_ports(_perseus_top_slot(), None) == []


def test_a_turret_that_takes_guns_directly_needs_no_child_lookup():
    [nose] = parse_ports([{"name": "hp_nose", "type": "Turret", "sizes": {"min": 4, "max": 4}, "editable": True,
                           "compatible_types": [{"type": "Turret"}, {"type": "WeaponGun"}],
                           "equipped_item": {"uuid": "varipuck"}}])
    assert nose.categories == [GUNS_CATEGORY, MOUNTS_CATEGORY] and not nose.needs_child_gun_ports


def test_a_port_with_required_tags_only_takes_parts_carrying_them():
    pdc = ShipPort(name="hp_pdc", port_type="Turret", size_min=2, size_max=2,
                   tags=frozenset({"PDC"}), required_tags=frozenset({"PDC"}))
    assert tags_allow({"name": "PPB-116 Pepperbox", "tags": ["PDC"], "required_tags": ["PDC"]}, pdc)
    assert not tags_allow({"name": "VariPuck S2 Gimbal Mount", "tags": ["gimbalMount"]}, pdc)
    assert not tags_allow({"name": "No detail"}, pdc), "unverifiable tags don't fit a tag-gated port"


def test_the_plain_variant_wins_over_an_odd_unrestricted_one():
    port = ShipPort(name="x", port_type="Turret", size_min=3, size_max=3)
    variants = [
        {"class_name": "Mount_Gimbal_S3_Polaris", "required_tags": ["RSI_Polaris"]},
        {"class_name": "Mount_Gimbal_S3_AllSizes", "required_tags": []},
        {"class_name": "Mount_Gimbal_S3", "required_tags": []},
    ]
    assert pick_fitting_variant(variants, port)["class_name"] == "Mount_Gimbal_S3"


def test_a_bare_gun_hardpoint_is_shopped_for_weapons():
    [gun] = parse_ports([{"name": "hp_gun", "type": "WeaponGun", "sizes": {"min": 2, "max": 2}}])
    assert gun.categories == [GUNS_CATEGORY] and gun.accepts_guns
