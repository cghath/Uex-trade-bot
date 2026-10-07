"""/ship-loadout's pure logic (bot/uex/ship_loadout.py): the four profiles, keep-stock, the
kept-mount gun rule, grouping, cost and the power pips total. Detail dicts follow the
shape of live wiki /items/{uuid} details (Avenger Titan and Gladius stock parts, wiki
4.10.1); the alternatives' numbers are made up in that same shape."""
import pytest

from bot.uex.ship_loadout import (
    BALANCED,
    BUDGET,
    NO_STATS,
    NOTHING_BEATS_STOCK,
    NOTHING_SOLD,
    ONLY_SCATTERGUNS,
    POINT_DEFENSE,
    STOCK_RACKS,
    PROFILE_BLURBS,
    PROFILES,
    STEALTH,
    STOCK_IS_BEST,
    STOCK_UNKNOWN,
    TANK,
    ARMOR_REFERENCE,
    beats_armor,
    LoadoutSlot,
    PowerTotal,
    SlotGroup,
    group_slots,
    gun_entry_port_name,
    is_point_defense,
    is_scattergun,
    locked_turret_gun_ports,
    is_gun_mount,
    kept_sections,
    loadout_gun_ports,
    merit_key,
    paginate_loadout,
    pick_for_slot,
    power_total,
    profile_stat,
    purchases,
    rank_candidates,
    same_part,
    shown_stat,
    slot_category,
    stat_change,
    stat_text,
    stock_uuids_by_port,
    summary_line,
    total_cost,
    upgrade_entry,
    value_per_auec,
)
from bot.uex.ship_parts import ShipPort

TITAN_TAGS = frozenset({"AEGS_Avenger_Base"})


def _usage(power, coolant):
    return {"usage": {"power": {"min": 0, "max": power}, "coolant": {"min": 0, "max": coolant}},
            "generation": {"coolant": None, "power": None}}


def _component(type_, block_key, block, *, name, uuid, size=1, em=1490, ir=0, health=180, power=3, coolant=3):
    return {"uuid": uuid, "name": name, "type": type_, "sub_type": "UNDEFINED", "size": size, "grade": "C",
            "class": "Military", "tags": [], "required_tags": [],
            "emission": {"ir": ir, "em_min": 0, "em_max": em, "em_decay": 0.15},
            "durability": {"health": health}, block_key: block, "resource_network": _usage(power, coolant)}


def _plant(name, gen, *, uuid=None, em=7430, health=270, **kw):
    detail = _component("PowerPlant", "power_plant", {"power_output": None, "power_segment_generation": gen},
                        name=name, uuid=uuid or f"pp-{name}", em=em, health=health, power=gen, coolant=gen, **kw)
    detail["resource_network"]["generation"]["power"] = gen
    return detail


def _cooler(name, gen, *, uuid=None, ir=7260, em=1490, health=180, **kw):
    detail = _component("Cooler", "cooler", {"cooling_rate": None, "coolant_segment_generation": gen},
                        name=name, uuid=uuid or f"cool-{name}", ir=ir, em=em, health=health, **kw)
    detail["resource_network"]["generation"]["coolant"] = gen
    return detail


def _shield(name, hp, *, uuid=None, **kw):
    return _component("Shield", "shield", {"max_health": hp}, name=name, uuid=uuid or f"shd-{name}", **kw)


def _qd(name, speed, *, uuid=None, em=15000, health=150, **kw):
    return _component("QuantumDrive", "quantum_drive", {"standard_jump": {"drive_speed": speed}},
                      name=name, uuid=uuid or f"qd-{name}", em=em, health=health, power=2, coolant=2, **kw)


def _radar(name, reach, *, uuid=None, em=1800, health=600, **kw):
    return _component("Radar", "radar", {"aim_assist": {"distance_min_assignment": 845, "distance_max_assignment": reach}},
                      name=name, uuid=uuid or f"rdr-{name}", em=em, health=health, power=5, coolant=5, **kw)


def _gun(name, dps, *, size=3, uuid=None, em=743, health=1024, power=1.5, alpha=None, kind=None):
    return {"uuid": uuid or f"gun-{name}", "name": name, "type": "WeaponGun", "sub_type": "Gun", "size": size,
            "grade": "A", "class": None, "tags": ["flightReady", "weaponMountUsable"], "required_tags": [],
            "emission": {"ir": 0, "em_min": 0, "em_max": em, "em_decay": 0.15}, "durability": {"health": health},
            "resource_network": _usage(power, power),
            "vehicle_weapon": {"type": kind, "damage": {"burst": dps, "alpha_total": alpha}}}


def _gimbal(size, *, gun_size=None, uuid=None, gun_editable=True):
    gun_size = size if gun_size is None else gun_size
    return {"uuid": uuid or f"gimbal-s{size}", "name": f"VariPuck S{size} Gimbal Mount", "class_name": f"Mount_Gimbal_S{size}",
            "type": "Turret", "sub_type": "GunTurret", "size": size, "grade": "A", "class": None,
            "tags": ["gimbalMount", "flightReady"], "required_tags": [], "durability": {"health": 1024},
            "turret": {"rotation_style": "SingleAxis", "mounts": 1, "min_size": gun_size, "max_size": gun_size},
            "resource_network": None,
            "ports": [{"name": "hardpoint_class_2", "type": "WeaponGun", "sizes": {"min": gun_size, "max": gun_size},
                       "editable": gun_editable, "compatible_types": [{"type": "WeaponGun", "sub_types": ["Gun"]}],
                       "equipped_item": None}]}


def _rack(name, missile_size, count, *, size=3, uuid=None):
    return {"uuid": uuid or f"rack-{name}", "name": name, "type": "MissileLauncher", "sub_type": "MissileRack",
            "size": size, "grade": "A", "class": None, "tags": ["flightReady"], "required_tags": [],
            "emission": None, "durability": {"health": 200},
            "missile_rack": {"missile_count": count, "missile_size": missile_size},
            "resource_network": {"usage": {"power": {"min": 0, "max": 0}, "coolant": {"min": None, "max": None}},
                                 "generation": {"coolant": None, "power": None}}}


def _sold(detail, price, *, distance="none", shop="Platinum Bay - CRU-L4"):
    """A candidate as candidates_for_port returns it: the wiki detail plus the cog's keys.
    Without a location there is no _distance_gm key at all."""
    candidate = {**detail, "_price_buy": float(price), "_terminal_name": shop, "_id_terminal": 1, "_uex_id": 1}
    if distance != "none":
        candidate["_distance_gm"] = distance
    return candidate


# Avenger Titan stock parts, as the live wiki has them.
ENDURANCE = _plant("Endurance", 15, uuid="49d355d9-b5a5-454a-a6e8-dedb4bdffa02")
BRACER = _cooler("Bracer", 34, uuid="4ec3037b-4b6f-4837-b00d-cd9b4836a82a", em=1490, ir=7260, power=3, coolant=3)
BULWARK = _shield("Bulwark", 2160, uuid="624e6c75-afd8-4606-a10e-45d12cb3c882")
EXPEDITION = _qd("Expedition", 189309100, uuid="4702547f-fa0f-4f6c-b2ed-9782ee9f518b")
ECOUTER = _radar("Ecouter", 1105, uuid="b0ccd135-4424-417c-95e4-594a88d9b72f")
REVENANT = _gun("Revenant Gatling", 1266, size=4, uuid="df89e9ca-8a11-477c-8abd-f4837a277dbe", em=33, health=1650,
                power=0.1)
OMNISKY = _gun("Omnisky IX Cannon", 546.8, uuid="95c85f25-ccd9-402e-b02a-522f5a97a654")
VARIPUCK_S3 = _gimbal(3, uuid="8197c45b-eaf4-4836-a8f0-837d1875cd9c")
VARIPUCK_S4 = _gimbal(4, uuid="ea49d194-0c0b-43e1-82fb-d72f35c1af05")
MSD_322 = _rack("MSD-322 Missile Rack", 2, 2, uuid="6bea95f5-2898-434c-bffe-4b2b332d6440")


def _port(name, port_type="Shield", size=1, **kw):
    return ShipPort(name=name, port_type=port_type, size_min=size, size_max=size, tags=TITAN_TAGS, **kw)


def _group(category, stock=None, *, port=None, count=1, stock_unknown=False, names=None):
    port = port or _port("hardpoint_slot")
    names = names or [f"{port.name}_{i}" for i in range(count)]
    return SlotGroup(tuple(LoadoutSlot(port, category, name, stock, stock_unknown) for name in names))


# -- Balanced: every slot by its category's key stat ----------------------------------------

@pytest.mark.parametrize("category,stock,better,worse", [
    ("Shield Generators", BULWARK, _shield("FR-66", 3300), _shield("INK", 1900)),
    ("Power Plants", ENDURANCE, _plant("Regulus", 18), _plant("JS-300", 12)),
    ("Coolers", BRACER, _cooler("Glacier", 40), _cooler("Frost-Star", 30)),
    ("Quantum Drives", EXPEDITION, _qd("Atlas", 230000000), _qd("Rush", 150000000)),
    ("Radar", ECOUTER, _radar("Surveyor", 1500), _radar("Cobb", 900)),
    ("Guns", OMNISKY, _gun("Mantis GT-220 Gatling", 853.3), _gun("CF-227 Badger Repeater", 400)),
])
def test_balanced_buys_the_best_key_stat_over_a_weaker_stock_part(category, stock, better, worse):
    pick = pick_for_slot(_group(category, stock), [_sold(worse, 100), _sold(better, 9000)], BALANCED)
    assert pick.part["name"] == better["name"]
    assert pick.reason is None and not pick.keeps_stock


