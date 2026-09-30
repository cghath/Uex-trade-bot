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
    TerminalYield,
    display_terminal_name,
    high_yield_refining_methods,
    rank_refinery_terminals,
    resolve_raw_commodity,
    select_terminals_to_show,
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


def test_rank_refinery_terminals_defaults_to_pure_yield_ranking_when_systems_are_unknown():
    """mining_star_systems omitted (or empty) must preserve the exact original ordering -
    every terminal's in_mining_system stays None (unknown), never a fabricated False."""
    yield_rows_by_commodity = {
        "Ore A": [
            {"id_terminal": 1, "terminal_name": "High Yield", "star_system_name": "Nyx", "yield_bonus": 10},
            {"id_terminal": 2, "terminal_name": "Low Yield", "star_system_name": "Stanton", "yield_bonus": 3},
        ],
    }
    ranked = rank_refinery_terminals(yield_rows_by_commodity, limit=5)
    assert [t.terminal_name for t in ranked] == ["High Yield", "Low Yield"]
    assert all(t.in_mining_system is None for t in ranked)


def test_rank_refinery_terminals_ranks_in_system_terminals_ahead_of_higher_yield_out_of_system_ones():
    """The user-reported real case: Quantainium's own highest yield bonus is at a Nyx
    refinery, but Quantainium can only be mined in Stanton - the Nyx terminal must never
    outrank a real Stanton option just because its raw yield bonus is bigger, but it must
    also still appear (not be silently dropped) once every in-system option is listed."""
    yield_rows_by_commodity = {
        "Quantainium (Raw)": [
            {"id_terminal": 1, "terminal_name": "Levski Refinery", "star_system_name": "Nyx", "yield_bonus": 5},
            {"id_terminal": 2, "terminal_name": "ARC-L1", "star_system_name": "Stanton", "yield_bonus": 3},
            {"id_terminal": 3, "terminal_name": "ARC-L2", "star_system_name": "Stanton", "yield_bonus": 3},
        ],
    }
    ranked = rank_refinery_terminals(yield_rows_by_commodity, limit=5, mining_star_systems={"Stanton"})
    assert [t.terminal_name for t in ranked] == ["ARC-L1", "ARC-L2", "Levski Refinery"]
    levski = next(t for t in ranked if t.terminal_name == "Levski Refinery")
    assert levski.in_mining_system is False
    arc_l1 = next(t for t in ranked if t.terminal_name == "ARC-L1")
    assert arc_l1.in_mining_system is True


def test_rank_refinery_terminals_out_of_system_terminal_can_still_be_excluded_by_the_limit():
    """An out-of-system terminal is ranked LAST among its own tier, not removed outright -
    if there are already `limit` in-system terminals, it simply doesn't make the cut, the
    same way any other terminal can be truncated by a plain limit."""
    yield_rows_by_commodity = {
        "Ore A": [
            {"id_terminal": i, "terminal_name": f"T{i}", "star_system_name": "Stanton", "yield_bonus": 1}
            for i in range(5)
        ] + [{"id_terminal": 99, "terminal_name": "Out of System", "star_system_name": "Nyx", "yield_bonus": 999}],
    }
    ranked = rank_refinery_terminals(yield_rows_by_commodity, limit=5, mining_star_systems={"Stanton"})
    assert len(ranked) == 5
    assert "Out of System" not in [t.terminal_name for t in ranked]


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


def test_record_refinery_yield_snapshot_persists_uex_source_timestamps(tmp_path):
    """Audit finding: UEX's own date_added/date_modified per row were fetched but never
    stored - only this bot's own collection day (recorded_day) was kept. These are
    distinct: recorded_day says when WE last collected, date_added/date_modified say what
    UEX's own record says, letting a future look-back tell "stale on UEX's own side too"
    apart from "we just haven't re-collected recently.\""""
    async def run():
        db = _make_db(tmp_path)
        await db.init()
        await db.record_refinery_yield_snapshot([
            {"id_commodity": 1, "id_terminal": 10, "commodity_name": "Quantainium (Raw)",
             "terminal_name": "Levski Refinery", "value": 5,
             "date_added": 1700000000, "date_modified": 1700500000},
        ])
        rows = await db.get_latest_refinery_yields_for_commodity(1)
        assert rows[0]["date_added"] == 1700000000
        assert rows[0]["date_modified"] == 1700500000

    asyncio.run(run())


