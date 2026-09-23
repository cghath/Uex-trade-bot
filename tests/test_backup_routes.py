"""Backup routes for a stock-limited recommendation (bot/uex/backup_routes.py).

Scenario used throughout: a 100 SCU ship at origin O (10) planning Neon (id 1) to D (20). Neon
has only 21 SCU in stock at O, so the plan uses all of it and 79 SCU of hold sits idle. E (30)
is a second destination, F (40) another. Cobalt (2) and Scrap (3) are the other commodities
that can leave O.
"""
from __future__ import annotations

import math
import random

from bot.uex.backup_routes import (
    BASELINE_NO_DATA, BASELINE_NO_DEMAND, BASELINE_OK, MIN_DETOUR_GAIN_PCT, BackupContext, _profit_comparison,
    build_backup_message, find_backup_routes,
)

O, D, E, F = 10, 20, 30, 40
NEON, COBALT, SCRAP = 1, 2, 3


def _row(commodity_id, terminal_id, name, **values):
    return {
        "id_commodity": commodity_id, "id_terminal": terminal_id, "commodity_name": name,
        "terminal_name": {O: "Origin", D: "Original Dest", E: "Other Dest", F: "Far Dest"}[terminal_id],
        "price_buy": None, "price_sell": None, "scu_buy": None, "scu_sell": None, "status_sell": 1,
        "star_system_name": "Stanton", **values,
    }


def _neon(*, stock=21, d_price=200, d_demand=500, e_price=190, e_demand=500):
    rows = [
        _row(NEON, O, "Neon", price_buy=100, scu_buy=stock),
        _row(NEON, D, "Neon", price_sell=d_price, scu_sell=d_demand),
    ]
    if e_price is not None:
        rows.append(_row(NEON, E, "Neon", price_sell=e_price, scu_sell=e_demand))
    return rows


def _cobalt_at_d():
    return [_row(COBALT, O, "Cobalt", price_buy=20, scu_buy=95), _row(COBALT, D, "Cobalt", price_sell=50, scu_sell=80)]


def _scrap_at_e(sell_price):
    return [_row(SCRAP, O, "Scrap", price_buy=5, scu_buy=400), _row(SCRAP, E, "Scrap", price_sell=sell_price, scu_sell=400)]


def _find(rows, **overrides):
    kwargs = dict(
        origin_terminal_id=O, destination_terminal_id=D, anchor_commodity_id=NEON, anchor_scu=21,
        anchor_buy_price=100, ship_capacity_scu=100,
    )
    kwargs.update(overrides)
    return find_backup_routes(rows, **kwargs)


def test_a_filler_at_the_same_pair_makes_a_fuller_hold_that_keeps_the_anchor():
    result = _find(_neon() + _cobalt_at_d())
    assert result.baseline_profit == 2100
    load = result.fuller_hold
    assert load is not None and load.destination_id == D
    assert [(item.commodity_name, item.quantity_scu) for item in load.cargo] == [("Neon", 21), ("Cobalt", 79)]
    assert load.profit == 2100 + 79 * 30 and load.cargo_scu == 100
    assert load.anchor_unsold_scu == 0


def test_nothing_else_trading_from_the_origin_means_no_alternative_and_a_baseline_to_continue_on():
    result = _find(_neon(e_price=None))
    assert not result.has_alternative
    assert result.baseline_profit == 2100


def test_a_different_destination_is_offered_only_when_clearly_better_than_not_detouring():
    # Same-destination best: Neon 2,100 + Cobalt 2,370 = 4,470. At E: Neon 1,890 + Scrap 79 x margin.
    rows = _neon() + _cobalt_at_d()
    marginal = _find(rows + _scrap_at_e(40))      # 1,890 + 79 x 35 = 4,655 -> only 4% better
    assert marginal.other_destination is None and marginal.fuller_hold is not None
    clear = _find(rows + _scrap_at_e(45))         # 1,890 + 79 x 40 = 5,050 -> 13% better
    assert clear.other_destination is not None and clear.other_destination.destination_id == E
    assert clear.other_destination.profit >= 4470 * (1 + MIN_DETOUR_GAIN_PCT / 100)
    assert clear.other_destination.cargo[0].commodity_name == "Neon", "the anchor is always kept"