def test_balanced_keeps_stock_when_nothing_sold_beats_it():
    pick = pick_for_slot(_group("Shield Generators", BULWARK), [_sold(_shield("INK", 1900), 100)], BALANCED)
    assert pick.part is None and pick.reason == STOCK_IS_BEST and pick.keeps_stock


def test_an_equal_part_is_not_worth_buying():
    pick = pick_for_slot(_group("Shield Generators", BULWARK), [_sold(_shield("Palisade", 2160), 100)], BALANCED)
    assert pick.reason == STOCK_IS_BEST


def test_the_stock_part_itself_on_sale_is_keep_stock():
    # The shop copy can carry the very same stats; matched by wiki uuid it's the stock part.
    pick = pick_for_slot(_group("Power Plants", ENDURANCE), [_sold(ENDURANCE, 8000)], BALANCED)
    assert pick.reason == STOCK_IS_BEST


def test_the_stock_part_matched_by_name_and_size_when_the_uuid_differs():
    shop_copy = {**ENDURANCE, "uuid": "another-uuid", "power_plant": {"power_segment_generation": 99}}
    pick = pick_for_slot(_group("Power Plants", ENDURANCE), [_sold(shop_copy, 8000)], BALANCED)
    assert pick.reason == STOCK_IS_BEST


def test_same_part_needs_the_same_size_when_only_names_match():
    # The S3 and S4 Revenant Gatling share one name.
    assert not same_part(_gun("Revenant Gatling", 900, size=3, uuid="a"), {**REVENANT, "uuid": "b"})
    assert same_part(_gun("Revenant Gatling", 900, size=4, uuid="a"), {**REVENANT, "uuid": "b"})
    assert same_part(REVENANT, REVENANT)
    assert not same_part(REVENANT, None)
    assert not same_part({"name": ""}, {"name": ""})


def test_same_part_by_wiki_uuid_even_when_the_names_differ():
    assert same_part({**ENDURANCE, "name": "Endurance (shop name)"}, ENDURANCE)


def test_a_stock_part_with_no_key_stat_is_never_better_than_a_rated_one():
    unrated_stock = {"name": "Old Shield", "size": 1}
    pick = pick_for_slot(_group("Shield Generators", unrated_stock), [_sold(_shield("INK", 1900), 100)], BALANCED)
    assert pick.part["name"] == "INK"


def test_tank_puts_a_part_with_no_durability_figure_last():
    no_health = {**_cooler("Unknown HP", 60), "durability": None}
    ranked = rank_candidates([_sold(no_health, 1), _sold(_cooler("Flimsy", 20, health=10), 1)], "Coolers", TANK)
    assert ranked[0]["name"] == "Flimsy"
    # Even after a known 0: a missing figure isn't read as 0 (which would tie, and let the
    # unknown part's stronger key stat win).
    ranked = rank_candidates([_sold(no_health, 1), _sold(_cooler("Broken", 20, health=0), 1)], "Coolers", TANK)
    assert ranked[0]["name"] == "Broken"


def test_an_empty_slot_buys_the_best_part():
    pick = pick_for_slot(_group("Guns"), [_sold(OMNISKY, 100), _sold(_gun("Mantis", 853.3), 200)], BALANCED)
    assert pick.part["name"] == "Mantis" and not pick.keeps_stock


def test_a_stock_part_whose_stats_failed_to_load_still_gets_the_best_pick_outside_budget():
    group = _group("Shield Generators", None, stock_unknown=True)
    pick = pick_for_slot(group, [_sold(_shield("FR-66", 3300), 100)], BALANCED)
    assert pick.part["name"] == "FR-66"


def test_no_candidates_and_no_rated_candidates_are_told_apart():
    assert pick_for_slot(_group("Radar", ECOUTER), [], BALANCED).reason == NOTHING_SOLD
    unrated = _sold({"name": "Mystery Radar", "size": 1}, 50)
    pick = pick_for_slot(_group("Radar", ECOUTER), [unrated], BALANCED)
    assert pick.reason == NO_STATS and pick.part is None


def test_a_part_with_no_stats_is_never_picked_even_when_cheapest_or_alone_in_an_empty_slot():
    unrated = _sold({"name": "Mystery Shield", "size": 1}, 1)
    ranked = rank_candidates([unrated, _sold(_shield("INK", 1900), 500)], "Shield Generators", BALANCED)
    assert [c["name"] for c in ranked] == ["INK"]
    assert pick_for_slot(_group("Shield Generators"), [unrated], BALANCED).part is None


# -- Ties: nearest shop with a location, cheapest without --------------------------------------

def test_ties_go_to_the_cheapest_shop_without_a_location():
    a, b = _shield("FR-66", 3300, uuid="a"), _shield("FR-66 copy", 3300, uuid="b")
    ranked = rank_candidates([_sold(a, 9000), _sold(b, 8000)], "Shield Generators", BALANCED)
    assert [c["uuid"] for c in ranked] == ["b", "a"]


def test_ties_go_to_the_nearest_shop_with_a_location_and_unknown_distance_last():
    a, b, c = (_shield(n, 3300, uuid=n) for n in ("far", "near", "unknown"))
    ranked = rank_candidates([_sold(a, 100, distance=40.0), _sold(c, 50, distance=None), _sold(b, 9000, distance=2.5)],
                             "Shield Generators", BALANCED)
    assert [x["uuid"] for x in ranked] == ["near", "far", "unknown"]


def test_a_better_part_still_beats_a_nearer_one():
    ranked = rank_candidates([_sold(_shield("INK", 1900), 100, distance=0.0), _sold(_shield("FR-66", 3300), 9000, distance=90.0)],
                             "Shield Generators", BALANCED)
    assert ranked[0]["name"] == "FR-66"


# -- Stealth: lowest EM everywhere; IR breaks cooler ties; guns still by DPS -----------------

def test_stealth_picks_the_lowest_em_component_even_with_a_weaker_key_stat():
    quiet = _plant("Quiet", 12, em=3000)
    loud = _plant("Loud", 20, em=9000)
    pick = pick_for_slot(_group("Power Plants", ENDURANCE), [_sold(loud, 100), _sold(quiet, 100)], STEALTH)
    assert pick.part["name"] == "Quiet"


def test_stealth_breaks_em_ties_on_the_key_stat():
    a, b = _shield("Weak", 2000, em=1000), _shield("Strong", 3000, em=1000)
    ranked = rank_candidates([_sold(a, 1), _sold(b, 9000)], "Shield Generators", STEALTH)
    assert ranked[0]["name"] == "Strong"


# -- Guns: never a scattergun; alpha damage breaks DPS ties ----------------------------------

# Live wiki 4.10.1 figures: the Dominance-3's 930 DPS counts all 8 pellets of a shot.
DOMINANCE = _gun("Dominance-3 Scattergun", 930, uuid="dominance", alpha=1116, kind="Laser Scattergun")
MANTIS = _gun("Mantis GT-220 Gatling", 853.3, uuid="mantis", alpha=32, kind="Ballistic Gatling")
AD4B = _gun("AD4B Ballistic Gatling", 1266, size=4, uuid="ad4b", alpha=84.4, kind="Ballistic Gatling")
REVENANT_WITH_ALPHA = {**REVENANT, "vehicle_weapon": {"type": "Ballistic Gatling",
                                                      "damage": {"burst": 1266, "alpha_total": 63.3}}}


def test_a_scattergun_is_known_by_its_type_or_its_name():
    assert is_scattergun(DOMINANCE)
    assert is_scattergun(_gun("Mystery", 500, kind="Plasma Scattergun"))
    assert is_scattergun(_gun("Predator Scattergun", 840))
    assert not is_scattergun(MANTIS) and not is_scattergun(OMNISKY) and not is_scattergun(None)


@pytest.mark.parametrize("profile", PROFILES)
def test_a_scattergun_is_never_recommended_whatever_its_dps_or_price(profile):
    cheap_scatter = _sold(DOMINANCE, 10)
    pick = pick_for_slot(_group("Guns", OMNISKY, count=2), [cheap_scatter, _sold(MANTIS, 24045)], profile)
    assert pick.part["name"] == "Mantis GT-220 Gatling"
    assert [c["name"] for c in rank_candidates([cheap_scatter, _sold(MANTIS, 24045)], "Guns", profile, OMNISKY)] == [
        "Mantis GT-220 Gatling"]


@pytest.mark.parametrize("profile", PROFILES)
def test_a_stock_scattergun_is_always_replaced(profile):
    weaker = _gun("Weaker Cannon", 300, alpha=200)
    pick = pick_for_slot(_group("Guns", DOMINANCE), [_sold(weaker, 1000)], profile)
    assert pick.part["name"] == "Weaker Cannon", "any other gun beats a stock scattergun, Budget's too"


@pytest.mark.parametrize("profile", PROFILES)
def test_only_scatterguns_for_sale_keeps_stock_and_says_why(profile):
    pick = pick_for_slot(_group("Guns", OMNISKY), [_sold(DOMINANCE, 10)], profile)
    assert pick.part is None and pick.keeps_stock and pick.reason == ONLY_SCATTERGUNS
    empty = pick_for_slot(_group("Guns"), [_sold(DOMINANCE, 10)], profile)
    assert kept_sections([empty], profile) == ["**The only guns sold for it are scatterguns**\n-# S1 Slot 0 Gun · empty slot"]


