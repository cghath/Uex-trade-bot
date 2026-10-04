"""Shared formatting/assembly helpers for route-recommendation embeds.

/best-route, /top-routes, /mixed-routes, /multi-stop-route, and /intelligence-brief each
independently grew the same handful of things: Discord-size-safe field chunking, per-item
risk/limiting-factor/market-status warnings, terminal-health warnings, a worst-case
confidence reduction, a cross-system/travel-time note, capital-access and approximate-
allocation disclosures. Several follow-up review rounds found a fix landing in one of
these commands and not the others simply because there was no single place to land it -
this module is that place. Pure and dependency-free like the rest of bot/uex/*.py; the
cogs decide where each piece of text goes (inline vs. a separate warnings field, footer
vs. standalone line), this module only decides what the text says.
"""
from __future__ import annotations

from typing import Any, Iterable, NamedTuple, Protocol

from bot.uex.commodity_risk import format_commodity_risk
from bot.uex.data_health import TerminalDataHealth, format_health_note
from bot.uex.mixed_routes import format_limiting_factors
from bot.uex.practical_routes import terminal_limit_notes
from bot.uex.price_outliers import PriceOutlierIndex, find_price_outlier, format_price_outlier_warning
from bot.uex.route_confidence import RouteConfidence, compute_route_confidence
from bot.uex.status import StatusLookup, resolve_status_label
from bot.uex.supply_demand import EvidenceLevel, effective_sell_scu, has_sell_side_demand

# Discord's real limit on one embed's TOTAL text (title + description + every field's name
# and value + footer, matching discord.py's own Embed.__len__) - not the same thing as any
# individual field's 1024-char limit. A handful of individually-legal fields can still sum
# well past this, and Discord rejects the ENTIRE send in that case, silently losing every
# field, not just the overflow ones.
DISCORD_EMBED_TOTAL_CHAR_LIMIT = 6000
# Reserve room for a final "N more omitted" notice a caller may still add after this
# module stops packing fields, so that notice itself never pushes the embed over the limit.
TRUNCATION_NOTICE_RESERVE = 100


def chunk_lines(lines: list[str], max_length: int = 1024) -> list[str]:
    """Pack text into Discord-safe field values without dropping oversized lines."""
    if max_length <= 0:
        raise ValueError("max_length must be positive")
    chunks: list[str] = []
    current = ""
    for original_line in lines:
        line = str(original_line)
        while len(line) > max_length:
            if current:
                chunks.append(current)
                current = ""
            chunks.append(line[:max_length])
            line = line[max_length:]
        candidate = f"{current}\n{line}" if current else line
        if len(candidate) > max_length:
            if current:
                chunks.append(current)
            current = line
        else:
            current = candidate
    if current:
        chunks.append(current)
    return chunks


def add_chunked_fields(embed: Any, *, name: str, lines: list[str], inline: bool = False) -> bool:
    """Add one logical field as many Discord-safe continuation fields as needed - but only
    if the WHOLE set fits within Discord's combined 6000-char embed limit, never just part
    of it. All-or-nothing, not a per-chunk check: a route's cargo-risk warning often lands
    in a trailing continuation chunk (built after the price/summary lines fill the first
    1024-char chunk), so a per-chunk budget check that added the first chunk and only then
    discovered the second didn't fit left that route visible on screen with its warning
    silently missing - worse than omitting the whole route, since a visible route with no
    warning reads as "checked and safe." Returns False (adding nothing at all) the moment
    the full set would overflow, so a caller adding several logical fields in a loop (e.g.
    one per route) can treat this one as entirely omitted and stop early.

    inline defaults to False (every pre-existing caller's own behavior) - pass True for a
    field a caller wants to keep sitting side-by-side with its neighbors (e.g. a short,
    normally-single-chunk field); a chunked continuation still renders inline too, Discord
    just wraps to a new row once the current one is full."""
    chunks = []
    projected_total = len(embed)
    for index, chunk in enumerate(chunk_lines(lines), 1):
        suffix = f" (continued {index})" if index > 1 else ""
        safe_name = f"{name[:256 - len(suffix)]}{suffix}"
        projected_total += len(safe_name) + len(chunk)
        chunks.append((safe_name, chunk))
    if projected_total > DISCORD_EMBED_TOTAL_CHAR_LIMIT - TRUNCATION_NOTICE_RESERVE:
        return False
    for safe_name, chunk in chunks:
        embed.add_field(name=safe_name, value=chunk, inline=inline)
    return True


