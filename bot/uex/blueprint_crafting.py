"""Pure recipe, quality and shopping-list calculations from Wiki detail responses.

Requirements are a selection tree, never the flattened `ingredients` inventory.
Stat multipliers multiply in requirement-path order, matching Wiki blueprintTuning.
Quantized ore qualities are read from matching material UUIDs, not generic sliders.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any


UNAVAILABLE = 'Crafting requirements temporarily unavailable.'


def number(value: Any) -> Decimal:
    if value is None or isinstance(value, bool):
        raise ValueError('Missing or invalid quantity')
    try:
        result = Decimal(str(value))
    except InvalidOperation as exc:
        raise ValueError('Invalid quantity') from exc
    if not result.is_finite():
        raise ValueError('Non-finite quantity')
    return result


def craft_count(value: int) -> int:
    if type(value) is not int or not 1 <= value <= 10000:
        raise ValueError('Craft count must be a whole number from 1 to 10000.')
    return value


@dataclass(frozen=True)
class Ingredient:
    path: str
    identity: str
    name: str
    aspect: str
    unit: str
    amount: Decimal
    ore_uuid: str | None
    min_quality: Decimal
    quality_values: tuple[int, ...]
    modifiers: tuple[dict, ...]


@dataclass(frozen=True)
class Group:
    path: str
    name: str
    required: int
    children: tuple[str, ...]


@dataclass(frozen=True)
class Recipe:
    uuid: str
    name: str
    game_version: str
    inputs: tuple[Ingredient, ...]
    groups: tuple[Group, ...]
    roots: tuple[str, ...]

    @classmethod
    def parse(cls, detail: dict) -> 'Recipe':
        if not isinstance(detail, dict) or not all(isinstance(detail.get(k), str) and detail[k].strip()
                                                  for k in ('uuid', 'output_name', 'game_version')):
            raise ValueError('Missing recipe identity or game version')
        roots = detail.get('requirement_groups')
        if not isinstance(roots, list) or not roots or len(roots) > 100:
            raise ValueError('Missing requirement groups')
        inputs, groups = [], []
        aspect_qualities: dict[tuple[str, str], tuple[int, ...]] = {}
        aspect_body = detail.get('aspects') if isinstance(detail.get('aspects'), dict) else {}
        for aspect in aspect_body.get('aspects') or []:
            if not isinstance(aspect, dict) or not isinstance(aspect.get('input'), dict):
                continue
            key, identity = aspect.get('key'), aspect['input'].get('uuid')
            values = (aspect.get('slider_min'), aspect.get('initial_quality'), aspect.get('slider_max'))
            if isinstance(key, str) and isinstance(identity, str) and all(type(v) is int for v in values):
                low, initial, high = values
                presets = set(values)
                if 0 <= low <= high:
                    presets.update(range(((low + 49) // 50) * 50, high + 1, 50))
                aspect_qualities[(key, identity)] = tuple(sorted(presets))

        def has_curve(modifier: dict) -> bool:
            if not (modifier.get('property_key') or modifier.get('property_uuid')):
                return False
            if modifier.get('value_segments'):
                return True
            quality_range, modifier_range = modifier.get('quality_range'), modifier.get('modifier_range')
            if not isinstance(quality_range, dict) or not isinstance(modifier_range, dict):
                return False
            try:
                tuple(number(quality_range[k]) for k in ('min', 'max'))
                tuple(number(modifier_range[k]) for k in ('at_min_quality', 'at_max_quality'))
            except (KeyError, ValueError):
                return False
            return True

        def visit(node, path, aspect='', modifiers=(), depth=0, aspect_key=''):
            if not isinstance(node, dict) or depth > 12 or len(inputs) + len(groups) > 200:
                raise ValueError('Invalid requirement tree')
            own_modifiers = node.get('modifiers') or []
            if not isinstance(own_modifiers, list) or not all(isinstance(m, dict) for m in own_modifiers):
                raise ValueError('Invalid modifiers')
            inherited = (*modifiers, *(modifier for modifier in own_modifiers if has_curve(modifier)))
            if node.get('kind') in ('group', 'root'):
                children = node.get('children')
                required = node.get('required_count')
                if not isinstance(children, list) or not children or len(children) > 25:
                    raise ValueError('Empty requirement group')
                if required is None and node.get('kind') == 'root':
                    required = len(children)
                if type(required) is not int or not 1 <= required <= len(children):
                    raise ValueError('Invalid required_count')
                paths = tuple(f'{path}.{i}' for i in range(len(children)))
                name = str(node.get('name') or node.get('key') or 'Inputs')
                key = str(node.get('key') or aspect_key)
                groups.append(Group(path, name, required, paths))
                for child, child_path in zip(children, paths):
                    visit(child, child_path, name, inherited, depth + 1, key)
                return
            kind, identity, name = node.get('kind'), node.get('uuid'), node.get('name')
            if kind not in ('resource', 'item') or not isinstance(identity, str) or not identity or not isinstance(name, str) or not name:
                raise ValueError('Missing stable ingredient identity')
            scu, count = node.get('quantity_scu'), node.get('quantity')
            if (scu is None) == (count is None):
                raise ValueError('Ingredient must have exactly one quantity unit')
            amount = number(scu if scu is not None else count)
            if amount <= 0 or (count is not None and amount != amount.to_integral_value()):
                raise ValueError('Invalid ingredient amount')
            minimum = number(node.get('min_quality') or 0)
            if not 0 <= minimum <= 1000:
                raise ValueError('Invalid minimum quality')
            inputs.append(Ingredient(path, f'{kind}:{identity}', name, aspect or name,
                                     'SCU' if scu is not None else 'items', amount,
                                     node.get('ore_uuid'), minimum,
                                     aspect_qualities.get((aspect_key, identity), ()) if kind == 'item' else (),
                                     inherited))

        for i, root in enumerate(roots):
            visit(root, str(i))
        return cls(detail['uuid'], detail['output_name'], detail['game_version'], tuple(inputs), tuple(groups),
                   tuple(str(i) for i in range(len(roots))))

    def selected(self, choices: dict[str, list[int]], *, complete=True) -> tuple[Ingredient, ...]:
        groups = {g.path: g for g in self.groups}
        inputs = {i.path: i for i in self.inputs}
        selected = []

        def walk(path):
            if path in inputs:
                selected.append(inputs[path])
                return
            group = groups[path]
            indexes = choices.get(path, list(range(len(group.children))) if group.required == len(group.children) else [])
            if (len(set(indexes)) != len(indexes) or any(type(i) is not int or i < 0 or i >= len(group.children) for i in indexes)
                    or len(indexes) != group.required):
                if complete:
                    raise ValueError(f'Choose {group.required} input(s) for {group.name}.')
                return
            for index in sorted(indexes):
                walk(group.children[index])

        for path in self.roots:
            walk(path)
        return tuple(selected)

    def plan(self, count: int, choices: dict, qualities: dict) -> dict:
        craft_count(count)
        ingredients = []
        for item in self.selected(choices):
            quality = qualities.get(item.path)
            if quality is not None:
                value = number(quality)
                if not item.min_quality <= value <= 1000:
                    raise ValueError('Quality outside recipe limits')
                # Range-valid isn't enough for an ingredient with real discrete quality
                # steps (e.g. multiples of 50 for an item-kind ingredient with matching
                # aspect data) - Discord's own dropdown never offers anything else, so this
                # is defense-in-depth for a non-UI caller, not a live-reachable gap.
                if item.quality_values and value not in {Decimal(v) for v in item.quality_values}:
                    raise ValueError('Quality outside recipe limits')
            ingredients.append({'identity': item.identity, 'name': item.name, 'unit': item.unit,
                                'amount': str(item.amount * count), 'quality': quality, 'aspect': item.aspect,
                                'path': item.path})
        return {'blueprint_uuid': self.uuid, 'name': self.name, 'game_version': self.game_version,
                'craft_count': count, 'choices': {k: list(v) for k, v in choices.items()},
                'qualities': dict(qualities), 'ingredients': ingredients}

    def modifiers(self, choices: dict, qualities: dict) -> dict[str, Decimal | None]:
        result = {}
        for item in self.selected(choices):
            for modifier in item.modifiers:
                key = modifier.get('property_key') or modifier.get('property_uuid')
                if not key:
                    continue
                value = modifier_at(modifier, qualities.get(item.path))
                previous = result.get(key, Decimal(1))
                result[key] = None if previous is None or value is None else previous * value
        return result

    def layout_blocks(self, count: int, choices: dict | None = None, qualities: dict | None = None,
                      options: dict[str, tuple[int, ...]] | None = None, *, note: str | None = None) -> tuple[str, ...]:
        """The Configure crafting reply as text blocks, shown divided by lines in a layout (the
        owner's pick from real-data mockups, 2026-10-04): a header, a section per material saying
        what its quality does, then the combined result ("Your craft"). Before a quality is picked
        a material shows what it can do across the qualities on offer, never "unavailable". The
        markdown reads the same as a plain message, so the blocks joined by blank lines are the
        text version.

        `options` is each input's pickable qualities (Blueprints.quality_options, floored at its
        minimum); a material without any has no quality menu, and its ranges use the stat curve's
        own ends. `note` is one more small-print header line (the menus' page)."""
        craft_count(count)
        choices, qualities, options = choices or {}, qualities or {}, options or {}
        header = [f'## {self.name}',
                  f"-# Configure crafting · {count} craft{'s' if count != 1 else ''} · game {self.game_version.split('-')[0]}"]
        if any(item.modifiers and options.get(item.path) for item in self.inputs):
            header.append('-# Pick a quality for each material below; stats change with it.')
        if note:
            header.append(f'-# {note}')
        blocks = ['\n'.join(header)]
        for group in self.groups:
            if group.required < len(group.children) and group.path not in choices:
                names = [next((x.name for x in self.inputs if x.path == p),
                              next((g.name for g in self.groups if g.path == p), p)) for p in group.children]
                blocks.append(f'### {group.name}\nChoose {group.required} of: ' + ', '.join(names))
        for item in self.selected(choices, complete=False):
            blocks.append(_material_block(item, count, qualities.get(item.path), options.get(item.path, ())))
        blocks.append(self._result_block(choices, qualities, options))
        return tuple(blocks)

    def _stat(self, key: str) -> dict:
        return next((m for i in self.inputs for m in i.modifiers
                     if (m.get('property_key') or m.get('property_uuid')) == key), {})

    def _summary(self, totals: dict[str, Decimal | None], *, bold: bool = False) -> str:
        """'Recoil **29% better** · Impact Force **11% better**': stats of one family that come out
        the same (recoil smoothness, handling and kick) are said once."""
        parts: list[list] = []
        for key, value in totals.items():
            if value is None:
                continue
            modifier = self._stat(key)
            label = modifier.get('label') or key
            change = describe_change(value, modifier.get('better_when'))
            words = label.split()
            for part in parts:
                if part[1] == change and len(words) == 2 and part[2] == words[0]:
                    part[0] = words[0]
                    break
            else:
                parts.append([label, change, words[0] if len(words) == 2 else None])
        return ' · '.join(f'{label} **{change}**' if bold else f'{label} {change}' for label, change, _ in parts)

    def _result_block(self, choices: dict, qualities: dict, options: dict) -> str:
        lines = ['### Your craft']
        try:
            totals = self.modifiers(choices, qualities)
        except ValueError:
            return '\n'.join([*lines, '-# Choose the materials above first.'])
        if not totals:
            return '\n'.join([*lines, "-# Material quality doesn't change this item's stats."])
        known = {key: value for key, value in totals.items() if value is not None}
        if known:
            lines.append(self._summary(known, bold=True))
        selected = self.selected(choices)
        if len(known) < len(totals):
            top = {item.path: (options.get(item.path) or (1000,))[-1] for item in selected}
            helps = all(_helps(modifier) for item in selected for modifier in item.modifiers)
            label = 'Best possible' if helps else 'At top quality'
            lines.append(f"-# Pick every quality to see {'the rest' if known else 'it'}. "
                         f'{label}: {self._summary(self.modifiers(choices, top))}')
        elif sum(1 for item in selected if item.modifiers) > 1:
            lines.append('-# Stats multiply across materials')
        return '\n'.join(lines)


def describe_change(value: Decimal, better_when: str | None) -> str:
    """A stat multiplier as a player reads it: '29% better', '11% worse', 'no change' (whole
    percent; '% higher'/'% lower' for a stat with no better direction)."""
    change = (value - Decimal(1)) * 100
    if abs(change) < Decimal('0.5'):
        return 'no change'
    if better_when in ('higher', 'lower'):
        good = (change > 0) == (better_when == 'higher')
        return f"{abs(change):.0f}% {'better' if good else 'worse'}"
    return f"{abs(change):.0f}% {'higher' if change > 0 else 'lower'}"


def _curve(modifier: dict) -> tuple:
    return (repr(modifier.get('quality_range')), repr(modifier.get('modifier_range')), modifier.get('better_when'),
            modifier.get('value_range_type'), repr(modifier.get('value_segments')))


def stat_groups(item: Ingredient) -> list[tuple[str, dict]]:
    """A material's stats as (label, modifier), stats with the same curve on one line:
    'Recoil (smoothness, handling, kick)'."""
    groups: list[tuple[list[str], dict]] = []
    for modifier in item.modifiers:
        label = modifier.get('label') or modifier.get('property_key') or 'Stat'
        for labels, first in groups:
            if _curve(first) == _curve(modifier):
                labels.append(label)
                break
        else:
            groups.append(([label], modifier))
    named = []
    for labels, modifier in groups:
        words = [label.split() for label in labels]
        if len(labels) > 1 and all(len(w) == 2 and w[0] == words[0][0] for w in words):
            name = f"{words[0][0]} ({', '.join(w[1].lower() for w in words)})"
        else:
            name = ' / '.join(labels)
        named.append((name, modifier))
    return named


def _change_at(modifier: dict, quality) -> str | None:
    value = modifier_at(modifier, quality)
    return None if value is None else describe_change(value, modifier.get('better_when'))


def _helps(modifier: dict) -> bool:
    """True when this stat improves as quality rises (the top quality is the best one)."""
    low, high = _quality_span(modifier, ())
    at_low, at_high = modifier_at(modifier, low), modifier_at(modifier, high)
    if at_low is None or at_high is None or modifier.get('better_when') not in ('higher', 'lower'):
        return False
    return at_high >= at_low if modifier['better_when'] == 'higher' else at_high <= at_low


def _quality_span(modifier: dict, values: tuple[int, ...]) -> tuple[int, int]:
    """The qualities a range is shown across: those on offer, else the stat curve's own ends."""
    if values:
        return values[0], values[-1]
    try:
        return tuple(int(number(modifier['quality_range'][k])) for k in ('min', 'max'))
    except (KeyError, TypeError, ValueError):
        return 0, 1000


def quality_choice_label(item: Ingredient, quality: int) -> str:
    """A quality menu option, 'Tungsten 858 — Recoil 29% better', so the closed menu says what was
    picked and what it does."""
    effects = '; '.join(f"{label.split(' (')[0]} {change}" for label, modifier in stat_groups(item)
                        if (change := _change_at(modifier, quality)) is not None)
    return (f'{item.name} {quality}' + (f' — {effects}' if effects else ''))[:100]


def _material_block(item: Ingredient, count: int, quality, values: tuple[int, ...]) -> str:
    amount = item.amount * count
    unit = 'item' if item.unit == 'items' and amount == 1 else item.unit
    title = item.name if item.aspect == item.name else f'{item.aspect} · {item.name}'
    lines = [f'### {title} · {amount:g} {unit}']
    if not item.modifiers:
        return '\n'.join([*lines, "-# Quality doesn't change any stat"])
    span = f'{values[0]}–{values[-1]}' if values else None
    mined = 'mined at ' if item.ore_uuid else ''
    if quality is None:
        lines.append(f'Quality not picked · {mined}`{span}`' if span else 'Quality not picked · none on record to pick from')
        for label, modifier in stat_groups(item):
            low, high = _quality_span(modifier, values)
            at_low, at_high = _change_at(modifier, low), _change_at(modifier, high)
            lines.append(f'-# {label}: {at_low} at {low} → {at_high} at {high}' if at_low and at_high
                         else f"-# {label}: changes with quality; the exact effect isn't published")
    else:
        lines.append(f'Quality `{quality}`' + (f' · mined at {span}' if span and item.ore_uuid else ''))
        for label, modifier in stat_groups(item):
            change = _change_at(modifier, quality)
            lines.append(f'{label} **{change}**' if change else f"{label}: the exact effect isn't published")
    return '\n'.join(lines)


def modifier_at(modifier: dict, quality) -> Decimal | None:
    if quality is None:
        return None
    try:
        if modifier.get('value_segments') or modifier.get('value_range_type') not in (None, 'linear'):
            return None  # never invent a linear curve for an unsupported response
        low, high = (number(modifier['quality_range'][k]) for k in ('min', 'max'))
        start, end = (number(modifier['modifier_range'][k]) for k in ('at_min_quality', 'at_max_quality'))
        q = min(high, max(low, number(quality)))
        return end if high == low else start + (end - start) * (q - low) / (high - low)
    except (ValueError, KeyError, TypeError):
        return None


def obtainable_qualities(detail: dict, ore_uuid: str) -> tuple[int, ...]:
    values = set()

    def walk(value):
        if isinstance(value, dict):
            if value.get('uuid') == ore_uuid and value.get('is_current') is not False:
                for quality in value.get('quality_quantized_values') or []:
                    if type(quality) is int and 0 <= quality <= 1000:
                        values.add(quality)
            for child in value.values():
                walk(child)
        elif isinstance(value, list):
            for child in value:
                walk(child)

    if isinstance(detail, dict) and detail.get('uuid') == ore_uuid:
        walk(detail.get('locations', []))
    return tuple(sorted(values))


def aggregate(plans: list[dict]) -> list[dict]:
    totals = {}
    for plan in plans:
        for item in plan['ingredients']:
            # Version and quality remain separate purchasing requirements too.
            key = (plan['game_version'], item['identity'], item['unit'], item.get('quality'))
            if key not in totals:
                totals[key] = dict(item, game_version=plan['game_version'], amount=Decimal(0))
            totals[key]['amount'] += number(item['amount'])
    return [dict(row, amount=str(row['amount'])) for _, row in sorted(totals.items(), key=lambda p: str(p[0]))]