@pytest.mark.parametrize("profile", [BALANCED, STEALTH, TANK])
def test_alpha_damage_breaks_a_dps_tie(profile):
    # The AD4B and Revenant Gatling both do 1,266 DPS: the AD4B hits harder per shot.
    pick = pick_for_slot(_group("Guns", REVENANT_WITH_ALPHA), [_sold(AD4B, 99999)], profile)
    assert pick.part["name"] == "AD4B Ballistic Gatling"
    tied = [_sold(_gun("Light", 500, alpha=20), 1), _sold(_gun("Heavy", 500, alpha=90), 9000)]
    assert rank_candidates(tied, "Guns", profile)[0]["name"] == "Heavy", "alpha before the cheaper shop"


def test_dps_still_leads_alpha():
    sledge = _gun("Sledge III Mass Driver Cannon", 450, alpha=1125)
    assert rank_candidates([_sold(sledge, 1), _sold(MANTIS, 1)], "Guns", BALANCED)[0]["name"] == "Mantis GT-220 Gatling"
    # An unknown alpha sorts after a known one, never as the hardest hitter.
    no_alpha = _gun("No Alpha", 500)
    assert rank_candidates([_sold(no_alpha, 1), _sold(_gun("Some", 500, alpha=1), 1)], "Guns", BALANCED)[0]["name"] == "Some"


def test_alpha_decides_between_guns_within_five_percent_dps():
    # Live wiki 4.10.1: the Omnisky IX (546.8 DPS, 218.7 alpha) and the CF-337 Panther (545.6, 43.7).
    omnisky, panther = _gun("Omnisky IX Cannon", 546.8, alpha=218.7), _gun("CF-337 Panther Repeater", 545.6, alpha=43.7)
    assert rank_candidates([_sold(panther, 1), _sold(omnisky, 9000)], "Guns", BALANCED)[0]["name"] == "Omnisky IX Cannon"
    fast = _gun("Fast", 1000, alpha=10)
    hard_hitter = _gun("Hard Hitter", 960, alpha=500)  # 4% less DPS: level, so alpha decides
    assert [c["name"] for c in rank_candidates([_sold(fast, 1), _sold(hard_hitter, 1)], "Guns", BALANCED)] == [
        "Hard Hitter", "Fast"]
    too_far = _gun("Too Far", 940, alpha=900)  # 6% less: DPS decides
    assert rank_candidates([_sold(too_far, 1), _sold(fast, 1)], "Guns", BALANCED)[0]["name"] == "Fast"


def test_the_band_is_measured_from_the_highest_dps_left():
    # C is within 5% of B but not of A, the leader: A and B are level, and C ranks after both.
    guns = [_gun("A", 1000, alpha=10), _gun("B", 960, alpha=500), _gun("C", 920, alpha=900)]
    assert [c["name"] for c in rank_candidates([_sold(g, 1) for g in guns], "Guns", TANK)] == ["B", "A", "C"]


def test_a_stock_gun_within_the_band_with_more_alpha_is_kept():
    fast = _sold(_gun("Fast", 1000, alpha=10), 1)
    kept = pick_for_slot(_group("Guns", _gun("Stock", 960, alpha=500)), [fast], BALANCED)
    assert kept.part is None and kept.reason == STOCK_IS_BEST
    replaced = pick_for_slot(_group("Guns", _gun("Stock", 940, alpha=500)), [fast], BALANCED)
    assert replaced.part["name"] == "Fast"


def test_a_gun_line_shows_dps_and_alpha():
    assert stat_change(AD4B, REVENANT_WITH_ALPHA, "Guns", BALANCED) == "1,266 DPS · `63.3 → 84.4` alpha"
    assert shown_stat(DOMINANCE, "Guns", STEALTH) == ("DPS / alpha", "930 DPS / 1,116 alpha")
    keep = kept_sections([pick_for_slot(_group("Guns", AD4B), [_sold(REVENANT_WITH_ALPHA, 1)], BALANCED)], BALANCED)
    assert "-# S1 Slot 0 Gun · AD4B Ballistic Gatling · 1,266 DPS / 84.4 alpha" in keep[0]
    # A gun with no alpha figure is still compared on DPS; the stock gun's alpha isn't shown.
    assert stat_change(_gun("X", 900), REVENANT_WITH_ALPHA, "Guns", BALANCED) == "`1,266 → 900` DPS"


# -- Guns inside a locked turret (the Idris-M's) ------------------------------------------------

def _tree_gun(name, size, uuid, *, editable=True):
    return {"name": name, "type": "WeaponGun", "editable": editable, "sizes": {"min": size, "max": size},
            "equipped_item_uuid": uuid}


def _locked(name, port_type, size, ports, sub_type=None):
    return {"name": name, "type": port_type, "editable": False, "sizes": {"min": size, "max": size},
            "equipped_item": {"name": name, "sub_type": sub_type}, "ports": ports}


# The shape of the live Idris-M tree (GET /vehicles/{uuid}, wiki 4.10.1): a manned turret
# ('TurretBase') holding two locked VariPuck S5 gimbals, each with an unlocked gun inside.
IDRIS_TREE = [
    _locked("hardpoint_left_turret", "TurretBase", 5, [
        _locked("hardpoint_weapon_left", "Turret", 5, [_tree_gun("hardpoint_class_2", 5, "galdereen")]),
        _locked("hardpoint_weapon_right", "Turret", 5, [_tree_gun("hardpoint_class_2", 5, "galdereen")]),
        {"name": "Screen_Radar", "type": "Display", "editable": False, "sizes": {"min": 1, "max": 1}},
    ], sub_type="MannedTurret"),
    _locked("hardpoint_pdc_01", "Turret", 2, [_tree_gun("hardpoint_turret_weapon", 1, "pdc-gun")], sub_type="PDCTurret"),
    {"name": "hardpoint_power_plant", "type": "PowerPlant", "sizes": {"min": 4, "max": 4},
     "ports": [_tree_gun("not_a_turret", 1, "x")]},
    _locked("hardpoint_locked_gun_turret", "Turret", 4, [_tree_gun("turret_left", 4, "locked", editable=False)]),
    _locked("hardpoint_deep_turret", "Turret", 5, [_locked("a", "Turret", 5, [_locked("b", "Turret", 5, [
        _locked("c", "Turret", 5, [_tree_gun("hardpoint_class_2", 5, "deep")])])])]),
]


def test_guns_inside_a_locked_turret_are_found_in_the_vehicle_tree():
    ports = locked_turret_gun_ports(IDRIS_TREE)
    assert [p.name for p in ports] == ["hardpoint_left_turret/hardpoint_weapon_left/hardpoint_class_2",
                                       "hardpoint_left_turret/hardpoint_weapon_right/hardpoint_class_2"]
    assert all(p.port_type == "WeaponGun" and (p.size_min, p.size_max) == (5, 5) and p.categories == ["Guns"]
               for p in ports), "a gun slot only: the locked gimbal stays"


def test_the_turret_search_stops_at_its_depth_limit():
    deep = "hardpoint_deep_turret/a/b/c/hardpoint_class_2"
    assert deep not in [p.name for p in locked_turret_gun_ports(IDRIS_TREE)]
    assert deep in [p.name for p in locked_turret_gun_ports(IDRIS_TREE, max_depth=4)]


def test_a_gun_label_says_turret_once_and_never_gun_twice():
    names = [f"hardpoint_front_{t}_turret/turret_{s}/hardpoint_class_2" for t in ("left", "right") for s in ("left", "right")]
    group = _group("Guns", None, port=_port("x", "WeaponGun", 4), names=names)
    assert group.label == "4x S4 Front Turret Gun", "not 'Front Turret · Turret Gun'"
    railgun = _group("Guns", None, port=_port("hardpoint_nose_railgun", "WeaponGun", 10), names=["hardpoint_nose_railgun"])
    assert railgun.label == "S10 Nose Railgun", "not 'Nose Railgun Gun'"


# -- 2026-10-03 range audit -----------------------------------------------------------------

@pytest.mark.parametrize("profile", [BALANCED, STEALTH, TANK])
def test_a_gun_that_beats_stock_on_alpha_is_bought_even_when_the_top_sold_gun_loses_to_stock(profile):
    """LOGIC-1: the stock gun (not sold) out-DPSes the top-ranked sold gun, but another sold gun
    is within 5% of stock's DPS with more alpha - it beats stock by the owner's own rule."""
    stock = _gun("Stock Gun", 1050, alpha=10)
    lead, band = _gun("Lead Gun", 1000, alpha=20), _gun("Band Gun", 955, alpha=100)
    pick = pick_for_slot(_group("Guns", stock), [_sold(lead, 100), _sold(band, 100)], profile)
    assert pick.part is not None and pick.part["name"] == "Lead Gun"


def test_a_gun_slot_keeps_stock_when_no_sold_gun_beats_it_head_to_head():
    stock = _gun("Stock Gun", 1050, alpha=10)
    pick = pick_for_slot(_group("Guns", stock), [_sold(_gun("Band Gun", 955, alpha=100), 1)], BALANCED)
    assert pick.part is None and pick.reason == STOCK_IS_BEST


def test_a_gun_only_as_good_as_stock_is_not_bought():
    twin = _gun("Twin Gun", 1050, alpha=10)
    pick = pick_for_slot(_group("Guns", _gun("Stock Gun", 1050, alpha=10)), [_sold(twin, 1)], BALANCED)
    assert pick.part is None and pick.reason == STOCK_IS_BEST