def test_record_refinery_yield_fetch_logs_the_response_count(tmp_path):
    """Audit finding: refresh_reference_data only ever logged a transient warning when a
    fetch hit the documented 500-row cap - nothing was persisted, so there was no way to
    look back and tell whether a past fetch was already truncated or how response size has
    trended over time."""
    async def run():
        db = _make_db(tmp_path)
        await db.init()
        await db.record_refinery_yield_fetch(215)
        await db.record_refinery_yield_fetch(500)
        async with db.connect() as conn:
            cursor = await conn.execute("SELECT response_count FROM refinery_yield_fetch_log ORDER BY id")
            rows = await cursor.fetchall()
        assert [r["response_count"] for r in rows] == [215, 500]

    asyncio.run(run())


# -- /refinery-advisor command end to end -----------------------------------------------

class _FakeInteraction:
    def __init__(self) -> None:
        self.response = NS(defer=AsyncMock())
        self.followup = NS(send=AsyncMock())


def _cog(db, *, commodities, methods=None, price_rows_by_commodity=None, star_systems=None):
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
            # Empty by default (no star-system reference data at all) rather than omitted -
            # every test below predates the cross-system disclosure feature and asserts on
            # the original pure-yield-bonus ranking, which requires mining_star_systems to
            # come back empty (-> None) so rank_refinery_terminals' new sort never activates.
            get_star_systems=AsyncMock(return_value=star_systems or []),
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


def test_refinery_advisor_flags_a_cross_system_terminal_instead_of_hiding_it(tmp_path):
    """End-to-end reproduction of the user-reported real case: Quantainium's own highest
    yield bonus is at a Nyx refinery, but the commodity's own ids_star_systems says it's
    only mineable in Stanton (id 1 here) - the command must rank the real Stanton option
    first, still SHOW the Nyx one (never silently drop a terminal with real data), and mark
    it with a visible warning plus a footer note explaining what the warning means."""
    async def run():
        db = _make_db(tmp_path)
        await db.init()
        await db.record_refinery_yield_snapshot([
            {"id_commodity": 1, "id_terminal": 10, "commodity_name": "Quantainium (Raw)",
             "terminal_name": "Levski Refinery", "star_system_name": "Nyx", "value": 5},
            {"id_commodity": 1, "id_terminal": 20, "commodity_name": "Quantainium (Raw)",
             "terminal_name": "ARC-L1", "star_system_name": "Stanton", "value": 3},
        ])
        commodities = [
            _raw(1, "Quantainium (Raw)", 100, ids_star_systems="1"),
            _refined(100, "Quantainium"),
        ]
        cog = _cog(
            db, commodities=commodities, methods=[],
            star_systems=[{"id": 1, "name": "Stanton"}, {"id": 2, "name": "Nyx"}],
        )
        interaction = _FakeInteraction()

        await cog.refinery_advisor.callback(cog, interaction, ore_1="Quantainium (Raw)", ore_2=None, ore_3=None)

        embed = interaction.followup.send.call_args.kwargs["embed"]
        fields = {f.name: f.value for f in embed.fields}
        best = fields["Best refineries by yield bonus"]
        assert best.index("ARC-L1") < best.index("Levski Refinery"), (
            "the real, reachable Stanton refinery must rank ahead of the higher-yield Nyx one"
        )
        levski_line = next(line for line in best.split("\n") if "Levski Refinery" in line)
        assert "⚠️" in levski_line, "the cross-system terminal must still appear, just flagged"
        arc_line = next(line for line in best.split("\n") if "ARC-L1" in line)
        assert "⚠️" not in arc_line, "the in-system terminal must not be flagged"
        assert "outside where this ore is actually mined" in embed.footer.text

    asyncio.run(run())


def test_refinery_advisor_omits_the_cross_system_footer_note_when_nothing_is_flagged(tmp_path):
    async def run():
        db = _make_db(tmp_path)
        await db.init()
        await db.record_refinery_yield_snapshot([
            {"id_commodity": 1, "id_terminal": 20, "commodity_name": "Quantainium (Raw)",
             "terminal_name": "ARC-L1", "star_system_name": "Stanton", "value": 3},
        ])
        commodities = [
            _raw(1, "Quantainium (Raw)", 100, ids_star_systems="1"),
            _refined(100, "Quantainium"),
        ]
        cog = _cog(db, commodities=commodities, methods=[], star_systems=[{"id": 1, "name": "Stanton"}])
        interaction = _FakeInteraction()

        await cog.refinery_advisor.callback(cog, interaction, ore_1="Quantainium (Raw)", ore_2=None, ore_3=None)

        embed = interaction.followup.send.call_args.kwargs["embed"]
        assert "outside where this ore is actually mined" not in embed.footer.text

    asyncio.run(run())