def test_dropping_the_anchor_is_offered_only_when_clearly_better_than_every_option_that_keeps_it():
    far = [
        _row(COBALT, O, "Cobalt", price_buy=20, scu_buy=95), _row(COBALT, F, "Cobalt", price_sell=90, scu_sell=95),
        _row(SCRAP, O, "Scrap", price_buy=5, scu_buy=400), _row(SCRAP, F, "Scrap", price_sell=60, scu_sell=400),
    ]
    result = _find(_neon(e_price=None) + far)
    load = result.without_anchor
    assert load is not None and load.destination_id == F
    assert all(item.commodity_name != "Neon" for item in load.cargo)
    assert load.profit >= result.best_keeping_anchor_profit * (1 + MIN_DETOUR_GAIN_PCT / 100)
    assert load.anchor_unsold_scu == 0

    weak_far = [
        _row(COBALT, O, "Cobalt", price_buy=20, scu_buy=95), _row(COBALT, F, "Cobalt", price_sell=22, scu_sell=95),
        _row(SCRAP, O, "Scrap", price_buy=5, scu_buy=400), _row(SCRAP, F, "Scrap", price_sell=7, scu_sell=400),
    ]
    assert _find(_neon(e_price=None) + weak_far).without_anchor is None


def test_a_load_that_still_includes_the_anchor_is_never_reported_as_dropping_it():
    """build_mixed_routes' best load from an origin may contain the anchor; that is covered by
    the keeping-the-anchor answers and must not reappear as 'if you haven't bought it'."""
    rows = _neon(stock=500) + _cobalt_at_d()
    result = _find(rows, anchor_scu=21)
    assert result.without_anchor is None or all(i.commodity_name != "Neon" for i in result.without_anchor.cargo)


def test_the_anchor_amount_a_destination_cannot_absorb_is_reported_as_unsold():
    rows = _neon(e_demand=10) + _cobalt_at_d() + _scrap_at_e(105)
    result = _find(rows)
    load = result.other_destination
    assert load is not None and load.destination_id == E
    assert load.cargo[0].quantity_scu == 10 and load.anchor_unsold_scu == 11
    assert load.cargo[0].profit == 10 * 190 - 21 * 100, "everything held was paid for, so stranded units cost money"


def test_the_system_filter_keeps_a_backup_from_leaving_the_players_system():
    rows = _neon() + _cobalt_at_d() + _scrap_at_e(105)
    for row in rows:
        if row["terminal_name"] == "Other Dest":
            row["star_system_name"] = "Nyx"
    assert _find(rows).other_destination is not None
    assert _find(rows, system="Stanton").other_destination is None


def test_the_space_only_filter_excludes_a_surface_origin():
    """Audit-confirmed defect: find_backup_routes never accepted a space_only parameter at
    all, even though the shared eligible_market_rows filter it already calls (via
    build_pair_opportunities/build_mixed_routes) has always supported one - a space-only
    tracked route's reroute suggestion could otherwise point at a terminal that violates
    that constraint. O (this whole test module's origin) is a plain surface terminal with
    no id_space_station, so space_only must exclude it - same shape as the star-system
    filter test above."""
    rows = _neon() + _cobalt_at_d() + _scrap_at_e(105)
    assert _find(rows).other_destination is not None
    assert not _find(rows, space_only=True).has_alternative


def test_a_budget_caps_the_fillers_after_the_anchor_is_paid_for():
    result = _find(_neon() + _cobalt_at_d(), budget=2100 + 100)  # 100 aUEC spare = 5 Cobalt at 20
    load = result.fuller_hold
    assert load is not None
    assert [(item.commodity_name, item.quantity_scu) for item in load.cargo] == [("Neon", 21), ("Cobalt", 5)]
    assert load.investment <= 2200


def test_when_the_original_pair_no_longer_pays_the_baseline_is_none_and_a_working_alternative_is_offered():
    rows = _neon(d_price=90) + _cobalt_at_d() + _scrap_at_e(105)   # Neon sells for less than it costs at D
    result = _find(rows)
    assert result.baseline_profit is None and result.fuller_hold is None
    assert result.baseline_status == BASELINE_NO_DEMAND
    assert result.other_destination is not None and result.other_destination.destination_id == E


