"""/blueprint-search material: - what an ore or mineral crafts.

Guarantees under test:
  1. Sorting (bot/uex/blueprint_materials.py) - categories in armor, guns, ship parts order; armor folded
     into sets by name, each colour its own set; a ship part says what it is; a default blueprint says so.
  2. Storage - the recipes are replaced whole or not at all, and only blueprints a player can get (a
     contract awards it, or it's unlocked by default) are ever listed.
  3. Sync - the recipes sync beside the contracts but apart from them: a failure in one never blocks the
     other, and a crawl of another game version or a shrunken one never replaces good recipes.
  4. The reply - one of blueprint:/material: is required; a material lists its blueprints, a category at a
     time, armor sets opening into piece buttons; picking one sends its usual reply as a new message; only
     whoever ran it can click; every page fits Discord's layout limits; a refused layout goes as text.
"""
from __future__ import annotations

import asyncio
from datetime import datetime
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock

import httpx
import pytest

from bot.cogs.blueprints import Blueprints, material_autocomplete
from bot.discord_ui import LAYOUT_TEXT_LIMIT
from bot.material_pages import (
    MaterialUsesView,
    _CategoryButton,
    _EntrySelect,
    _PageButton,
    _PieceButton,
    text_listing,
)
from bot.uex.blueprint_materials import (
    ARMOR,
    GUNS,
    OTHER,
    SHIP_PARTS,
    ArmorSet,
    BlueprintRecipe,
    MaterialUse,
    armor_sets,
    by_category,
    category_of,
    entry_line,
    header,
    pages,
    parse_blueprint_rows,
)
from tests.test_blueprints_cog import FakeFollowup, FakeWiki, _interaction, _is_layout, _layout_text, _make

# Real names and blueprint ids from the contract fixture, so the ones in a contract pool really are.
BATTERY = ("743efa96-8a73-40a7-ae6a-0890f34a3731", "Arclight Pistol Battery (30 cap)", "WeaponAttachment")
VENTURE = [("dabddb4f-e559-4991-ba80-95a3a70a1fef", "Venture Arms", "Char_Armor_Arms"),
           ("1b1c4e5e-2a8b-4180-9857-1fe1b3322093", "Venture Core", "Char_Armor_Torso"),
           ("f1b95cd0-5dde-4b32-9efb-dd87f2aa5567", "Venture Legs", "Char_Armor_Legs")]
HORIZON = ("23451667-daed-476c-9eb8-b25e174929d6", "Horizon Helmet", "Char_Armor_Helmet")
ARBOR = ("e8e41bb2-0fd2-47a3-99ea-5dbe2abe4b92", "Arbor MH2 Mining Laser", "WeaponMining")
P4AR = ("00000000-0000-4000-8000-0000000000a4", "P4-AR Rifle", "WeaponPersonal")        # unlocked by default
GHOST = ("00000000-0000-4000-8000-00000000dead", "Ghost Rifle", "WeaponPersonal")       # no contract, not default


def _listing_row(uuid, name, kind, materials, *, default=False):
    return {"uuid": uuid, "output_name": name, "output": {"type": kind}, "is_available_by_default": default,
            "ingredients": [{"name": m, "kind": "resource", "quantity_scu": 0.1} for m in materials]}


LISTING = [
    _listing_row(*BATTERY, ["Tungsten"]),
    *(_listing_row(*piece, ["Tungsten", "Aslarite"]) for piece in VENTURE),
    _listing_row(*HORIZON, ["Tungsten"]),
    _listing_row(*ARBOR, ["Tungsten", "Hadanite"]),
    _listing_row(*P4AR, ["Tungsten"], default=True),
    _listing_row(*GHOST, ["Tungsten", "Quantainium"]),
]


