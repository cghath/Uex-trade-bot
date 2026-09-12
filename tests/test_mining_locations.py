"""Where to Mine: pure resolve/describe logic (bot/uex/mining_locations.py) and the
/where-to-mine command end to end."""
from __future__ import annotations

import asyncio
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock

from bot.cogs.mining_locations import MiningLocations
from bot.uex.mining_locations import describe_mining_locations, resolve_mineable_commodity


def _raw(id, name, **overrides):
    base = dict(id=id, name=name, is_raw=1)
    base.update(overrides)
    return base


# -- resolve_mineable_commodity -------------------------------------------------------

def test_resolve_mineable_commodity_matches_exact_name():
    commodities = [_raw(1, "Ouratite (Raw)")]
    match = resolve_mineable_commodity(commodities, "ouratite (raw)")
    assert match is not None and match["id"] == 1


def test_resolve_mineable_commodity_includes_hand_minable_non_refinable_material():
    """Jaclium has is_refinable=0 but is still is_raw - it must resolve here even though
    it's excluded from resolve_raw_commodity's refinery-scoped pool."""
    commodities = [_raw(1, "Jaclium (Ore)", is_refinable=0)]
    match = resolve_mineable_commodity(commodities, "Jaclium (Ore)")
    assert match is not None and match["id"] == 1


def test_resolve_mineable_commodity_ignores_a_non_raw_commodity():
    commodities = [dict(id=1, name="Gold", is_raw=0)]
    assert resolve_mineable_commodity(commodities, "Gold") is None


def test_resolve_mineable_commodity_matches_a_unique_substring():
    commodities = [_raw(1, "Quantainium (Raw)"), _raw(2, "Beryl (Raw)")]
    match = resolve_mineable_commodity(commodities, "quanta")
    assert match is not None and match["id"] == 1


def test_resolve_mineable_commodity_refuses_an_ambiguous_substring():
    commodities = [_raw(1, "Beryl (Raw)"), _raw(2, "Bexalite (Raw)")]
    assert resolve_mineable_commodity(commodities, "be") is None


# -- describe_mining_locations ---------------------------------------------------------

def test_describe_mining_locations_resolves_every_reference_field():
    commodity = _raw(
        1, "Ouratite (Raw)",
        ids_star_systems="64", ids_planets="242", ids_moons="18,25", ids_poi="270,271",
    )
    info = describe_mining_locations(
        commodity,
        star_systems_by_id={64: "Stanton"},
        planets_by_id={242: "Crusader"},
        moons_by_id={18: "Yela", 25: "Daymar"},
        poi_rows_by_id={
            270: {"name": "Yela Ring", "is_mining_related": 1, "moon_name": "Yela"},
            271: {"name": "Some Trade Post", "is_mining_related": 0, "moon_name": "Yela"},
        },
    )
    assert info.commodity_name == "Ouratite (Raw)"
    assert info.star_systems == ["Stanton"]
    assert info.planets == ["Crusader"]
    assert info.moons == ["Daymar", "Yela"]
    assert info.mining_pois == ["Yela Ring (Yela)"], "a non-mining-related POI must be excluded"
    assert info.difficulty == "high", "Ouratite's real instability (600) alone is enough to reach the high tier"
    assert info.mining_profile is not None and info.mining_profile.instability == 600
    assert info.hotspots and info.hotspots[0].location == "Aberdeen"


def test_describe_mining_locations_difficulty_is_none_for_an_unlisted_material():
    """Location data and difficulty data come from entirely separate sources - a commodity
    with no difficulty-table entry must get None there, not a fabricated default tier."""
    commodity = _raw(1, "Test Ore")
    info = describe_mining_locations(
        commodity, star_systems_by_id={}, planets_by_id={}, moons_by_id={}, poi_rows_by_id={},
    )
    assert info.difficulty is None
    assert info.mining_profile is None
    assert info.hotspots == []


def test_describe_mining_locations_drops_ids_with_no_matching_reference_row():
    """A stale/unknown id (reference data out of sync) must be silently dropped, not
    surfaced as a fabricated placeholder name."""
    commodity = _raw(1, "Test Ore", ids_star_systems="64,999", ids_moons="18")
    info = describe_mining_locations(
        commodity, star_systems_by_id={64: "Stanton"}, planets_by_id={}, moons_by_id={}, poi_rows_by_id={},
    )
    assert info.star_systems == ["Stanton"]
    assert info.moons == []


def test_describe_mining_locations_handles_no_location_data_at_all():
    commodity = _raw(1, "Diamond (Raw)")
    info = describe_mining_locations(
        commodity, star_systems_by_id={}, planets_by_id={}, moons_by_id={}, poi_rows_by_id={},
    )
    assert info.star_systems == [] and info.planets == [] and info.moons == [] and info.mining_pois == []


def test_describe_mining_locations_poi_without_any_context_shows_bare_name():
    commodity = _raw(1, "Test Ore", ids_poi="1")
    info = describe_mining_locations(
        commodity, star_systems_by_id={}, planets_by_id={}, moons_by_id={},
        poi_rows_by_id={1: {"name": "Deep Space Belt", "is_mining_related": 1}},
    )
    assert info.mining_pois == ["Deep Space Belt"]


# -- /where-to-mine command end to end ---------------------------------------------------