def side_health_warnings(
    *,
    origin_health: TerminalDataHealth | None,
    destination_health: TerminalDataHealth | None,
    origin_label: str = "Origin",
    destination_label: str = "Destination",
) -> list[str]:
    """'{label}: {note}' for whichever side(s) have a real data-health warning."""
    warnings: list[str] = []
    for label, health in ((origin_label, origin_health), (destination_label, destination_health)):
        if note := format_health_note(health):
            warnings.append(f"{label}: {note}")
    return warnings


class _CargoItemLike(Protocol):
    id_commodity: int
    commodity_name: str
    source: dict[str, Any]
    destination: dict[str, Any]
    limiting_factors: tuple[str, ...]
    quantity_scu: float
    buy_price: float
    sell_price: float
    profit_per_scu: float
    profit: float


def _terminal_id(row: dict[str, Any]) -> int | None:
    try:
        return int(row["id_terminal"]) if row.get("id_terminal") is not None else None
    except (TypeError, ValueError):
        return None


def price_outlier_warnings(
    index: PriceOutlierIndex,
    *,
    id_commodity: int | None,
    origin_id: int | None,
    buy_price: float | None,
    destination_id: int | None,
    sell_price: float | None,
) -> list[str]:
    """A "⚠️ origin buy/destination sell price is N.Nx below/above the median of M other
    terminals" line for each side whose price is a confirmed outlier against every other
    terminal trading the same commodity in the same snapshot (bot/uex/price_outliers.py).
    Shared by cargo_item_warnings and /best-route, which shows single-commodity routes."""
    lines = []
    for terminal_id, side, price, label in (
        (origin_id, "buy", buy_price, "origin buy"), (destination_id, "sell", sell_price, "destination sell"),
    ):
        if terminal_id is None:
            continue
        outlier = find_price_outlier(index, id_commodity=id_commodity, id_terminal=terminal_id, side=side,
                                     price=price)
        if outlier is not None:
            lines.append(f"⚠️ {format_price_outlier_warning(outlier, label=label)}")
    return lines


class CargoNotes(NamedTuple):
    """What cargo_item_warnings says about one cargo item, piece by piece, for a caller that
    lays the pieces out itself (/multi-stop-route's per-leg sections)."""

    risk: str | None  # "⚠️ Cargo risk: ..." (format_commodity_risk); None when there's none
    limit: str  # "limited by demand (destination will take ~84 SCU)"
    market_status: tuple[str, ...]  # ("origin High", "destination Low"); empty when unknown
    outliers: tuple[str, ...]  # "⚠️ origin buy price ..." (price_outlier_warnings)


def cargo_item_notes(
    item: _CargoItemLike,
    *,
    status_lookup: StatusLookup,
    price_outlier_index: PriceOutlierIndex | None = None,
) -> CargoNotes:
    """cargo_item_warnings' facts before they're joined into lines - see it for each one."""
    limit_text = format_limiting_factors(item.limiting_factors)
    if "demand" in item.limiting_factors:
        # The item's own quantity_scu is already capped to this same number (or lower, by
        # ship space/budget) - showing the destination's own real ceiling separately tells
        # the player whether there was more demand than they could take advantage of.
        destination_capacity = effective_sell_scu(
            item.destination.get("scu_sell"), item.destination.get("status_sell")
        )
        if destination_capacity:
            limit_text += f" (destination will take ~{destination_capacity:,.0f} SCU)"
    buy_status = resolve_status_label(status_lookup, "buy", item.source.get("status_buy"))
    sell_status = resolve_status_label(status_lookup, "sell", item.destination.get("status_sell"))
    market_status = tuple(
        f"{side} {status}" for side, status in (("origin", buy_status), ("destination", sell_status)) if status
    )
    outliers: tuple[str, ...] = ()
    if price_outlier_index is not None:
        outliers = tuple(price_outlier_warnings(
            price_outlier_index, id_commodity=item.id_commodity,
            origin_id=_terminal_id(item.source), buy_price=item.buy_price,
            destination_id=_terminal_id(item.destination), sell_price=item.sell_price,
        ))
    return CargoNotes(format_commodity_risk(item.source), limit_text, market_status, outliers)


