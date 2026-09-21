"""Backup routes for a stock-limited recommendation: a plan B that KEEPS the commodity the
player may already have bought.

A stock-limited route (bot.uex.route_presentation.hedge_room) plans to use ALL the stock or
demand on record, so its cargo estimate is fragile and any spare hold space sits idle.
find_hedge_cargo answers that at the SAME terminal pair, which real data shows is rarely
possible (about one warned route in five - most pairs have a single profitable commodity).
This widens the search from the same ORIGIN, deliberately anchored on the original commodity:
a player at the buy terminal may already hold it, so every option below either keeps it or is
labelled as only for someone who hasn't bought it yet.

The anchor is modelled as CARGO THE PLAYER HOLDS (anchor_scu, bought at anchor_buy_price - the
price the route list showed), never as stock still to be bought at the origin. That matters:
a player who bought the origin's whole stock leaves it at 0, and a search that needed stock on
record there would drop the very commodity being kept. Only the destination side of the anchor
is read from the market rows. The fillers and the "if you haven't bought yet" load are things
still to be bought, so they do use the origin's stock.

Pure and dependency-free like the rest of bot/uex/ - the caller supplies the market rows.
The search is anchor-first (the held anchor, then fill what is left of the hold), not a global
optimum over every split - it answers "what can I add or where else can this go", which is what
a player holding the anchor is asking.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

from bot.uex.mixed_routes import (
    MixedCargoItem,
    allocate_pair_cargo,
    allocation_is_exact,
    build_mixed_routes,
    build_pair_opportunities,
    eligible_market_rows,
)
from bot.uex.route_presentation import approximation_note, cargo_item_line, travel_warning
from bot.uex.supply_demand import has_sell_side_demand

# Terminal-to-terminal travel time is not part of any profit figure here, so a different
# destination (or dropping the commodity) has to be CLEARLY better than the option that needs
# no detour before it is worth offering. A gain smaller than this is noise next to the trip.
MIN_DETOUR_GAIN_PCT = 10.0
# Commodities added to the anchor - anchor + 2 fillers matches build_mixed_routes' 3-item cap.
MAX_FILLER_COMMODITIES = 2
# How many pinned mixed loads to scan for the best one that does not include the anchor.
WITHOUT_ANCHOR_SCAN_LIMIT = 10

# Why the original route could not be priced (BackupResult.baseline_status).
BASELINE_OK = "ok"
BASELINE_NO_DEMAND = "no_demand"  # a row exists but the destination no longer buys it at a profit
BASELINE_NO_DATA = "no_data"  # no row at all for the anchor at the destination in the collected data


@dataclass(frozen=True)
class BackupLoad:
    destination_id: int
    destination_name: str
    cargo: tuple[MixedCargoItem, ...]
    cargo_scu: float
    investment: float
    profit: float
    is_exact: bool
    # SCU of the anchor the player is holding that this destination cannot absorb (its demand
    # is lower than what they hold). 0 when everything sells; always 0 for a load that drops the
    # anchor entirely.
    anchor_unsold_scu: float = 0.0


@dataclass(frozen=True)
class BackupResult:
    # Profit of simply continuing as planned - the held anchor alone at the original
    # destination - on the CURRENT market rows. None when it can't be priced (see baseline_status).
    baseline_profit: float | None
    # Same origin AND destination, anchor plus fillers, when the fillers add profit.
    fuller_hold: BackupLoad | None
    # Keeps the anchor but sells at a different destination, when clearly better than the
    # no-detour option (MIN_DETOUR_GAIN_PCT).
    other_destination: BackupLoad | None
    # Drops the anchor entirely - only meaningful to a player who has not bought it yet -
    # when clearly better than every option that keeps it.
    without_anchor: BackupLoad | None
    baseline_status: str = BASELINE_OK

    @property
    def has_alternative(self) -> bool:
        return any((self.fuller_hold, self.other_destination, self.without_anchor))

    @property
    def best_keeping_anchor_profit(self) -> float:
        return max(
            [self.baseline_profit or 0.0]
            + [load.profit for load in (self.fuller_hold, self.other_destination) if load is not None]
        )


def _num(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _held_anchor_load(
    *,
    anchor_commodity_id: int,
    anchor_name: str,
    held_scu: int,
    buy_price: float,
    origin_row: dict[str, Any],
    destination_row: dict[str, Any],
    destination_id: int,
    capacity: int,
    budget: float,
    fillers_pairs: list[tuple[dict[str, Any], dict[str, Any]]],
) -> BackupLoad | None:
    """The held anchor sold at this destination, then (if fillers_pairs) up to
    MAX_FILLER_COMMODITIES others fill the hold the anchor doesn't occupy."""
    sell_price = _num(destination_row.get("price_sell"))
    demand = math.floor(_num(destination_row.get("scu_sell")))
    sold = min(held_scu, demand)
    if sold < 1 or sell_price <= buy_price:
        return None
    anchor = MixedCargoItem(
        id_commodity=anchor_commodity_id,
        commodity_name=anchor_name,
        quantity_scu=float(sold),
        buy_price=buy_price,
        sell_price=sell_price,
        available_scu=float(demand),
        # Everything held was paid for, whether or not this destination can absorb all of it, so
        # the profit charges the full cost - a destination that strands units is worse for it.
        investment=held_scu * buy_price,
        profit=sold * sell_price - held_scu * buy_price,
        source=origin_row,
        destination=destination_row,
    )
    cargo = [anchor]
    exact = True
    remaining_capacity = capacity - held_scu
    if fillers_pairs and remaining_capacity >= 1:
        remaining_budget = budget if math.isinf(budget) else max(0.0, budget - anchor.investment)
        cargo.extend(allocate_pair_cargo(
            fillers_pairs, capacity=remaining_capacity, budget=remaining_budget,
            max_commodities=MAX_FILLER_COMMODITIES, min_commodities=1,
        ))
        exact = allocation_is_exact(num_pairs=len(fillers_pairs), capacity=remaining_capacity)
    return BackupLoad(
        destination_id=destination_id,
        destination_name=str(destination_row.get("terminal_name") or "Unknown"),
        cargo=tuple(cargo),
        cargo_scu=held_scu + sum(item.quantity_scu for item in cargo[1:]),
        investment=sum(item.investment for item in cargo),
        profit=sum(item.profit for item in cargo),
        is_exact=exact,
        anchor_unsold_scu=float(held_scu - sold),
    )


