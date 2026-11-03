"""Typo-tolerant ore names for /where-to-mine and /refinery-advisor. Ported from aiv2 commit c3c14ec.

Both commands autocomplete their ore, but a player can still send what they typed without
picking a suggestion - and 'Quantanium' then failed outright. The two lookups now end in a
typo tier (resolve_raw_material_name). Checked against the real raw-material list in
tests/fixtures/raw_materials.json (UEX /commodities, 2026-10-01).
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock

import pytest
from cryptography.fernet import Fernet

from bot.cogs.mining_locations import MiningLocations
from bot.cogs.refinery import Refinery
from bot.db.database import Database
from bot.uex.mining_locations import resolve_mineable_commodity
from bot.uex.refinery import resolve_raw_commodity
from bot.uex.trading import resolve_raw_material_name, without_form_tag

COMMODITIES = json.loads(
    (Path(__file__).parent / "fixtures" / "raw_materials.json").read_text(encoding="utf-8"))["commodities"]


@pytest.mark.parametrize("resolve", [resolve_mineable_commodity, resolve_raw_commodity], ids=["mine", "refine"])
@pytest.mark.parametrize("typed", ["Quantanium", "quantanium", "quantainum", "quantanium ore", "Quantainium"])
def test_quantainium_typos_resolve_in_both_ore_lookups(resolve, typed):
    assert resolve(COMMODITIES, typed)["name"] == "Quantainium (Raw)"


@pytest.mark.parametrize("resolve", [resolve_mineable_commodity, resolve_raw_commodity], ids=["mine", "refine"])
def test_no_single_typo_of_a_real_ore_resolves_to_a_different_ore(resolve):
    """Every real raw material with one letter dropped or two adjacent letters swapped: it may
    decline, but it must never land on another ore - a wrong ore shown is worse than asking."""
    wrong, right = [], 0
    for commodity in COMMODITIES:
        base = without_form_tag(commodity["name"])
        if len(base) < 5:
            continue
        for i in range(1, len(base) - 1):
            for typo in (base[:i] + base[i + 1:], base[:i] + base[i + 1] + base[i] + base[i + 2:]):
                if typo.lower() == base.lower():
                    continue
                hit = resolve(COMMODITIES, typo)
                if hit is None:
                    continue
                if without_form_tag(hit["name"]).lower() == base.lower():
                    right += 1
                else:
                    wrong.append((typo, hit["name"], commodity["name"]))
    assert wrong == []
    assert right > 300, "the typo tier should resolve most single typos"


def test_the_typo_tier_declines_when_two_real_names_are_too_close_to_call():
    assert resolve_raw_material_name("aranite", ["Laranite (Raw)", "Taranite (Raw)", "Caranite (Raw)"]) is None


def test_the_typo_tier_never_merges_two_real_materials_sharing_a_base_name():
    assert resolve_raw_material_name("corundm", ["Corundum (Raw)", "Corundum (Ore)"]) is None


def test_a_typo_of_a_non_refinable_ore_never_lands_on_a_refinable_one():
    # 'Ahorite' is Aphorite (not refinable); scored against refinable ores alone it resolved to Torite.
    assert resolve_raw_commodity(COMMODITIES, "Ahorite") is None
    assert resolve_raw_material_name("Ahorite", ["Torite (Ore)"], compete_with=["Aphorite", "Torite (Ore)"]) is None
    # /where-to-mine takes every raw material, so there it's simply Aphorite.
    assert resolve_mineable_commodity(COMMODITIES, "Ahorite")["name"] == "Aphorite"


def test_without_form_tag_strips_the_raw_or_ore_tag():
    assert without_form_tag("Quantainium (Raw)") == "Quantainium"
    assert without_form_tag("Gold (Ore)") == "Gold"
    assert without_form_tag("Laranite") == "Laranite"


def test_nonsense_still_finds_nothing():
    for resolve in (resolve_mineable_commodity, resolve_raw_commodity):
        assert resolve(COMMODITIES, "zzzzzz") is None
        assert resolve(COMMODITIES, "   ") is None


# -- through the real commands ------------------------------------------------------------

def _interaction():
    return NS(response=NS(defer=AsyncMock()), followup=NS(send=AsyncMock()))


def test_where_to_mine_answers_a_typed_typo():
    async def run():
        cog = MiningLocations.__new__(MiningLocations)
        cog.bot = NS(uex=NS(
            get_commodities=AsyncMock(return_value=COMMODITIES),
            get_star_systems=AsyncMock(return_value=[]), get_planets=AsyncMock(return_value=[]),
            get_moons=AsyncMock(return_value=[]), get_poi=AsyncMock(return_value=[]),
        ))
        interaction = _interaction()
        await cog.where_to_mine.callback(cog, interaction, ore="Quantanium")
        return interaction.followup.send.call_args

    call = asyncio.run(run())
    assert call.kwargs["embed"].title == "Quantainium (Raw) — Where to Mine"


def test_refinery_advisor_answers_a_typed_typo(tmp_path):
    async def run():
        db = Database(tmp_path / "ore_typos.sqlite3", Fernet(Fernet.generate_key()))
        await db.init()
        cog = Refinery.__new__(Refinery)

        async def no_prices(**kwargs):
            return []

        cog.bot = NS(db=db, uex=NS(
            get_commodities=AsyncMock(return_value=COMMODITIES),
            get_refineries_methods=AsyncMock(return_value=[]),
            get_commodities_prices=no_prices,
            get_star_systems=AsyncMock(return_value=[]),
        ))
        interaction = _interaction()
        await cog.refinery_advisor.callback(cog, interaction, ore_1="Quantanium", ore_2=None, ore_3=None)
        return interaction.followup.send.call_args

    call = asyncio.run(run())
    assert "embed" in call.kwargs, call
    assert call.kwargs["embed"].title.startswith("Quantainium (Raw)")
