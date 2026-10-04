import asyncio
import copy
import dataclasses
import json
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock

import discord

import pytest

from bot.cogs.blueprint_planner import (
    BlueprintResultView,
    ChoiceSelect,
    CraftConfigView,
    CraftLaunchView,
    CraftLayoutView,
    MineButton,
    QualitySelect,
    open_craft_config,
)
from bot.uex.blueprint_crafting import Group, Recipe, UNAVAILABLE
from tests.test_blueprints_cog import FakeWiki, _make


FIXTURE = Path(__file__).parent / "fixtures" / "blueprint_crafting_rifle.json"


def _detail():
    return json.loads(FIXTURE.read_text(encoding="utf-8-sig"))


def test_craft_quantity_is_rendered_and_three_actual_aspects_get_independent_controls(tmp_path):
    async def run():
        cog, _ = await _make(tmp_path, FakeWiki(), seed=True)
        cog._client.get_blueprint_detail = AsyncMock(return_value=_detail())
        result = await cog.search("Killshot Rifle", craft_quantity=5)
        return cog, result

    cog, result = asyncio.run(run())
    # The reply only says how many copies; the materials live behind Configure crafting (2026-10-03).
    assert result.recipe is not None and "**Crafting 5 copies**" in result.crafting
    assert "**Crafting 5 copies**" in "\n".join(result.pages) and "SCU" not in "\n".join(result.pages)
    recipe = result.recipe
    quality = {item.path: (325, 521, 1000) for item in recipe.inputs if item.ore_uuid}
    view = CraftConfigView(cog, recipe, 5, quality)
    assert "5 crafts" in view.text() and "0.20 SCU" in view.text(), "frame quantity scales from .04 to .20"
    controls = [child for child in view.children if isinstance(child, QualitySelect)]
    assert len(controls) == 3
    assert {control.path for control in controls} == {item.path for item in recipe.inputs}


def test_detail_version_mismatch_keeps_contracts_and_marks_recipe_unavailable(tmp_path):
    async def run():
        cog, _ = await _make(tmp_path, FakeWiki(), seed=True)
        detail = copy.deepcopy(_detail())
        detail["game_version"] = "different-patch"
        cog._client.get_blueprint_detail = AsyncMock(return_value=detail)
        return await cog.search("Killshot Rifle")

    result = asyncio.run(run())
    text = "\n".join(result.pages)
    assert result.status == "found" and result.recipe is None
    assert UNAVAILABLE in text and "Killshot" in text


def test_recipe_failure_message_does_not_replace_contract_result(tmp_path):
    async def run():
        wiki = FakeWiki()
        wiki.detail_status = 503
        cog, _ = await _make(tmp_path, wiki, seed=True)
        return await cog.search("Killshot Rifle")

    result = asyncio.run(run())
    assert result.status == "found" and UNAVAILABLE in "\n".join(result.pages)
    assert "contract" in "\n".join(result.pages).lower()


def test_the_layout_puts_configure_crafting_beside_its_line_and_every_other_button_under_it(tmp_path):
    async def run():
        cog, _ = await _make(tmp_path, FakeWiki(), seed=True)
        cog._client.get_blueprint_detail = AsyncMock(return_value=_detail())
        result = await cog.search("Killshot Rifle")
        sent = []

        async def send(**kwargs):
            sent.append(kwargs)
            return NS(id=1)

        await cog.deliver(send, result)
        return result, sent

    result, (kwargs,) = asyncio.run(run())
    view = kwargs["view"]
    assert isinstance(view, BlueprintResultView) and "content" not in kwargs and "embed" not in kwargs
    (section,) = [item for item in view.walk_children() if isinstance(item, discord.ui.Section)]
    assert section.accessory.label == "Configure crafting" and "**Crafting**" in section.children[0].content
    rows = [item for item in view.walk_children() if isinstance(item, discord.ui.ActionRow)]
    labels = [button.label for row in rows for button in row.children]
    assert labels[0] == "Add to shopping list" and all(label.startswith("Mine ") for label in labels[1:])
    assert all(len(row.children) <= 5 for row in rows)
    assert view.content_length() <= 4000 and "Craft 1" not in "".join(
        item.content for item in view.walk_children() if isinstance(item, discord.ui.TextDisplay))