def cargo_item_warnings(
    item: _CargoItemLike,
    *,
    status_lookup: StatusLookup,
    prefix: str = "",
    price_outlier_index: PriceOutlierIndex | None = None,
) -> list[str]:
    """Risk, limiting-factor, buy/sell market-status and (given `price_outlier_index`)
    cross-terminal price-disagreement lines for one MixedCargoItem-shaped object. `prefix`
    is prepended to each line verbatim (e.g. "Leg 2 ") - not inserted after a warning emoji,
    since these lines don't all carry one.

    price_outlier_index (index_commodity_prices, built once per market snapshot by the
    caller) flags this item's buy or sell price when it's a confirmed outlier against every
    other terminal trading the same commodity. None skips the check: a caller with no
    snapshot in scope just doesn't get it."""
    notes = cargo_item_notes(item, status_lookup=status_lookup, price_outlier_index=price_outlier_index)
    lines: list[str] = []
    if notes.risk:
        lines.append(f"{prefix}{item.commodity_name}: {notes.risk}")
    lines.append(f"{prefix}{item.commodity_name}: {notes.limit}")
    if notes.market_status:
        lines.append(f"{prefix}{item.commodity_name} market status: {' · '.join(notes.market_status)}")
    for warning in notes.outliers:
        # "⚠️ origin buy price ..." -> "⚠️ Gold origin buy price ...", so a multi-item
        # load says which cargo it's about.
        lines.append(f"{prefix}⚠️ {item.commodity_name} {warning.removeprefix('⚠️ ')}")
    return lines


def cargo_item_line(item: _CargoItemLike, *, risk: bool = False) -> str:
    """'• **Name:** N SCU · +P/SCU · **T profit**' - one line per cargo item.

    `risk` adds the commodity's own cargo-risk label (format_commodity_risk), for cargo suggested beside a route with
    no warning lines of its own: a hedge, or another commodity in a backup load. PR #96 removed the risk-tolerance
    filter on the grounds that every route labels risky cargo, but these lines carried no label, so an illegal hedge
    showed up looking unflagged (workflow audit 2026-09-30)."""
    line = (
        f"• **{item.commodity_name}:** {item.quantity_scu:,.0f} SCU · "
        f"+{item.profit_per_scu:,.0f}/SCU · **{item.profit:,.0f} profit**"
    )
    if risk and (label := format_commodity_risk(item.source)):
        line += f" · {label}"
    return line


def cargo_confidences(
    cargo: Iterable[_CargoItemLike],
    *,
    origin_health: TerminalDataHealth | None,
    destination_health: TerminalDataHealth | None,
) -> list[RouteConfidence]:
    """One RouteConfidence per cargo item sharing the same origin/destination (a leg or a
    mixed-route load) - callers combine across items (and, for multi-stop, across legs)
    with `worst_confidence`."""
    return [
        compute_route_confidence(
            origin_health=origin_health,
            destination_health=destination_health,
            origin_report_count=item.source.get("buy_report_count"),
            destination_report_count=item.destination.get("sell_report_count"),
            volatility_origin=item.source.get("volatility_buy"),
            volatility_destination=item.destination.get("volatility_sell"),
            origin_available=item.source.get("scu_buy", 0) > 0,
            destination_available=has_sell_side_demand(
                item.destination.get("scu_sell"), item.destination.get("status_sell")
            ),
        )
        for item in cargo
    ]


def worst_confidence(confidences: Iterable[RouteConfidence]) -> RouteConfidence:
    """A route/chain is only as trustworthy as its least-confident piece."""
    return min(confidences, key=lambda value: value.score)


def system_crossing(origin_system: object, destination_system: object) -> tuple[str, str] | None:
    """(origin, destination) star system names when both are known and differ, else None."""
    origin = str(origin_system).strip() if origin_system else ""
    destination = str(destination_system).strip() if destination_system else ""
    return (origin, destination) if origin and destination and origin != destination else None


