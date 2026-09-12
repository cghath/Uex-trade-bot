"""Refinery Advisor: pure matching/ranking logic (bot/uex/refinery.py), the DB's
latest-day yield lookup, and the /refinery-advisor command end to end."""
from __future__ import annotations

import asyncio
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock

from cryptography.fernet import Fernet

from bot.cogs.refinery import Refinery
from bot.db.database import Database
from bot.uex.refinery import (
    display_terminal_name,
    high_yield_refining_methods,
    rank_refinery_terminals,
    resolve_raw_commodity,
)


def _make_db(tmp_path) -> Database:
    return Database(tmp_path / "refinery.sqlite3", Fernet(Fernet.generate_key()))


def _raw(id, name, id_parent, **overrides):
    base = dict(id=id, id_parent=id_parent, name=name, is_raw=1, is_refinable=1)
    base.update(overrides)
    return base


def _refined(id, name):
    return dict(id=id, id_parent=None, name=name, is_raw=0, is_refinable=0, is_refined=1)


# -- resolve_raw_commodity ----------------------------------------------------------

def test_resolve_raw_commodity_matches_exact_name_case_insensitively():
    commodities = [_raw(1, "Quantainium (Raw)", 100)]
    match = resolve_raw_commodity(commodities, "quantainium (raw)")
    assert match is not None and match["id"] == 1


def test_resolve_raw_commodity_ignores_a_same_named_commodity_that_isnt_refinable():
    """A commodity flagged is_raw but NOT is_refinable (or vice versa) must not match -
    only the exact set /refinery-advisor's own autocomplete suggests."""
    commodities = [_raw(1, "Gold", 100, is_refinable=0)]
    assert resolve_raw_commodity(commodities, "Gold") is None


def test_resolve_raw_commodity_matches_a_unique_substring():
    commodities = [_raw(1, "Quantainium (Raw)", 100), _raw(2, "Beryl (Raw)", 200)]
    match = resolve_raw_commodity(commodities, "quanta")
    assert match is not None and match["id"] == 1


def test_resolve_raw_commodity_refuses_an_ambiguous_substring():
    commodities = [_raw(1, "Beryl (Raw)", 200), _raw(2, "Bexalite (Raw)", 300)]
    assert resolve_raw_commodity(commodities, "be") is None


def test_resolve_raw_commodity_empty_query_returns_none():
    assert resolve_raw_commodity([_raw(1, "Gold Ore", 100)], "   ") is None


# -- high_yield_refining_methods ------------------------------------------------------

def test_high_yield_refining_methods_keeps_only_rating_yield_three():
    methods = [
        dict(name="Cormack", rating_yield=1, rating_cost=2, rating_speed=3),
        dict(name="Dinyx Solventation", rating_yield=3, rating_cost=1, rating_speed=1),
        dict(name="Pyrometric Chromalysis", rating_yield=3, rating_cost=3, rating_speed=1),
    ]
    result = high_yield_refining_methods(methods)
    assert [m["name"] for m in result] == ["Dinyx Solventation", "Pyrometric Chromalysis"]


def test_high_yield_refining_methods_sorts_cheapest_then_fastest():
    methods = [
        dict(name="High cost, fast", rating_yield=3, rating_cost=3, rating_speed=3),
        dict(name="Low cost, slow", rating_yield=3, rating_cost=1, rating_speed=1),
        dict(name="Low cost, fast", rating_yield=3, rating_cost=1, rating_speed=3),
    ]
    result = high_yield_refining_methods(methods)
    assert [m["name"] for m in result] == ["Low cost, fast", "Low cost, slow", "High cost, fast"]


# -- display_terminal_name -------------------------------------------------------------

def test_display_terminal_name_appends_the_system_when_not_already_present():
    """Most /refineries_yields terminal_name values (e.g. "Refinement Center - Levski")
    carry no system information at all - the system must be appended so every
    recommendation is equally clear about where to fly."""
    assert display_terminal_name("Refinement Center - Levski", "Nyx") == "Refinement Center - Levski (Nyx)"