def test_a_layout_greys_out_every_nested_button_when_it_times_out(tmp_path):
    async def run():
        cog, _ = await _make(tmp_path, FakeWiki(), seed=True)
        recipe = Recipe.parse(_detail())
        view = BlueprintResultView(cog, ("## Rifle",), "**Crafting**\nSee the materials.", recipe, 1)
        view.message = NS(id=1, flags=NS(ephemeral=False), edit=AsyncMock(),
                          channel=NS(get_partial_message=lambda _id: NS(edit=AsyncMock())))
        await view.on_timeout()
        return view

    view = asyncio.run(run())
    buttons = [item for item in view.walk_children() if isinstance(item, discord.ui.Button)]
    assert len(buttons) >= 3 and all(button.disabled for button in buttons)
    view.message.edit.assert_awaited_once()


def test_more_buttons_than_fit_one_row_wrap_to_a_second(tmp_path):
    async def run():
        cog, _ = await _make(tmp_path, FakeWiki(), seed=True)
        recipe = NS(inputs=[NS(ore_uuid=f"ore-{i}", name=f"Ore {i}") for i in range(6)])
        return BlueprintResultView(cog, ("## Rifle",), "**Crafting**", recipe, 1)

    view = asyncio.run(run())
    rows = [item for item in view.walk_children() if isinstance(item, discord.ui.ActionRow)]
    assert [len(row.children) for row in rows] == [5, 1], "Add to shopping list and five Mine buttons (the cap)"


def test_without_a_recipe_the_layout_says_so_and_has_no_buttons(tmp_path):
    async def run():
        cog, _ = await _make(tmp_path, FakeWiki(), seed=True)
        return BlueprintResultView(cog, ("## Rifle",), f"-# {UNAVAILABLE}", None, 1)

    view = asyncio.run(run())
    assert not [item for item in view.walk_children() if isinstance(item, discord.ui.Button)]
    assert any(UNAVAILABLE in item.content for item in view.walk_children() if isinstance(item, discord.ui.TextDisplay))


def test_mining_buttons_reuse_the_existing_deterministic_lookup_without_a_prompt():
    recipe = Recipe.parse(_detail())
    mining = NS(build_where_to_mine_embed=AsyncMock(return_value=(NS(), None)))
    cog = NS()
    view = CraftLaunchView(cog, recipe, 1)
    buttons = [child for child in view.children if isinstance(child, MineButton)]
    assert len(buttons) == 2, "Iron appears twice but gets one lookup button; Hephaestanite gets the other"

    async def run():
        interaction = NS(
            response=NS(defer=AsyncMock()), followup=NS(send=AsyncMock()),
            client=NS(get_cog=lambda name: mining if name == "MiningLocations" else None),
        )
        await buttons[0].callback(interaction)
        return interaction

    interaction = asyncio.run(run())
    mining.build_where_to_mine_embed.assert_awaited_once_with(buttons[0].material)
    interaction.response.defer.assert_awaited_once_with(ephemeral=True)


def test_item_aspect_quality_landmarks_create_a_dropdown_without_an_ore_uuid():
    raw = _detail()
    item = raw['requirement_groups'][2]['children'][0]
    item.update(kind='item', uuid='carinite-item', name='Carinite', quantity=1, quantity_scu=None)
    raw['requirement_groups'][2]['key'] = 'REGULATOR'
    raw['aspects']['aspects'].append({
        'key': 'REGULATOR', 'input': {'uuid': 'carinite-item'},
        'slider_min': 0, 'initial_quality': 500, 'slider_max': 1000,
    })
    recipe = Recipe.parse(raw)
    options = {item.path: item.quality_values for item in recipe.inputs}
    view = CraftConfigView(NS(), recipe, 1, options)
    controls = [child for child in view.children if isinstance(child, QualitySelect)]
    carinite = next(control for control in controls if 'Carinite' in control.placeholder)
    assert [option.value for option in carinite.options] == ['none', *[str(value) for value in range(0, 1001, 50)]]


# -- selectors beyond what one Discord message can hold are paged, never silently dropped -----------------

