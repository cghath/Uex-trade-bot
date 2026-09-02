"""Trade-route math built on top of raw /commodities_prices rows."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass
class TradeRoute:
    commodity_name: str
    buy_terminal: str
    buy_price: float
    sell_terminal: str
    sell_price: float
    # Stock SCU at each terminal, when UEX reports it - used for cargo/SCU math in the cog.
    scu_buy_available: float | None = None
    scu_sell_wanted: float | None = None
    # Raw UEX status codes (see bot/uex/status.py) - resolved to labels in the cog, not here,
    # to keep this module dependency-free of the lookup table.
    status_buy_code: int | None = None
    status_sell_code: int | None = None
    buy_terminal_id: int | None = None
    sell_terminal_id: int | None = None

    @property
    def profit_per_unit(self) -> float:
        return round(self.sell_price - self.buy_price, 2)

    @property
    def margin_pct(self) -> float:
        if self.buy_price <= 0:
            return 0.0
        return round((self.profit_per_unit / self.buy_price) * 100, 1)


def best_sell_locations(price_rows: list[dict[str, Any]], limit: int = 5) -> list[dict[str, Any]]:
    """Sort commodities_prices rows by sell price, highest first, dropping terminals not buying."""
    sellable = [r for r in price_rows if (r.get("price_sell") or 0) > 0]
    sellable.sort(key=lambda r: r.get("price_sell", 0), reverse=True)
    return sellable[:limit]


def best_buy_locations(price_rows: list[dict[str, Any]], limit: int = 5) -> list[dict[str, Any]]:
    """Sort commodities_prices rows by buy price, lowest first, dropping terminals not selling it to you."""
    buyable = [r for r in price_rows if (r.get("price_buy") or 0) > 0]
    buyable.sort(key=lambda r: r.get("price_buy", 0))
    return buyable[:limit]


def best_routes(price_rows: list[dict[str, Any]], limit: int = 5) -> list[TradeRoute]:
    """Given all price rows for one commodity (across terminals), find the best buy->sell pairs.

    This is a simple single-commodity round trip finder: cheapest buy terminals paired against
    the best sell terminals, excluding same-terminal pairs. It does NOT account for distance/travel
    time between terminals since UEX's terminal records don't give flight time - only which
    star system/planet/city a terminal is in, which is enough for a rough "same system" sanity check.
    """
    buys = best_buy_locations(price_rows, limit=limit)
    sells = best_sell_locations(price_rows, limit=limit)

    routes: list[TradeRoute] = []
    for buy in buys:
        for sell in sells:
            if buy.get("id_terminal") == sell.get("id_terminal"):
                continue
            route = TradeRoute(
                commodity_name=buy.get("commodity_name", "Unknown"),
                buy_terminal=buy.get("terminal_name", "Unknown"),
                buy_price=buy.get("price_buy", 0),
                sell_terminal=sell.get("terminal_name", "Unknown"),
                sell_price=sell.get("price_sell", 0),
                scu_buy_available=buy.get("scu_buy"),
                scu_sell_wanted=sell.get("scu_sell"),
                status_buy_code=buy.get("status_buy"),
                status_sell_code=sell.get("status_sell"),
                buy_terminal_id=buy.get("id_terminal"),
                sell_terminal_id=sell.get("id_terminal"),
            )
            if route.profit_per_unit > 0:
                routes.append(route)

    routes.sort(key=lambda r: r.profit_per_unit, reverse=True)
    return routes[:limit]
