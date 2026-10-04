import copy
import json
from decimal import Decimal
from pathlib import Path

import pytest

from bot.uex import blueprint_crafting as crafting


def rifle():
    return json.loads((Path(__file__).parent / 'fixtures/blueprint_crafting_rifle.json').read_text(encoding='utf-8-sig'))


def test_three_independent_inputs_scale_and_multiply_shared_stats():
    recipe = crafting.Recipe.parse(rifle())
    assert len(recipe.inputs) == 3
    qualities = {'0.0': 1000, '1.0': 0, '2.0': 500}
    plan = recipe.plan(5, {}, qualities)
    assert [x['amount'] for x in plan['ingredients']] == ['0.20', '0.10', '0.10']
    assert recipe.modifiers({}, qualities)['weapon_recoil_kick'] == Decimal('0.96')
    assert recipe.modifiers({}, qualities)['weapon_damage'] == Decimal('1.000')


def test_choose_one_is_not_all_required_and_units_never_merge():
    raw = rifle()
    alternative = copy.deepcopy(raw['requirement_groups'][0]['children'][0])
    alternative.update(kind='item', quantity=2, quantity_scu=None)
    raw['requirement_groups'][0]['children'].append(alternative)
    recipe = crafting.Recipe.parse(raw)
    with pytest.raises(ValueError, match='Choose'):
        recipe.plan(5, {}, {})
    plan = recipe.plan(5, {'0': [1]}, {})
    totals = crafting.aggregate([plan, plan])
    assert {(r['unit'], r['amount']) for r in totals if r['name'] == 'Iron'} == {('items', '20'), ('SCU', '0.20')}


@pytest.mark.parametrize('count', [True, 0, -1, 1.5, 10001])
def test_invalid_craft_counts_are_rejected(count):
    with pytest.raises(ValueError):
        crafting.Recipe.parse(rifle()).plan(count, {}, {})


def test_quality_values_are_material_specific_and_discrete():
    ore = {'uuid': 'iron', 'locations': [{'resources': [{'materials': [
        {'uuid': 'iron', 'quality_quantized_values': [325, 521, 1000]},
        {'uuid': 'iron', 'is_current': False, 'quality_quantized_values': [777]},
        {'uuid': 'other', 'quality_quantized_values': [1, 500]},
    ]}]}]}
    assert crafting.obtainable_qualities(ore, 'iron') == (325, 521, 1000)
    assert crafting.obtainable_qualities({'uuid': 'iron', 'quality_min': 0, 'quality_max': 1000}, 'iron') == ()


def test_malformed_recipe_is_unavailable_not_a_partial_shopping_list():
    raw = rifle()
    raw['requirement_groups'][1]['children'][0]['quantity_scu'] = float('nan')
    with pytest.raises(ValueError):
        crafting.Recipe.parse(raw)


def test_item_input_uses_blueprint_quality_landmarks_and_ignores_empty_duplicate_modifier():
    raw = rifle()
    group = raw['requirement_groups'][2]
    group['key'] = 'REGULATOR'
    group['name'] = 'Power Regulator'
    group['children'][0].update(
        kind='item', uuid='carinite-item', name='Carinite', quantity=1, quantity_scu=None,
        modifiers=[{
            'property_key': 'weapon_damage', 'label': 'Impact Force', 'better_when': 'neutral',
            'quality_range': {'min': None, 'max': None},
            'modifier_range': {'at_min_quality': None, 'at_max_quality': None},
            'value_range_type': None, 'value_segments': None,
        }],
    )
    raw['aspects']['aspects'].append({
        'key': 'REGULATOR', 'input': {'uuid': 'carinite-item'},
        'slider_min': 0, 'initial_quality': 500, 'slider_max': 1000,
    })
    recipe = crafting.Recipe.parse(raw)
    carinite = next(item for item in recipe.inputs if item.name == 'Carinite')
    assert carinite.quality_values == tuple(range(0, 1001, 50))
    assert len(carinite.modifiers) == 2, "the valid group modifiers remain; the empty child duplicate is removed"


def test_plan_rejects_an_in_range_quality_that_is_not_one_of_the_discrete_values():
    """Audit-confirmed defect (hardening, not a live incident - Discord's own dropdown
    never offers anything else): plan() only range-checked a chosen quality against
    min_quality..1000, never checking it was actually one of the material's own discrete
    quality_values (real for an item-kind ingredient with matching aspect data - here,
    multiples of 50, same fixture as the test above). A non-UI caller could otherwise
    submit an in-range but non-discrete value."""
    raw = rifle()
    group = raw['requirement_groups'][2]
    group['key'] = 'REGULATOR'
    group['name'] = 'Power Regulator'
    group['children'][0].update(
        kind='item', uuid='carinite-item', name='Carinite', quantity=1, quantity_scu=None,
        modifiers=[{
            'property_key': 'weapon_damage', 'label': 'Impact Force', 'better_when': 'neutral',
            'quality_range': {'min': None, 'max': None},
            'modifier_range': {'at_min_quality': None, 'at_max_quality': None},
            'value_range_type': None, 'value_segments': None,
        }],
    )
    raw['aspects']['aspects'].append({
        'key': 'REGULATOR', 'input': {'uuid': 'carinite-item'},
        'slider_min': 0, 'initial_quality': 500, 'slider_max': 1000,
    })
    recipe = crafting.Recipe.parse(raw)
    carinite = next(item for item in recipe.inputs if item.name == 'Carinite')
    assert 325 not in carinite.quality_values, "sanity check: 325 isn't a multiple of 50"

    with pytest.raises(ValueError, match='Quality outside recipe limits'):
        recipe.plan(5, {}, {carinite.path: 325})

    recipe.plan(5, {}, {carinite.path: 500})  # a real discrete value must still be accepted


