"""Renders the /commodity-history price chart as a PNG for Discord.

Follows the house dataviz method (see the dataviz skill): dark surface to match
Discord's default theme, fixed categorical color order (blue=sell, orange=buy per
slots 1/2), 2px lines, endpoint-only direct labels, hairline recessive gridlines,
a legend since this is always a 2-series chart. This is a static image (no
hover/tooltip layer - that only applies to interactive HTML/SVG charts).
"""
from __future__ import annotations

import io
import math
from datetime import datetime, timezone

import matplotlib

matplotlib.use("Agg")  # headless rendering, no display needed
import matplotlib.pyplot as plt
import matplotlib.dates as mdates
import matplotlib.ticker as mticker

# Dark-mode tokens straight from the dataviz skill's reference palette.
_SURFACE = "#1a1a19"
_INK_PRIMARY = "#ffffff"
_INK_SECONDARY = "#c3c2b7"
_INK_MUTED = "#898781"
_GRIDLINE = "#2c2c2a"
_BASELINE = "#383835"
_SELL_COLOR = "#3987e5"  # categorical slot 1 (blue), dark step
_BUY_COLOR = "#d95926"  # categorical slot 2 (orange), dark step


def render_price_history_chart(
    *,
    commodity_name: str,
    terminal_name: str,
    history_rows: list[dict],
) -> io.BytesIO | None:
    """Build a price-over-time line chart from /commodities_prices_history rows.

    Expects each row to have `date_added` (unix timestamp) and price_buy/price_sell.
    Returns a PNG in a BytesIO buffer, or None if there's nothing plottable.
    """
    points = []
    for row in history_rows:
        ts = row.get("date_added")
        if ts is None:
            continue
        try:
            when = datetime.fromtimestamp(int(ts), tz=timezone.utc)
        except (ValueError, OSError, OverflowError):
            continue
        points.append((when, row.get("price_buy"), row.get("price_sell")))

    if not points:
        return None

    points.sort(key=lambda p: p[0])
    times = [p[0] for p in points]
    buy_prices = [p[1] for p in points]
    sell_prices = [p[2] for p in points]

    has_buy = any(v is not None and v > 0 for v in buy_prices)
    has_sell = any(v is not None and v > 0 for v in sell_prices)

    def _to_plot_series(values: list) -> list[float]:
        """matplotlib only breaks-and-resumes a line across gaps for float NaN, not
        Python None - a None in the middle of a list otherwise truncates the whole line
        from that point on. A price of 0 (terminal doesn't trade it) is treated the same
        as missing, so it doesn't render as a fake drop to zero."""
        return [math.nan if v is None or v <= 0 else float(v) for v in values]

    plot_sell = _to_plot_series(sell_prices)
    plot_buy = _to_plot_series(buy_prices)

    fig, ax = plt.subplots(figsize=(9, 5), dpi=100)
    fig.patch.set_facecolor(_SURFACE)
    ax.set_facecolor(_SURFACE)

    # Hairline, recessive, solid gridlines - drawn first so data sits on top.
    ax.grid(True, color=_GRIDLINE, linewidth=1, linestyle="-", zorder=0)
    ax.set_axisbelow(True)

    def _last_point(values: list[float]) -> tuple | None:
        """Time/value of the last real (non-NaN) reading, so the endpoint dot and label
        sit on the line's actual visible end rather than the chart's last timestamp if
        that particular series has no reading there."""
        for i in range(len(values) - 1, -1, -1):
            if not math.isnan(values[i]):
                return times[i], values[i]
        return None

    if has_sell:
        ax.plot(times, plot_sell, color=_SELL_COLOR, linewidth=2, solid_joinstyle="round",
                 solid_capstyle="round", label="Sell price", zorder=3)
        last_point = _last_point(plot_sell)
        if last_point is not None:
            last_time, last_sell = last_point
            ax.scatter([last_time], [last_sell], s=64, color=_SELL_COLOR, edgecolors=_SURFACE,
                       linewidths=2, zorder=4)
            ax.annotate(f"{last_sell:,.0f}", (last_time, last_sell), color=_INK_PRIMARY,
                        fontsize=9, xytext=(8, 0), textcoords="offset points", va="center")

    if has_buy:
        ax.plot(times, plot_buy, color=_BUY_COLOR, linewidth=2, solid_joinstyle="round",
                 solid_capstyle="round", label="Buy price", zorder=3)
        last_point = _last_point(plot_buy)
        if last_point is not None:
            last_time, last_buy = last_point
            ax.scatter([last_time], [last_buy], s=64, color=_BUY_COLOR, edgecolors=_SURFACE,
                       linewidths=2, zorder=4)
            ax.annotate(f"{last_buy:,.0f}", (last_time, last_buy), color=_INK_PRIMARY,
                        fontsize=9, xytext=(8, 0), textcoords="offset points", va="center")

    if not has_buy and not has_sell:
        plt.close(fig)
        return None

    # Text stays in ink tokens, never the series color (per house style).
    ax.set_title(f"{commodity_name} at {terminal_name}", color=_INK_PRIMARY, fontsize=13, loc="left", pad=12)
    ax.set_ylabel("aUEC / unit", color=_INK_SECONDARY, fontsize=10)

    ax.yaxis.set_major_formatter(mticker.FuncFormatter(lambda x, _: f"{x:,.0f}"))
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%b %d"))
    fig.autofmt_xdate(rotation=0, ha="center")

    for spine in ax.spines.values():
        spine.set_visible(False)
    ax.spines["bottom"].set_visible(True)
    ax.spines["bottom"].set_color(_BASELINE)

    ax.tick_params(colors=_INK_MUTED, labelsize=9)

    if has_buy and has_sell:
        legend = ax.legend(
            loc="upper left", frameon=False, labelcolor=_INK_SECONDARY, fontsize=9,
        )
        for text in legend.get_texts():
            text.set_color(_INK_SECONDARY)

    fig.tight_layout()

    buffer = io.BytesIO()
    fig.savefig(buffer, format="png", facecolor=_SURFACE)
    plt.close(fig)
    buffer.seek(0)
    return buffer
