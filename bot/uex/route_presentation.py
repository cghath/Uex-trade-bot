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

from typing import Any, Iterable, Protocol

from bot.uex.commodity_risk import format_commodity_risk
from bot.uex.data_health import TerminalDataHealth, format_health_note
from bot.uex.mixed_routes import format_limiting_factors
from bot.uex.route_confidence import RouteConfidence, compute_route_confidence
from bot.uex.status import StatusLookup, resolve_status_label
from bot.uex.supply_demand import has_sell_side_demand

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


def add_chunked_fields(embed: Any, *, name: str, lines: list[str]) -> bool:
    """Add one logical field as many Discord-safe continuation fields as needed - but only
    if the WHOLE set fits within Discord's combined 6000-char embed limit, never just part
    of it. All-or-nothing, not a per-chunk check: a route's cargo-risk warning often lands
    in a trailing continuation chunk (built after the price/summary lines fill the first
    1024-char chunk), so a per-chunk budget check that added the first chunk and only then
    discovered the second didn't fit left that route visible on screen with its warning
    silently missing - worse than omitting the whole route, since a visible route with no
    warning reads as "checked and safe." Returns False (adding nothing at all) the moment
    the full set would overflow, so a caller adding several logical fields in a loop (e.g.
    one per route) can treat this one as entirely omitted and stop early."""
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
        embed.add_field(name=safe_name, value=chunk, inline=False)
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
    commodity_name: str
    source: dict[str, Any]
    destination: dict[str, Any]
    limiting_factors: tuple[str, ...]
    quantity_scu: float
    profit_per_scu: float
    profit: float


def cargo_item_warnings(item: _CargoItemLike, *, status_lookup: StatusLookup, prefix: str = "") -> list[str]:
    """Risk, limiting-factor, and buy/sell market-status lines for one MixedCargoItem-
    shaped object. `prefix` is prepended to each line verbatim (e.g. "Leg 2 ") - not
    inserted after a warning emoji, since these lines don't all carry one."""
    lines: list[str] = []
    if risk := format_commodity_risk(item.source):
        lines.append(f"{prefix}{item.commodity_name}: {risk}")
    lines.append(f"{prefix}{item.commodity_name}: {format_limiting_factors(item.limiting_factors)}")
    buy_status = resolve_status_label(status_lookup, "buy", item.source.get("status_buy"))
    sell_status = resolve_status_label(status_lookup, "sell", item.destination.get("status_sell"))
    if buy_status or sell_status:
        status_bits = []
        if buy_status:
            status_bits.append(f"origin {buy_status}")
        if sell_status:
            status_bits.append(f"destination {sell_status}")
        lines.append(f"{prefix}{item.commodity_name} market status: {' · '.join(status_bits)}")
    return lines


def cargo_item_line(item: _CargoItemLike) -> str:
    """'• **Name:** N SCU · +P/SCU · **T profit**' - one line per cargo item."""
    return (
        f"• **{item.commodity_name}:** {item.quantity_scu:,.0f} SCU · "
        f"+{item.profit_per_scu:,.0f}/SCU · **{item.profit:,.0f} profit**"
    )


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
    origin = str(origin_system).strip() if origin_system else ""
    destination = str(destination_system).strip() if destination_system else ""
    cross_system = bool(origin) and bool(destination) and origin != destination
    if cross_system:
        if has_real_distance:
            return f"⚠️ {prefix}crosses systems: {origin} → {destination}"
        return f"⚠️ {prefix}Cross-system route: {origin} → {destination}; compare profit against travel time"
    if has_real_distance:
        return None
    return f"⚠️ {prefix}Travel time/distance is not included in this ranking"


def capital_access_note(scope: str) -> str:
    """scope describes how many stops the check covers, e.g. 'both ends', 'every stop'."""
    return f"Capital-ship access confirmed: XL hangar or external cargo loading dock at {scope}"


def approximation_note(is_exact: bool, *, per_leg: bool = False) -> str | None:
    """Lowercase, footer-joinable fragment (e.g. append after ' · '); None when the
    allocation is exact. A caller needing a standalone warning line instead of a footer
    fragment should prefix it with '⚠️ ' and capitalize the first letter."""
    if is_exact:
        return None
    kind = "per-leg cargo allocation" if per_leg else "cargo allocation"
    return f"{kind} for this route is approximate, not proven-optimal"