def travel_warning(
    origin_system: object,
    destination_system: object,
    *,
    has_real_distance: bool,
    prefix: str = "",
) -> str | None:
    """One shared warning line about cross-system travel / distance-data completeness.

    has_real_distance=False (no per-route distance/GM figure exists anywhere in this
    embed - /best-route's self-derived fallback, /mixed-routes, /intelligence-brief's
    mixed-route recommendations): always returns exactly one line - a cross-system-
    specific note when both systems are known and differ, otherwise a generic reminder
    that travel time isn't factored into the ranking at all (covers both "same system"
    and "system data missing" - neither has anything more specific to say here).

    has_real_distance=True (a real UEX distance/GM figure is already shown elsewhere for
    this route - /best-route's own UEX-routes branch, /top-routes, /multi-stop-route's
    per-leg lines): stays silent unless both systems are known and differ, since there's
    nothing to add when they match or when system data is simply missing.
    """
    if crossing := system_crossing(origin_system, destination_system):
        origin, destination = crossing
        if has_real_distance:
            return f"⚠️ {prefix}crosses systems: {origin} → {destination}"
        return f"⚠️ {prefix}Cross-system route: {origin} → {destination}; compare profit against travel time"
    if has_real_distance:
        return None
    return f"⚠️ {prefix}Travel time/distance is not included in this ranking"


def capital_access_note(scope: str) -> str:
    """scope describes how many stops the check covers, e.g. 'both ends', 'every stop'."""
    return f"Capital-ship access confirmed: XL hangar or external cargo loading dock at {scope}"


def format_evidence_note(level: EvidenceLevel, *, label: str) -> str:
    """Turn an EvidenceLevel (bot.uex.supply_demand.classify_supply_evidence) into one
    display line - always returns something, deliberately never omits the line the way
    the pre-Evidence-Level-Labels code paths silently did for a None SCU figure. A "0 SCU"
    confirmed-empty report and a genuinely unknown one must never read the same; only the
    "current"/"aging" branches print a quantity at all."""
    if level.tier == "current":
        return f"{label}: **{level.quantity_scu:,.0f} SCU** (current report)"
    if level.tier == "aging":
        return f"{label}: **{level.quantity_scu:,.0f} SCU** (older report - verify before departure)"
    if level.tier == "inferred":
        return (
            f"{label}: no current report - historically available ~{level.historical_availability_pct:.0f}% "
            f"of the time ({level.observed_hours:.0f}h observed)"
        )
    return f"{label}: no information reported - not the same as confirmed zero"


def stock_headroom_warning(limited_by: str, *, mixed_routes_command: str = "/mixed-routes") -> str | None:
    """A cargo estimate capped by real stock/demand (limited_by == "stock", from
    bot.uex.ships.estimate_route_cargo) is planning to use the ENTIRE quantity UEX
    currently reports, not a portion of it - if that figure is even slightly stale, or
    someone else buys/sells into it first, the player gets less than planned the moment
    they arrive. limited_by == "ship" or "budget" means the opposite: the player's own
    ship/budget capped them below the full reported amount, so there's already real
    headroom against exactly this kind of drift, and nothing to warn about. Framed as a
    nudge toward mixed-routes hedging with a second commodity, not a hard error, since a
    stock-limited route is still the single best option available - it's just fragile."""
    if limited_by != "stock":
        return None
    return (
        f"Uses the entire stock/demand currently on record - if it's lower on arrival, "
        f"{mixed_routes_command} can hedge with a second commodity so your hold isn't left half-empty"
    )


class HedgeRoom(NamedTuple):
    """Spare cargo space (and, if the player set a budget, spare money) a hedge could fill."""

    capacity_scu: float
    budget: float | None