def _many_input_recipe(inputs: int, *, choice_groups: int = 0) -> tuple[Recipe, dict]:
    """The real rifle recipe with `inputs` quality-capable inputs (cloned from its first one) and optional
    required-choice groups over them, plus obtainable qualities for every input."""
    base = Recipe.parse(_detail())
    template = base.inputs[0]
    cloned = tuple(
        dataclasses.replace(template, path=f"{template.path}-x{k}", name=f"{template.name} {k}") for k in range(inputs)
    )
    groups = tuple(
        Group(path=f"group-{k}", name=f"Pick {k}", required=1, children=(cloned[0].path, cloned[1].path))
        for k in range(choice_groups)
    )
    recipe = dataclasses.replace(base, inputs=cloned, groups=groups)
    return recipe, {item.path: (0, 500, 1000) for item in cloned}


def _selectors(view):
    return [child for child in view.walk_children() if isinstance(child, discord.ui.Select)]


def _nav(view):
    return [child for child in view.walk_children() if getattr(child, "label", "") in ("Previous options", "Next options")]


def _text(view) -> str:
    """What the reply says: the plain message's text, or the layout's text blocks."""
    if isinstance(view, CraftConfigView):
        return view.text()
    return "\n\n".join(item.content for item in view.walk_children() if isinstance(item, discord.ui.TextDisplay))


def _interaction():
    return NS(response=NS(edit_message=AsyncMock()))


def _fixed_blocks(self, count, choices=None, qualities=None, options=None, *, note=None):
    return (note or "config",)


BOTH_FORMS = pytest.mark.parametrize("form", [CraftLayoutView, CraftConfigView])


@BOTH_FORMS
def test_four_selectors_fit_one_page_with_no_paging_controls(monkeypatch, form):
    """Discord fits five rows: four selects plus the button row. The old cap counted the button among
    four children, so a fourth selector was dropped even though it fit."""
    monkeypatch.setattr(Recipe, "layout_blocks", _fixed_blocks)

    async def run():
        recipe, options = _many_input_recipe(4)
        view = form(NS(), recipe, 1, options)
        assert len(_selectors(view)) == 4 and view.page_count == 1 and not _nav(view)
        assert _text(view) == "config", "no page note when everything fits"

    asyncio.run(run())


@BOTH_FORMS
def test_more_selectors_than_fit_are_paged_and_every_one_is_reachable(monkeypatch, form):
    monkeypatch.setattr(Recipe, "layout_blocks", _fixed_blocks)

    async def run():
        recipe, options = _many_input_recipe(6)
        view = form(NS(), recipe, 1, options)
        seen = {select.path for select in _selectors(view)}
        assert len(_selectors(view)) == 4 and view.page_count == 2
        previous, following = _nav(view)
        assert previous.disabled and not following.disabled
        assert _text(view).startswith("Menus page 1 of 2")

        interaction = _interaction()
        await view.next_button.callback(interaction)
        interaction.response.edit_message.assert_awaited_once()
        seen |= {select.path for select in _selectors(view)}
        assert len(_selectors(view)) == 2 and _text(view).startswith("Menus page 2 of 2")
        assert not previous.disabled and following.disabled

        await view.previous_button.callback(_interaction())
        assert len(_selectors(view)) == 4 and previous.disabled
        assert seen == {item.path for item in recipe.inputs}, "no selector may be unreachable"

    asyncio.run(run())


@BOTH_FORMS
def test_a_quality_chosen_on_a_later_page_is_kept_when_paging_back(monkeypatch, form):
    monkeypatch.setattr(Recipe, "layout_blocks", _fixed_blocks)

    async def run():
        recipe, options = _many_input_recipe(6)
        view = form(NS(), recipe, 1, options)
        await view.next_button.callback(_interaction())
        chosen = _selectors(view)[0]
        chosen._values = ["500"]  # what discord.py fills in from the interaction payload
        await chosen.callback(_interaction())
        await view.previous_button.callback(_interaction())
        await view.next_button.callback(_interaction())
        assert view.qualities == {chosen.path: 500}
        assert [option.label for option in chosen.options if option.default] == [
            next(option.label for option in chosen.options if option.value == "500")], "the pick stays shown"

    asyncio.run(run())


@BOTH_FORMS
def test_required_material_choices_come_before_optional_quality_selectors(monkeypatch, form):
    """A plan can't be built without its required choices, so if anything is pushed to a later page it
    must be an optional quality selector, never a required choice."""
    monkeypatch.setattr(Recipe, "layout_blocks", _fixed_blocks)

    async def run():
        recipe, options = _many_input_recipe(6, choice_groups=2)
        view = form(NS(), recipe, 1, options)
        first_page = _selectors(view)
        assert view.page_count == 2 and len(view.selectors) == 8
        assert all(isinstance(select, ChoiceSelect) for select in first_page[:2])
        assert not any(isinstance(select, ChoiceSelect) for select in view.selectors[2:])

    asyncio.run(run())