class RecipeWiki(FakeWiki):
    """The contract fake plus the wiki's blueprint listing (GET /blueprints, paged)."""

    def __init__(self, listing=None, **kwargs):
        super().__init__(**kwargs)
        self.listing = [dict(r) for r in (listing if listing is not None else LISTING)]
        self.listing_status = 200
        self.listing_version = None  # None: the version the probe reports

    def handler(self, request: httpx.Request) -> httpx.Response:
        if not request.url.path.endswith("/blueprints"):
            return super().handler(request)
        if self.listing_status != 200:
            self.log.append("listing-error")
            return httpx.Response(self.listing_status)
        size, page = int(request.url.params["page[size]"]), int(request.url.params["page[number]"])
        self.log.append(f"listing:{page}")
        rows = [dict(r, game_version=self.listing_version or self.version) for r in self.listing]
        last = max(1, -(-len(rows) // size))
        return httpx.Response(200, json={"data": rows[(page - 1) * size: page * size],
                                         "meta": {"current_page": page, "last_page": last, "total": len(rows)}})


def _use(name, kind="WeaponPersonal", *, uuid=None, default=False):
    return MaterialUse(uuid or name, name, kind, default)


# -- 1. sorting --------------------------------------------------------------------------------


def test_listing_rows_parse_one_recipe_per_blueprint_and_skip_what_is_not_one():
    rows = [
        _listing_row("a", "  Aves   Helmet ", "Char_Armor_Helmet", ["Tungsten", " Tungsten ", "Iron"]),
        _listing_row("a", "Aves Helmet (again)", "Char_Armor_Helmet", ["Gold"]),
        {"uuid": "b", "output_name": "", "output": {"type": "Shield"}},
        {"output_name": "No id"},
        "not a row",
        {"uuid": "c", "output_name": "Bare", "output": None, "ingredients": None},
    ]
    assert parse_blueprint_rows(rows) == [
        BlueprintRecipe("a", "Aves Helmet", "Char_Armor_Helmet", False, ("Tungsten", "Iron")),
        BlueprintRecipe("c", "Bare", "", False, ()),
    ]


def test_categories_come_in_armor_guns_ship_parts_order_and_a_ship_part_by_what_it_is():
    uses = [_use("Zeta Cooler", "Cooler"), _use("Agni", "QuantumDrive"), _use("Aves Helmet", "Char_Armor_Helmet"),
            _use("Mug", "Misc"), _use("Alpha Shield", "Shield"), _use("Killshot", "WeaponPersonal"),
            _use("Scope", "WeaponAttachment"), _use("Arbor", "WeaponMining")]
    grouped = by_category(uses)
    assert list(grouped) == [ARMOR, GUNS, SHIP_PARTS, OTHER]
    assert [u.name for u in grouped[SHIP_PARTS]] == ["Alpha Shield", "Zeta Cooler", "Agni", "Arbor"]
    assert [u.name for u in grouped[GUNS]] == ["Killshot", "Scope"]
    assert category_of("Char_Clothing_Hat") == ARMOR and category_of("Vehicle") == OTHER
    assert entry_line(grouped[SHIP_PARTS][2]) == "**Agni** · quantum drive"
    assert entry_line(_use("P4-AR Rifle", default=True)) == "**P4-AR Rifle** · unlocked by default"


def test_armor_folds_into_sets_by_name_each_colour_its_own():
    uses = [_use("ADP-mk4 Legs Woodland", "Char_Armor_Legs"), _use("ADP-mk4 Helmet Woodland", "Char_Armor_Helmet"),
            _use("ADP-mk4 Core Woodland", "Char_Armor_Torso"), _use("ADP-mk4 Arms", "Char_Armor_Arms"),
            _use("Field Recon Suit Helmet", "Char_Armor_Helmet", default=True),
            _use("Field Recon Suit Core", "Char_Armor_Torso", default=True), _use("Explorer Hat", "Char_Clothing_Hat")]
    sets = armor_sets(uses)
    assert [(s.name, [piece for piece, _ in s.pieces]) for s in sets] == [
        ("ADP-mk4", ["arms"]),
        ("ADP-mk4 Woodland", ["helmet", "core", "legs"]),
        ("Explorer Hat", [""]),
        ("Field Recon", ["helmet", "core"]),
    ]
    assert entry_line(sets[1]) == "**ADP-mk4 Woodland** · helmet, core, legs"
    assert entry_line(sets[2]) == "**Explorer Hat**"
    assert entry_line(sets[3]) == "**Field Recon** · helmet, core · unlocked by default"


def test_pages_and_the_header_count_what_is_listed():
    assert pages(list(range(45))) == [list(range(20)), list(range(20, 40)), list(range(40, 45))]
    assert pages([]) == [[]]
    grouped = by_category([_use("Agni", "QuantumDrive")])
    assert header("Quantainium", grouped).split("\n")[:2] == [
        "## Quantainium", "Used in **1 blueprint** you can get · Ship parts 1"]


# -- 2. storage --------------------------------------------------------------------------------


def _recipes(listing=LISTING):
    return parse_blueprint_rows(listing)


def test_only_blueprints_a_player_can_get_are_listed_matched_ignoring_case(tmp_path):
    async def run():
        cog, db = await _make(tmp_path, RecipeWiki(), seed=True)
        await db.replace_blueprint_recipes(_recipes(), game_version="v1")
        return await db.get_material_uses("tUNGSTEN"), await db.get_material_names(), await db.get_material_uses("Quantainium")

    uses, names, ghost_only = asyncio.run(run())
    assert {u.name for u in uses} == {BATTERY[1], *(p[1] for p in VENTURE), HORIZON[1], ARBOR[1], P4AR[1]}
    assert GHOST[1] not in {u.name for u in uses}, "no contract and not default: left out"
    assert next(u for u in uses if u.name == P4AR[1]).is_default
    assert names == [("Tungsten", 7), ("Aslarite", 3), ("Hadanite", 1)], "Quantainium's only blueprint is unobtainable"
    assert ghost_only == []


def test_recipes_are_replaced_whole_or_not_at_all(tmp_path):
    async def run():
        _cog, db = await _make(tmp_path, RecipeWiki(), seed=True)
        with pytest.raises(ValueError):
            await db.replace_blueprint_recipes([], game_version="v1")
        empty = await db.get_blueprint_recipe_state()
        await db.replace_blueprint_recipes(_recipes(), game_version="v1", synced_at=datetime(2026, 10, 7, 12, 0, 0))
        first = (await db.get_blueprint_recipe_state(), await db.get_material_names())
        broken = [*_recipes()[:2], BlueprintRecipe(BATTERY[0] + "x", "Twice", "Shield", False, ("Iron", "Iron"))]
        with pytest.raises(Exception):
            await db.replace_blueprint_recipes(broken, game_version="v2")
        after_failure = (await db.get_blueprint_recipe_state(), await db.get_material_names())
        await db.replace_blueprint_recipes(_recipes([LISTING[0]]), game_version="v3")
        return empty, first, after_failure, await db.get_material_names()

    empty, first, after_failure, replaced = asyncio.run(run())
    assert empty is None
    assert first[0] == ("v1", datetime(2026, 10, 7, 12, 0, 0), len(LISTING))
    assert after_failure == first, "a write that dies midway leaves the previous recipes"
    assert replaced == [("Tungsten", 1)], "a new snapshot leaves nothing of the old one"


# -- 3. sync -----------------------------------------------------------------------------------


def test_the_loop_syncs_recipes_and_contracts_apart_so_a_failure_in_one_never_blocks_the_other(tmp_path):
    async def run():
        wiki = RecipeWiki()
        cog, db = await _make(tmp_path, wiki)
        wiki.listing_status = 503
        await cog.refresh_snapshot.coro(cog)  # the REAL loop body
        contracts_without_recipes = (await db.get_blueprint_snapshot_state(), await db.get_blueprint_recipe_state())
        wiki.listing_status = 200
        db.replace_blueprint_snapshot = AsyncMock(side_effect=RuntimeError("disk full"))
        wiki.version = "4.11.0-LIVE.1"
        await cog.refresh_snapshot.coro(cog)
        return contracts_without_recipes, await db.get_blueprint_snapshot_state(), await db.get_blueprint_recipe_state()

    (contracts, no_recipes), contracts_after, recipes = asyncio.run(run())
    assert contracts is not None and no_recipes is None
    assert contracts_after == contracts, "the contracts' failed swap kept the old snapshot"
    assert recipes[0] == "4.11.0-LIVE.1" and recipes[2] == len(LISTING), "...and the recipes synced anyway"


def test_a_crawl_of_another_version_or_a_shrunken_one_never_replaces_good_recipes(tmp_path):
    async def run():
        wiki = RecipeWiki()
        cog, db = await _make(tmp_path, wiki, seed=True)
        assert await cog.sync_recipes() == "synced"
        good = await db.get_blueprint_recipe_state()
        assert await cog.sync_recipes() == "current"
        outcomes = []
        wiki.version, wiki.listing_version = "4.11.0-LIVE.1", "4.10.0-LIVE.1"
        outcomes.append(await _outcome(cog.sync_recipes()))
        wiki.listing_version, wiki.listing = None, wiki.listing[:1]
        outcomes.append(await _outcome(cog.sync_recipes()))
        return good, outcomes, await db.get_blueprint_recipe_state(), wiki.count("listing")

    good, outcomes, after, crawls = asyncio.run(run())
    assert outcomes == ["SnapshotRejected", "SnapshotRejected"]
    assert after == good and crawls == 3, "the unchanged version wasn't crawled again"


async def _outcome(coro):
    try:
        return await coro
    except Exception as exc:  # noqa: BLE001 - the test reports which
        return type(exc).__name__


# -- 4. the reply ------------------------------------------------------------------------------


def _command_interaction(cog, *, fail_layouts=False):
    interaction = _interaction(cog, fail_layouts=fail_layouts)
    interaction.user = NS(id=1)
    interaction.response.send_message = AsyncMock()
    interaction.client.db = cog.bot.db
    return interaction


def _run_command(cog, interaction, *, blueprint=None, material=None):
    return Blueprints.blueprint_search.callback(cog, interaction, blueprint, material)


def _click_interaction(user_id=1):
    return NS(user=NS(id=user_id), followup=FakeFollowup(),
              response=NS(edit_message=AsyncMock(), send_message=AsyncMock(), type=None))


async def _click(view, item, interaction):
    if await view.interaction_check(interaction):
        await item.callback(interaction)


def _items(view, kind):
    return [item for item in view.walk_children() if isinstance(item, kind)]


def test_the_command_needs_exactly_one_of_blueprint_and_material(tmp_path):
    async def run():
        cog, _db = await _make(tmp_path, RecipeWiki(), seed=True)
        replies = []
        for kwargs in ({}, {"blueprint": BATTERY[1], "material": "Tungsten"}):
            interaction = _command_interaction(cog)
            await _run_command(cog, interaction, **kwargs)
            replies.append((interaction.response.send_message.await_args, interaction.followup.sent))
        return replies

    for call, sent in asyncio.run(run()):
        assert call.kwargs["ephemeral"] and "`material:`" in call.args[0] and sent == []


def test_a_material_lists_its_blueprints_a_category_at_a_time_syncing_what_is_missing(tmp_path):
    async def run():
        cog, _db = await _make(tmp_path, RecipeWiki())  # nothing stored yet: both snapshots sync on first use
        interaction = _command_interaction(cog)
        await _run_command(cog, interaction, material="tungsten")
        return interaction.followup.sent

    (sent,) = asyncio.run(run())
    view = sent["view"]
    assert isinstance(view, MaterialUsesView) and sent["allowed_mentions"].everyone is False
    body = _layout_text(view)
    assert body.startswith("## Tungsten\nUsed in **7 blueprints** you can get · Armor 4 · Guns 2 · Ship parts 1")
    assert "### Armor · 4 blueprints in 2 sets\n**Horizon** · helmet\n**Venture** · core, arms, legs" in body
    assert GHOST[1] not in body
    assert [b.label for b in _items(view, _CategoryButton)] == ["Armor 4", "Guns 2", "Ship parts 1"]
    assert _items(view, _EntrySelect)[0].placeholder == "Pick an armor set"


def test_an_unknown_material_says_so_with_a_near_match(tmp_path):
    async def run():
        cog, _db = await _make(tmp_path, RecipeWiki(), seed=True)
        interaction = _command_interaction(cog)
        await _run_command(cog, interaction, material="Tungstn @everyone")
        interaction2 = _command_interaction(cog)
        await _run_command(cog, interaction2, material="Tungstn")
        return interaction.followup.sent, interaction2.followup.sent

    (unknown,), (near,) = asyncio.run(run())
    assert "No blueprint you can get uses" in unknown["content"] and "@​everyone" in unknown["content"]
    assert "Did you mean: Tungsten?" in near["content"]


def test_a_refused_layout_sends_every_category_as_text(tmp_path):
    async def run():
        cog, _db = await _make(tmp_path, RecipeWiki(), seed=True)
        interaction = _command_interaction(cog, fail_layouts=True)
        await _run_command(cog, interaction, material="Tungsten")
        return interaction.followup.sent

    sent = asyncio.run(run())
    text = "\n".join(s["content"] for s in sent)
    assert not any(_is_layout(s) for s in sent)
    assert "### Armor · 4 blueprints in 2 sets" in text and "**Arbor MH2 Mining Laser** · mining laser" in text
    assert "**P4-AR Rifle** · unlocked by default" in text


def test_categories_sets_and_pieces_open_the_blueprints_usual_reply_as_a_new_message(tmp_path):
    async def run():
        cog, _db = await _make(tmp_path, RecipeWiki(), seed=True)
        interaction = _command_interaction(cog)
        await _run_command(cog, interaction, material="Tungsten")
        view = interaction.followup.sent[0]["view"]

        one_piece = _click_interaction()
        select = _items(view, _EntrySelect)[0]
        select._values = ["0"]  # Horizon: a helmet alone opens at once, no piece buttons
        await _click(view, select, one_piece)
        assert _items(view, _PieceButton) == []
        assert _layout_text(one_piece.followup.sent[0]["view"]).startswith(f"## {HORIZON[1]}")

        pick_set = _click_interaction()
        select = _items(view, _EntrySelect)[0]
        select._values = ["1"]  # Venture
        await _click(view, select, pick_set)
        pieces = [b.label for b in _items(view, _PieceButton)]
        chosen = [o.label for o in _items(view, _EntrySelect)[0].options if o.default]

        open_piece = _click_interaction()
        await _click(view, next(b for b in _items(view, _PieceButton) if b.label == "Core"), open_piece)

        guns = _click_interaction()
        await _click(view, next(b for b in _items(view, _CategoryButton) if b.label == "Guns 2"), guns)
        guns_text = _layout_text(view)
        open_default = _click_interaction()
        select = _items(view, _EntrySelect)[0]
        select._values = [str([o.label for o in select.options].index(P4AR[1]))]
        await _click(view, select, open_default)

        open_battery = _click_interaction()
        select = _items(view, _EntrySelect)[0]
        select._values = [str([o.label for o in select.options].index(BATTERY[1]))]
        await _click(view, select, open_battery)
        return pieces, chosen, open_piece, guns_text, open_default, open_battery, pick_set

    pieces, chosen, open_piece, guns_text, open_default, open_battery, pick_set = asyncio.run(run())
    assert pieces == ["Core", "Arms", "Legs"] and chosen == ["Venture"]
    assert pick_set.followup.sent == [] and pick_set.response.edit_message.await_count == 1
    (core,) = open_piece.followup.sent
    assert open_piece.response.edit_message.await_count == 1, "the click is answered before the reply is sent"
    assert _layout_text(core["view"]).startswith("## Venture Core")
    assert "### Guns · 2\n**Arclight Pistol Battery (30 cap)**\n**P4-AR Rifle** · unlocked by default" in guns_text
    (default,) = open_default.followup.sent
    assert default["content"].startswith("## P4-AR Rifle\nUnlocked by default: every player can craft it")
    assert _layout_text(open_battery.followup.sent[0]["view"]).startswith(f"## {BATTERY[1]}")


def test_only_whoever_ran_it_can_click(tmp_path):
    async def run():
        cog, _db = await _make(tmp_path, RecipeWiki(), seed=True)
        interaction = _command_interaction(cog)
        await _run_command(cog, interaction, material="Tungsten")
        view = interaction.followup.sent[0]["view"]
        stranger = _click_interaction(user_id=2)
        await _click(view, _items(view, _CategoryButton)[1], stranger)
        return view, stranger

    view, stranger = asyncio.run(run())
    assert view.category == ARMOR and stranger.response.edit_message.await_count == 0
    assert stranger.response.send_message.await_args.kwargs["ephemeral"]


def _big_material():
    colours = ["Woodland", "Arctic", "Desert", "Night", "Crimson Red Edition", "Urban Grey Camo"]
    uses = []
    for n in range(40):
        for colour in colours[: 1 + n % len(colours)]:
            for piece, kind in (("Helmet", "Char_Armor_Helmet"), ("Core", "Char_Armor_Torso"), ("Arms", "Char_Armor_Arms"),
                                ("Legs", "Char_Armor_Legs"), ("Backpack", "Char_Armor_Backpack")):
                uses.append(_use(f"Overlord Heavy Armor Mk{n} {piece} {colour}", kind))
    uses += [_use(f"Behring Precision Rifle Variant Number {n} Long Name Edition", "WeaponPersonal") for n in range(70)]
    uses += [_use(f"Aegis Dynamics Extended Ship Component {n}", "QuantumDrive") for n in range(70)]
    return uses


def test_every_page_of_the_biggest_material_fits_discords_layout_limits():
    async def run():
        uses = _big_material()
        view = MaterialUsesView("Aslarite", uses, owner_id=1, opener=AsyncMock())
        seen = 0
        for category in list(view.grouped):
            await view.show_category(_click_interaction(), category)
            while True:
                seen += 1
                assert view.content_length() <= LAYOUT_TEXT_LIMIT
                select = _items(view, _EntrySelect)[0]
                assert 0 < len(select.options) <= 25
                if category == ARMOR:
                    select._values = ["0"]
                    await view.pick(_click_interaction(), select.entries[0])
                    assert len(_items(view, _PieceButton)) == 5
                    assert view.content_length() <= LAYOUT_TEXT_LIMIT
                nxt = _items(view, _PageButton)[-1]
                if nxt.disabled:
                    break
                await view.turn_page(_click_interaction(), +1)
        return view, seen

    view, seen = asyncio.run(run())
    assert seen > 10
    assert "-# Page 4 of 4" in _layout_text(view)


def test_an_idle_list_greys_out_and_says_so():
    async def run():
        view = MaterialUsesView("Tungsten", [_use("Killshot"), _use("Agni", "QuantumDrive")], owner_id=1, opener=AsyncMock())
        view.message = NS(edit=AsyncMock(), flags=NS(ephemeral=True))
        await view.on_timeout()
        return view

    view = asyncio.run(run())
    controls = [item for item in view.walk_children() if hasattr(item, "disabled")]
    assert controls and all(item.disabled for item in controls)
    assert "Closed after 15 minutes idle" in _layout_text(view)
    assert view.message.edit.await_args.kwargs["view"] is view


def test_text_listing_covers_every_category_in_full():
    text = text_listing("Tungsten", _big_material())
    assert text.count("**Behring Precision Rifle") == 70 and text.count("· quantum drive") == 70
    assert "### Armor · " in text and text.index("### Armor") < text.index("### Guns") < text.index("### Ship parts")


def test_material_autocomplete_puts_names_starting_with_the_typed_text_first(tmp_path):
    async def run():
        cog, db = await _make(tmp_path, RecipeWiki(), seed=True)
        await db.replace_blueprint_recipes(_recipes(), game_version="v1")
        interaction = _command_interaction(cog)
        return await material_autocomplete(interaction, "a"), await material_autocomplete(interaction, "")

    typed, everything = asyncio.run(run())
    assert [c.value for c in typed] == ["Aslarite", "Hadanite"]
    assert [c.name for c in everything] == ["Tungsten · 7 blueprints", "Aslarite · 3 blueprints", "Hadanite · 1 blueprint"]


def test_the_armor_set_shown_as_picked_survives_a_redraw():
    view = MaterialUsesView("Aslarite", [_use("Aves Helmet", "Char_Armor_Helmet"), _use("Aves Core", "Char_Armor_Torso")],
                            owner_id=1, opener=AsyncMock())
    view.picked = ArmorSet("Aves", view.entries[0].pieces)
    view.render()
    assert [o.default for o in _items(view, _EntrySelect)[0].options] == [True]



# -- audit fixes (2026-10-07) ------------------------------------------------------------------


def test_a_material_whose_pages_would_not_all_fit_goes_as_text_from_the_start(tmp_path, monkeypatch):
    import bot.material_pages as material_pages

    async def run():
        cog, _db = await _make(tmp_path, RecipeWiki(), seed=True)
        monkeypatch.setattr(material_pages, "LAYOUT_TEXT_LIMIT", 200)
        interaction = _command_interaction(cog)
        await _run_command(cog, interaction, material="Tungsten")
        return interaction.followup.sent

    sent = asyncio.run(run())
    assert sent and not any(_is_layout(s) for s in sent)
    assert "**Arbor MH2 Mining Laser** · mining laser" in "\n".join(s["content"] for s in sent)


def test_fits_checks_every_page_and_leaves_the_view_as_it_was():
    view = MaterialUsesView("Aslarite", _big_material(), owner_id=1, opener=AsyncMock())
    before = _layout_text(view)
    assert view.fits() and _layout_text(view) == before and view.category == ARMOR and view.picked is None


def test_a_failed_first_recipe_sync_is_not_retried_by_every_query(tmp_path):
    async def run():
        wiki = RecipeWiki()
        cog, _db = await _make(tmp_path, wiki, seed=True)
        wiki.listing_status = 503
        replies = []
        for _ in range(3):
            interaction = _command_interaction(cog)
            await _run_command(cog, interaction, material="Tungsten")
            replies.append(interaction.followup.sent[0]["content"])
        return replies, wiki.count("listing-error")

    replies, crawls = asyncio.run(run())
    assert all("aren't available right now" in reply for reply in replies)
    assert crawls == 3, "one sync (the client tries a failing page 3 times), not one per query"


def test_upper_case_blueprint_ids_still_match_the_contracts():
    (recipe,) = parse_blueprint_rows([_listing_row(BATTERY[0].upper(), BATTERY[1], BATTERY[2], ["Iron"])])
    assert recipe.uuid == BATTERY[0]


def test_a_material_spelt_two_ways_is_one_autocomplete_entry(tmp_path):
    async def run():
        _cog, db = await _make(tmp_path, RecipeWiki(), seed=True)
        listing = [_listing_row(*BATTERY, ["Tungsten"]), _listing_row(*HORIZON, ["tungsten"])]
        await db.replace_blueprint_recipes(_recipes(listing), game_version="v1")
        return await db.get_material_names()

    (only,) = asyncio.run(run())
    assert only[1] == 2


def test_a_contract_blueprint_opened_while_the_contract_data_is_gone_says_so(tmp_path):
    async def run():
        cog, _db = await _make(tmp_path, RecipeWiki())
        cog.get_index = AsyncMock(return_value=None)
        sent = FakeFollowup()
        await cog.open_blueprint(sent.send, MaterialUse(*BATTERY))
        return sent.sent

    (reply,) = asyncio.run(run())
    assert "isn't available right now" in reply["content"]