def test_budget_never_buys_a_shop_copy_of_the_fitted_part():
    """LOGIC-3: the shop's copy of the stock part can carry another uuid and better stats; the
    other profiles call it stock, so Budget mustn't sell it as an upgrade."""
    shop_copy = {**ENDURANCE, "uuid": "another-uuid", "power_plant": {"power_segment_generation": 16}}
    pick = pick_for_slot(_group("Power Plants", ENDURANCE), [_sold(shop_copy, 8000)], BUDGET)
    assert pick.part is None and pick.reason == NOTHING_BEATS_STOCK


def _graded_shield(name, grade, regen, health):
    detail = _shield(name, 3000, health=health)
    detail["grade"] = grade
    detail["shield"]["regen_rate"] = regen
    return detail


# Live wiki 4.10.1: one line, three grades, the same 3,000 shield HP.
CONCORD = _graded_shield("7SA 'Concord'", "A", 660, 200)
ARBITER = _graded_shield("6SA 'Arbiter'", "B", 600, 170)
RHADA = _graded_shield("5SA 'Rhada'", "C", 570, 150)


@pytest.mark.parametrize("profile", [BALANCED, TANK])
def test_a_tie_on_the_key_stat_goes_to_the_better_grade_not_the_nearer_shop(profile):
    """The research pass's side finding: the shop broke the tie, so Balanced could buy the C.
    (Budget rightly takes the cheapest per shield HP, and Stealth goes by EM first.)"""
    sold = [_sold(RHADA, 1000, distance=1.0), _sold(ARBITER, 5000, distance=2.0), _sold(CONCORD, 9000, distance=40.0)]
    assert pick_for_slot(_group("Shield Generators"), sold, profile).part["name"] == "7SA 'Concord'"


def test_a_better_grade_with_the_same_key_stat_is_not_worth_buying_over_stock():
    pick = pick_for_slot(_group("Shield Generators", RHADA), [_sold(CONCORD, 9000)], BALANCED)
    assert pick.part is None and pick.reason == STOCK_IS_BEST


# -- The heavy-fighter armor gate (the owner's call, 2026-10-03) -----------------------------

def _typed_gun(name, dps, *, physical=0.0, energy=0.0, pellets=1, size=3, uuid=None):
    """A gun with the wiki's per-type damage per shot and pellets per shot (4.10.1 fields)."""
    gun = _gun(name, dps, size=size, uuid=uuid, alpha=physical + energy)
    gun["vehicle_weapon"]["damage"]["alpha"] = {"physical": physical, "energy": energy, "distortion": 0}
    gun["vehicle_weapon"]["modes"] = [{"mode": "Auto", "pellets_per_shot": pellets}]
    return gun


# Live wiki 4.10.1: the S3 picks either side of the gate.
MANTIS_4101 = _typed_gun("Mantis GT-220 Gatling", 853.3, physical=32)
M5A_4101 = _typed_gun("M5A Cannon", 683.6, energy=410.2)


def test_beats_armor_checks_each_damage_type_per_pellet():
    assert ARMOR_REFERENCE == {"physical": 54.0, "energy": 40.0}
    assert beats_armor(M5A_4101) is True and beats_armor(MANTIS_4101) is False
    assert beats_armor(_typed_gun("Edge", 1, physical=54)) is True, "at the threshold gets through"
    assert beats_armor(_typed_gun("Split", 1, physical=30, energy=30)) is False, "types aren't added up"
    assert beats_armor(_typed_gun("Pellets", 1, energy=480, pellets=8)) is True, "60 a pellet"
    assert beats_armor(_typed_gun("Weak pellets", 1, energy=240, pellets=8)) is False, "30 a pellet"
    assert beats_armor(_gun("No split", 900, alpha=500)) is None, "nothing to judge by"


@pytest.mark.parametrize("profile", [BALANCED, STEALTH, TANK])
def test_a_gun_that_gets_through_heavy_fighter_armor_ranks_first(profile):
    pick = pick_for_slot(_group("Guns"), [_sold(MANTIS_4101, 24045), _sold(M5A_4101, 69137)], profile)
    assert pick.part["name"] == "M5A Cannon"


def test_a_stock_gun_that_bounces_off_is_replaced_and_the_line_says_why():
    pick = pick_for_slot(_group("Guns", MANTIS_4101), [_sold(M5A_4101, 69137)], BALANCED)
    assert pick.part["name"] == "M5A Cannon"
    assert upgrade_entry(pick, BALANCED).splitlines()[1].endswith(" — stock can't get through heavy-fighter armor")


def test_the_gate_note_only_appears_when_the_gate_decided():
    stock = _typed_gun("Stock", 500, energy=100)
    pick = pick_for_slot(_group("Guns", stock), [_sold(M5A_4101, 1)], BALANCED)
    assert pick.part["name"] == "M5A Cannon" and "heavy-fighter" not in upgrade_entry(pick, BALANCED)


def test_guns_on_the_same_side_of_the_gate_still_go_by_dps_and_the_band():
    weak_fast, weak_slow = _typed_gun("Weak fast", 900, physical=20), _typed_gun("Weak slow", 700, physical=30)
    assert pick_for_slot(_group("Guns"), [_sold(weak_slow, 1), _sold(weak_fast, 1)], BALANCED).part["name"] == "Weak fast"
    level = _typed_gun("Level", 1250, physical=63.3)
    hard = _typed_gun("Hard hitter", 1266, physical=84.4)
    assert pick_for_slot(_group("Guns"), [_sold(level, 1), _sold(hard, 1)], BALANCED).part["name"] == "Hard hitter"


def test_a_gun_with_no_per_type_damage_is_not_held_back():
    unknown = _gun("Unknown split", 1000, alpha=50)
    pick = pick_for_slot(_group("Guns"), [_sold(unknown, 1), _sold(M5A_4101, 1)], BALANCED)
    assert pick.part["name"] == "Unknown split"


# The research's S1 case (figures illustrative): the stock Deadbolt I gets through; the
# YellowJacket, with more DPS, fires 8-damage rounds that bounce off even a Gladius - DPS alone
# used to buy it (audit LOGIC-2).
DEADBOLT_I = _typed_gun("Deadbolt I", 300, physical=80, size=1)
YELLOWJACKET = _typed_gun("YellowJacket", 320, physical=8, size=1)
CF117 = _typed_gun("CF-117 Bulldog", 340, energy=45, size=1)


def test_budget_never_buys_a_gun_that_loses_to_stock_head_to_head():
    pick = pick_for_slot(_group("Guns", DEADBOLT_I), [_sold(YELLOWJACKET, 100), _sold(CF117, 900)], BUDGET)
    assert pick.part["name"] == "CF-117 Bulldog", "the cheaper YellowJacket bounces off armor"


def test_budget_never_pays_for_less_dps_even_to_get_through_armor():
    pick = pick_for_slot(_group("Guns", MANTIS_4101), [_sold(M5A_4101, 100)], BUDGET)
    assert pick.part is None and pick.reason == NOTHING_BEATS_STOCK


def test_budget_still_replaces_a_stock_scattergun_with_any_gun():
    scatter = _typed_gun("Dominance-3 Scattergun", 930, energy=1116, pellets=8)
    pick = pick_for_slot(_group("Guns", scatter), [_sold(MANTIS_4101, 100)], BUDGET)
    assert pick.part["name"] == "Mantis GT-220 Gatling"


# -- Point defense: a PDC slot keeps its stock turret -------------------------------------------

# Live wiki 4.10.1: every stock PDC is the M2C "Swarm"; the Pepperbox is a PDCTurret too, but its
# turret rank (the gun size it holds, S5) beat the Swarm's S1.
SWARM = {"uuid": "swarm", "name": 'M2C "Swarm"', "type": "Turret", "sub_type": "PDCTurret", "size": 2,
         "tags": ["PDC"], "required_tags": [], "ports": [{"name": "hardpoint_turret_weapon", "sizes": {"min": 1, "max": 1}}],
         "durability": {"health": 1500}, "emission": {"em_max": 100, "ir": 0}}
PEPPERBOX = {**SWARM, "uuid": "pepperbox", "name": 'PPB-116 "Pepperbox"',
             "ports": [{"name": "turret_centre", "sizes": {"min": 5, "max": 5}}], "durability": {"health": 9000},
             "emission": {"em_max": 1, "ir": 0}}
PDC_PORT = ShipPort(name="hardpoint_pdc_top_right", port_type="Turret", size_min=2, size_max=2,
                    tags=frozenset({"rsi_perseus", "PDC"}), required_tags=frozenset({"PDC"}))


@pytest.mark.parametrize("profile", PROFILES)
def test_a_pdc_slot_keeps_its_stock_turret_in_every_profile(profile):
    group = _group("Turrets", SWARM, port=PDC_PORT, count=6,
                   names=[f"hardpoint_pdc_{side}" for side in ("top_right", "top_left", "bottom_right",
                                                               "bottom_left", "rear_bottom", "rear_top")])
    pick = pick_for_slot(group, [_sold(PEPPERBOX, 1)], profile)
    assert pick.part is None and pick.keeps_stock and pick.reason == POINT_DEFENSE
    assert kept_sections([pick], profile) == [
        '**Point defense** · shoots down incoming missiles, never runs out of ammo\n-# 6x S2 PDC · M2C "Swarm"']


# -- Missile racks: the ship's own are kept ---------------------------------------------------

