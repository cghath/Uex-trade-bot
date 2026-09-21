import asyncio
import copy
import dataclasses
import json
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock

import discord

from bot.cogs.blueprint_planner import ChoiceSelect, CraftConfigView, CraftLaunchView, MineButton, QualitySelect
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
    assert result.recipe is not None and "Craft 5" in "\n".join(result.pages)
    assert "0.20 SCU" in "\n".join(result.pages), "frame quantity scales from .04 to .20"
    recipe = result.recipe
    quality = {item.path: (325, 521, 1000) for item in recipe.inputs if item.ore_uuid}
    view = CraftConfigView(cog, recipe, 5, quality)
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
    return [child for child in view.children if isinstance(child, discord.ui.Select)]


def _nav(view):
    return [child for child in view.children if getattr(child, "label", "") in ("Previous options", "Next options")]


def _interaction():
    return NS(response=NS(edit_message=AsyncMock()))


def test_four_selectors_fit_one_page_with_no_paging_controls(monkeypatch):
    """Discord fits five rows: four selects plus the button row. The old cap counted the button among
    four children, so a fourth selector was dropped even though it fit."""
    monkeypatch.setattr(Recipe, "lines", lambda self, count, choices=None, qualities=None: ["config"])

    async def run():
        recipe, options = _many_input_recipe(4)
        view = CraftConfigView(NS(), recipe, 1, options)
        assert len(_selectors(view)) == 4 and view.page_count == 1 and not _nav(view)
        assert view.text() == "config", "no page header when everything fits"

    asyncio.run(run())


def test_more_selectors_than_fit_are_paged_and_every_one_is_reachable(monkeypatch):
    monkeypatch.setattr(Recipe, "lines", lambda self, count, choices=None, qualities=None: ["config"])

    async def run():
        recipe, options = _many_input_recipe(6)
        view = CraftConfigView(NS(), recipe, 1, options)
        seen = {select.path for select in _selectors(view)}
        assert len(_selectors(view)) == 4 and view.page_count == 2
        previous, following = _nav(view)
        assert previous.disabled and not following.disabled
        assert view.text().startswith("Options page 1 of 2")

        interaction = _interaction()
        await view.next_button.callback(interaction)
        interaction.response.edit_message.assert_awaited_once()
        seen |= {select.path for select in _selectors(view)}
        assert len(_selectors(view)) == 2 and view.text().startswith("Options page 2 of 2")
        assert not previous.disabled and following.disabled

        await view.previous_button.callback(_interaction())
        assert len(_selectors(view)) == 4 and previous.disabled
        assert seen == {item.path for item in recipe.inputs}, "no selector may be unreachable"

    asyncio.run(run())


def test_a_quality_chosen_on_a_later_page_is_kept_when_paging_back(monkeypatch):
    monkeypatch.setattr(Recipe, "lines", lambda self, count, choices=None, qualities=None: ["config"])

    async def run():
        recipe, options = _many_input_recipe(6)
        view = CraftConfigView(NS(), recipe, 1, options)
        await view.next_button.callback(_interaction())
        chosen = _selectors(view)[0]
        chosen._values = ["500"]  # what discord.py fills in from the interaction payload
        await chosen.callback(_interaction())
        await view.previous_button.callback(_interaction())
        await view.next_button.callback(_interaction())
        assert view.qualities == {chosen.path: 500}

    asyncio.run(run())


def test_required_material_choices_come_before_optional_quality_selectors(monkeypatch):
    """A plan can't be built without its required choices, so if anything is pushed to a later page it
    must be an optional quality selector, never a required choice."""
    monkeypatch.setattr(Recipe, "lines", lambda self, count, choices=None, qualities=None: ["config"])

    async def run():
        recipe, options = _many_input_recipe(6, choice_groups=2)
        view = CraftConfigView(NS(), recipe, 1, options)
        first_page = _selectors(view)
        assert view.page_count == 2 and len(view.selectors) == 8
        assert all(isinstance(select, ChoiceSelect) for select in first_page[:2])
        assert not any(isinstance(select, ChoiceSelect) for select in view.selectors[2:])

    asyncio.run(run())


def test_a_very_large_recipe_still_builds_and_pages_within_discords_layout_limits(monkeypatch):
    """discord.py raises if a page ever needs more than five rows, so simply walking every page proves it."""
    monkeypatch.setattr(Recipe, "lines", lambda self, count, choices=None, qualities=None: ["config"])

    async def run():
        recipe, options = _many_input_recipe(13)
        view = CraftConfigView(NS(), recipe, 1, options)
        assert view.page_count == 4
        for _ in range(view.page_count - 1):
            await view.next_button.callback(_interaction())
        assert len(_selectors(view)) == 1 and view.next_button.disabled

    asyncio.run(run())