def test_display_terminal_name_skips_a_system_already_embedded_in_the_name():
    """Gateway terminals disambiguate same-named gateways across systems by embedding the
    system directly in terminal_name (e.g. two different "Nyx Gateway" terminals exist,
    one in Pyro and one in Stanton) - appending again would read as
    "Nyx Gateway (Stanton) (Stanton)"."""
    name = display_terminal_name("Refinement Processing - Nyx Gateway (Stanton)", "Stanton")
    assert name == "Refinement Processing - Nyx Gateway (Stanton)"


def test_display_terminal_name_returns_the_bare_name_when_system_is_unknown():
    assert display_terminal_name("Refinement Center - Levski", None) == "Refinement Center - Levski"


# -- rank_refinery_terminals ----------------------------------------------------------

def test_rank_refinery_terminals_sums_scores_across_requested_commodities():
    yield_rows_by_commodity = {
        "Ore A": [
            {"id_terminal": 1, "terminal_name": "T1", "star_system_name": "Stanton", "yield_bonus": 10},
            {"id_terminal": 2, "terminal_name": "T2", "yield_bonus": 8},
        ],
        "Ore B": [
            {"id_terminal": 1, "terminal_name": "T1", "star_system_name": "Stanton", "yield_bonus": 5},
            {"id_terminal": 3, "terminal_name": "T3", "yield_bonus": 20},
        ],
    }
    ranked = rank_refinery_terminals(yield_rows_by_commodity, limit=5)
    assert [t.terminal_name for t in ranked] == ["T3", "T1", "T2"]
    t1 = next(t for t in ranked if t.terminal_name == "T1")
    assert t1.combined_score == 15
    assert t1.per_commodity == {"Ore A": 10, "Ore B": 5}
    assert t1.star_system_name == "Stanton"
    t2 = next(t for t in ranked if t.terminal_name == "T2")
    assert "Ore B" not in t2.per_commodity, "a terminal with no data for an ore must not fabricate one"


def test_rank_refinery_terminals_respects_the_limit():
    yield_rows_by_commodity = {
        "Ore A": [{"id_terminal": i, "terminal_name": f"T{i}", "yield_bonus": i} for i in range(10)],
    }
    ranked = rank_refinery_terminals(yield_rows_by_commodity, limit=3)
    assert len(ranked) == 3
    assert [t.terminal_name for t in ranked] == ["T9", "T8", "T7"]


def test_rank_refinery_terminals_breaks_ties_by_terminal_name():
    yield_rows_by_commodity = {
        "Ore A": [
            {"id_terminal": 2, "terminal_name": "Zed", "yield_bonus": 10},
            {"id_terminal": 1, "terminal_name": "Alpha", "yield_bonus": 10},
        ],
    }
    ranked = rank_refinery_terminals(yield_rows_by_commodity, limit=5)
    assert [t.terminal_name for t in ranked] == ["Alpha", "Zed"]


# -- Database.get_latest_refinery_yields_for_commodity ---------------------------------

def test_get_latest_refinery_yields_for_commodity_only_returns_the_most_recent_day(tmp_path):
    """Seeds an older-dated row with a HIGHER yield bonus directly (bypassing
    record_refinery_yield_snapshot's own date('now') write) to prove the query actually
    filters by the latest recorded_day - not just sorting all history by yield_bonus,
    which this stale row would otherwise win."""
    async def run():
        db = _make_db(tmp_path)
        await db.init()
        await db.record_refinery_yield_snapshot([
            {"id_commodity": 1, "id_terminal": 10, "commodity_name": "Quantainium (Raw)",
             "terminal_name": "Today Refinery", "value": 5, "value_week": 5, "value_month": 5},
        ])
        async with db.connect() as conn:
            await conn.execute(
                """INSERT INTO refinery_yield_observations
                   (id_commodity, id_terminal, recorded_day, commodity_name, terminal_name, yield_bonus)
                   VALUES (1, 20, '2020-01-01', 'Quantainium (Raw)', 'Old Refinery', 99)"""
            )
            await conn.commit()

        rows = await db.get_latest_refinery_yields_for_commodity(1)

        assert [r["terminal_name"] for r in rows] == ["Today Refinery"], (
            "the stale higher-yield row from a previous day must not be included"
        )

    asyncio.run(run())


