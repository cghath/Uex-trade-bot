"""Pure logic for /blueprint-search material: - what an ore or mineral is used to craft. No
Discord, no I/O: the cog syncs every blueprint's recipe from the Star Citizen Wiki API
(WikiApiClient.get_blueprints) and asks the database for a material's blueprints; this sorts
them into what the reply shows.

The owner's decisions (2026-10-07, from real-data mockups of Aslarite, Hadanite, Tungsten and
Quantainium):
- An option on /blueprint-search, not a command of its own.
- Only blueprints a player can get: one a contract awards (the snapshot's pool) or one unlocked
  by default. The wiki lists 1,606 on 4.10.1, of which 917 have neither; they're left out.
- Categories in this order: armor, guns, ship parts (anything else last), each a button.
- Armor folded into sets, one per colour ("Antium Jet" is its own set), picked from a menu; the
  set's pieces are then buttons.
- Just what the material is used for: no amounts, no stats. A ship part says what it is
  ("Agni · quantum drive"), since its name alone doesn't.
- Picking a blueprint opens its usual /blueprint-search reply.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Iterable

ARMOR, GUNS, SHIP_PARTS, OTHER = "Armor", "Guns", "Ship parts", "Other"
CATEGORIES = (ARMOR, GUNS, SHIP_PARTS, OTHER)
# Blueprint lines on one page: one menu holds 25, and 20 keeps a page easy to scan.
PAGE_SIZE = 20

# What a ship part is, in the Ship Parts Finder's order.
SHIP_PART_TAGS = {
    "WeaponGun": "ship gun", "Shield": "shield", "PowerPlant": "power plant", "Cooler": "cooler",
    "QuantumDrive": "quantum drive", "Radar": "radar", "WeaponMining": "mining laser",
    "MiningModifier": "mining module", "SalvageModifier": "salvage module", "Container": "ore pod",
    "DockingCollar": "fuel collar",
}
_SHIP_PART_ORDER = list(SHIP_PART_TAGS.values())
# An armor piece by its type, in the order a set lists them.
ARMOR_PIECES = {
    "Char_Armor_Helmet": "helmet", "Char_Armor_Torso": "core", "Char_Armor_Arms": "arms",
    "Char_Armor_Legs": "legs", "Char_Armor_Undersuit": "undersuit", "Char_Armor_Backpack": "backpack",
}
_PIECE_ORDER = list(ARMOR_PIECES.values())
# The words a piece's name adds to its set's: "ADP-mk4 Helmet Woodland" is set "ADP-mk4 Woodland".
_PIECE_WORDS = re.compile(r"\b(Helmet|Core|Arms|Legs|Backpack|Armor|Suit)\b")


@dataclass(frozen=True)
class BlueprintRecipe:
    """One blueprint from the wiki's listing: what it makes and the materials it takes."""
    uuid: str
    name: str
    output_type: str
    is_default: bool
    materials: tuple[str, ...]


@dataclass(frozen=True)
class MaterialUse:
    """A blueprint a material is used in."""
    uuid: str
    name: str
    output_type: str
    is_default: bool = False


@dataclass(frozen=True)
class ArmorSet:
    """Armor pieces sharing a name ("Aves": helmet, core, arms, legs). Clothing and anything
    else that isn't a piece is a set of one, with no piece name."""
    name: str
    pieces: tuple[tuple[str, MaterialUse], ...]


def parse_blueprint_rows(rows: Iterable[Any]) -> list[BlueprintRecipe]:
    """Usable blueprints from the wiki API's blueprint listing, one per uuid; a row without a uuid
    or a name is skipped. Material names are the ingredients' own ("Aslarite", "Hadanite")."""
    seen: set[str] = set()
    out: list[BlueprintRecipe] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        uuid, name = row.get("uuid"), row.get("output_name")
        if not isinstance(uuid, str) or not isinstance(name, str) or not name.strip():
            continue
        uuid = uuid.strip().lower()  # the contracts store their blueprint ids lowercased; compared as text
        if not uuid or uuid in seen:
            continue
        output = row.get("output") if isinstance(row.get("output"), dict) else {}
        ingredients = row.get("ingredients") if isinstance(row.get("ingredients"), list) else []
        materials = tuple(dict.fromkeys(
            " ".join(i["name"].split()) for i in ingredients
            if isinstance(i, dict) and isinstance(i.get("name"), str) and i["name"].strip()))
        seen.add(uuid)
        out.append(BlueprintRecipe(uuid, " ".join(name.split()), str(output.get("type") or ""),
                                   bool(row.get("is_available_by_default")), materials))
    return out