def test_no_ship_or_no_planned_cargo_yields_an_empty_result():
    rows = _neon() + _cobalt_at_d()
    assert not _find(rows, ship_capacity_scu=0).has_alternative
    assert not _find(rows, anchor_scu=0).has_alternative


def test_a_backup_is_never_worse_than_continuing_as_planned_across_random_markets():
    """The invariant behind 'Backup route' being worth pressing: whatever it offers that keeps
    the anchor earns at least what continuing as planned would, and dropping the anchor is only
    ever offered when it clearly beats everything that keeps it."""
    rng = random.Random(20260921)
    checked = 0
    for _ in range(150):
        rows = []
        for commodity_id in range(1, 6):
            for terminal_id in (O, D, E, F):
                buy = rng.randint(5, 120)
                rows.append(_row(commodity_id, terminal_id, f"C{commodity_id}",
                                 price_buy=buy if rng.random() < 0.7 else None,
                                 scu_buy=rng.randint(1, 60),
                                 price_sell=buy + rng.randint(-10, 60) if rng.random() < 0.7 else None,
                                 scu_sell=rng.randint(1, 80)))
        anchor = rng.randint(1, 5)
        result = find_backup_routes(
            rows, origin_terminal_id=O, destination_terminal_id=D, anchor_commodity_id=anchor,
            anchor_scu=rng.randint(1, 40), anchor_buy_price=rng.randint(5, 120),
            ship_capacity_scu=rng.choice([20, 100, 400]),
        )
        baseline = result.baseline_profit or 0.0
        for load in (result.fuller_hold, result.other_destination):
            if load is not None:
                checked += 1
                assert load.profit >= baseline - 1e-6
                assert load.cargo[0].id_commodity == anchor and load.cargo_scu <= 400
        if result.without_anchor is not None:
            assert result.without_anchor.profit >= result.best_keeping_anchor_profit * (1 + MIN_DETOUR_GAIN_PCT / 100) - 1e-6
            assert all(item.id_commodity != anchor for item in result.without_anchor.cargo)
        assert not math.isnan(result.best_keeping_anchor_profit)
    assert checked > 20, "the random markets must actually exercise the offered-load paths"


def test_a_player_who_bought_the_whole_stock_still_gets_options_that_keep_the_commodity():
    """Buying out the origin leaves it at 0 stock. The commodity they now HOLD must not vanish
    from the search because the origin no longer lists any for sale."""
    result = _find(_neon(stock=0) + _cobalt_at_d())
    assert result.baseline_profit == 2100 and result.baseline_status == BASELINE_OK
    assert result.fuller_hold is not None
    assert [(i.commodity_name, i.quantity_scu) for i in result.fuller_hold.cargo] == [("Neon", 21), ("Cobalt", 79)]


def test_no_price_at_all_for_the_commodity_at_the_destination_is_reported_as_no_data_not_unprofitable():
    rows = [r for r in _neon() if not (r["id_commodity"] == NEON and r["id_terminal"] == D)] + _cobalt_at_d()
    result = _find(rows)
    assert result.baseline_profit is None and result.baseline_status == BASELINE_NO_DATA


def test_a_commodity_the_destination_no_longer_buys_is_reported_as_no_demand():
    result = _find(_neon(d_demand=0))
    assert result.baseline_profit is None and result.baseline_status == BASELINE_NO_DEMAND


def test_an_origin_the_players_filters_exclude_offers_nothing():
    rows = _neon() + _cobalt_at_d()
    for row in rows:
        row["star_system_name"] = "Nyx" if row["terminal_name"] == "Origin" else "Stanton"
    result = _find(rows, system="Stanton")
    assert not result.has_alternative and result.baseline_profit is None


def _context(**overrides):
    values = dict(
        origin_terminal_id=O, origin_name="Origin", destination_terminal_id=D, destination_name="Original Dest",
        anchor_commodity_id=NEON, anchor_name="Neon", anchor_scu=21, anchor_buy_price=100, ship_capacity_scu=100,
    )
    values.update(overrides)
    return BackupContext(**values)