def test_refinery_advisor_guards_against_oversized_fields_instead_of_crashing(tmp_path):
    """Audit finding: /refinery-advisor built its fields with plain embed.add_field() calls,
    unlike /price - a long enough terminal name could exceed Discord's 1024-char per-field
    or 6000-char combined embed limits. Now routed through add_chunked_fields like every
    sibling command: an oversized section is omitted (noted in the footer) rather than
    raising and losing the whole response."""
    async def run():
        db = _make_db(tmp_path)
        await db.init()
        long_name = "X" * 2000
        await db.record_refinery_yield_snapshot([
            {"id_commodity": 1, "id_terminal": i, "commodity_name": "Quantainium (Raw)",
             "terminal_name": f"{long_name}{i}", "value": 5}
            for i in range(1, 6)
        ])
        commodities = [_raw(1, "Quantainium (Raw)", 100), _refined(100, "Quantainium")]
        cog = _cog(db, commodities=commodities, methods=_METHODS)
        interaction = _FakeInteraction()

        await cog.refinery_advisor.callback(cog, interaction, ore_1="Quantainium (Raw)", ore_2=None, ore_3=None)

        embed = interaction.followup.send.call_args.kwargs["embed"]
        fields = {f.name: f.value for f in embed.fields}
        assert "Best refineries by yield bonus" not in fields, "an oversized section must be omitted, not raise"
        assert "omitted" in embed.footer.text

    asyncio.run(run())


# -- select_terminals_to_show: never drop a real in-system refinery ----------------------------


def _ty(id_terminal, name, score, in_system):
    return TerminalYield(
        id_terminal=id_terminal, terminal_name=name, combined_score=score,
        star_system_name="Stanton" if in_system else "Nyx", in_mining_system=in_system,
    )


def test_select_terminals_to_show_keeps_every_in_system_terminal_past_the_minimum():
    """Real case: Quantainium has 6 Stanton refineries and the old flat top-5 cut dropped one."""
    ranked = [_ty(i, f"S{i}", 10 - i, True) for i in range(6)] + [_ty(90, "N1", 20, False)]
    shown = select_terminals_to_show(ranked, min_shown=5, max_in_system=12)
    assert [t.terminal_name for t in shown] == ["S0", "S1", "S2", "S3", "S4", "S5", "N1"]


def test_select_terminals_to_show_always_keeps_the_best_out_of_system_option_visible():
    ranked = [_ty(i, f"S{i}", 10 - i, True) for i in range(8)] + [
        _ty(90, "N1", 20, False), _ty(91, "N2", 19, False),
    ]
    shown = select_terminals_to_show(ranked, min_shown=5, max_in_system=12)
    assert shown[-1].terminal_name == "N1", "the single best cross-system terminal is kept, flagged by the caller"
    assert "N2" not in [t.terminal_name for t in shown]


def test_select_terminals_to_show_fills_to_the_minimum_when_few_terminals_are_in_system():
    ranked = [_ty(1, "S1", 5, True)] + [_ty(10 + i, f"N{i}", 9 - i, False) for i in range(6)]
    shown = select_terminals_to_show(ranked, min_shown=5, max_in_system=12)
    assert [t.terminal_name for t in shown] == ["S1", "N0", "N1", "N2", "N3"]


def test_select_terminals_to_show_caps_a_pathological_in_system_count():
    ranked = [_ty(i, f"S{i}", 100 - i, True) for i in range(20)]
    shown = select_terminals_to_show(ranked, min_shown=5, max_in_system=12)
    assert len(shown) == 12


def test_select_terminals_to_show_degrades_to_top_n_when_mining_systems_are_unknown():
    ranked = [
        TerminalYield(id_terminal=i, terminal_name=f"T{i}", combined_score=10 - i, in_mining_system=None)
        for i in range(8)
    ]
    shown = select_terminals_to_show(ranked, min_shown=5, max_in_system=12)
    assert [t.terminal_name for t in shown] == ["T0", "T1", "T2", "T3", "T4"]