# Live wiki 4.10.1 (2026-10-02): the Cutlass Black's two S4 rack slots hold the MSD-442 (4x S2);
# the rack rank (missile size first) swapped them for the MSD-414 (1x S4), the owner's catch.
MSD_442 = _rack("MSD-442 Missile Rack", 2, 4, size=4)
MSD_414 = _rack("MSD-414 Missile Rack", 4, 1, size=4)
MSD_423 = _rack("MSD-423 Missile Rack", 3, 2, size=4)
RACK_PORT = _port("hardpoint_missilerack_left", "MissileLauncher", 4)


@pytest.mark.parametrize("profile", PROFILES)
def test_a_missile_rack_keeps_the_ships_own_in_every_profile(profile):
    group = _group("Missile Racks", MSD_442, port=RACK_PORT, count=2)
    pick = pick_for_slot(group, [_sold(MSD_414, 17846), _sold(MSD_423, 11899)], profile)
    assert pick.part is None and pick.keeps_stock and pick.reason == STOCK_RACKS
    # What it holds, whatever the profile ranks by (not Tank's component HP).
    assert kept_sections([pick], profile) == [
        "**Missile racks** · more missiles or bigger ones is your call\n"
        "-# 2x S4 Left Missile Rack · MSD-442 Missile Rack · 4x S2 missiles"]


def test_a_rack_whose_stock_part_is_unknown_is_still_kept_never_swapped_blind():
    pick = pick_for_slot(_group("Missile Racks", None, port=RACK_PORT, stock_unknown=True), [_sold(MSD_414, 1)], BALANCED)
    assert pick.part is None and pick.reason == STOCK_RACKS
    assert kept_sections([pick], BALANCED)[0].endswith("-# S4 Left 0 Missile Rack · stock part")


def test_an_empty_rack_slot_still_gets_the_best_rack():
    pick = pick_for_slot(_group("Missile Racks", port=RACK_PORT), [_sold(MSD_442, 8350), _sold(MSD_414, 17846)], BALANCED)
    assert pick.part["name"] == "MSD-414 Missile Rack"


def test_a_pdc_slot_is_known_by_its_stock_part_or_its_slot_tag():
    plain_port = _port("hardpoint_turret_top", "Turret", 2)
    assert is_point_defense(_group("Turrets", SWARM, port=plain_port))
    assert is_point_defense(_group("Turrets", None, port=PDC_PORT, stock_unknown=True))
    assert not is_point_defense(_group("Turrets", VARIPUCK_S3, port=plain_port))
    # Its stock part unknown, a PDC slot is still kept, never swapped blind.
    pick = pick_for_slot(_group("Turrets", None, port=PDC_PORT, stock_unknown=True), [_sold(PEPPERBOX, 1)], BALANCED)
    assert pick.part is None and pick.reason == POINT_DEFENSE


def test_stealth_puts_a_part_with_no_em_figure_after_every_known_one():
    no_em = {**_shield("Unknown EM", 9000), "emission": None}
    ranked = rank_candidates([_sold(no_em, 1), _sold(_shield("Loud", 100, em=50000), 1)], "Shield Generators", STEALTH)
    assert ranked[0]["name"] == "Loud"


def test_stealth_coolers_go_by_em_first_then_ir():
    low_ir = _cooler("Low IR", 30, ir=4000, em=5000)
    low_em = _cooler("Low EM", 30, ir=7000, em=100)
    ranked = rank_candidates([_sold(low_ir, 1), _sold(low_em, 1)], "Coolers", STEALTH)
    assert [c["name"] for c in ranked] == ["Low EM", "Low IR"]
    # The real Bracer and Ultra-Flow tie on EM (1,490, wiki 4.10.1): the lower IR wins.
    same_em = rank_candidates([_sold(_cooler("Bracer", 34, ir=7260, em=1490), 1),
                               _sold(_cooler("Ultra-Flow", 34, ir=7130, em=1490), 1)], "Coolers", STEALTH)
    assert same_em[0]["name"] == "Ultra-Flow"


def test_stealth_coolers_equal_on_em_and_ir_go_by_the_key_stat_before_price():
    weak, strong = _cooler("Weak", 20, ir=4000, em=500), _cooler("Strong", 40, ir=4000, em=500)
    ranked = rank_candidates([_sold(weak, 1), _sold(strong, 9000)], "Coolers", STEALTH)
    assert [c["name"] for c in ranked] == ["Strong", "Weak"]


def test_stealth_non_cooler_ignores_ir():
    ranked = rank_candidates([_sold(_shield("Hot", 2000, ir=9000, em=100), 1), _sold(_shield("Cool", 2000, ir=0, em=500), 1)],
                             "Shield Generators", STEALTH)
    assert ranked[0]["name"] == "Hot"


def test_stealth_keeps_stock_when_the_stock_part_is_already_quietest():
    pick = pick_for_slot(_group("Power Plants", ENDURANCE), [_sold(_plant("Louder", 30, em=9000), 1)], STEALTH)
    assert pick.reason == STOCK_IS_BEST


def test_stealth_guns_still_go_by_dps():
    quiet_weak = _gun("Quiet", 300, em=10)
    loud_strong = _gun("Loud", 900, em=5000)
    pick = pick_for_slot(_group("Guns", OMNISKY), [_sold(quiet_weak, 1), _sold(loud_strong, 1)], STEALTH)
    assert pick.part["name"] == "Loud"


def test_stealth_missile_racks_with_no_emission_fall_back_to_the_key_stat():
    # An empty rack slot: one with a stock rack keeps it.
    pick = pick_for_slot(_group("Missile Racks"), [_sold(_rack("MSD-423", 2, 4), 1)], STEALTH)
    assert pick.part["name"] == "MSD-423"


# -- Tank: shields by HP, other components by their own durability ----------------------------

def test_tank_shields_go_by_shield_hp_not_component_health():
    sturdy_weak = _shield("Sturdy", 1500, health=900)
    strong = _shield("Strong", 3000, health=100)
    pick = pick_for_slot(_group("Shield Generators", BULWARK), [_sold(sturdy_weak, 1), _sold(strong, 1)], TANK)
    assert pick.part["name"] == "Strong"


def test_tank_components_go_by_durability_with_the_key_stat_as_tie_break():
    sturdy = _cooler("Sturdy", 25, health=500)
    strong = _cooler("Strong", 50, health=200)
    ranked = rank_candidates([_sold(strong, 1), _sold(sturdy, 1)], "Coolers", TANK)
    assert ranked[0]["name"] == "Sturdy"
    tie = rank_candidates([_sold(_cooler("A", 25, health=500), 1), _sold(_cooler("B", 40, health=500), 1)], "Coolers", TANK)
    assert tie[0]["name"] == "B"


def test_tank_keeps_a_sturdier_stock_part_over_a_stronger_one():
    pick = pick_for_slot(_group("Radar", ECOUTER), [_sold(_radar("Long Reach", 3000, health=300), 1)], TANK)
    assert pick.reason == STOCK_IS_BEST
    assert pick_for_slot(_group("Radar", ECOUTER), [_sold(_radar("Long Reach", 3000, health=300), 1)], BALANCED).part


def test_tank_guns_still_go_by_dps():
    pick = pick_for_slot(_group("Guns", OMNISKY), [_sold(_gun("Sturdy", 300, health=9000), 1),
                                                    _sold(_gun("Strong", 900, health=10), 1)], TANK)
    assert pick.part["name"] == "Strong"


# -- Budget: key stat per aUEC, only among parts beating stock --------------------------------

def test_budget_picks_the_most_key_stat_per_auec_among_upgrades():
    best = _shield("FR-66", 3300)       # 3300 / 30000 = 0.11 per aUEC
    value = _shield("Palisade", 2400)   # 2400 / 6000  = 0.40 per aUEC
    downgrade = _shield("INK", 1900)    # 1900 / 100   = 19 per aUEC, but weaker than stock
    pick = pick_for_slot(_group("Shield Generators", BULWARK),
                         [_sold(best, 30000), _sold(value, 6000), _sold(downgrade, 100)], BUDGET)
    assert pick.part["name"] == "Palisade"


def test_budget_is_the_best_value_upgrade_not_the_cheapest_one():
    cheapest = _shield("Cheapest", 2200)   # 2200 / 1000 = 2.2 per aUEC
    better_value = _shield("Value", 3300)  # 3300 / 1100 = 3.0 per aUEC
    pick = pick_for_slot(_group("Shield Generators", BULWARK), [_sold(cheapest, 1000), _sold(better_value, 1100)],
                         BUDGET)
    assert pick.part["name"] == "Value"


def test_budget_empty_slot_with_no_priced_part_says_nothing_is_sold_not_nothing_beats_stock():
    pick = pick_for_slot(_group("Guns"), [_sold(_gun("Unpriced", 600), 0)], BUDGET)
    assert pick.reason == NOTHING_SOLD and not pick.keeps_stock and pick.part is None


def test_merit_key_compares_budget_like_balanced():
    # Budget's own ranking is value per aUEC (rank_candidates), but how good a part itself is
    # stays its key stat - never Tank's component health.
    for detail, category in ((BULWARK, "Shield Generators"), (ENDURANCE, "Power Plants"), (OMNISKY, "Guns")):
        assert merit_key(detail, category, BUDGET) == merit_key(detail, category, BALANCED)