def hedge_room(cargo: Any, *, ship_cargo_scu: float | None, budget: float | None = None) -> HedgeRoom | None:
    """The single definition of when a route deserves a hedge suggestion (see
    bot.uex.mixed_routes.find_hedge_cargo) and how much room the hedge has to work with.
    `cargo` is a bot.uex.ships.estimate_route_cargo result.

    Only a stock-limited haul (limited_by == "stock", the same condition
    stock_headroom_warning warns on) leaves anything idle: the anchor commodity's real
    stock/demand ran out before the ship's hold or the player's budget did. A ship- or
    budget-limited haul already used everything of that kind, so there is nothing to fill.
    Returns None whenever no hedge is warranted - not stock-limited, no ship capacity on
    record to measure spare room against, or no spare capacity/budget left - so callers can
    use `is None` to skip the market lookup entirely.

    Shared by /best-route and /top-routes (with or without its origin/destination pins)
    so the rule lives in one place rather than being re-derived per
    command. budget is None for a command with no budget concept (/best-route), and
    also when the route's own investment couldn't be computed - both mean "don't cap the
    hedge by money", never "the budget is zero"."""
    if cargo.limited_by != "stock" or ship_cargo_scu is None:
        return None
    spare_capacity = ship_cargo_scu - cargo.max_scu
    if spare_capacity <= 0:
        return None
    if budget is None or cargo.investment is None:
        return HedgeRoom(spare_capacity, None)
    spare_budget = budget - cargo.investment
    if spare_budget <= 0:
        return None
    return HedgeRoom(spare_capacity, spare_budget)


def approximation_note(is_exact: bool, *, per_leg: bool = False) -> str | None:
    """Lowercase, footer-joinable fragment (e.g. append after ' · '); None when the
    allocation is exact. A caller needing a standalone warning line instead of a footer
    fragment should prefix it with '⚠️ ' and capitalize the first letter."""
    if is_exact:
        return None
    kind = "per-leg cargo allocation" if per_leg else "cargo allocation"
    return f"{kind} for this route is approximate, not proven-optimal"


def missing_ship_note(ship_query: str | None, *, lookup_failed: bool) -> str:
    """Footer fragment for a route command with no ship to do cargo math with. Three
    different causes all used to read "set a default ship" (audit MSG-4) - telling a player
    who already has one to set one, when really UEX was down or the saved name no longer
    matches a ship."""
    if not ship_query:
        return "set a default ship with /set-trading-preferences for cargo/run-profit numbers"
    if lookup_failed:
        return "UEX's ship list didn't load, so no cargo/run-profit numbers this time"
    return (f"'{ship_query}' didn't match a single ship - pick one from autocomplete or update it with "
            "/set-trading-preferences")


def missing_ship_cargo_line(ship_query: str | None, *, lookup_failed: bool) -> str:
    """The per-route "Cargo: unknown" line, with the same three causes as missing_ship_note."""
    if not ship_query:
        return "Cargo: unknown (set a ship with /set-trading-preferences to see haulable SCU)"
    if lookup_failed:
        return "Cargo: unknown (UEX's ship list didn't load)"
    return f"Cargo: unknown ('{ship_query}' didn't match a single ship)"


def format_gm(distance_gm: float) -> str:
    """'17 Gm', '17.5 Gm': UEX's distances are mostly whole gigameters."""
    return f"{distance_gm:,.0f} Gm" if float(distance_gm).is_integer() else f"{distance_gm:,.1f} Gm"


class MultiStopLegFacts(NamedTuple):
    """What /multi-stop-route looks up for each leg on top of the route itself."""

    distance_gm: float | None  # UEX's live distance; None when it couldn't be fetched
    origin_health: TerminalDataHealth | None
    destination_health: TerminalDataHealth | None


def multi_stop_footer(*, is_exact: bool, budget: float | None, filters_note: str | None,
                      capital_access_only: bool) -> str:
    """The small print under a /multi-stop-route route. The owner cut it down (2026-10-04) to
    what a player can act on: prices move, the cargo split is approximate (only when it is -
    see approximation_note), and the options in force."""
    parts = ["Prices can change before you arrive"]
    if not is_exact:
        parts.append("the cargo split is approximate")
    if budget is not None:
        parts.append(f"starting budget {float(budget):,.0f} aUEC")
    if filters_note:
        parts.append(filters_note)
    if capital_access_only:
        parts.append(capital_access_note("every stop"))
    return " · ".join(parts)