def test_get_latest_refinery_yields_for_commodity_is_empty_when_never_collected(tmp_path):
    async def run():
        db = _make_db(tmp_path)
        await db.init()
        assert await db.get_latest_refinery_yields_for_commodity(999) == []

    asyncio.run(run())


# -- /refinery-advisor command end to end -----------------------------------------------

class _FakeInteraction:
    def __init__(self) -> None:
        self.response = NS(defer=AsyncMock())
        self.followup = NS(send=AsyncMock())


def _cog(db, *, commodities, methods=None, price_rows_by_commodity=None):
    cog = Refinery.__new__(Refinery)

    async def get_commodities_prices(**kwargs):
        name = kwargs.get("commodity_name")
        return (price_rows_by_commodity or {}).get(name, [])

    cog.bot = NS(
        db=db,
        uex=NS(
            get_commodities=AsyncMock(return_value=commodities),
            get_refineries_methods=AsyncMock(return_value=methods or []),
            get_commodities_prices=get_commodities_prices,
        ),
    )
    return cog


_METHODS = [
    {"name": "Cormack", "rating_yield": 1, "rating_cost": 2, "rating_speed": 3},
    {"name": "Dinyx Solventation", "rating_yield": 3, "rating_cost": 1, "rating_speed": 1},
]


def test_refinery_advisor_happy_path_for_one_ore(tmp_path):
    async def run():
        db = _make_db(tmp_path)
        await db.init()
        await db.record_refinery_yield_snapshot([
            {"id_commodity": 1, "id_terminal": 10, "commodity_name": "Quantainium (Raw)",
             "terminal_name": "Levski Refinery", "star_system_name": "Nyx", "value": 5},
        ])
        commodities = [_raw(1, "Quantainium (Raw)", 100), _refined(100, "Quantainium")]
        cog = _cog(
            db, commodities=commodities, methods=_METHODS,
            price_rows_by_commodity={
                "Quantainium": [{"terminal_name": "Levski", "price_sell": 9000.0, "id_terminal": 10}],
            },
        )
        interaction = _FakeInteraction()

        await cog.refinery_advisor.callback(cog, interaction, ore_1="Quantainium (Raw)", ore_2=None, ore_3=None)

        interaction.response.defer.assert_awaited_once()
        embed = interaction.followup.send.call_args.kwargs["embed"]
        assert "Quantainium (Raw)" in embed.title
        fields = {f.name: f.value for f in embed.fields}
        assert "Levski Refinery (Nyx)" in fields["Best refineries by yield bonus"], (
            "the system must be shown consistently even for a terminal whose own UEX "
            "terminal_name text has no system suffix baked in"
        )
        assert "Dinyx Solventation" in fields["High-yield refining methods"]
        assert "Cormack" not in fields["High-yield refining methods"], "low-yield methods must be excluded"
        assert "Levski" in fields["Quantainium — best sell price"]
        assert "9000.00" in fields["Quantainium — best sell price"]

    asyncio.run(run())


def test_refinery_advisor_shows_the_system_consistently_across_terminals(tmp_path):
    """Reproduces the exact inconsistency a user spotted live: UEX's own terminal_name
    embeds a system suffix for a gateway terminal ("Nyx Gateway (Stanton)") but not for a
    plain one ("ARC-L1") - both must show their system the same way in the embed."""
    async def run():
        db = _make_db(tmp_path)
        await db.init()
        await db.record_refinery_yield_snapshot([
            {"id_commodity": 1, "id_terminal": 10, "commodity_name": "Quantainium (Raw)",
             "terminal_name": "Refinement Processing - Nyx Gateway (Stanton)",
             "star_system_name": "Stanton", "value": 3},
            {"id_commodity": 1, "id_terminal": 20, "commodity_name": "Quantainium (Raw)",
             "terminal_name": "Refinement Processing - ARC-L1", "star_system_name": "Stanton", "value": 3},
        ])
        commodities = [_raw(1, "Quantainium (Raw)", 100), _refined(100, "Quantainium")]
        cog = _cog(db, commodities=commodities, methods=[])
        interaction = _FakeInteraction()

        await cog.refinery_advisor.callback(cog, interaction, ore_1="Quantainium (Raw)", ore_2=None, ore_3=None)

        embed = interaction.followup.send.call_args.kwargs["embed"]
        fields = {f.name: f.value for f in embed.fields}
        best = fields["Best refineries by yield bonus"]
        assert "Nyx Gateway (Stanton)" in best and "(Stanton) (Stanton)" not in best
        assert "ARC-L1 (Stanton)" in best

    asyncio.run(run())


