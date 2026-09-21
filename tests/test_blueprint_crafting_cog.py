import asyncio
import copy
import json
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock

from bot.cogs.blueprint_planner import CraftConfigView, CraftLaunchView, MineButton, QualitySelect
from bot.uex.blueprint_crafting import Recipe, UNAVAILABLE
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