@BOTH_FORMS
def test_a_very_large_recipe_still_builds_and_pages_within_discords_layout_limits(monkeypatch, form):
    """discord.py raises if a page ever needs more than five rows (or a layout more than 40
    components), so simply walking every page proves it."""
    monkeypatch.setattr(Recipe, "layout_blocks", _fixed_blocks)

    async def run():
        recipe, options = _many_input_recipe(13)
        view = form(NS(), recipe, 1, options)
        assert view.page_count == 4
        for _ in range(view.page_count - 1):
            await view.next_button.callback(_interaction())
        assert len(_selectors(view)) == 1 and view.next_button.disabled

    asyncio.run(run())



# -- Configure crafting as a layout (option A, the owner's pick 2026-10-04) ---------------------------

class _Followup:
    def __init__(self, refuse_layouts=0):
        self.sent, self.refuse_layouts = [], refuse_layouts

    async def send(self, *args, **kwargs):
        if isinstance(kwargs.get("view"), discord.ui.LayoutView) and self.refuse_layouts:
            self.refuse_layouts -= 1
            raise discord.HTTPException(NS(status=400, reason="Bad Request"), "Invalid Form Body")
        self.sent.append((args, kwargs))
        return NS(id=len(self.sent))


def _open(refuse_layouts=0):
    recipe = Recipe.parse(_detail())
    options = {item.path: (325, 521, 1000) for item in recipe.inputs}
    cog = NS(quality_options=AsyncMock(return_value=options))
    interaction = NS(response=NS(defer=AsyncMock()), followup=_Followup(refuse_layouts))

    async def run():
        await open_craft_config(cog, recipe, 1, interaction)
        return interaction

    return recipe, asyncio.run(run())


def test_configure_crafting_opens_as_a_private_layout():
    recipe, interaction = _open()
    (args, kwargs), = interaction.followup.sent
    view = kwargs["view"]
    assert isinstance(view, CraftLayoutView) and kwargs["ephemeral"] is True and not args
    container = view.children[0]
    assert isinstance(container, discord.ui.Container)
    texts = [item.content for item in container.children if isinstance(item, discord.ui.TextDisplay)]
    assert texts == list(recipe.layout_blocks(1, {}, {}, view.options))
    assert texts[0].startswith(f"## {recipe.name}") and recipe.uuid not in "".join(texts), "no blueprint id"
    assert [select.placeholder for select in _selectors(view)] == [
        "Iron quality (Frame)", "Hephaestanite quality (Stock)", "Iron quality (Barrel)"]
    assert [button.label for button in view.walk_children() if isinstance(button, discord.ui.Button)] == [
        "Add to shopping list"]
    assert view.message is not None, "it greys out through its message when it times out"


def test_picking_a_quality_redraws_the_layout_with_its_effect_and_keeps_it_shown():
    recipe, interaction = _open()
    view = interaction.followup.sent[0][1]["view"]
    frame = _selectors(view)[0]
    assert [option.label for option in frame.options][:2] == ["Not picked", "Iron 325 — Recoil 7% worse"]
    frame._values = ["1000"]
    click = _interaction()
    asyncio.run(frame.callback(click))
    click.response.edit_message.assert_awaited_once()
    assert click.response.edit_message.call_args.kwargs["view"] is view
    text = _text(view)
    assert "Quality `1000` · mined at 325–1000\nRecoil (smoothness, handling, kick) **20% better**" in text
    assert "### Your craft\n-# Pick every quality to see it." in text, (
        "the Stock moves recoil too, so the total waits for it")
    assert [option.label for option in _selectors(view)[0].options if option.default] == ["Iron 1000 — Recoil 20% better"]


def test_a_layout_discord_refuses_is_sent_as_the_same_text():
    recipe, interaction = _open(refuse_layouts=1)
    (args, kwargs), = interaction.followup.sent
    view = kwargs["view"]
    assert isinstance(view, CraftConfigView) and kwargs["ephemeral"] is True
    assert args[0] == "\n\n".join(recipe.layout_blocks(1, {}, {}, view.options))
    assert [select.placeholder for select in _selectors(view)][0] == "Iron quality (Frame)"
