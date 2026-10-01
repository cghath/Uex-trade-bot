"""Trade-route math built on top of raw /commodities_prices rows."""
from __future__ import annotations

import difflib
from dataclasses import dataclass
from typing import Any

from bot.uex.supply_demand import BUY_SIDE_OUT_OF_STOCK_CODE, SELL_SIDE_NO_DEMAND_CODE


def _status_code(value: Any) -> int | None:
    try:
        return int(float(value)) if value is not None else None
    except (TypeError, ValueError):
        return None


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
    def roi_pct(self) -> float:
        """Profit per aUEC spent, as UEX's own price_roi (audit MSG-16: this was named
        margin_pct, but margin is profit per aUEC of the sale - UEX's price_margin)."""
        if self.buy_price <= 0:
            return 0.0
        return round((self.profit_per_unit / self.buy_price) * 100, 1)


def best_sell_locations(price_rows: list[dict[str, Any]], limit: int = 5) -> list[dict[str, Any]]:
    """Sort commodities_prices rows by sell price, highest first, dropping terminals not
    buying - either because price_sell itself isn't reported, or because UEX's own status
    confirms zero real demand (code 7, "Maximum Inventory, No Demand") even when a stale
    positive price_sell is still on record. A merely-unreported scu_sell is NOT grounds
    for exclusion on its own - a real Out-of-Stock sell-side terminal (low/no on-hand
    stock) genuinely wants to buy but often has no live confirmed transaction amount; only
    the confirmed no-demand status is treated as disqualifying."""
    sellable = [
        r for r in price_rows
        if (r.get("price_sell") or 0) > 0
        and _status_code(r.get("status_sell")) != SELL_SIDE_NO_DEMAND_CODE
    ]
    sellable.sort(key=lambda r: r.get("price_sell", 0), reverse=True)
    return sellable[:limit]


def best_buy_locations(price_rows: list[dict[str, Any]], limit: int = 5) -> list[dict[str, Any]]:
    """Sort commodities_prices rows by buy price, lowest first, dropping terminals not
    selling it to you - either because price_buy itself isn't reported, or because UEX's
    own status confirms the terminal is out of stock (code 1) even when a stale positive
    price_buy is still on record."""
    buyable = [
        r for r in price_rows
        if (r.get("price_buy") or 0) > 0
        and _status_code(r.get("status_buy")) != BUY_SIDE_OUT_OF_STOCK_CODE
    ]
    buyable.sort(key=lambda r: r.get("price_buy", 0))
    return buyable[:limit]


def _commodity_key(row: dict[str, Any]) -> str:
    return str(row.get("commodity_name") or "").strip().casefold()


def _commodity_id(value: Any) -> int | None:
    try:
        number = int(float(value)) if value is not None else None
    except (TypeError, ValueError):
        return None
    return number if number is not None and number > 0 else None


def rows_for_commodity(price_rows: list[dict[str, Any]], commodity: str) -> tuple[list[dict[str, Any]], list[str]]:
    """The rows for the commodity a player TYPED, out of a /commodities_prices commodity_name
    lookup - which UEX matches as a case-insensitive SUBSTRING, confirmed live: 'Gold' also
    returns Golden Medmon (whose 71,000 topped /price's Gold list while real Gold sold for
    ~31,000), 'Tin' returns Astatine (listed first, so /best-route's rows[0] routed Astatine)
    and HexaPolyMesh Coating.

    Returns (the rows for exactly `commodity`, []) - or, when no row names it exactly but
    every named row is the same ONE commodity (partial typing, 'Laranit'), (every row, []) -
    or ([], the distinct names, sorted) when the rows span several commodities and none is
    the one asked for, so the caller asks which one instead of guessing. Case and
    surrounding whitespace are ignored; rows with no commodity_name are kept. For a name
    taken from UEX's own commodity list use rows_for_known_commodity, which has no
    partial-typing leniency. Ported from aiv2 commit c4f1aa6."""
    wanted = commodity.strip().casefold()
    if any(_commodity_key(r) == wanted for r in price_rows):
        return [r for r in price_rows if _commodity_key(r) in (wanted, "")], []
    names: dict[str, str] = {}
    for row in price_rows:
        if key := _commodity_key(row):
            names.setdefault(key, str(row["commodity_name"]).strip())
    if len(names) <= 1:
        return list(price_rows), []
    return [], sorted(names.values(), key=str.casefold)


def rows_for_known_commodity(
    price_rows: list[dict[str, Any]], name: str, id_commodity: Any = None,
) -> list[dict[str, Any]]:
    """The rows for ONE commodity already known from UEX's own /commodities list (its exact
    name, and its id when there is one), out of a commodity_name lookup that UEX matches by
    SUBSTRING (see rows_for_commodity). A row carrying an id_commodity is kept only when
    it's this id; a row without one only when its commodity_name is exactly this name, or
    it has none. No partial-name leniency: the name is already exact, so a row naming
    anything else - Golden Medmon for Gold, Astatine for Tin - is another commodity even
    when no row is this one."""
    wanted_id = _commodity_id(id_commodity)
    wanted_name = name.strip().casefold()

    def keep(row: dict[str, Any]) -> bool:
        row_id = _commodity_id(row.get("id_commodity"))
        if wanted_id is not None and row_id is not None:
            return row_id == wanted_id
        return _commodity_key(row) in (wanted_name, "")

    return [r for r in price_rows if keep(r)]


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


def _is_tradeable(commodity: dict[str, Any]) -> bool:
    return bool(commodity.get("is_buyable") or commodity.get("is_sellable"))


def resolve_tradeable_commodity(commodities: list[dict[str, Any]], query: str) -> dict[str, Any] | None:
    """A typed commodity name resolved to UEX's own record: an exact (case-insensitive)
    match first, else a substring match only if it's unique - the same rule as
    resolve_ship. Scoped to tradeable commodities, the ones commodity_name_autocomplete
    offers. Price and stock alerts used to save whatever was typed, so a typo made an
    alert that could never fire (audit UX-5)."""
    query_lower = query.strip().lower()
    if not query_lower:
        return None
    tradeable = [c for c in commodities if _is_tradeable(c) and c.get("name")]
    for commodity in tradeable:
        if commodity["name"].strip().lower() == query_lower:
            return commodity
    matches = [c for c in tradeable if query_lower in c["name"].lower()]
    return matches[0] if len(matches) == 1 else None


def suggest_commodity_names(commodities: list[dict[str, Any]], query: str, limit: int = 3) -> list[str]:
    """Up to `limit` tradeable names close to a query that didn't resolve: the ambiguous
    substring matches first, else the nearest spellings."""
    query_lower = query.strip().lower()
    names = sorted({c["name"] for c in commodities if _is_tradeable(c) and c.get("name")})
    contains = [n for n in names if query_lower and query_lower in n.lower()]
    if contains:
        return contains[:limit]
    by_lower = {n.lower(): n for n in names}
    return [by_lower[n] for n in difflib.get_close_matches(query_lower, list(by_lower), n=limit, cutoff=0.6)]


def unknown_commodity_message(commodities: list[dict[str, Any]], query: str) -> str:
    suggestions = suggest_commodity_names(commodities, query)
    hint = f" Did you mean {', '.join(f'**{s}**' for s in suggestions)}?" if suggestions else ""
    return (f"Couldn't find a tradeable commodity called **{query.strip()}**.{hint} "
            "Pick one from the autocomplete list.")