def find_backup_routes(
    market_rows: list[dict[str, Any]],
    *,
    origin_terminal_id: int,
    destination_terminal_id: int,
    anchor_commodity_id: int,
    anchor_scu: float,
    anchor_buy_price: float,
    ship_capacity_scu: float,
    budget: float | None = None,
    capital_access_only: bool = False,
    auto_load_only: bool = False,
    system: str | None = None,
) -> BackupResult:
    """Plan B for a stock-limited route from origin_terminal_id to destination_terminal_id
    carrying anchor_scu of anchor_commodity_id, bought at anchor_buy_price. The filters are the
    same ones the route list applied, so a backup never leaves the player's system / auto-load /
    dock-access limits.

    Three independent answers (each None when not clearly better than its reference):
    fuller_hold beats simply continuing; other_destination must beat fuller_hold (or
    continuing, if there is no fuller hold) by MIN_DETOUR_GAIN_PCT; without_anchor must beat
    every option that keeps the anchor by the same margin, and is the one that only makes
    sense for a player who has not bought yet."""
    capacity = math.floor(float(ship_capacity_scu or 0))
    held = min(math.floor(float(anchor_scu or 0)), capacity)
    buy_price = _num(anchor_buy_price)
    if capacity <= 0 or held <= 0 or buy_price <= 0:
        return BackupResult(None, None, None, None, BASELINE_NO_DATA)
    capital = math.inf if budget is None else max(0.0, float(budget))

    filters = dict(capital_access_only=capital_access_only, auto_load_only=auto_load_only, system=system)
    eligible = eligible_market_rows(market_rows, **filters)
    origin_row = next((r for r in eligible if _int(r.get("id_terminal")) == origin_terminal_id), None)
    anchor_name = str(next(
        (r.get("commodity_name") for r in market_rows if _int(r.get("id_commodity")) == anchor_commodity_id
         and r.get("commodity_name")), "Unknown",
    ))
    # Where the held anchor can be sold: destination-side rows only, no origin stock involved.
    anchor_destinations: dict[int, dict[str, Any]] = {}
    for row in eligible:
        terminal_id = _int(row.get("id_terminal"))
        if (
            terminal_id is None or terminal_id == origin_terminal_id
            or _int(row.get("id_commodity")) != anchor_commodity_id
            or _num(row.get("price_sell")) <= buy_price
            or not has_sell_side_demand(row.get("scu_sell"), row.get("status_sell"))
        ):
            continue
        anchor_destinations.setdefault(terminal_id, row)

    original_rows = [r for r in market_rows if _int(r.get("id_commodity")) == anchor_commodity_id
                     and _int(r.get("id_terminal")) == destination_terminal_id]
    if origin_row is None:
        # The origin itself is filtered out (or unknown), so nothing from here is offerable.
        return BackupResult(None, None, None, None, BASELINE_OK if original_rows else BASELINE_NO_DATA)

    opportunities = build_pair_opportunities(market_rows, **filters)

    def load_for(destination_id: int, *, with_fillers: bool) -> BackupLoad | None:
        row = anchor_destinations.get(destination_id)
        if row is None:
            return None
        fillers = [
            pair for pair in opportunities.get((origin_terminal_id, destination_id), [])
            if _int(pair[0].get("id_commodity")) != anchor_commodity_id
        ] if with_fillers else []
        return _held_anchor_load(
            anchor_commodity_id=anchor_commodity_id, anchor_name=anchor_name, held_scu=held, buy_price=buy_price,
            origin_row=origin_row, destination_row=row, destination_id=destination_id, capacity=capacity,
            budget=capital, fillers_pairs=fillers,
        )

    baseline_load = load_for(destination_terminal_id, with_fillers=False)
    baseline = baseline_load.profit if baseline_load is not None else None
    if baseline_load is not None:
        status = BASELINE_OK
    else:
        status = BASELINE_NO_DEMAND if original_rows else BASELINE_NO_DATA
    full_load = load_for(destination_terminal_id, with_fillers=True)
    fuller_hold = full_load if (
        full_load is not None and len(full_load.cargo) > 1 and full_load.profit > (baseline or 0.0)
    ) else None
    no_detour_profit = full_load.profit if full_load is not None else 0.0

    other_destination: BackupLoad | None = None
    for destination_id in anchor_destinations:
        if destination_id == destination_terminal_id:
            continue
        candidate = load_for(destination_id, with_fillers=True)
        if candidate is not None and (other_destination is None or candidate.profit > other_destination.profit):
            other_destination = candidate
    if other_destination is not None and not _clearly_better(other_destination.profit, no_detour_profit):
        other_destination = None

    keeping = max(
        [baseline or 0.0]
        + [load.profit for load in (fuller_hold, other_destination) if load is not None]
    )
    without_anchor: BackupLoad | None = None
    for route in build_mixed_routes(
        market_rows, ship_capacity_scu=capacity, budget=budget, limit=WITHOUT_ANCHOR_SCAN_LIMIT,
        origin_terminal_id=origin_terminal_id, **filters,
    ):
        if any(item.id_commodity == anchor_commodity_id for item in route.cargo):
            continue
        without_anchor = BackupLoad(
            destination_id=route.destination_id, destination_name=route.destination_name, cargo=route.cargo,
            cargo_scu=route.cargo_scu, investment=route.investment, profit=route.profit, is_exact=route.is_exact,
        )
        break  # build_mixed_routes returns best-first
    if without_anchor is not None and not _clearly_better(without_anchor.profit, keeping):
        without_anchor = None
    return BackupResult(baseline, fuller_hold, other_destination, without_anchor, status)