def test_refinery_advisor_combines_multiple_ores_from_the_same_haul(tmp_path):
    async def run():
        db = _make_db(tmp_path)
        await db.init()
        await db.record_refinery_yield_snapshot([
            {"id_commodity": 1, "id_terminal": 10, "commodity_name": "Ore A",
             "terminal_name": "Best Combined", "value": 10},
            {"id_commodity": 2, "id_terminal": 10, "commodity_name": "Ore B",
             "terminal_name": "Best Combined", "value": 5},
            {"id_commodity": 1, "id_terminal": 20, "commodity_name": "Ore A",
             "terminal_name": "Ore A Only", "value": 30},
        ])
        commodities = [
            _raw(1, "Ore A", 100), _raw(2, "Ore B", 200),
            _refined(100, "Refined A"), _refined(200, "Refined B"),
        ]
        cog = _cog(db, commodities=commodities, methods=_METHODS)
        interaction = _FakeInteraction()

        await cog.refinery_advisor.callback(cog, interaction, ore_1="Ore A", ore_2="Ore B", ore_3=None)

        embed = interaction.followup.send.call_args.kwargs["embed"]
        fields = {f.name: f.value for f in embed.fields}
        best = fields["Best refineries by yield bonus"]
        # "Ore A Only" (score 30) must outrank "Best Combined" (score 10+5=15) since the
        # ranking is a straightforward sum, not a per-commodity match count.
        assert best.index("Ore A Only") < best.index("Best Combined")
        ore_a_only_line = next(line for line in best.split("\n") if "Ore A Only" in line)
        assert "no data: Ore B" in ore_a_only_line

    asyncio.run(run())


def test_refinery_advisor_reports_unmatched_ores_without_dropping_resolved_ones(tmp_path):
    async def run():
        db = _make_db(tmp_path)
        await db.init()
        commodities = [_raw(1, "Ore A", 100), _refined(100, "Refined A")]
        cog = _cog(db, commodities=commodities, methods=[])
        interaction = _FakeInteraction()

        await cog.refinery_advisor.callback(cog, interaction, ore_1="Ore A", ore_2="Nonexistent Ore", ore_3=None)

        embed = interaction.followup.send.call_args.kwargs["embed"]
        assert "Nonexistent Ore" in embed.description
        assert "Ore A" in embed.title

    asyncio.run(run())


def test_refinery_advisor_all_ores_unmatched_sends_a_plain_message(tmp_path):
    async def run():
        db = _make_db(tmp_path)
        await db.init()
        cog = _cog(db, commodities=[_raw(1, "Ore A", 100)], methods=[])
        interaction = _FakeInteraction()

        await cog.refinery_advisor.callback(cog, interaction, ore_1="Nonexistent", ore_2=None, ore_3=None)

        message = interaction.followup.send.call_args.args[0]
        assert "Nonexistent" in message
        assert "embed" not in interaction.followup.send.call_args.kwargs

    asyncio.run(run())


def test_refinery_advisor_deduplicates_the_same_ore_entered_twice(tmp_path):
    async def run():
        db = _make_db(tmp_path)
        await db.init()
        commodities = [_raw(1, "Ore A", 100), _refined(100, "Refined A")]
        cog = _cog(db, commodities=commodities, methods=[])
        interaction = _FakeInteraction()

        await cog.refinery_advisor.callback(cog, interaction, ore_1="Ore A", ore_2="ore a", ore_3=None)

        embed = interaction.followup.send.call_args.kwargs["embed"]
        sell_price_fields = [f for f in embed.fields if f.name.startswith("Refined A")]
        assert len(sell_price_fields) == 1, "the same ore entered twice must not produce two sell-price fields"

    asyncio.run(run())