def test_refinery_advisor_shows_every_in_system_refinery_and_discloses_the_rest(tmp_path):
    """End-to-end: 7 Stanton refineries + 3 higher-yield Nyx ones. All 7 Stanton must appear
    (the old top-5 cut hid two), the best Nyx one appears flagged, and the footer says the
    list is still a subset so nothing is silently omitted."""
    async def run():
        db = _make_db(tmp_path)
        await db.init()
        rows = [
            {"id_commodity": 1, "id_terminal": 100 + i, "commodity_name": "Quantainium (Raw)",
             "terminal_name": f"Stanton Refinery {i}", "star_system_name": "Stanton", "value": i}
            for i in range(7)
        ] + [
            {"id_commodity": 1, "id_terminal": 200 + i, "commodity_name": "Quantainium (Raw)",
             "terminal_name": f"Nyx Refinery {i}", "star_system_name": "Nyx", "value": 20 + i}
            for i in range(3)
        ]
        await db.record_refinery_yield_snapshot(rows)
        commodities = [_raw(1, "Quantainium (Raw)", 100, ids_star_systems="1"), _refined(100, "Quantainium")]
        cog = _cog(db, commodities=commodities, star_systems=[{"id": 1, "name": "Stanton"}, {"id": 2, "name": "Nyx"}])
        interaction = _FakeInteraction()

        await cog.refinery_advisor.callback(cog, interaction, ore_1="Quantainium (Raw)", ore_2=None, ore_3=None)

        embed = interaction.followup.send.call_args.kwargs["embed"]
        best = {f.name: f.value for f in embed.fields}["Best refineries by yield bonus"]
        for i in range(7):
            assert f"Stanton Refinery {i}" in best
        nyx_line = next(line for line in best.split("\n") if "Nyx Refinery 2" in line)
        assert "⚠️" in nyx_line
        assert "Nyx Refinery 0" not in best
        assert "Showing 8 of 10 refineries" in embed.footer.text

    asyncio.run(run())


# -- a multi-ore haul is judged against the systems where EVERY ore is mined --------------------------


def test_combine_mining_systems_leaves_a_single_ore_exactly_as_it_was():
    from bot.uex.refinery import combine_mining_systems

    one = combine_mining_systems({"Quantainium (Raw)": {"Stanton"}})
    assert one.systems == frozenset({"Stanton"}) and one.note is None
    both = combine_mining_systems({"A": {"Stanton", "Pyro"}})
    assert both.systems == frozenset({"Stanton", "Pyro"}) and both.note is None


def test_combine_mining_systems_with_nothing_known_falls_back_to_yield_only_ordering():
    from bot.uex.refinery import combine_mining_systems

    for data in ({}, {"A": set()}, {"A": set(), "B": set()}):
        result = combine_mining_systems(data)
        assert result.systems == frozenset() and result.note is None


def test_combine_mining_systems_uses_the_intersection_and_says_so_when_it_narrows_the_union():
    from bot.uex.refinery import combine_mining_systems

    result = combine_mining_systems({"A": {"Stanton"}, "B": {"Stanton", "Pyro"}})
    assert result.systems == frozenset({"Stanton"}), "a Pyro refinery is not somewhere ore A can be picked up"
    assert "Stanton" in result.note and "every one of these ores" in result.note
    assert "Pyro" not in result.note


def test_combine_mining_systems_says_nothing_when_every_ore_is_mined_in_the_same_places():
    from bot.uex.refinery import combine_mining_systems

    result = combine_mining_systems({"A": {"Stanton", "Pyro"}, "B": {"Pyro", "Stanton"}, "C": {"Stanton", "Pyro"}})
    assert result.systems == frozenset({"Stanton", "Pyro"}) and result.note is None


def test_combine_mining_systems_discloses_when_the_ores_share_no_system_and_falls_back_to_the_union():
    from bot.uex.refinery import combine_mining_systems

    result = combine_mining_systems({"Ore B": {"Pyro"}, "Ore A": {"Stanton"}})
    assert result.systems == frozenset({"Stanton", "Pyro"})
    assert "aren't mined in a common system" in result.note
    assert "Ore A: Stanton; Ore B: Pyro" in result.note, "listed per ore, in a stable order"


def test_combine_mining_systems_does_not_let_an_ore_with_unknown_systems_narrow_or_hide_anything():
    from bot.uex.refinery import combine_mining_systems

    result = combine_mining_systems({"Known": {"Stanton"}, "Mystery": set()})
    assert result.systems == frozenset({"Stanton"})
    assert "No mining-location data for Mystery" in result.note and "Known is mined" in result.note
    both = combine_mining_systems({"A": {"Stanton"}, "B": {"Stanton"}, "Mystery": set()})
    assert "A, B are mined" in both.note and "aren't mined in a common" not in both.note


def _multi_ore_cog(db, star_systems, **ore_systems):
    commodities = [
        _raw(1, "Ore A", 100, ids_star_systems=ore_systems.get("a", "")),
        _raw(2, "Ore B", 200, ids_star_systems=ore_systems.get("b", "")),
        _refined(100, "Refined A"), _refined(200, "Refined B"),
    ]
    return _cog(db, commodities=commodities, methods=[], star_systems=star_systems)


_SYSTEMS = [{"id": 1, "name": "Stanton"}, {"id": 2, "name": "Pyro"}]