def test_a_stat_change_reads_as_better_or_worse_in_whole_percent():
    assert crafting.describe_change(Decimal('0.976'), 'lower') == '2% better'
    assert crafting.describe_change(Decimal('1.0918'), 'higher') == '9% better'
    assert crafting.describe_change(Decimal('0.9'), 'higher') == '10% worse'
    assert crafting.describe_change(Decimal('1.004'), 'higher') == 'no change'
    assert crafting.describe_change(Decimal('1.2'), 'neutral') == '20% higher'


# -- Configure crafting's text (option A, the owner's pick 2026-10-04) --------------------------------

OPTIONS = {'0.0': (325, 521, 1000), '1.0': (325, 521, 1000), '2.0': (325, 521, 1000)}


def test_nothing_picked_shows_what_each_material_can_do_never_unavailable():
    header, frame, stock, barrel, result = crafting.Recipe.parse(rifle()).layout_blocks(1, {}, {}, OPTIONS)
    assert header.splitlines() == ['## Killshot Rifle', '-# Configure crafting · 1 craft · game 4.10.0',
                                   '-# Pick a quality for each material below; stats change with it.']
    assert frame.splitlines() == [
        '### Frame · Iron · 0.04 SCU', 'Quality not picked · mined at `325–1000`',
        '-# Recoil (smoothness, handling, kick): 7% worse at 325 → 20% better at 1000']
    assert barrel.splitlines()[2:] == ['-# Impact Force: 3% worse at 325 → 8% better at 1000',
                                       '-# Fire Rate: 4% worse at 325 → 12% better at 1000']
    assert result.splitlines() == ['### Your craft', '-# Pick every quality to see it. Best possible: '
                                   'Recoil 36% better · Impact Force 8% better · Fire Rate 12% better']
    text = '\n'.join([header, frame, stock, barrel, result])
    assert 'unavailable' not in text and 'requires known quality' not in text


def test_picked_materials_show_their_effect_and_the_totals_multiply():
    recipe = crafting.Recipe.parse(rifle())
    partly = recipe.layout_blocks(5, {}, {'0.0': 1000, '1.0': 521}, OPTIONS)
    assert partly[0].splitlines()[1] == '-# Configure crafting · 5 crafts · game 4.10.0'
    assert partly[1].splitlines() == ['### Frame · Iron · 0.20 SCU', 'Quality `1000` · mined at 325–1000',
                                      'Recoil (smoothness, handling, kick) **20% better**']
    assert partly[-1].splitlines() == ['### Your craft', 'Recoil **21% better**',
                                       '-# Pick every quality to see the rest. Best possible: Recoil 36% better'
                                       ' · Impact Force 8% better · Fire Rate 12% better']
    whole = recipe.layout_blocks(1, {}, {'0.0': 1000, '1.0': 521, '2.0': 325}, OPTIONS)
    assert whole[-1].splitlines() == ['### Your craft',
                                      'Recoil **21% better** · Impact Force **3% worse** · Fire Rate **4% worse**',
                                      '-# Stats multiply across materials']


def test_a_material_without_stats_or_menus_and_a_one_item_amount_read_plainly():
    raw = rifle()
    child = raw['requirement_groups'][2]['children'][0]
    child.update(kind='item', uuid='carinite-item', name='Carinite', quantity=1, quantity_scu=None, modifiers=[])
    raw['requirement_groups'][2]['modifiers'] = []
    raw['requirement_groups'][2]['name'] = 'Power Regulator'
    recipe = crafting.Recipe.parse(raw)
    blocks = recipe.layout_blocks(1, {}, {}, {'0.0': (325, 1000)})
    assert blocks[3].splitlines() == ['### Power Regulator · Carinite · 1 item', "-# Quality doesn't change any stat"]
    assert blocks[2].splitlines()[1] == 'Quality not picked · none on record to pick from', "no menu for it"
    assert blocks[2].splitlines()[2] == '-# Recoil (smoothness, handling, kick): 20% worse at 0 → 20% better at 1000'


def test_top_quality_is_only_called_best_when_every_stat_improves_with_it():
    raw = rifle()
    for modifier in raw['requirement_groups'][2]['modifiers']:
        if modifier.get('label') == 'Fire Rate':
            modifier['modifier_range'] = {'at_min_quality': 1.12, 'at_max_quality': 0.88}
    result = crafting.Recipe.parse(raw).layout_blocks(1, {}, {}, OPTIONS)[-1]
    assert '. At top quality: ' in result and 'Fire Rate 12% worse' in result


def test_a_stat_with_an_unsupported_curve_says_its_effect_isnt_published():
    raw = rifle()
    for modifier in raw['requirement_groups'][2]['modifiers']:
        if modifier.get('label') == 'Fire Rate':
            modifier['value_segments'] = [{'quality': 0, 'value': 0.9}, {'quality': 1000, 'value': 1.1}]
    recipe = crafting.Recipe.parse(raw)
    assert "-# Fire Rate: changes with quality; the exact effect isn't published" in recipe.layout_blocks(
        1, {}, {}, OPTIONS)[3].splitlines()
    assert "Fire Rate: the exact effect isn't published" in recipe.layout_blocks(
        1, {}, {'2.0': 1000}, OPTIONS)[3].splitlines()


def test_a_quality_menu_option_says_what_that_quality_does():
    recipe = crafting.Recipe.parse(rifle())
    frame, _, barrel = recipe.inputs
    assert crafting.quality_choice_label(frame, 1000) == 'Iron 1000 — Recoil 20% better'
    assert crafting.quality_choice_label(barrel, 325) == 'Iron 325 — Impact Force 3% worse; Fire Rate 4% worse'