def test_budget_needs_a_strict_improvement_on_stock():
    pick = pick_for_slot(_group("Shield Generators", BULWARK), [_sold(_shield("Equal", 2160), 1)], BUDGET)
    assert pick.part is None and pick.reason == NOTHING_BEATS_STOCK and pick.keeps_stock


def test_budget_guns_go_by_dps_per_auec_among_guns_beating_stock_dps():
    cheap_upgrade = _gun("Cheap", 600)    # 0.06 DPS/aUEC
    pricey = _gun("Pricey", 1200)         # 0.024 DPS/aUEC
    cheap_downgrade = _gun("Weak", 500)   # beats nothing
    pick = pick_for_slot(_group("Guns", OMNISKY),
                         [_sold(pricey, 50000), _sold(cheap_upgrade, 10000), _sold(cheap_downgrade, 10)], BUDGET)
    assert pick.part["name"] == "Cheap"


def test_budget_fills_an_empty_slot_with_the_best_value_part():
    pick = pick_for_slot(_group("Guns"), [_sold(_gun("A", 600), 10000), _sold(_gun("B", 300), 1000)], BUDGET)
    assert pick.part["name"] == "B"


def test_budget_will_not_guess_when_the_stock_part_cannot_be_rated():
    upgrade = [_sold(_shield("FR-66", 3300), 100)]
    assert pick_for_slot(_group("Shield Generators", None, stock_unknown=True), upgrade, BUDGET).reason == STOCK_UNKNOWN
    unrated_stock = {"name": "Old Shield", "size": 1}
    assert pick_for_slot(_group("Shield Generators", unrated_stock), upgrade, BUDGET).reason == STOCK_UNKNOWN
    assert rank_candidates(upgrade, "Shield Generators", BUDGET, unrated_stock) == []


def test_budget_value_ties_go_to_the_cheaper_shop():
    a, b = _shield("A", 3000, uuid="a"), _shield("B", 3000, uuid="b")
    ranked = rank_candidates([_sold(a, 3000, distance=50.0), _sold(b, 3000, distance=1.0)], "Shield Generators", BUDGET,
                             BULWARK)
    assert ranked[0]["uuid"] == "b"


def test_value_per_auec_needs_a_price_and_a_stat():
    assert value_per_auec(_sold(_shield("A", 3000), 1500)) == 2.0
    assert value_per_auec({**_shield("A", 3000), "_price_buy": 0}) is None
    assert value_per_auec({"name": "x", "_price_buy": 10}) is None


def test_an_unknown_profile_is_refused():
    with pytest.raises(ValueError):
        pick_for_slot(_group("Radar", ECOUTER), [_sold(_radar("R", 3000), 1)], "Glass Cannon")
    with pytest.raises(ValueError):
        pick_for_slot(_group("Radar", ECOUTER), [], "Glass Cannon")
    with pytest.raises(ValueError):
        merit_key(ECOUTER, "Radar", "Glass Cannon")
    assert PROFILES == ("Balanced", "Stealth", "Tank", "Budget")


# -- Guns keep the ship's own mount ------------------------------------------------------------

NOSE = _port("hardpoint_weapon_class2_nose", "Turret", 4, accepts_guns=True, equipped_uuid=VARIPUCK_S4["uuid"])
LEFT_WING = _port("hardpoint_weapon_gun_class1_left_wing", "Turret", 3, accepts_guns=True, equipped_uuid=VARIPUCK_S3["uuid"])
RIGHT_WING = _port("hardpoint_weapon_gun_class1_right_wing", "Turret", 3, accepts_guns=True,
                   equipped_uuid=VARIPUCK_S3["uuid"])


def test_a_stock_gimbal_is_a_mount_and_a_gun_is_not():
    assert is_gun_mount(VARIPUCK_S3)
    assert not is_gun_mount(OMNISKY)
    assert not is_gun_mount(None)
    assert not is_gun_mount(ENDURANCE)
    # The wiki type decides, whatever the sub_type: a manned or remote turret holds its guns
    # in its own ports too, and a gun is never a mount.
    assert is_gun_mount({"type": "Turret", "sub_type": "MannedTurret"})
    assert not is_gun_mount({"type": "WeaponGun", "sub_type": "GunTurret"})


def test_under_a_stock_gimbal_the_gun_slot_is_the_gimbals_own():
    (gun_port,) = loadout_gun_ports(NOSE, VARIPUCK_S4)
    assert gun_port.name == "hardpoint_weapon_class2_nose/hardpoint_class_2"
    assert gun_port.port_type == "WeaponGun"
    # Same size as the gimbal on live data (S4 gimbal, S4 gun) - not one size down.
    assert (gun_port.size_min, gun_port.size_max) == (4, 4)
    assert "gimbalMount" in gun_port.tags and "AEGS_Avenger_Base" in gun_port.tags
    assert slot_category(gun_port) == "Guns"


def test_the_gun_size_comes_from_the_mounts_ports_whatever_it_is():
    (gun_port,) = loadout_gun_ports(NOSE, _gimbal(4, gun_size=3))
    assert (gun_port.size_min, gun_port.size_max) == (3, 3)


def test_a_fixed_gun_or_empty_hardpoint_is_filled_at_its_own_size():
    x1 = _port("hardpoint_weapon", "WeaponGun", 1, accepts_guns=True, equipped_uuid="42e2ddb1")
    assert loadout_gun_ports(x1, _gun("M3A Cannon", 303.8, size=1)) == [x1]
    assert loadout_gun_ports(LEFT_WING, None) == [LEFT_WING]


def test_a_mount_whose_gun_slot_is_locked_has_nothing_to_fill():
    assert loadout_gun_ports(LEFT_WING, _gimbal(3, gun_editable=False)) == []


def test_a_gun_is_saved_under_the_hardpoints_own_name_unless_the_mount_holds_several():
    (gun_port,) = loadout_gun_ports(NOSE, VARIPUCK_S4)
    assert gun_entry_port_name(NOSE, gun_port, 1) == NOSE.name
    assert gun_entry_port_name(NOSE, NOSE, 1) == NOSE.name
    assert gun_entry_port_name(NOSE, gun_port, 2) == gun_port.name


def test_slot_category_fills_gun_hardpoints_with_guns_and_skips_locked_slots():
    assert slot_category(NOSE) == "Guns"
    pdc = _port("hardpoint_pdc_top_left", "Turret", 2, equipped_uuid="a113eb5a")
    assert slot_category(pdc) == "Turrets"
    assert slot_category(_port("hardpoint_turret_remote_top", "Turret", 3, editable=False)) is None
    assert slot_category(_port("hardpoint_power_plant", "PowerPlant")) == "Power Plants"
    assert slot_category(_port("hardpoint_paint", "Paint")) is None


def test_stock_uuids_reach_the_gun_inside_a_mount_and_a_turrets_gimbal():
    # GET /vehicles/{uuid}'s nested shape: the Titan nose and the Perseus top remote turret.
    raw = [
        {"name": "hardpoint_weapon_class2_nose", "equipped_item_uuid": VARIPUCK_S4["uuid"],
         "ports": [{"name": "hardpoint_class_2", "equipped_item": {"uuid": REVENANT["uuid"]}, "ports": None}]},
        {"name": "hardpoint_turret_remote_top", "equipped_item_uuid": "remote-turret",
         "ports": [{"name": "hardpoint_gimbal_left", "equipped_item_uuid": VARIPUCK_S3["uuid"],
                    "ports": [{"name": "hardpoint_class_2", "equipped_item_uuid": "mantis"}]}]},
        {"name": "hardpoint_empty", "equipped_item": None},
        {"type": "no name"},
        "junk",
    ]
    assert stock_uuids_by_port(raw) == {
        "hardpoint_weapon_class2_nose": VARIPUCK_S4["uuid"],
        "hardpoint_weapon_class2_nose/hardpoint_class_2": REVENANT["uuid"],
        "hardpoint_turret_remote_top": "remote-turret",
        "hardpoint_turret_remote_top/hardpoint_gimbal_left": VARIPUCK_S3["uuid"],
        "hardpoint_turret_remote_top/hardpoint_gimbal_left/hardpoint_class_2": "mantis",
        "hardpoint_empty": "",
    }
    assert stock_uuids_by_port(None) == {}


def test_a_listed_empty_slot_and_an_unlisted_one_are_told_apart():
    # The nose gimbal's row lists its gun slot, empty; the wing's row has no ports at all, so
    # what's in its gun slot isn't known - absent, never "" (which the cog reads as empty).
    raw = [{"name": "nose", "equipped_item_uuid": "gimbal", "ports": [{"name": "gun", "equipped_item": None}]},
           {"name": "wing", "equipped_item_uuid": "gimbal", "ports": None}]
    tree = stock_uuids_by_port(raw)
    assert tree["nose/gun"] == "" and "wing/gun" not in tree


def test_stock_uuid_paths_match_the_gun_slot_names():
    (gun_port,) = loadout_gun_ports(NOSE, VARIPUCK_S4)
    raw = [{"name": NOSE.name, "ports": [{"name": "hardpoint_class_2", "equipped_item_uuid": REVENANT["uuid"]}]}]
    assert stock_uuids_by_port(raw)[gun_port.name] == REVENANT["uuid"]


# -- Grouping identical slots -----------------------------------------------------------------

def _titan_gun_slots():
    slots = []
    for hardpoint, mount, stock in ((NOSE, VARIPUCK_S4, REVENANT), (LEFT_WING, VARIPUCK_S3, OMNISKY),
                                    (RIGHT_WING, VARIPUCK_S3, OMNISKY)):
        ports = loadout_gun_ports(hardpoint, mount)
        for gun_port in ports:
            slots.append(LoadoutSlot(gun_port, "Guns", gun_entry_port_name(hardpoint, gun_port, len(ports)), stock))
    return slots