def test_a_player_who_already_holds_the_commodity_is_told_to_carry_on_when_only_the_unbought_option_exists():
    """Only 'if you haven't bought it yet' has something to offer - the player who HAS bought must still
    get an answer for their own situation instead of a message that seems to ignore them."""
    far = [
        _row(COBALT, O, "Cobalt", price_buy=20, scu_buy=95), _row(COBALT, F, "Cobalt", price_sell=90, scu_sell=95),
        _row(SCRAP, O, "Scrap", price_buy=5, scu_buy=400), _row(SCRAP, F, "Scrap", price_sell=60, scu_sell=400),
    ]
    result = _find(_neon(e_price=None) + far)
    assert result.without_anchor is not None and result.fuller_hold is None and result.other_destination is None
    message = build_backup_message(result, _context())
    assert "Already bought Neon? Nothing I can find beats continuing to **Original Dest** as planned." in message.description
    assert [name for name, _ in message.sections] == ["If you haven't bought Neon yet"]


def test_the_carry_on_line_is_not_added_when_an_option_that_keeps_the_commodity_exists():
    result = _find(_neon() + _cobalt_at_d())
    message = build_backup_message(result, _context())
    assert "Already bought" not in message.description
    assert message.sections[0][0] == "Same trip, fuller hold"


def test_with_nothing_better_the_message_says_so_and_names_the_original_destination():
    message = build_backup_message(_find(_neon(e_price=None)), _context())
    assert message.sections == ()
    assert "Nothing I can find beats your current plan - continue to **Original Dest** as planned." == message.description


# -- _profit_comparison: a negative/zero reference must never be run through a percentage ------
# division, which inverts or explodes the sign into garbled text like "+-215%" (a confirmed
# real case: enough of what the player holds goes unsold at the original destination that
# continuing as planned is itself a loss - see BackupLoad.anchor_unsold_scu).

def test_profit_comparison_with_a_positive_reference_shows_a_normal_percentage():
    text = _profit_comparison(150, 100, "carrying it alone")
    assert "+50" in text and "+50%" in text and "carrying it alone" in text and "100" in text


def test_profit_comparison_never_produces_a_double_signed_percentage():
    for candidate in (-500, -50, 0, 50, 500):
        for reference in (-500, -100, -1, 0, 1, 100, 500):
            text = _profit_comparison(candidate, reference, "the reference")
            assert "+-" not in text and "-+" not in text, (candidate, reference, text)


def test_profit_comparison_with_a_negative_reference_and_a_profitable_candidate_says_it_turns_the_loss_around():
    text = _profit_comparison(480, -1890, "carrying Neon alone")
    assert "turns a 1,890 aUEC loss" in text and "480 profit" in text and "%" not in text


def test_profit_comparison_with_two_losses_says_how_much_of_the_loss_is_avoided():
    text = _profit_comparison(-200, -1890, "carrying Neon alone")
    assert "avoids 1,690 aUEC of the loss" in text and "loses 1,890" in text and "%" not in text


def test_profit_comparison_with_a_break_even_reference_says_so_without_a_percentage():
    text = _profit_comparison(300, 0, "carrying Neon alone")
    assert "breaks even" in text and "%" not in text and "+300" in text


def test_a_loss_making_original_route_is_described_in_aUEC_not_a_garbled_percentage():
    """2 of the 21 SCU of held Neon sell at Original Dest, the rest is stranded - a real
    negative baseline_profit, reachable through find_backup_routes itself, not a synthetic
    _profit_comparison call."""
    rows = _neon(d_price=105, d_demand=2) + _cobalt_at_d()
    result = _find(rows)
    assert result.baseline_profit is not None and result.baseline_profit < 0
    assert result.fuller_hold is not None and result.fuller_hold.profit > 0

    message = build_backup_message(result, _context())
    text = " ".join(line for _, lines in message.sections for line in lines)
    assert "+-" not in text
    assert "turns a 1,890 aUEC loss" in text


def test_a_baseline_of_exactly_zero_still_gets_a_comparison_line_not_a_silently_dropped_one():
    """baseline == 0.0 is falsy in Python but is real, known information - the message must
    still compare against it (as 'breaks even'), not silently omit the line the way a bare
    truthy check would (the same class of bug as the negative-reference garbling above)."""
    rows = _neon(d_price=210, d_demand=10) + _cobalt_at_d()
    result = _find(rows)
    assert result.baseline_profit == 0.0

    message = build_backup_message(result, _context())
    text = " ".join(line for _, lines in message.sections for line in lines)
    assert "breaks even" in text
