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


def test_modifier_display_is_a_semantic_percentage_change():
    assert crafting.format_modifier(Decimal('0.976'), 'lower') == '2.40% improvement (lower)'
    assert crafting.format_modifier(Decimal('1.0918'), 'higher') == '9.18% improvement (higher)'
    assert crafting.format_modifier(Decimal('0.9'), 'higher') == '10.00% worse (lower)'
    assert crafting.format_modifier(Decimal('1'), 'higher') == '0.00% change'