def test_identical_wing_guns_group_and_the_nose_stays_apart():
    groups = group_slots(_titan_gun_slots())
    assert [g.label for g in groups] == ["S4 Nose Gun", "2x S3 Wing Gun"]
    assert [g.count for g in groups] == [1, 2]
    assert [s.entry_port_name for s in groups[1].slots] == [LEFT_WING.name, RIGHT_WING.name]


def test_same_slots_with_different_stock_stay_apart_but_share_candidates():
    mantis, panther = _gun("Mantis GT-220 Gatling", 853.3), _gun("CF-337 Panther Repeater", 545.6)
    port = _port("hardpoint_gun_nose/hardpoint_class_2", "WeaponGun", 3, accepts_guns=True)
    slots = [LoadoutSlot(port, "Guns", "hardpoint_gun_nose", mantis),
             LoadoutSlot(port, "Guns", "hardpoint_gun_left_wing", panther),
             LoadoutSlot(port, "Guns", "hardpoint_gun_right_wing", panther)]
    groups = group_slots(slots)
    assert [g.count for g in groups] == [1, 2]
    assert groups[0].fit_key == groups[1].fit_key


def test_a_slot_with_unknown_stock_does_not_group_with_known_stock():
    port = _port("hardpoint_shield_generator_left")
    groups = group_slots([LoadoutSlot(port, "Shield Generators", "a", BULWARK),
                          LoadoutSlot(port, "Shield Generators", "b", None, stock_unknown=True),
                          LoadoutSlot(port, "Shield Generators", "c", None)])
    assert len(groups) == 3


def test_slots_with_different_tags_do_not_group():
    a = _port("hardpoint_pdc_top_left", "Turret", 2)
    b = ShipPort("hardpoint_pdc_top_right", "Turret", 2, 2, tags=TITAN_TAGS | {"Other"})
    assert len(group_slots([LoadoutSlot(a, "Turrets", a.name), LoadoutSlot(b, "Turrets", b.name)])) == 2


def test_slots_differing_only_in_required_tags_or_port_type_do_not_group():
    # Each also keys the one-load-per-shape candidate cache: merged, the second slot would be
    # shown the first one's parts.
    plain = _port("hardpoint_gun_left", "WeaponGun", 3)
    reliant_only = _port("hardpoint_gun_right", "WeaponGun", 3, required_tags=frozenset({"MISC_Reliant_Base"}))
    assert len(group_slots([LoadoutSlot(plain, "Guns", plain.name), LoadoutSlot(reliant_only, "Guns", "r")])) == 2
    hardpoint = _port("hardpoint_gun_right", "Turret", 3, accepts_guns=True)
    assert len(group_slots([LoadoutSlot(plain, "Guns", plain.name), LoadoutSlot(hardpoint, "Guns", "h")])) == 2


def test_stock_parts_sharing_a_name_but_not_a_part_do_not_group():
    # The S3 and S4 Revenant Gatling share one name; so can two wiki items of one size.
    port = _port("hardpoint_gun", "WeaponGun", 3)
    s3, s4 = {"name": "Revenant Gatling", "size": 3}, {"name": "Revenant Gatling", "size": 4}
    assert len(group_slots([LoadoutSlot(port, "Guns", "a", s3), LoadoutSlot(port, "Guns", "b", s4)])) == 2
    one, other = {**REVENANT, "uuid": "one"}, {**REVENANT, "uuid": "other"}
    assert len(group_slots([LoadoutSlot(port, "Guns", "a", one), LoadoutSlot(port, "Guns", "b", other)])) == 2


@pytest.mark.parametrize("names,category,size,label", [
    (["hardpoint_cooler_left", "hardpoint_cooler_right"], "Coolers", 1, "2x S1 Cooler"),
    (["hardpoint_shield_generator_left", "hardpoint_shield_generator_right"], "Shield Generators", 1,
     "2x S1 Shield Generator"),
    (["hardpoint_weapon_missilerack_right_wing", "hardpoint_weapon_missilerack_left_wing"], "Missile Racks", 3,
     "2x S3 Wing Missile Rack"),
    (["hardpoint_power_plant"], "Power Plants", 1, "S1 Power Plant"),
    (["hardpoint_turret_remote_top/hardpoint_gimbal_left", "hardpoint_turret_remote_top/hardpoint_gimbal_right"], "Guns", 3,
     "2x S3 Turret Remote Top · Gimbal Gun"),
    (["hardpoint_turret_remote_top/hardpoint_gimbal_left", "hardpoint_turret_remote_bottom/hardpoint_gimbal_left"], "Guns",
     3, "2x S3 Turret Remote · Gimbal Left Gun"),
    # A whole middle segment differing leaves one separator, not two.
    (["hardpoint_turret_top/hardpoint_left/hardpoint_gun", "hardpoint_turret_top/hardpoint_right/hardpoint_gun"], "Guns", 3,
     "2x S3 Turret Top · Gun"),
    (["alpha", "beta"], "Turrets", 2, "2x S2 Gun Mounts"),
])
def test_group_labels(names, category, size, label):
    port = _port("x", size=size)
    assert SlotGroup(tuple(LoadoutSlot(port, category, n) for n in names)).label == label


def test_a_size_range_slot_label():
    port = ShipPort("hardpoint_weapon_gun", "WeaponGun", 1, 3)
    assert SlotGroup((LoadoutSlot(port, "Guns", port.name),)).label == "S1-3 Weapon Gun"


# -- Purchases and total cost -----------------------------------------------------------------

def test_purchases_are_per_slot_and_skip_kept_stock():
    wings = group_slots(_titan_gun_slots())[1]
    buy = pick_for_slot(wings, [_sold(_gun("Mantis", 853.3), 12000)], BALANCED)
    keep = pick_for_slot(_group("Shield Generators", BULWARK), [_sold(_shield("INK", 1900), 100)], BALANCED)
    empty = pick_for_slot(_group("Radar"), [], BALANCED)
    picks = [buy, keep, empty]
    assert [(slot.entry_port_name, part["name"]) for slot, part in purchases(picks)] == [
        (LEFT_WING.name, "Mantis"), (RIGHT_WING.name, "Mantis")]
    assert total_cost(picks) == 24000
    assert not empty.keeps_stock and empty.reason == NOTHING_SOLD


def test_total_cost_of_nothing_is_zero():
    assert total_cost([]) == 0


# -- Power and cooling: warn only ---------------------------------------------------------------

def _keep(category, stock, count=1):
    return pick_for_slot(_group(category, stock, count=count), [], BALANCED)


def _titan_stock_picks():
    return [_keep("Power Plants", ENDURANCE), _keep("Coolers", BRACER, 2), _keep("Shield Generators", BULWARK, 2),
            _keep("Quantum Drives", EXPEDITION), _keep("Radar", ECOUTER), _keep("Guns", REVENANT),
            _keep("Guns", OMNISKY, 2), _keep("Missile Racks", MSD_322, 2)]


def test_titan_stock_power_total_is_the_plants_output():
    assert power_total(_titan_stock_picks()) == PowerTotal(15, 1, 0)
    assert power_total(_titan_stock_picks()).text() == "⚡ 15 power pips"


def test_the_parts_maximum_draw_is_never_compared():
    """The stock Titan's parts could take 22.1 pips at full power against the plant's 15 -
    that warned on nearly every stock ship. No ship is meant to run everything at max, so the
    line is the plant's total, whatever the parts could draw."""
    picks = _titan_stock_picks()
    greedy = {**BULWARK, "resource_network": {"usage": {"power": {"min": 0, "max": 500}}}}
    picks[2] = _keep("Shield Generators", greedy, 2)
    assert power_total(picks) == power_total(_titan_stock_picks())
    assert "22" not in power_total(_titan_stock_picks()).text()


def test_a_bought_plant_counts_instead_of_the_stock_one():
    picks = _titan_stock_picks()
    picks[0] = pick_for_slot(picks[0].group, [_sold(_plant("Big", 30), 100)], BALANCED)
    assert picks[0].part is not None
    assert power_total(picks).pips == 30


def test_several_plants_add_up_and_only_plants_count():
    total = power_total([_keep("Power Plants", _plant("Big", 15), 2), _keep("Coolers", BRACER, 2),
                         _keep("Shield Generators", BULWARK)])
    assert total == PowerTotal(30, 2, 0)
    assert total.text() == "⚡ 30 power pips"


def test_power_generation_falls_back_to_the_resource_network_figure():
    plant = _plant("P", 20)
    plant["power_plant"] = {"power_output": None}
    assert power_total([_keep("Power Plants", plant)]).pips == 20


def test_a_plant_with_no_output_figure_makes_the_total_a_lower_bound_not_a_guess():
    no_figure = {**_plant("P", 4), "power_plant": {}, "resource_network": {}}
    total = power_total([_keep("Power Plants", ENDURANCE), _keep("Power Plants", no_figure)])
    assert total == PowerTotal(15, 2, 1)
    assert total.text() == "⚡ at least 15 power pips: no output figure for 1 of the 2 power plants"
    assert power_total([_keep("Power Plants", no_figure)]).text() == (
        "⚡ power pips unknown: no output figure for the power plant")


