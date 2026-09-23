"""Pure bridging logic for the in-design Ship Parts Finder (bot/uex/ship_parts.py)."""
from __future__ import annotations

from bot.uex.ship_parts import (
    PORT_TYPE_TO_UEX_CATEGORY,
    ShipPort,
    candidate_items_for_port,
    filter_to_sold_items,
    group_ports_by_category,
    parse_ports,
    sold_item_ids,
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

def test_candidate_items_for_port_filters_by_category_and_size_range():
    port = ShipPort(name="x", port_type="PowerPlant", size_min=2, size_max=3)
    catalog = [
        _catalog_row(1, "Power Plants", "1"),  # too small
        _catalog_row(2, "Power Plants", "2"),  # fits
        _catalog_row(3, "Power Plants", "3"),  # fits
        _catalog_row(4, "Power Plants", "4"),  # too big
        _catalog_row(5, "Coolers", "2"),  # wrong category
    ]
    result = candidate_items_for_port(catalog, port)
    assert {row["id"] for row in result} == {2, 3}


def test_candidate_items_for_port_skips_non_numeric_or_missing_size():
    port = ShipPort(name="x", port_type="PowerPlant", size_min=1, size_max=4)
    catalog = [
        _catalog_row(1, "Power Plants", ""),
        _catalog_row(2, "Power Plants", None),
        _catalog_row(3, "Power Plants", "not-a-number"),
        _catalog_row(4, "Power Plants", "2"),
    ]
    result = candidate_items_for_port(catalog, port)
    assert {row["id"] for row in result} == {4}


def test_candidate_items_for_port_returns_empty_for_an_unmapped_port_type():
    port = ShipPort(name="x", port_type="Armor", size_min=1, size_max=1)
    catalog = [_catalog_row(1, "Miscellaneous", "1")]
    assert candidate_items_for_port(catalog, port) == []


# -- sold_item_ids / filter_to_sold_items --------------------------------------------------

def test_sold_item_ids_collects_distinct_valid_ids():
    rows = [{"id_item": 1}, {"id_item": "2"}, {"id_item": 1}, {"id_item": None}, {}]
    assert sold_item_ids(rows) == {1, 2}


def test_filter_to_sold_items_keeps_only_sold_candidates():
    candidates = [_catalog_row(1, "Power Plants", "1"), _catalog_row(2, "Power Plants", "1")]
    assert filter_to_sold_items(candidates, {2}) == [candidates[1]]


def test_filter_to_sold_items_skips_a_candidate_with_a_malformed_id():
    candidates = [{"id": "not-an-int", "category": "Power Plants"}]
    assert filter_to_sold_items(candidates, {1}) == []