async def _seed_two_refineries(db):
    await db.init()
    await db.record_refinery_yield_snapshot([
        {"id_commodity": 1, "id_terminal": 10, "commodity_name": "Ore A", "terminal_name": "Stanton Refinery",
         "star_system_name": "Stanton", "value": 3},
        {"id_commodity": 2, "id_terminal": 10, "commodity_name": "Ore B", "terminal_name": "Stanton Refinery",
         "star_system_name": "Stanton", "value": 3},
        {"id_commodity": 1, "id_terminal": 20, "commodity_name": "Ore A", "terminal_name": "Pyro Refinery",
         "star_system_name": "Pyro", "value": 9},
        {"id_commodity": 2, "id_terminal": 20, "commodity_name": "Ore B", "terminal_name": "Pyro Refinery",
         "star_system_name": "Pyro", "value": 9},
    ])


def test_a_refinery_only_near_one_of_the_ores_is_flagged_for_the_combined_haul(tmp_path):
    """Regression: the union of {Stanton} and {Stanton, Pyro} is {Stanton, Pyro}, so the Pyro refinery - useless
    for picking up Ore A, which is Stanton-only - went unflagged and ranked on its higher yield."""
    async def run():
        db = _make_db(tmp_path)
        await _seed_two_refineries(db)
        cog = _multi_ore_cog(db, _SYSTEMS, a="1", b="1,2")
        interaction = _FakeInteraction()
        await cog.refinery_advisor.callback(cog, interaction, ore_1="Ore A", ore_2="Ore B", ore_3=None)
        return interaction.followup.send.call_args.kwargs["embed"]

    embed = asyncio.run(run())
    best = next(f.value for f in embed.fields if f.name == "Best refineries by yield bonus")
    assert best.index("Stanton Refinery") < best.index("Pyro Refinery"), "the refinery reachable for both ores ranks first"
    assert "⚠️" in next(line for line in best.split("\n") if "Pyro Refinery" in line)
    assert "⚠️" not in next(line for line in best.split("\n") if "Stanton Refinery" in line)
    assert "judged against Stanton, the only system where every one of these ores is mined" in embed.footer.text


def test_ores_from_different_systems_are_ranked_by_the_union_and_the_footer_explains_why(tmp_path):
    async def run():
        db = _make_db(tmp_path)
        await _seed_two_refineries(db)
        cog = _multi_ore_cog(db, _SYSTEMS, a="1", b="2")
        interaction = _FakeInteraction()
        await cog.refinery_advisor.callback(cog, interaction, ore_1="Ore A", ore_2="Ore B", ore_3=None)
        return interaction.followup.send.call_args.kwargs["embed"]

    embed = asyncio.run(run())
    best = next(f.value for f in embed.fields if f.name == "Best refineries by yield bonus")
    assert best.index("Pyro Refinery") < best.index("Stanton Refinery"), "no system is preferred: higher yield first"
    assert "⚠️" not in best
    assert "aren't mined in a common system (Ore A: Stanton; Ore B: Pyro)" in embed.footer.text


def test_a_single_ore_gets_no_haul_note(tmp_path):
    async def run():
        db = _make_db(tmp_path)
        await _seed_two_refineries(db)
        cog = _multi_ore_cog(db, _SYSTEMS, a="1", b="1")
        interaction = _FakeInteraction()
        await cog.refinery_advisor.callback(cog, interaction, ore_1="Ore A", ore_2=None, ore_3=None)
        return interaction.followup.send.call_args.kwargs["embed"]

    embed = asyncio.run(run())
    assert "combined haul" not in embed.footer.text and "common system" not in embed.footer.text


def test_a_failed_star_system_lookup_degrades_to_yield_only_ordering_without_a_note(tmp_path):
    from bot.uex.exceptions import UexApiError

    async def run():
        db = _make_db(tmp_path)
        await _seed_two_refineries(db)
        cog = _multi_ore_cog(db, _SYSTEMS, a="1", b="2")
        cog.bot.uex.get_star_systems = AsyncMock(side_effect=UexApiError("timeout"))
        interaction = _FakeInteraction()
        await cog.refinery_advisor.callback(cog, interaction, ore_1="Ore A", ore_2="Ore B", ore_3=None)
        return interaction.followup.send.call_args.kwargs["embed"]

    embed = asyncio.run(run())
    best = next(f.value for f in embed.fields if f.name == "Best refineries by yield bonus")
    assert best.index("Pyro Refinery") < best.index("Stanton Refinery") and "⚠️" not in best
    assert "common system" not in embed.footer.text and "No mining-location data" not in embed.footer.text
