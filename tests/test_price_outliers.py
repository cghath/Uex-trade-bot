"""Unit tests for bot/uex/price_outliers.py - the cross-terminal price-disagreement check
added after a real UEX data-entry error (Rayari Kaltag showing 2,614 aUEC/SCU for Fresh
Food while every other nearby terminal showed ~21,614) went undetected by every existing
route-confidence signal. See the module docstring for why this compares against sibling
terminals in the same snapshot instead of a corroboration-count gate.
"""
from __future__ import annotations

from bot.uex.price_outliers import (
    MIN_SIBLINGS_FOR_COMPARISON,
    OUTLIER_RATIO_THRESHOLD,
    find_price_outlier,
    format_price_outlier_warning,
    index_commodity_prices,
)


def _rows(*entries: tuple[int, int, float, float]) -> list[dict]:
    """(id_commodity, id_terminal, price_buy, price_sell) -> market_rows-shaped dicts."""
    return [
        dict(id_commodity=c, id_terminal=t, price_buy=buy, price_sell=sell)
        for c, t, buy, sell in entries
    ]


def test_index_commodity_prices_groups_by_commodity_and_side():
    rows = _rows((1, 10, 100, 150), (1, 11, 110, 160), (2, 10, 500, 600))
    index = index_commodity_prices(rows)
    assert sorted(index[(1, "buy")]) == [(10, 100), (11, 110)]
    assert sorted(index[(1, "sell")]) == [(10, 150), (11, 160)]
    assert index[(2, "buy")] == [(10, 500)]


def test_index_commodity_prices_skips_missing_and_non_positive_prices():
    rows = [
        dict(id_commodity=1, id_terminal=10, price_buy=0, price_sell=None),
        dict(id_commodity=1, id_terminal=11, price_buy=None, price_sell=-5),
        dict(id_commodity=None, id_terminal=12, price_buy=100, price_sell=150),
        dict(id_commodity=1, id_terminal=None, price_buy=100, price_sell=150),
    ]
    index = index_commodity_prices(rows)
    assert index == {}


def test_find_price_outlier_flags_the_real_fresh_food_incident_shape():
    # Kaltag showed 2,614 while four sibling terminals all agreed around 21,614 - an ~8x
    # gap, later confirmed in-game to be UEX's own data error (2,614 was wrong).
    index = index_commodity_prices(_rows(
        (120, 72, 2614, 0),
        (120, 39, 21614, 0),
        (120, 40, 21500, 0),
        (120, 41, 21700, 0),
        (120, 42, 21614, 0),
    ))
    outlier = find_price_outlier(index, id_commodity=120, id_terminal=72, side="buy", price=2614)
    assert outlier is not None
    assert outlier.sibling_count == 4
    assert outlier.sibling_median == 21614
    assert outlier.ratio > OUTLIER_RATIO_THRESHOLD


def test_find_price_outlier_flags_a_price_far_above_the_median_too():
    index = index_commodity_prices(_rows(
        (1, 10, 100000, 0), (1, 11, 1000, 0), (1, 12, 1100, 0), (1, 13, 900, 0),
    ))
    outlier = find_price_outlier(index, id_commodity=1, id_terminal=10, side="buy", price=100000)
    assert outlier is not None
    assert outlier.price > outlier.sibling_median


def test_find_price_outlier_returns_none_within_the_threshold():
    # 2x the sibling median is real terminal-to-terminal variance, not the ~8x incident case.
    index = index_commodity_prices(_rows(
        (1, 10, 2000, 0), (1, 11, 1000, 0), (1, 12, 1050, 0), (1, 13, 950, 0),
    ))
    assert find_price_outlier(index, id_commodity=1, id_terminal=10, side="buy", price=2000) is None


def test_find_price_outlier_returns_none_with_too_few_siblings():
    index = index_commodity_prices(_rows((1, 10, 2614, 0), (1, 39, 21614, 0)))
    assert len(index[(1, "buy")]) - 1 < MIN_SIBLINGS_FOR_COMPARISON
    assert find_price_outlier(index, id_commodity=1, id_terminal=10, side="buy", price=2614) is None


def test_find_price_outlier_excludes_the_checked_terminal_from_its_own_comparison_pool():
    # Terminal 10 has only 2 genuine siblings (11, 12) - its own row must not count toward
    # MIN_SIBLINGS_FOR_COMPARISON just because it's also present in the index.
    index = index_commodity_prices(_rows(
        (1, 10, 2614, 0), (1, 11, 21614, 0), (1, 12, 21500, 0),
    ))
    assert find_price_outlier(index, id_commodity=1, id_terminal=10, side="buy", price=2614) is None


def test_find_price_outlier_checks_buy_and_sell_independently():
    index = index_commodity_prices(_rows(
        (1, 10, 2614, 500), (1, 11, 21614, 510), (1, 12, 21500, 490), (1, 13, 21700, 505),
    ))
    assert find_price_outlier(index, id_commodity=1, id_terminal=10, side="buy", price=2614) is not None
    assert find_price_outlier(index, id_commodity=1, id_terminal=10, side="sell", price=500) is None


def test_format_price_outlier_warning_names_direction_and_median():
    index = index_commodity_prices(_rows(
        (120, 72, 2614, 0), (120, 39, 21614, 0), (120, 40, 21500, 0), (120, 41, 21700, 0),
    ))
    outlier = find_price_outlier(index, id_commodity=120, id_terminal=72, side="buy", price=2614)
    text = format_price_outlier_warning(outlier, label="origin buy")
    assert "origin buy" in text
    assert "2,614" in text
    assert "below" in text
    assert "21,614" in text  # median of the three siblings (21500, 21614, 21700)