def test_a_stock_plant_that_failed_to_load_is_unknown_not_zero():
    picks = [pick_for_slot(_group("Power Plants", None, stock_unknown=True), [], BALANCED)]
    assert power_total(picks) == PowerTotal(0, 1, 1)


def test_no_power_plant_means_no_power_line():
    assert power_total([_keep("Coolers", BRACER), _keep("Radar", ECOUTER)]).text() is None
    assert power_total([_keep("Power Plants", None)]).text() is None, "an empty slot nothing is bought for"


# -- What each line shows ------------------------------------------------------------------------

def test_profile_stat_is_the_figure_the_profile_chose_by():
    assert profile_stat(BULWARK, "Shield Generators", BALANCED) == ("shield HP", 2160)
    assert profile_stat(BULWARK, "Shield Generators", STEALTH) == ("EM", 1490)
    assert profile_stat(BRACER, "Coolers", STEALTH) == ("EM", 1490)
    assert profile_stat({**BRACER, "emission": {"em_max": None, "ir": 7260}}, "Coolers", STEALTH) == ("IR", 7260)
    assert profile_stat(BULWARK, "Shield Generators", TANK) == ("shield HP", 2160)
    assert profile_stat(ENDURANCE, "Power Plants", TANK) == ("component HP", 270)
    assert profile_stat(REVENANT, "Guns", STEALTH) == ("DPS", 1266)
    assert profile_stat(REVENANT, "Guns", TANK) == ("DPS", 1266)
    assert profile_stat(MSD_322, "Missile Racks", STEALTH) == ("missile size", 202)
    assert profile_stat(BULWARK, "Shield Generators", BUDGET) == ("shield HP", 2160)
    assert profile_stat(None, "Radar", BALANCED) is None


@pytest.mark.parametrize("label,value,text", [
    ("DPS", 1266.4, "1,266 DPS"),
    ("shield HP", 2160, "2,160 shield HP"),
    ("power generation", 15, "15 power pips"),
    ("cooling", 34, "34 cooling segments"),
    ("aim assist range", 1105, "1,105 m aim assist"),
    ("quantum speed", 189309100, "189.3 Mm/s"),
    ("gun size held", 301, "holds 1x S3"),
    ("missile size", 202, "2x S2 missiles"),
    ("EM", 7430, "EM 7,430"),
    ("IR", 7260, "IR 7,260"),
    ("component HP", 270, "270 component HP"),
])
def test_stat_text(label, value, text):
    assert stat_text(label, value) == text


def test_a_gun_shows_its_projectile_speed_beside_dps_and_alpha():
    """The owner's call (2026-10-07): velocity matters for landing hits. Shown, compared with
    the stock gun's, never ranked by."""
    fast = {**_gun("Fast", 900, alpha=60), "vehicle_weapon": {"damage": {"burst": 900, "alpha_total": 60},
                                                             "ammunition": {"speed": 1332}}}
    slow = {**_gun("Slow", 700, alpha=40), "vehicle_weapon": {"damage": {"burst": 700, "alpha_total": 40},
                                                             "ammunition": {"speed": 1184}}}
    assert shown_stat(fast, "Guns", BALANCED) == ("DPS / alpha / speed", "900 DPS / 60 alpha / 1,332 m/s")
    assert stat_change(fast, slow, "Guns", BALANCED) == "`700 → 900` DPS · `40 → 60` alpha · `1,184 → 1,332` m/s"
    # A stock gun with no speed on the wiki: the rest is still compared, the speed shown alone.
    assert stat_change(fast, _gun("Old", 700, alpha=40), "Guns", BALANCED) == "`700 → 900` DPS · `40 → 60` alpha · 1,332 m/s"
    assert merit_key(fast, "Guns", BALANCED) == merit_key({**fast, "vehicle_weapon": {
        "damage": {"burst": 900, "alpha_total": 60}, "ammunition": {"speed": 1}}}, "Guns", BALANCED), "never ranked by"


def test_stat_change():
    assert stat_change(_gun("Mantis", 853.3), OMNISKY, "Guns", BALANCED) == "`547 → 853` DPS"
    assert stat_change(_plant("Quiet", 12, em=3000), ENDURANCE, "Power Plants", STEALTH) == "EM `7,430 → 3,000`"
    assert stat_change(_gun("Mantis", 853.3), None, "Guns", BALANCED) == "853 DPS"
    assert stat_change({"name": "x"}, OMNISKY, "Guns", BALANCED) == ""
    # A stock part missing the figure the pick was chosen by isn't compared on a different one.
    no_em_stock = {**BULWARK, "emission": None}
    assert stat_change(_shield("Quiet", 2000, em=900), no_em_stock, "Shield Generators", STEALTH) == "EM 900"


def test_a_stealth_cooler_shows_em_and_ir_together():
    """EM decides, but real coolers tie on it often and then IR decides: showing EM alone
    would read 'EM 1,490 (was EM 1,490 stock)' for a real upgrade."""
    assert shown_stat(BRACER, "Coolers", STEALTH) == ("EM / IR", "EM 1,490 / IR 7,260")
    assert stat_change(_cooler("Ultra-Flow", 34, ir=7130, em=1490), BRACER, "Coolers", STEALTH) == (
        "EM 1,490 · IR `7,260 → 7,130`")
    # Only a Stealth cooler: other profiles, and other Stealth components, show the one figure.
    assert shown_stat(BRACER, "Coolers", BALANCED) == ("cooling", "34 cooling segments")
    assert shown_stat(BULWARK, "Shield Generators", STEALTH) == ("EM", "EM 1,490")
    # A cooler missing either figure shows the one it has, and isn't compared with a stock
    # part shown by both.
    no_em = {**BRACER, "emission": {"em_max": None, "ir": 7130}}
    assert shown_stat(no_em, "Coolers", STEALTH) == ("IR", "IR 7,130")
    assert stat_change(no_em, BRACER, "Coolers", STEALTH) == "IR 7,130"


def test_kept_slots_with_the_same_part_figure_and_reason_are_one_line():
    """The Polaris's four torpedo racks (each its own slot group, left/right and upper/lower)
    read as one line, counted, in the words every slot's name shares."""
    names = ["hardpoint_torpedo_right_upper", "hardpoint_torpedo_left_lower", "hardpoint_torpedo_right_lower"]
    picks = [pick_for_slot(_group("Missile Racks", MSD_442, port=_port(name, "MissileLauncher", 4), names=[name]), [],
                           BALANCED) for name in names]
    other = pick_for_slot(_group("Missile Racks", MSD_423, port=_port("hardpoint_rack", "MissileLauncher", 4),
                                 names=["hardpoint_rack"]), [], BALANCED)
    (section,) = kept_sections([*picks, other], BALANCED)
    assert section.splitlines() == [
        "**Missile racks** · more missiles or bigger ones is your call",
        "-# 3x S4 Torpedo · MSD-442 Missile Rack · 4x S2 missiles",
        "-# S4 Rack · MSD-423 Missile Rack · 2x S3 missiles",
    ]


def test_an_override_reason_heads_its_own_section():
    pick = _keep("Coolers", BRACER, 2)
    sections = kept_sections([pick, _keep("Radar", ECOUTER)], BALANCED, ["the wiki didn't respond", None])
    assert sections[0].startswith("**The wiki didn't respond**\n-# 2x S1 Slot · Bracer")
    assert sections[1].startswith("**No shop sells a part that fits**\n-# S1 Slot 0 · Ecouter")


def test_keeping_a_stealth_cooler_shows_both_figures():
    assert "-# 2x S1 Slot · Bracer · EM 1,490 / IR 7,260" in kept_sections([_keep("Coolers", BRACER, 2)], STEALTH)[0]


def test_summary_line_counts_one_part_in_the_singular():
    buy = pick_for_slot(_group("Shield Generators", BULWARK), [_sold(_shield("FR-66", 3300), 30000)], BALANCED)
    assert summary_line([buy]) == "**30,000 aUEC** for 1 part"
    two = pick_for_slot(_group("Shield Generators", BULWARK, count=2), [_sold(_shield("FR-66", 3300), 30000)], BALANCED)
    assert summary_line([two]) == "**60,000 aUEC** for 2 parts"
    assert summary_line([_keep("Power Plants", ENDURANCE)]) == (
        "**Nothing to buy**: every slot keeps what it has · ⚡ 15 power pips")


def test_paginate_loadout_keeps_whole_entries_within_the_budget_and_the_header_on_every_page():
    head = "## Ship"
    one = paginate_loadout(head, ["a" * 10], ["b" * 10], 1000)
    assert one == [(head, "### Upgrades\n" + "a" * 10, "### Keeping stock\n" + "b" * 10)]
    assert paginate_loadout(head, [], [], 1000) == [(head,)], "always one page"
    pages = paginate_loadout(head, ["a" * 40, "c" * 40], ["b" * 40], 80)
    assert pages == [(head, "### Upgrades\n" + "a" * 40), (head, "### Upgrades\n" + "c" * 40),
                     (head, "### Keeping stock\n" + "b" * 40)]
    assert all(len("\n\n".join(page)) <= 80 for page in pages)
    # An entry too long for any page gets one of its own rather than being cut.
    assert paginate_loadout(head, ["x" * 500], [], 80) == [(head, "### Upgrades\n" + "x" * 500)]


def test_profile_blurbs_name_the_stat_not_the_code_term():
    assert all("key stat" not in blurb for blurb in PROFILE_BLURBS.values())
    assert all(len(f"{profile} - {PROFILE_BLURBS[profile]}") <= 100 for profile in PROFILES)