class _FakeInteraction:
    def __init__(self) -> None:
        self.response = NS(defer=AsyncMock())
        self.followup = NS(send=AsyncMock())


def _cog(*, commodities, star_systems=None, planets=None, moons=None, pois=None):
    cog = MiningLocations.__new__(MiningLocations)
    cog.bot = NS(
        uex=NS(
            get_commodities=AsyncMock(return_value=commodities),
            get_star_systems=AsyncMock(return_value=star_systems or []),
            get_planets=AsyncMock(return_value=planets or []),
            get_moons=AsyncMock(return_value=moons or []),
            get_poi=AsyncMock(return_value=pois or []),
        )
    )
    return cog


def test_where_to_mine_happy_path():
    async def run():
        commodity = _raw(1, "Ouratite (Raw)", ids_star_systems="64", ids_moons="18", ids_poi="270")
        cog = _cog(
            commodities=[commodity],
            star_systems=[{"id": 64, "name": "Stanton"}],
            moons=[{"id": 18, "name": "Yela"}],
            pois=[{"id": 270, "name": "Yela Ring", "is_mining_related": 1, "moon_name": "Yela"}],
        )
        interaction = _FakeInteraction()

        await cog.where_to_mine.callback(cog, interaction, ore="Ouratite (Raw)")

        interaction.response.defer.assert_awaited_once()
        embed = interaction.followup.send.call_args.kwargs["embed"]
        fields = {f.name: f.value for f in embed.fields}
        assert fields["Star system(s)"] == "Stanton"
        assert fields["Moon(s)"] == "Yela"
        assert "Yela Ring (Yela)" in fields["Named mining sites"]
        assert "High" in fields["Mining difficulty"]
        assert "resistance 0.6" in fields["Mining difficulty"]
        assert "instability 600" in fields["Mining difficulty"]
        assert "Optimal charge window: 0.6" in fields["Mining difficulty"]
        assert "**Aberdeen** — 10%" in fields["Richest known concentration"]
        assert "community-sourced" in embed.footer.text

    asyncio.run(run())


def test_where_to_mine_jaclium_shows_hathor_caves_as_its_only_hotspot():
    """The one ore whose real source is a special gameplay loop rather than a standard
    deposit - confirming /where-to-mine surfaces that via the hotspot table even when
    UEX's own location fields are completely empty for it."""
    async def run():
        commodity = _raw(1, "Jaclium (Ore)", is_refinable=0)
        cog = _cog(commodities=[commodity])
        interaction = _FakeInteraction()

        await cog.where_to_mine.callback(cog, interaction, ore="Jaclium (Ore)")

        embed = interaction.followup.send.call_args.kwargs["embed"]
        fields = {f.name: f.value for f in embed.fields}
        assert fields["Richest known concentration"] == "**Hathor Caves** — 19%"
        assert embed.description is None, "a real (if unusual) location is now known - must not still claim none is"

    asyncio.run(run())


def test_where_to_mine_reports_no_match_for_an_unknown_ore():
    async def run():
        cog = _cog(commodities=[_raw(1, "Ouratite (Raw)")])
        interaction = _FakeInteraction()

        await cog.where_to_mine.callback(cog, interaction, ore="Nonexistent")

        message = interaction.followup.send.call_args.args[0]
        assert "Nonexistent" in message
        assert "embed" not in interaction.followup.send.call_args.kwargs

    asyncio.run(run())


def test_where_to_mine_includes_a_hand_minable_material_with_no_refined_form():
    async def run():
        commodity = _raw(1, "Jaclium (Ore)", is_refinable=0, ids_star_systems="64")
        cog = _cog(commodities=[commodity], star_systems=[{"id": 64, "name": "Stanton"}])
        interaction = _FakeInteraction()

        await cog.where_to_mine.callback(cog, interaction, ore="Jaclium (Ore)")

        embed = interaction.followup.send.call_args.kwargs["embed"]
        assert "Jaclium (Ore)" in embed.title

    asyncio.run(run())


def test_where_to_mine_reports_no_data_when_nothing_resolves():
    """A commodity absent from BOTH UEX's location fields and the static difficulty table
    must show no fields at all - not a stray "Mining difficulty" field with nothing behind
    it."""
    async def run():
        commodity = _raw(1, "Completely Unknown Ore")
        cog = _cog(commodities=[commodity])
        interaction = _FakeInteraction()

        await cog.where_to_mine.callback(cog, interaction, ore="Completely Unknown Ore")

        embed = interaction.followup.send.call_args.kwargs["embed"]
        assert "No location data" in embed.description
        assert len(embed.fields) == 0

    asyncio.run(run())


def test_where_to_mine_shows_difficulty_even_when_location_is_unknown():
    """Location linkage and mining difficulty are independent lookups - UEX not knowing
    where Diamond is found must not hide a difficulty rating that comes from a completely
    separate (static, non-UEX) source."""
    async def run():
        commodity = _raw(1, "Diamond (Raw)")
        cog = _cog(commodities=[commodity])
        interaction = _FakeInteraction()

        await cog.where_to_mine.callback(cog, interaction, ore="Diamond (Raw)")

        embed = interaction.followup.send.call_args.kwargs["embed"]
        assert "No location data" in embed.description
        fields = {f.name: f.value for f in embed.fields}
        assert "Low" in fields["Mining difficulty"]

    asyncio.run(run())