def category_of(output_type: str) -> str:
    if output_type.startswith("Char_"):
        return ARMOR
    if output_type in ("WeaponPersonal", "WeaponAttachment"):
        return GUNS
    if output_type in SHIP_PART_TAGS:
        return SHIP_PARTS
    return OTHER


def by_category(uses: Iterable[MaterialUse]) -> dict[str, list[MaterialUse]]:
    """The material's blueprints per category, in CATEGORIES order (only the ones it has), each
    by name - a ship part by what it is first."""
    grouped: dict[str, list[MaterialUse]] = {}
    for use in uses:
        grouped.setdefault(category_of(use.output_type), []).append(use)

    def ship_part_key(use: MaterialUse) -> tuple:
        tag = SHIP_PART_TAGS.get(use.output_type)
        return (_SHIP_PART_ORDER.index(tag) if tag in _SHIP_PART_ORDER else len(_SHIP_PART_ORDER), use.name.lower())

    return {category: sorted(grouped[category], key=ship_part_key if category == SHIP_PARTS
                             else lambda u: u.name.lower())
            for category in CATEGORIES if grouped.get(category)}


def armor_sets(uses: Iterable[MaterialUse]) -> list[ArmorSet]:
    """Armor pieces folded into their sets, by set name; a set's pieces in helmet, core, arms,
    legs, undersuit, backpack order. Each colour is its own set (the owner's call)."""
    sets: dict[str, list[tuple[str, MaterialUse]]] = {}
    for use in uses:
        piece = ARMOR_PIECES.get(use.output_type)
        if piece is None:
            sets.setdefault(use.name, []).append(("", use))
            continue
        name = " ".join(_PIECE_WORDS.sub(" ", use.name).split()) or use.name
        sets.setdefault(name, []).append((piece, use))
    return [ArmorSet(name, tuple(sorted(pieces, key=lambda p: (_PIECE_ORDER.index(p[0]) if p[0] in _PIECE_ORDER
                                                              else len(_PIECE_ORDER), p[1].name.lower()))))
            for name, pieces in sorted(sets.items(), key=lambda item: item[0].lower())]


def entry_line(entry: ArmorSet | MaterialUse) -> str:
    """'**Aves** · helmet, core, arms, legs', '**Agni** · quantum drive', '**P4-AR Rifle**'.
    A blueprint unlocked by default says so: it has no contract to show."""
    if isinstance(entry, ArmorSet):
        pieces = [piece for piece, _ in entry.pieces if piece]
        default = all(use.is_default for _, use in entry.pieces)
        return f"**{entry.name}**" + (f" · {', '.join(pieces)}" if pieces else "") + (
            " · unlocked by default" if default else "")
    tag = SHIP_PART_TAGS.get(entry.output_type)
    return f"**{entry.name}**" + (f" · {tag}" if tag else "") + (" · unlocked by default" if entry.is_default else "")


def category_entries(category: str, uses: list[MaterialUse]) -> list[ArmorSet | MaterialUse]:
    """What one category lists: armor as sets, everything else blueprint by blueprint."""
    return armor_sets(uses) if category == ARMOR else list(uses)


def pages(entries: list, size: int = PAGE_SIZE) -> list[list]:
    """Entries in pages of `size`; always at least one (possibly empty) page."""
    return [entries[i:i + size] for i in range(0, len(entries), size)] or [[]]


def header(material: str, grouped: dict[str, list[MaterialUse]]) -> str:
    total = sum(len(uses) for uses in grouped.values())
    counts = " · ".join(f"{category} {len(uses)}" for category, uses in grouped.items())
    return (f"## {material}\nUsed in **{total} blueprint{'s' if total != 1 else ''}** you can get · {counts}\n"
            "-# Pick a blueprint to see where to get it and what it takes")


def category_title(category: str, uses: list[MaterialUse], entries: list) -> str:
    if category == ARMOR:
        return f"### Armor · {len(uses)} blueprint{'s' if len(uses) != 1 else ''} in {len(entries)} set{'s' if len(entries) != 1 else ''}"
    return f"### {category} · {len(uses)}"