def _clearly_better(candidate_profit: float, reference_profit: float) -> bool:
    """A detour must beat its reference by MIN_DETOUR_GAIN_PCT; with no reference profit at
    all (the original route no longer works), anything profitable qualifies."""
    if candidate_profit <= 0:
        return False
    if reference_profit <= 0:
        return True
    return candidate_profit >= reference_profit * (1 + MIN_DETOUR_GAIN_PCT / 100)


# -- the message a player reads --------------------------------------------------------------

@dataclass(frozen=True)
class BackupContext:
    """Everything the Backup route button needs to redo the search later, captured when the
    route message was built (the button outlives the command call that made it)."""

    origin_terminal_id: int
    origin_name: str
    destination_terminal_id: int
    destination_name: str
    anchor_commodity_id: int
    anchor_name: str
    anchor_scu: float
    anchor_buy_price: float
    ship_capacity_scu: float
    ship_name: str | None = None
    budget: float | None = None
    capital_access_only: bool = False
    auto_load_only: bool = False
    system: str | None = None


@dataclass(frozen=True)
class BackupMessage:
    title: str
    description: str
    sections: tuple[tuple[str, tuple[str, ...]], ...]
    footer: str


def run_backup_search(market_rows: list[dict[str, Any]], context: BackupContext) -> BackupResult:
    """find_backup_routes for a captured BackupContext - the one call both the button and its
    tests make, so the context fields can never drift from the search's parameters."""
    return find_backup_routes(
        market_rows,
        origin_terminal_id=context.origin_terminal_id,
        destination_terminal_id=context.destination_terminal_id,
        anchor_commodity_id=context.anchor_commodity_id,
        anchor_scu=context.anchor_scu,
        anchor_buy_price=context.anchor_buy_price,
        ship_capacity_scu=context.ship_capacity_scu,
        budget=context.budget,
        capital_access_only=context.capital_access_only,
        auto_load_only=context.auto_load_only,
        system=context.system,
    )


def _load_lines(load: BackupLoad, anchor_commodity_id: int) -> list[str]:
    lines = []
    for item in load.cargo:
        line = cargo_item_line(item)
        lines.append(line + " (yours)" if item.id_commodity == anchor_commodity_id else line)
    lines.append(f"Total: **{load.profit:,.0f} profit** on {load.investment:,.0f} aUEC · {load.cargo_scu:,.0f} SCU")
    if note := approximation_note(load.is_exact):
        lines.append("⚠️ " + note[0].upper() + note[1:])
    return lines


