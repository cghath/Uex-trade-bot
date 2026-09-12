"""Unit tests for bot/uex/route_presentation.py - the shared warning/confidence/
chunking helpers /best-route, /top-routes, /mixed-routes, /multi-stop-route, and
/intelligence-brief now all call instead of each maintaining its own copy. See
CONTRIBUTING.md and PROJECT_CONTEXT.md's "Centralized Route Presentation" entry for why:
several follow-up review rounds found a fix applied to one of these commands and not the
others, purely because there was nowhere to land it once. These tests pin the shared
functions' behavior directly, independent of any one command's embed-building code -
tests/test_prices_chunked_fields.py already covers add_chunked_fields' atomic-budget
behavior in depth via its re-export in bot.cogs.prices, so that isn't repeated here.
"""
from __future__ import annotations

import discord

from bot.uex.data_health import classify_terminal_health
from bot.uex.mixed_routes import MixedCargoItem
from bot.uex.route_presentation import (
    add_chunked_fields,
    approximation_note,
    capital_access_note,
    cargo_confidences,
    cargo_item_line,
    cargo_item_warnings,
    format_evidence_note,
    side_health_warnings,
    travel_warning,
    worst_confidence,
)
from bot.uex.supply_demand import EvidenceLevel


def _stale_health():
    # last_update_days_percentage (ttl_remaining) <= 0 -> "stale", a real warning.
    return classify_terminal_health(dict(
        terminal_name="Terminal", last_update_days_percentage=0, prices_updated_percentage=100,
    ))


def _fresh_health():
    # ttl_remaining > 50 and coverage >= 50 -> "fresh", no warning.
    return classify_terminal_health(dict(
        terminal_name="Terminal", last_update_days_percentage=80, prices_updated_percentage=100,
    ))


def _item(**overrides) -> MixedCargoItem:
    source = dict(scu_buy=10, status_buy=1)
    destination = dict(scu_sell=10, status_sell=1)
    source.update(overrides.pop("source", {}))
    destination.update(overrides.pop("destination", {}))
    defaults = dict(
        id_commodity=1, commodity_name="Ore", quantity_scu=10, buy_price=100, sell_price=200,
        available_scu=10, investment=1000, profit=1000, source=source, destination=destination,
        limiting_factors=("stock",),
    )
    defaults.update(overrides)
    return MixedCargoItem(**defaults)


def test_cargo_item_line_formats_quantity_per_scu_and_profit():
    item = _item(commodity_name="Gold", quantity_scu=12, buy_price=100, sell_price=150, profit=600)
    line = cargo_item_line(item)
    assert "Gold" in line
    assert "12 SCU" in line
    assert "+50" in line  # profit_per_scu = sell_price - buy_price
    assert "600 profit" in line


def test_cargo_item_warnings_surfaces_risk_limiting_factors_and_market_status():
    item = _item(
        source=dict(
            is_illegal=1, is_explosive=0, is_volatile_qt=0, is_volatile_time=0, is_buggy=0,
            status_buy=1,
        ),
        destination=dict(status_sell=1),
        limiting_factors=("stock", "demand"),
    )
    status_lookup = {
        "buy": {1: dict(name_short="High Supply")},
        "sell": {1: dict(name_short="Low Inventory")},
    }
    lines = cargo_item_warnings(item, status_lookup=status_lookup)
    joined = "\n".join(lines)
    assert "Cargo risk:" in joined
    assert "limited by stock & demand" in joined
    assert "origin High Supply" in joined and "destination Low Inventory" in joined


def test_cargo_item_warnings_omits_market_status_when_no_codes_resolve():
    item = _item(source=dict(status_buy=None), destination=dict(status_sell=None))
    lines = cargo_item_warnings(item, status_lookup={"buy": {}, "sell": {}})
    assert not any("market status" in line for line in lines)


def test_cargo_item_warnings_shows_the_destination_capacity_when_demand_is_limiting():
    """When demand is the reason this item's quantity was capped, the raw destination
    figure (the same effective_sell_scu /price shows) is worth showing alongside the
    category label - it answers "was there more demand than I could take advantage of."""
    item = _item(destination=dict(scu_sell=250, status_sell=1), limiting_factors=("demand",))
    lines = cargo_item_warnings(item, status_lookup={"buy": {}, "sell": {}})
    joined = "\n".join(lines)
    assert "limited by demand (destination will take ~250 SCU)" in joined


def test_cargo_item_warnings_omits_destination_capacity_when_demand_isnt_limiting():
    """Showing the destination's ceiling only makes sense when demand is actually why the
    quantity landed where it did - if stock or cargo space capped it instead, the
    destination's own (higher, non-binding) capacity isn't the relevant number."""
    item = _item(destination=dict(scu_sell=250, status_sell=1), limiting_factors=("stock",))
    lines = cargo_item_warnings(item, status_lookup={"buy": {}, "sell": {}})
    joined = "\n".join(lines)
    assert "destination will take" not in joined