def multi_stop_blocks(
    route: Any,
    *,
    index: int,
    ship_name: str,
    space_only: bool,
    leg_facts: list[MultiStopLegFacts],
    confidence: RouteConfidence,
    footer: str,
    status_lookup: StatusLookup,
    price_outlier_index: PriceOutlierIndex | None = None,
) -> tuple[str, ...]:
    """One /multi-stop-route route (a bot.uex.multi_stop_routes.MultiStopRoute) as text blocks
    for a layout: the summary, a section per leg, then the small print. The owner's pick from
    real-data mockups (2026-10-04, option B):

    - Each leg's warnings sit under that leg, not in one list at the bottom. ⚠️ lines stay full
      size; the rest (what limited a load, market status, the leg's money) is small print.
    - A station's own facts - container size, no freight elevator or dock, player-owned, stale
      data, a cargo center - are said once, under the first leg that stops there, by name. The
      old list said "Origin"/"Destination" and repeated them for a station that ends one leg
      and starts the next.
    - Refuel and repair aren't listed (the owner asked them out); a cargo center still is.
    - Every leg shows its own profit, a one-commodity leg included.

    Markdown that reads the same as a plain message, so the blocks joined by blank lines are
    the text version when the layout can't be sent."""
    legs = route.legs
    path = " → ".join([legs[0].origin_name, *(leg.destination_name for leg in legs)])
    distances = [facts.distance_gm for facts in leg_facts]
    known_gm = sum(distance for distance in distances if distance is not None)
    if any(distance is None for distance in distances):
        distance = f"**~{format_gm(known_gm)}** (some legs' distance unavailable)"
    else:
        distance = f"**{format_gm(known_gm)}** total"
    blocks = ["\n".join([
        f"## #{index} · {path}",
        f"Profit **{route.profit:,.0f} aUEC** · ROI **{route.roi_pct:.1f}%**",
        f"Investment **{route.investment:,.0f}** · Revenue **{route.revenue:,.0f} aUEC**",
        f"Distance {distance} · Confidence **{confidence.label} ({confidence.score}/100)**",
        f"-# {len(legs)}-leg chain for {ship_name} · ranked by profit (ROI as a tie-breaker)"
        f"{' · space stations only' if space_only else ''}",
    ])]
    stations_described: set[int] = set()
    for number, (leg, facts) in enumerate(zip(legs, leg_facts), 1):
        lines = [f"### Leg {number} · {leg.origin_name} → {leg.destination_name}"]
        for item in leg.cargo:
            notes = cargo_item_notes(item, status_lookup=status_lookup, price_outlier_index=price_outlier_index)
            lines.append(f"**{item.commodity_name}** · {item.quantity_scu:,.0f} SCU · "
                         f"+{item.profit_per_scu:,.0f}/SCU · **+{item.profit:,.0f}**")
            details = notes.limit[:1].upper() + notes.limit[1:]
            if notes.market_status:
                details += f" · market status: {', '.join(notes.market_status)}"
            lines.append(f"-# {details}")
            if notes.risk:
                lines.append(f"⚠️ {item.commodity_name}: {notes.risk.removeprefix('⚠️ ')}")
            lines += [f"⚠️ {item.commodity_name} {warning.removeprefix('⚠️ ')}" for warning in notes.outliers]
        leg_distance = format_gm(facts.distance_gm) if facts.distance_gm is not None else "distance unavailable"
        lines.append(f"-# Investment {leg.investment:,.0f} · revenue {leg.revenue:,.0f} · "
                     f"leg profit {leg.profit:,.0f} · {leg_distance}")
        source, destination = leg.cargo[0].source, leg.cargo[0].destination
        if crossing := system_crossing(source.get("star_system_name"), destination.get("star_system_name")):
            lines.append(f"⚠️ Crosses systems: `{crossing[0]}` → `{crossing[1]}`")
        small_print = []
        for station_id, name, terminal, health in (
            (leg.origin_id, leg.origin_name, source, facts.origin_health),
            (leg.destination_id, leg.destination_name, destination, facts.destination_health),
        ):
            if station_id in stations_described:
                continue
            stations_described.add(station_id)
            lines += terminal_limit_notes(name, terminal)
            if note := format_health_note(health):
                lines.append(f"⚠️ {name}: {note.removeprefix('⚠️ ')}")
            if terminal.get("is_cargo_center"):
                small_print.append(f"-# {name} has a cargo center")
        blocks.append("\n".join([*lines, *small_print]))
    blocks.append(f"-# {footer}")
    return tuple(blocks)