def _gain_pct(profit: float, reference: float) -> float:
    return (profit - reference) / reference * 100


def _system_of(load: BackupLoad, side: str) -> object:
    item = load.cargo[0]
    return (item.source if side == "origin" else item.destination).get("star_system_name")


def _baseline_problem(result: BackupResult, context: BackupContext) -> str | None:
    """Why the original route couldn't be priced - said plainly instead of implying a profit
    figure that doesn't exist. None when it could be."""
    if result.baseline_status == BASELINE_NO_DEMAND:
        return (f"{context.destination_name} no longer looks like a place to sell {context.anchor_name} "
                f"(no demand, or the price is below what it cost)")
    if result.baseline_status == BASELINE_NO_DATA:
        return f"I don't have a current {context.anchor_name} price at {context.destination_name}"
    return None


def build_backup_message(result: BackupResult, context: BackupContext) -> BackupMessage:
    anchor = context.anchor_name
    sections: list[tuple[str, tuple[str, ...]]] = []
    baseline = result.baseline_profit
    problem = _baseline_problem(result, context)

    if result.fuller_hold is not None:
        load = result.fuller_hold
        lines = [f"Sell at **{context.destination_name}** as planned and fill the spare space:"]
        lines += _load_lines(load, context.anchor_commodity_id)
        if baseline:
            lines.append(f"+{load.profit - baseline:,.0f} profit (+{_gain_pct(load.profit, baseline):.0f}%) "
                         f"over carrying {anchor} alone ({baseline:,.0f})")
        sections.append(("Same trip, fuller hold", tuple(lines)))

    if result.other_destination is not None:
        load = result.other_destination
        reference = result.fuller_hold.profit if result.fuller_hold is not None else baseline
        lines = [f"Keep your {anchor} and sell it at **{load.destination_name}** instead:"]
        lines += _load_lines(load, context.anchor_commodity_id)
        if reference:
            lines.append(f"{_gain_pct(load.profit, reference):+.0f}% vs the same trip to {context.destination_name} "
                         f"({reference:,.0f})")
        elif problem:
            lines.append(f"Your original route couldn't be priced: {problem}.")
        if load.anchor_unsold_scu > 0:
            lines.append(f"⚠️ Only {load.cargo[0].quantity_scu:,.0f} of your {context.anchor_scu:,.0f} SCU of {anchor} "
                         f"sells there - {load.anchor_unsold_scu:,.0f} SCU would be left over")
        if note := travel_warning(_system_of(load, "origin"), _system_of(load, "destination"), has_real_distance=False):
            lines.append(note)
        sections.append((f"Different destination: {load.destination_name}"[:250], tuple(lines)))

    if result.without_anchor is not None:
        load = result.without_anchor
        keeping = result.best_keeping_anchor_profit
        lines = [f"The best load from **{context.origin_name}** without it, selling at **{load.destination_name}**:"]
        lines += _load_lines(load, context.anchor_commodity_id)
        if keeping:
            lines.append(f"{_gain_pct(load.profit, keeping):+.0f}% vs the best option that keeps {anchor}")
        elif problem:
            lines.append(f"Your original route couldn't be priced: {problem}.")
        if note := travel_warning(_system_of(load, "origin"), _system_of(load, "destination"), has_real_distance=False):
            lines.append(note)
        sections.append((f"If you haven't bought {anchor} yet"[:250], tuple(lines)))

    if result.has_alternative:
        description = f"Options for your {context.anchor_scu:,.0f} SCU of {anchor}, from current market data:"
        if result.fuller_hold is None and result.other_destination is None and result.baseline_status == BASELINE_OK:
            # Only the "haven't bought yet" option exists. Someone who already holds the commodity
            # must not be left with no answer for their situation - tell them plainly to carry on.
            description += (
                f"\nAlready bought {anchor}? Nothing I can find beats continuing to "
                f"**{context.destination_name}** as planned."
            )
    elif problem:
        description = (
            f"{problem[0].upper()}{problem[1:]}, and I found nothing to replace the route. "
            f"Check prices at the terminal before you commit."
        )
    else:
        description = (
            f"Nothing I can find beats your current plan - continue to **{context.destination_name}** as planned."
        )

    footer = (
        f"Stock can be lower on arrival, so verify before buying · assumes you're carrying "
        f"{context.anchor_scu:,.0f} SCU of {anchor}"
    )
    if context.ship_name:
        footer += f" · {context.ship_name}, {context.ship_capacity_scu:,.0f} SCU hold"
    return BackupMessage(
        title=f"Backup route: {anchor} from {context.origin_name}"[:250],
        description=description,
        sections=tuple(sections),
        footer=footer[:2000],
    )