def test_cargo_item_warnings_omits_destination_capacity_when_status_confirms_no_demand():
    """Same status-code-7 override /price already uses - a stale positive scu_sell must
    never be shown as a real capacity when the status confirms zero real demand, which
    would otherwise contradict the market-status line shown right below it."""
    item = _item(destination=dict(scu_sell=250, status_sell=7), limiting_factors=("demand",))
    lines = cargo_item_warnings(item, status_lookup={"buy": {}, "sell": {}})
    joined = "\n".join(lines)
    assert "destination will take" not in joined


def test_cargo_item_warnings_prefix_is_prepended_to_every_line():
    item = _item(source=dict(is_illegal=1))
    lines = cargo_item_warnings(item, status_lookup={"buy": {}, "sell": {}}, prefix="Leg 2 ")
    assert lines, "expected at least the risk and limiting-factor lines"
    assert all(line.startswith("Leg 2 ") for line in lines)


def test_side_health_warnings_only_includes_sides_with_a_real_warning():
    warnings = side_health_warnings(origin_health=_stale_health(), destination_health=_fresh_health())
    assert len(warnings) == 1
    assert warnings[0].startswith("Origin:")


def test_side_health_warnings_uses_custom_labels():
    stale = _stale_health()
    warnings = side_health_warnings(
        origin_health=stale, destination_health=None,
        origin_label="Leg 2 Origin", destination_label="Leg 2 Destination",
    )
    assert warnings[0].startswith("Leg 2 Origin:")


def test_cargo_confidences_and_worst_confidence_pick_the_weakest_item():
    strong_item = _item(source=dict(scu_buy=100, status_buy=1), destination=dict(scu_sell=100, status_sell=1))
    weak_item = _item(source=dict(scu_buy=0, status_buy=7), destination=dict(scu_sell=0, status_sell=7))
    confidences = cargo_confidences([strong_item, weak_item], origin_health=None, destination_health=None)
    assert len(confidences) == 2
    assert worst_confidence(confidences).score == min(c.score for c in confidences)


def test_travel_warning_is_silent_when_systems_match_and_distance_is_already_shown():
    assert travel_warning("Stanton", "Stanton", has_real_distance=True) is None


def test_travel_warning_still_speaks_up_on_same_system_when_no_distance_exists():
    note = travel_warning("Stanton", "Stanton", has_real_distance=False)
    assert note is not None
    assert "not included in this ranking" in note


def test_travel_warning_cross_system_wording_differs_by_whether_distance_is_known():
    with_distance = travel_warning("Pyro", "Stanton", has_real_distance=True)
    without_distance = travel_warning("Pyro", "Stanton", has_real_distance=False)
    assert "crosses systems" in with_distance
    assert "Cross-system route" in without_distance
    assert "Pyro → Stanton" in with_distance and "Pyro → Stanton" in without_distance


def test_travel_warning_prefix_lands_right_after_the_warning_emoji():
    note = travel_warning("Pyro", "Stanton", has_real_distance=True, prefix="Leg 3 ")
    assert note == "⚠️ Leg 3 crosses systems: Pyro → Stanton"


def test_travel_warning_treats_missing_system_data_like_same_system():
    assert travel_warning(None, "Stanton", has_real_distance=True) is None
    assert travel_warning(None, "Stanton", has_real_distance=False) is not None


def test_capital_access_note_names_the_scope():
    assert "both ends" in capital_access_note("both ends")
    assert "every stop" in capital_access_note("every stop")


def test_approximation_note_is_none_when_exact():
    assert approximation_note(True) is None
    assert approximation_note(True, per_leg=True) is None


def test_approximation_note_distinguishes_per_leg_from_whole_route():
    assert "per-leg cargo allocation" in approximation_note(False, per_leg=True)
    whole_route = approximation_note(False, per_leg=False)
    assert "cargo allocation" in whole_route
    assert "per-leg" not in whole_route


def test_format_evidence_note_shows_confirmed_zero_distinctly_from_no_information():
    """The exact bug Evidence-Level Labels exists to fix: a confirmed-empty report and a
    genuinely unknown one must read as visibly different text, not both as nothing."""
    zero = format_evidence_note(EvidenceLevel(tier="current", quantity_scu=0), label="Stock")
    unknown = format_evidence_note(EvidenceLevel(tier="unknown"), label="Stock")
    assert zero != unknown
    assert "0 SCU" in zero
    assert "no information" in unknown.lower()
    assert "0" not in unknown


def test_format_evidence_note_aging_tier_recommends_verifying():
    note = format_evidence_note(
        EvidenceLevel(tier="aging", quantity_scu=250), label="Demand"
    )
    assert "250 SCU" in note
    assert "verify" in note.lower()


def test_format_evidence_note_inferred_tier_cites_the_historical_basis():
    note = format_evidence_note(
        EvidenceLevel(tier="inferred", historical_availability_pct=62.5, observed_hours=48.0),
        label="Stock",
    )
    assert "62" in note
    assert "48" in note
    assert "historically" in note.lower()


def test_add_chunked_fields_is_the_canonical_home_for_the_atomic_budget_check():
    """bot.cogs.prices re-exports this under its historical _add_chunked_fields name for
    backward-compat test/monkeypatch reasons - this confirms the real implementation
    works when imported directly from its new home too."""
    embed = discord.Embed(title="Test")
    assert add_chunked_fields(embed, name="Route", lines=["a short line"]) is True
    assert len(embed.fields) == 1
