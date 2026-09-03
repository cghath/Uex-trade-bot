"""Price lookup and trade-route commands backed by UEX /commodities_prices."""
from __future__ import annotations

import asyncio
import logging

import discord
from discord import app_commands
from discord.ext import commands

from bot.cogs.ships import ship_name_autocomplete
from bot.uex.exceptions import UexApiError, describe_uex_api_error
from bot.uex.data_health import classify_terminal_health, format_health_note
from bot.uex.route_confidence import coalesce_report_count, compute_route_confidence
from bot.uex.practical_routes import route_in_system, route_practical_notes, route_supports_auto_load
from bot.uex.commodity_risk import format_commodity_risk
from bot.uex.supply_demand import analyze_terminal_market_history, has_sell_side_demand
from bot.uex.ships import estimate_route_cargo, resolve_ship
from bot.uex.status import build_status_lookup, resolve_status_label
from bot.uex.trading import best_buy_locations, best_routes, best_sell_locations
from bot.uex.mixed_routes import build_mixed_routes, requires_capital_cargo_access
from bot.uex.multi_stop_routes import build_multi_stop_routes

logger = logging.getLogger("uexbot.prices")

MAX_FIELD_ROWS = 5

# Confirmed live via UEX /terminals: exactly these three values exist for star_system_name.
SYSTEM_CHOICES = [
    app_commands.Choice(name="Stanton", value="Stanton"),
    app_commands.Choice(name="Pyro", value="Pyro"),
    app_commands.Choice(name="Nyx", value="Nyx"),
]


def _positive_int(value: object) -> int | None:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def _chunk_lines(lines: list[str], max_length: int = 1024) -> list[str]:
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


def _add_chunked_fields(embed: discord.Embed, *, name: str, lines: list[str]) -> None:
    """Add one logical field as many Discord-safe continuation fields as needed."""
    for index, chunk in enumerate(_chunk_lines(lines), 1):
        suffix = f" (continued {index})" if index > 1 else ""
        safe_name = f"{name[:256 - len(suffix)]}{suffix}"
        embed.add_field(name=safe_name, value=chunk, inline=False)


async def commodity_name_autocomplete(interaction: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
    """Autocomplete for a plain commodity-name text option, same pattern as
    ship_name_autocomplete (bot/cogs/ships.py) and item_name_autocomplete
    (bot/cogs/marketplace.py) - scoped to /commodities (cached 12h client-side), restricted to
    commodities actually flagged tradeable (is_buyable or is_sellable), matching the same
    "tradeable" definition Trends.refresh_trending already uses."""
    try:
        commodities = await interaction.client.uex.get_commodities()
    except UexApiError:
        return []
    tradeable = [c for c in commodities if c.get("is_buyable") or c.get("is_sellable")]
    current_lower = current.lower()
    matches = [c for c in tradeable if current_lower in (c.get("name") or "").lower()][:25]
    return [app_commands.Choice(name=(c.get("name") or "")[:100], value=c.get("name") or "") for c in matches]


async def terminal_history_autocomplete(
    interaction: discord.Interaction, current: str
) -> list[app_commands.Choice[str]]:
    """Suggest collected terminals for the commodity already entered in the command."""
    commodity = str(getattr(interaction.namespace, "commodity", "") or "").strip()
    if not commodity:
        return []
    names = await interaction.client.db.find_terminal_market_names(commodity, current, limit=25)
    return [app_commands.Choice(name=name[:100], value=name[:100]) for name in names]


class Prices(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    async def _get_status_lookup(self) -> dict:
        """Best-effort readable-label lookup for status_buy/status_sell codes. Cached
        24h client-side, so this is cheap; a failure here just means labels are omitted,
        never a hard error for the calling command."""
        try:
            status_data = await self.bot.uex.get_commodities_status()
        except UexApiError as exc:
            logger.info("Status labels unavailable: %s", exc)
            return {"buy": {}, "sell": {}}
        return build_status_lookup(status_data)

    @app_commands.command(name="price", description="Show current buy/sell prices for a commodity across terminals.")
    @app_commands.describe(commodity="Commodity name, e.g. 'Gold' or 'Laranite'")
    @app_commands.autocomplete(commodity=commodity_name_autocomplete)
    async def price(self, interaction: discord.Interaction, commodity: str) -> None:
        await interaction.response.defer()
        try:
            rows = await self.bot.uex.get_commodities_prices(commodity_name=commodity)
        except UexApiError as exc:
            await interaction.followup.send(describe_uex_api_error(exc))
            return

        if not rows:
            await interaction.followup.send(f"No price data found for '{commodity}'. Check the spelling.")
            return

        commodity_display = rows[0].get("commodity_name", commodity)
        embed = discord.Embed(title=f"{commodity_display} — Prices", color=discord.Color.blurple())

        top_sell = best_sell_locations(rows, limit=MAX_FIELD_ROWS)
        top_buy = best_buy_locations(rows, limit=MAX_FIELD_ROWS)
        status_lookup = await self._get_status_lookup()

        terminal_ids = [
            terminal_id
            for r in [*top_sell, *top_buy]
            if (terminal_id := _positive_int(r.get("id_terminal"))) is not None
        ]
        health_rows = await self.bot.db.get_terminal_data_health_by_ids(terminal_ids)
        health_notes = {
            terminal_id: note
            for terminal_id, row in health_rows.items()
            if (note := format_health_note(classify_terminal_health(row)))
        }

        if top_sell:
            lines = []
            for r in top_sell:
                label = resolve_status_label(status_lookup, "sell", r.get("status_sell"))
                label_text = f" · {label}" if label else ""
                health_note = health_notes.get(_positive_int(r.get("id_terminal")))
                health_text = f" · {health_note}" if health_note else ""
                lines.append(f"**{r['terminal_name']}** — {r['price_sell']:.2f} aUEC/unit{label_text}{health_text}")
            embed.add_field(name="Best places to SELL", value="\n".join(lines), inline=False)
        if top_buy:
            lines = []
            for r in top_buy:
                label = resolve_status_label(status_lookup, "buy", r.get("status_buy"))
                label_text = f" · {label}" if label else ""
                health_note = health_notes.get(_positive_int(r.get("id_terminal")))
                health_text = f" · {health_note}" if health_note else ""
                lines.append(f"**{r['terminal_name']}** — {r['price_buy']:.2f} aUEC/unit{label_text}{health_text}")
            embed.add_field(name="Best places to BUY", value="\n".join(lines), inline=False)

        embed.set_footer(text="Data from UEX Corp · cached up to 30 min · status = current stock/demand level")
        await interaction.followup.send(embed=embed)

    @app_commands.command(
        name="terminal-history",
        description="Show historical supply and demand reliability for one commodity at a terminal.",
    )
    @app_commands.describe(
        commodity="Commodity name, e.g. 'Gold' or 'Laranite'",
        terminal="Exact terminal name as shown by /price",
    )
    @app_commands.autocomplete(
        commodity=commodity_name_autocomplete,
        terminal=terminal_history_autocomplete,
    )
    async def terminal_history(
        self, interaction: discord.Interaction, commodity: str, terminal: str
    ) -> None:
        await interaction.response.defer()
        state, observations = await self.bot.db.get_terminal_market_history(commodity, terminal)
        if not state:
            suggestions = await self.bot.db.find_terminal_market_names(commodity, terminal)
            suggestion_text = f" Try: {', '.join(suggestions[:5])}" if suggestions else ""
            await interaction.followup.send(
                f"No collected history found for **{commodity}** at **{terminal}**.{suggestion_text}"
            )
            return

        history = analyze_terminal_market_history(observations, observed_until=state["last_seen"])
        if not history:
            await interaction.followup.send(
                f"History collection has started for **{state['commodity_name']}** at "
                f"**{state['terminal_name']}**, but it needs another collector cycle before analysis."
            )
            return

        color = discord.Color.green() if history.enough_history else discord.Color.gold()
        embed = discord.Embed(
            title=f"{state['commodity_name']} — {state['terminal_name']}",
            description="Time-weighted from locally collected terminal states.",
            color=color,
        )
        embed.add_field(name="Supply available", value=f"**{history.supply_available_pct:.1f}%** of observed time")
        embed.add_field(name="Buyer demand", value=f"**{history.demand_available_pct:.1f}%** of observed time")
        embed.add_field(name="State changes", value=f"**{history.state_changes}**", inline=True)
        footer = f"Observed for {history.observed_hours:.1f} hours"
        if not history.enough_history:
            footer += " · preliminary: needs at least 24 hours"
        embed.set_footer(text=footer)
        await interaction.followup.send(embed=embed)

    @app_commands.command(name="best-route", description="Find the most profitable buy->sell terminal pair for a commodity.")
    @app_commands.describe(
        commodity="Commodity name, e.g. 'Gold' or 'Laranite'",
        ship="Optional: check cargo for a specific ship instead of your default (/set-default-ship)",
        auto_load_only="Only show routes where both the origin and destination terminal offer UEX's auto-load",
        system="Optional: require both ends of the route to be in this star system",
    )
    @app_commands.rename(auto_load_only="auto-load-only")
    @app_commands.choices(system=SYSTEM_CHOICES)
    @app_commands.autocomplete(ship=ship_name_autocomplete, commodity=commodity_name_autocomplete)
    async def best_route(
        self,
        interaction: discord.Interaction,
        commodity: str,
        ship: str | None = None,
        auto_load_only: bool = False,
        system: app_commands.Choice[str] | None = None,
    ) -> None:
        await interaction.response.defer()
        system_value = system.value if system else None
        try:
            rows = await self.bot.uex.get_commodities_prices(commodity_name=commodity)
        except UexApiError as exc:
            await interaction.followup.send(describe_uex_api_error(exc))
            return

        if not rows:
            await interaction.followup.send(f"No price data found for '{commodity}'. Check the spelling.")
            return

        id_commodity = rows[0].get("id_commodity")
        commodity_display = rows[0].get("commodity_name", commodity)

        # Resolve the ship to use for cargo math: an explicit /best-route option wins,
        # otherwise fall back to the user's saved default (/set-default-ship). Either way
        # this is optional - with no ship known we still show stock-limited cargo when
        # UEX reports it, just without a ship-capacity comparison.
        ship_query = ship or await self.bot.db.get_default_ship(interaction.user.id)
        ship_vehicle = None
        if ship_query:
            try:
                vehicles = await self.bot.uex.get_vehicles()
                ship_vehicle = resolve_ship(vehicles, ship_query)
            except UexApiError as exc:
                logger.info("Vehicle lookup failed for '%s': %s", ship_query, exc)
        ship_cargo_scu = ship_vehicle.get("scu") if ship_vehicle else None
        status_lookup = await self._get_status_lookup()

        risk_warning: str | None = None
        if id_commodity is not None:
            commodity_references = await self.bot.db.get_commodity_references([int(id_commodity)])
            risk_warning = format_commodity_risk(commodity_references.get(int(id_commodity)))

        # Prefer UEX's own precomputed routes (real inter-terminal distance, ROI, profit,
        # and a UEX quality score) over our own buy/sell pairing, which has no distance data.
        uex_routes: list[dict] = []
        if id_commodity is not None:
            try:
                uex_routes = await self.bot.uex.get_commodities_routes(id_commodity=id_commodity)
            except UexApiError as exc:
                logger.info("commodities_routes unavailable for %s, falling back: %s", commodity_display, exc)

        if uex_routes:
            # Filter the FULL candidate list before ranking/truncating to MAX_FIELD_ROWS,
            # not after - filtering an already-sliced top-5 can throw away every route
            # that would have passed just because they weren't the top 5 by profit.
            route_terminal_ids = [
                terminal_id
                for route in uex_routes
                for terminal_id in (
                    _positive_int(route.get("id_terminal_origin")),
                    _positive_int(route.get("id_terminal_destination")),
                )
                if terminal_id is not None
            ]
            terminal_references = await self.bot.db.get_terminal_references_by_ids(route_terminal_ids)
            candidates = uex_routes
            if auto_load_only:
                candidates = [
                    r for r in candidates
                    if route_supports_auto_load(
                        terminal_references.get(_positive_int(r.get("id_terminal_origin"))),
                        terminal_references.get(_positive_int(r.get("id_terminal_destination"))),
                    )
                ]
                if not candidates:
                    await interaction.followup.send(
                        f"No auto-load-capable routes found for '{commodity_display}' right now."
                    )
                    return
            if system_value is not None:
                candidates = [
                    r for r in candidates
                    if route_in_system(
                        terminal_references.get(_positive_int(r.get("id_terminal_origin"))),
                        terminal_references.get(_positive_int(r.get("id_terminal_destination"))),
                        system_value,
                    )
                ]
                if not candidates:
                    await interaction.followup.send(
                        f"No routes confirmed entirely within {system_value} found for '{commodity_display}' right now."
                    )
                    return
            ranked = sorted(candidates, key=lambda r: r.get("profit") or 0, reverse=True)[:MAX_FIELD_ROWS]
            ranked_terminal_ids = [
                terminal_id
                for route in ranked
                for terminal_id in (
                    _positive_int(route.get("id_terminal_origin")),
                    _positive_int(route.get("id_terminal_destination")),
                )
                if terminal_id is not None
            ]
            health_rows = await self.bot.db.get_terminal_data_health_by_ids(ranked_terminal_ids)
            health_notes = {
                terminal_id: note
                for terminal_id, row in health_rows.items()
                if (note := format_health_note(classify_terminal_health(row)))
            }
            live_signals = {
                terminal_id: row
                for row in rows
                if (terminal_id := _positive_int(row.get("id_terminal"))) is not None
            }
            embed = discord.Embed(title=f"{commodity_display} — Best Trade Routes", color=discord.Color.green())
            if risk_warning:
                embed.description = risk_warning
            for r in ranked:
                origin = r.get("origin_terminal_name", "Unknown")
                dest = r.get("destination_terminal_name", "Unknown")
                origin_id = _positive_int(r.get("id_terminal_origin"))
                destination_id = _positive_int(r.get("id_terminal_destination"))
                price_origin = r.get("price_origin") or 0
                price_destination = r.get("price_destination") or 0
                # price_margin/price_roi from UEX are both PERCENTAGES (margin relative to
                # sell price, ROI relative to buy price) - not aUEC amounts. The per-unit
                # aUEC difference is computed here directly so it's never mislabeled.
                per_unit_diff = price_destination - price_origin
                margin_pct = r.get("price_margin")
                roi_pct = r.get("price_roi")
                distance = r.get("distance")
                score = r.get("score")

                value_lines = [
                    f"Buy {price_origin:.2f} / Sell {price_destination:.2f} (+{per_unit_diff:.2f} aUEC/unit)"
                ]
                for side, terminal_id in (("origin", origin_id), ("destination", destination_id)):
                    health_note = health_notes.get(terminal_id) if terminal_id is not None else None
                    if health_note:
                        value_lines.append(f"{side.title()}: {health_note}")

                buy_status = resolve_status_label(status_lookup, "buy", r.get("status_origin"))
                sell_status = resolve_status_label(status_lookup, "sell", r.get("status_destination"))
                if buy_status or sell_status:
                    status_bits = []
                    if buy_status:
                        status_bits.append(f"buy side: {buy_status}")
                    if sell_status:
                        status_bits.append(f"sell side: {sell_status}")
                    value_lines.append(" · ".join(status_bits))

                cargo = estimate_route_cargo(
                    per_unit_profit=per_unit_diff,
                    origin_scu_available=r.get("scu_origin"),
                    destination_scu_wanted=r.get("scu_destination"),
                    ship_cargo_scu=ship_cargo_scu,
                )
                if cargo is not None:
                    limit_note = {
                        "ship": f"limited by {ship_vehicle.get('name')}'s cargo hold" if ship_vehicle else "limited by ship capacity",
                        "stock": "limited by available stock, not your ship",
                    }.get(cargo.limited_by, "")
                    cargo_line = f"Cargo: **{cargo.max_scu:,.0f} SCU**"
                    if limit_note:
                        cargo_line += f" ({limit_note})"
                    if cargo.run_profit is not None:
                        cargo_line += f"\nRun profit: **{cargo.run_profit:,.0f} aUEC** for this haul"
                    value_lines.append(cargo_line)
                elif not ship_vehicle:
                    value_lines.append("Cargo: unknown (set a ship with /set-default-ship to see haulable SCU)")

                pct_bits = []
                if margin_pct is not None:
                    pct_bits.append(f"margin {margin_pct:.1f}%")
                if roi_pct is not None:
                    pct_bits.append(f"ROI {roi_pct:.1f}%")
                if pct_bits:
                    value_lines.append(" · ".join(pct_bits))
                loc_bits = []
                if distance is not None:
                    loc_bits.append(f"{distance:.1f} GM")
                if score is not None:
                    loc_bits.append(f"UEX score {score:,.0f}")
                if loc_bits:
                    value_lines.append(" · ".join(loc_bits))
                origin_signal = live_signals.get(origin_id, {})
                destination_signal = live_signals.get(destination_id, {})
                confidence = compute_route_confidence(
                    origin_health=(classify_terminal_health(health_rows[origin_id])
                                   if origin_id in health_rows else None),
                    destination_health=(classify_terminal_health(health_rows[destination_id])
                                        if destination_id in health_rows else None),
                    origin_report_count=coalesce_report_count(
                        origin_signal.get("price_buy_users_rows"),
                        origin_signal.get("scu_buy_users_rows"),
                    ),
                    destination_report_count=coalesce_report_count(
                        destination_signal.get("price_sell_users_rows"),
                        destination_signal.get("scu_sell_users_rows"),
                    ),
                    volatility_origin=r.get("volatility_origin"),
                    volatility_destination=r.get("volatility_destination"),
                    origin_available=bool(r.get("scu_origin") and r.get("scu_origin") > 0),
                    destination_available=has_sell_side_demand(
                        r.get("scu_destination"), r.get("status_destination")
                    ),
                )
                value_lines.append(f"Confidence: **{confidence.label} ({confidence.score}/100)**")
                practical_notes = route_practical_notes(
                    terminal_references.get(origin_id),
                    terminal_references.get(destination_id),
                )
                value_lines.extend(practical_notes)
                _add_chunked_fields(embed, name=f"{origin} → {dest}", lines=value_lines)
            footer = "Data from UEX Corp /commodities_routes"
            if not ship_vehicle:
                footer += " · set a default ship with /set-default-ship for cargo/run-profit numbers"
            embed.set_footer(text=footer)
            await interaction.followup.send(embed=embed)
            return

        # Fallback: derive routes ourselves from raw price rows (no distance data available).
        # best_routes' own `limit` caps BOTH how many buy/sell-side terminals get
        # cross-joined AND how many final routes it returns - a fixed constant here
        # (previously 25) still silently excludes any commodity traded at more terminals
        # than that before the filter ever runs. len(rows)**2 is a real upper bound on
        # possible profitable pairs (each row can be at most one buy AND one sell
        # candidate), so it can never truncate anything - filter the true full list,
        # slice to MAX_FIELD_ROWS after.
        routes = best_routes(rows, limit=max(len(rows), 1) ** 2)
        if not routes:
            await interaction.followup.send(f"No profitable buy/sell pair found for '{commodity}' right now.")
            return

        route_terminal_ids = [
            terminal_id
            for route in routes
            for terminal_id in (route.buy_terminal_id, route.sell_terminal_id)
            if terminal_id is not None
        ]
        fallback_references = await self.bot.db.get_terminal_references_by_ids(route_terminal_ids)
        if auto_load_only:
            routes = [
                route for route in routes
                if route_supports_auto_load(
                    fallback_references.get(route.buy_terminal_id),
                    fallback_references.get(route.sell_terminal_id),
                )
            ]
            if not routes:
                await interaction.followup.send(
                    f"No auto-load-capable routes found for '{commodity}' right now."
                )
                return
        if system_value is not None:
            routes = [
                route for route in routes
                if route_in_system(
                    fallback_references.get(route.buy_terminal_id),
                    fallback_references.get(route.sell_terminal_id),
                    system_value,
                )
            ]
            if not routes:
                await interaction.followup.send(
                    f"No routes confirmed entirely within {system_value} found for '{commodity}' right now."
                )
                return
        routes = routes[:MAX_FIELD_ROWS]
        ranked_terminal_ids = [
            terminal_id
            for route in routes
            for terminal_id in (route.buy_terminal_id, route.sell_terminal_id)
            if terminal_id is not None
        ]
        route_health_rows = await self.bot.db.get_terminal_data_health_by_ids(ranked_terminal_ids)
        health_notes = {
            terminal_id: note
            for terminal_id, row in route_health_rows.items()
            if (note := format_health_note(classify_terminal_health(row)))
        }
        live_signals = {
            terminal_id: row
            for row in rows
            if (terminal_id := _positive_int(row.get("id_terminal"))) is not None
        }

        embed = discord.Embed(
            title=f"{routes[0].commodity_name} — Best Trade Routes",
            color=discord.Color.green(),
        )
        if risk_warning:
            embed.description = risk_warning
        for route in routes:
            value_lines = [
                f"Buy {route.buy_price:.2f} / Sell {route.sell_price:.2f}\n"
                f"Profit: **{route.profit_per_unit:.2f} aUEC/unit** ({route.margin_pct}%)"
            ]
            for side, terminal_id in (("origin", route.buy_terminal_id), ("destination", route.sell_terminal_id)):
                health_note = health_notes.get(terminal_id)
                if health_note:
                    value_lines.append(f"{side.title()}: {health_note}")

            buy_status = resolve_status_label(status_lookup, "buy", route.status_buy_code)
            sell_status = resolve_status_label(status_lookup, "sell", route.status_sell_code)
            if buy_status or sell_status:
                status_bits = []
                if buy_status:
                    status_bits.append(f"buy side: {buy_status}")
                if sell_status:
                    status_bits.append(f"sell side: {sell_status}")
                value_lines.append(" · ".join(status_bits))

            cargo = estimate_route_cargo(
                per_unit_profit=route.profit_per_unit,
                origin_scu_available=route.scu_buy_available,
                destination_scu_wanted=route.scu_sell_wanted,
                ship_cargo_scu=ship_cargo_scu,
            )
            if cargo is not None:
                limit_note = {
                    "ship": f"limited by {ship_vehicle.get('name')}'s cargo hold" if ship_vehicle else "limited by ship capacity",
                    "stock": "limited by available stock, not your ship",
                }.get(cargo.limited_by, "")
                cargo_line = f"Cargo: **{cargo.max_scu:,.0f} SCU**"
                if limit_note:
                    cargo_line += f" ({limit_note})"
                if cargo.run_profit is not None:
                    cargo_line += f"\nRun profit: **{cargo.run_profit:,.0f} aUEC** for this haul"
                value_lines.append(cargo_line)
            elif not ship_vehicle:
                value_lines.append("Cargo: unknown (set a ship with /set-default-ship to see haulable SCU)")

            origin_signal = live_signals.get(route.buy_terminal_id, {})
            destination_signal = live_signals.get(route.sell_terminal_id, {})
            confidence = compute_route_confidence(
                origin_health=(classify_terminal_health(route_health_rows[route.buy_terminal_id])
                               if route.buy_terminal_id in route_health_rows else None),
                destination_health=(classify_terminal_health(route_health_rows[route.sell_terminal_id])
                                    if route.sell_terminal_id in route_health_rows else None),
                origin_report_count=coalesce_report_count(
                    origin_signal.get("price_buy_users_rows"),
                    origin_signal.get("scu_buy_users_rows"),
                ),
                destination_report_count=coalesce_report_count(
                    destination_signal.get("price_sell_users_rows"),
                    destination_signal.get("scu_sell_users_rows"),
                ),
                volatility_origin=origin_signal.get("volatility_price_buy"),
                volatility_destination=destination_signal.get("volatility_price_sell"),
                origin_available=bool(route.scu_buy_available and route.scu_buy_available > 0),
                destination_available=has_sell_side_demand(
                    route.scu_sell_wanted, route.status_sell_code
                ),
            )
            value_lines.append(f"Confidence: **{confidence.label} ({confidence.score}/100)**")
            if id_commodity is not None:
                value_lines.extend(
                    route_practical_notes(
                        fallback_references.get(route.buy_terminal_id),
                        fallback_references.get(route.sell_terminal_id),
                    )
                )
            # This fallback (no UEX /commodities_routes data for this commodity) has no real
            # distance figure the way the primary branch above does - say so explicitly
            # instead of silently ranking purely on price the way /mixed-routes already does
            # for the same reason.
            origin_system = (fallback_references.get(route.buy_terminal_id) or {}).get("star_system_name")
            destination_system = (fallback_references.get(route.sell_terminal_id) or {}).get("star_system_name")
            if origin_system and destination_system and origin_system != destination_system:
                value_lines.append(
                    f"⚠️ Cross-system route: {origin_system} → {destination_system}; compare profit against travel time"
                )
            else:
                value_lines.append("⚠️ Travel time/distance is not included in this ranking")

            _add_chunked_fields(
                embed,
                name=f"{route.buy_terminal} → {route.sell_terminal}",
                lines=value_lines,
            )

        footer = "Data from UEX Corp · does not account for travel time between terminals"
        if not ship_vehicle:
            footer += " · set a default ship with /set-default-ship for cargo/run-profit numbers"
        embed.set_footer(text=footer)
        await interaction.followup.send(embed=embed)

    @app_commands.command(
        name="mixed-routes",
        description="Find the five best two- or three-commodity loads for your ship and budget.",
    )
    @app_commands.describe(
        ship="Optional: use a specific ship instead of your saved default",
        budget="Optional maximum aUEC to invest in the cargo",
        space_only="Exclude surface terminals; require both ends to be confirmed space stations",
        auto_load_only="Only show loads where both the origin and destination terminal offer UEX's auto-load",
        system="Optional: require both ends of the load to be in this star system",
    )
    @app_commands.rename(space_only="space-only", auto_load_only="auto-load-only")
    @app_commands.choices(system=SYSTEM_CHOICES)
    @app_commands.autocomplete(ship=ship_name_autocomplete)
    async def mixed_routes(
        self,
        interaction: discord.Interaction,
        ship: str | None = None,
        budget: app_commands.Range[float, 1, 1_000_000_000] | None = None,
        space_only: bool = False,
        auto_load_only: bool = False,
        system: app_commands.Choice[str] | None = None,
    ) -> None:
        await interaction.response.defer()
        system_value = system.value if system else None

        ship_query = ship or await self.bot.db.get_default_ship(interaction.user.id)
        if not ship_query:
            await interaction.followup.send(
                "Set a default ship with `/set-default-ship`, or provide the `ship` option, "
                "so mixed routes can be ranked against a real cargo limit."
            )
            return
        try:
            vehicles = await self.bot.uex.get_vehicles()
        except UexApiError as exc:
            await interaction.followup.send(describe_uex_api_error(exc))
            return
        ship_vehicle = resolve_ship(vehicles, ship_query)
        if not ship_vehicle or not ship_vehicle.get("scu"):
            await interaction.followup.send(
                f"I couldn't resolve a cargo capacity for **{ship_query}**. "
                "Choose a ship from autocomplete or update `/set-default-ship`."
            )
            return

        market_rows = await self.bot.db.get_mixed_route_market_rows()
        capital_access_only = requires_capital_cargo_access(ship_vehicle)
        if capital_access_only:
            try:
                stations = await self.bot.uex.get_space_stations()
            except UexApiError as exc:
                await interaction.followup.send(
                    "I couldn't verify XL-hangar/loading-dock access for this capital ship, "
                    f"so I won't recommend potentially unusable routes: {exc}"
                )
                return
            stations_by_id = {
                int(station["id"]): station
                for station in stations
                if station.get("id") is not None and int(station["id"]) > 0
            }
            for row in market_rows:
                station_id = int(row.get("id_space_station") or 0)
                station = stations_by_id.get(station_id, {})
                row["station_pad_types"] = station.get("pad_types")
                row["station_has_loading_dock"] = station.get("has_loading_dock")
        # Cargo allocation can run an exact combinatorial search per candidate route
        # (see allocate_pair_cargo) - dense market data can make that expensive enough
        # to matter, and this call would otherwise run synchronously on the bot's one
        # asyncio event loop, delaying every other interaction and background poller
        # for as long as it takes. Offload it to a worker thread instead.
        routes = await asyncio.to_thread(
            build_mixed_routes,
            market_rows,
            ship_capacity_scu=float(ship_vehicle["scu"]),
            budget=float(budget) if budget is not None else None,
            limit=5,
            max_commodities=3,
            space_only=space_only,
            capital_access_only=capital_access_only,
            auto_load_only=auto_load_only,
            system=system_value,
        )
        if not routes:
            budget_note = " within that budget" if budget is not None else ""
            safety_note = " using confirmed space stations only" if space_only else ""
            access_note = " with confirmed capital-ship cargo access" if capital_access_only else ""
            auto_load_note = " with auto-load at the origin" if auto_load_only else ""
            system_note = f" entirely within {system_value}" if system_value else ""
            await interaction.followup.send(
                f"No two- or three-commodity loads fit **{ship_vehicle.get('name', ship_query)}**"
                f"{budget_note}{safety_note}{access_note}{auto_load_note}{system_note} right now."
            )
            return

        terminal_ids = [terminal_id for route in routes for terminal_id in (route.origin_id, route.destination_id)]
        health_rows = await self.bot.db.get_terminal_data_health_by_ids(terminal_ids)
        status_lookup = await self._get_status_lookup()
        embeds: list[discord.Embed] = []
        for index, route in enumerate(routes, 1):
            origin_health = (
                classify_terminal_health(health_rows[route.origin_id])
                if route.origin_id in health_rows else None
            )
            destination_health = (
                classify_terminal_health(health_rows[route.destination_id])
                if route.destination_id in health_rows else None
            )
            cargo_lines = [
                f"• **{item.commodity_name}:** {item.quantity_scu:,.0f} SCU · "
                f"+{item.profit_per_scu:,.0f}/SCU · **{item.profit:,.0f} profit**"
                for item in route.cargo
            ]
            warnings: list[str] = []
            for side, health in (("Origin", origin_health), ("Destination", destination_health)):
                if note := format_health_note(health):
                    warnings.append(f"{side}: {note}")
            for item in route.cargo:
                if risk := format_commodity_risk(item.source):
                    warnings.append(f"{item.commodity_name}: {risk}")
                source_stock = float(item.source.get("scu_buy") or 0)
                destination_demand = float(item.destination.get("scu_sell") or 0)
                if item.available_scu < float(ship_vehicle["scu"]):
                    if source_stock <= destination_demand:
                        warnings.append(
                            f"⚠️ {item.commodity_name}: origin stock limits this load to {item.available_scu:,.0f} SCU"
                        )
                    else:
                        warnings.append(
                            f"⚠️ {item.commodity_name}: destination demand limits this load to {item.available_scu:,.0f} SCU"
                        )
                buy_status = resolve_status_label(status_lookup, "buy", item.source.get("status_buy"))
                sell_status = resolve_status_label(status_lookup, "sell", item.destination.get("status_sell"))
                if buy_status or sell_status:
                    status_bits = []
                    if buy_status:
                        status_bits.append(f"origin {buy_status}")
                    if sell_status:
                        status_bits.append(f"destination {sell_status}")
                    warnings.append(f"{item.commodity_name} market status: {' · '.join(status_bits)}")
            warnings.extend(route_practical_notes(route.cargo[0].source, route.cargo[0].destination))
            if capital_access_only:
                warnings.append("Capital-ship access confirmed: XL hangar or external cargo loading dock at both ends")
            origin_system = route.cargo[0].source.get("star_system_name")
            destination_system = route.cargo[0].destination.get("star_system_name")
            if origin_system and destination_system and origin_system != destination_system:
                warnings.append(
                    f"⚠️ Cross-system route: {origin_system} → {destination_system}; compare profit against travel time"
                )
            else:
                warnings.append("⚠️ Travel time/distance is not included in this ranking")

            item_confidences = [
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
                for item in route.cargo
            ]
            confidence = min(item_confidences, key=lambda value: value.score)
            value_lines = [
                *cargo_lines,
                f"Cargo: **{route.cargo_scu:,.0f}/{float(ship_vehicle['scu']):,.0f} SCU**",
                f"Investment: **{route.investment:,.0f}** · Revenue: **{route.revenue:,.0f} aUEC**",
                f"Profit: **{route.profit:,.0f} aUEC** · ROI: **{route.roi_pct:.1f}%**",
                f"Confidence: **{confidence.label} ({confidence.score}/100)**",
            ]
            route_embed = discord.Embed(
                title=f"#{index} {route.origin_name} → {route.destination_name}",
                description=(
                    f"Mixed load for **{ship_vehicle.get('name', ship_query)}** · "
                    f"ranked by estimated haul profit{' · space stations only' if space_only else ''}"
                ),
                color=discord.Color.green(),
            )
            route_embed.add_field(
                name="Cargo plan",
                value="\n".join(value_lines),
                inline=False,
            )
            # Keep warnings in their own field so Discord's 1,024-character route-field
            # limit can never silently trim them from a profitable-looking result.
            unique_warnings = list(dict.fromkeys(warnings))
            warning_chunks = _chunk_lines(unique_warnings)
            for warning_index, warning_chunk in enumerate(warning_chunks, 1):
                continuation = f" (continued {warning_index})" if warning_index > 1 else ""
                route_embed.add_field(
                    name=f"Warnings & practical checks{continuation}",
                    value=warning_chunk,
                    inline=False,
                )
            footer = "Collected UEX data · prices can change before arrival · warnings do not change profit ranking"
            if budget is not None:
                footer += f" · budget {float(budget):,.0f} aUEC"
            if space_only:
                footer += " · surface terminals excluded"
            if capital_access_only:
                footer += " · capital access confirmed at both ends"
            if not route.is_exact:
                footer += " · cargo allocation for this route is approximate, not proven-optimal"
            route_embed.set_footer(text=footer)
            embeds.append(route_embed)
        await interaction.followup.send(embeds=embeds)

    @app_commands.command(
        name="multi-stop-route",
        description="Chain 2-3 profitable hops across multiple stops for your ship and budget.",
    )
    @app_commands.describe(
        ship="Optional: use a specific ship instead of your saved default",
        budget="Optional starting aUEC to invest - profit compounds into later legs",
        space_only="Exclude surface terminals; require every stop to be a confirmed space station",
        auto_load_only="Only show chains where every stop offers UEX's auto-load",
        system="Optional: require every stop in the chain to be in this star system",
    )
    @app_commands.rename(space_only="space-only", auto_load_only="auto-load-only")
    @app_commands.choices(system=SYSTEM_CHOICES)
    @app_commands.autocomplete(ship=ship_name_autocomplete)
    async def multi_stop_route(
        self,
        interaction: discord.Interaction,
        ship: str | None = None,
        budget: app_commands.Range[float, 1, 1_000_000_000] | None = None,
        space_only: bool = False,
        auto_load_only: bool = False,
        system: app_commands.Choice[str] | None = None,
    ) -> None:
        await interaction.response.defer()
        system_value = system.value if system else None

        ship_query = ship or await self.bot.db.get_default_ship(interaction.user.id)
        if not ship_query:
            await interaction.followup.send(
                "Set a default ship with `/set-default-ship`, or provide the `ship` option, "
                "so a multi-stop chain can be ranked against a real cargo limit."
            )
            return
        try:
            vehicles = await self.bot.uex.get_vehicles()
        except UexApiError as exc:
            await interaction.followup.send(describe_uex_api_error(exc))
            return
        ship_vehicle = resolve_ship(vehicles, ship_query)
        if not ship_vehicle or not ship_vehicle.get("scu"):
            await interaction.followup.send(
                f"I couldn't resolve a cargo capacity for **{ship_query}**. "
                "Choose a ship from autocomplete or update `/set-default-ship`."
            )
            return

        market_rows = await self.bot.db.get_mixed_route_market_rows()
        capital_access_only = requires_capital_cargo_access(ship_vehicle)
        if capital_access_only:
            try:
                stations = await self.bot.uex.get_space_stations()
            except UexApiError as exc:
                await interaction.followup.send(
                    "I couldn't verify XL-hangar/loading-dock access for this capital ship, "
                    f"so I won't recommend potentially unusable routes: {exc}"
                )
                return
            stations_by_id = {
                int(station["id"]): station
                for station in stations
                if station.get("id") is not None and int(station["id"]) > 0
            }
            for row in market_rows:
                station_id = int(row.get("id_space_station") or 0)
                station = stations_by_id.get(station_id, {})
                row["station_pad_types"] = station.get("pad_types")
                row["station_has_loading_dock"] = station.get("has_loading_dock")

        # See the matching comment in mixed_routes above: multi-stop's DFS can call the
        # same exact allocator far more often per command, so offloading it matters even
        # more here.
        routes = await asyncio.to_thread(
            build_multi_stop_routes,
            market_rows,
            ship_capacity_scu=float(ship_vehicle["scu"]),
            budget=float(budget) if budget is not None else None,
            limit=5,
            max_commodities=3,
            space_only=space_only,
            capital_access_only=capital_access_only,
            auto_load_only=auto_load_only,
            system=system_value,
        )
        if not routes:
            budget_note = " within that budget" if budget is not None else ""
            safety_note = " using confirmed space stations only" if space_only else ""
            access_note = " with confirmed capital-ship cargo access" if capital_access_only else ""
            auto_load_note = " with auto-load at every stop" if auto_load_only else ""
            system_note = f" entirely within {system_value}" if system_value else ""
            await interaction.followup.send(
                f"No multi-stop chains fit **{ship_vehicle.get('name', ship_query)}**"
                f"{budget_note}{safety_note}{access_note}{auto_load_note}{system_note} right now."
            )
            return

        terminal_ids = [terminal_id for route in routes for terminal_id in route.stops]
        health_rows = await self.bot.db.get_terminal_data_health_by_ids(terminal_ids)
        status_lookup = await self._get_status_lookup()
        for index, route in enumerate(routes, 1):
            path_label = " → ".join(
                [route.legs[0].origin_name, *(leg.destination_name for leg in route.legs)]
            )
            route_embed = discord.Embed(
                title=f"#{index} {path_label}",
                description=(
                    f"{len(route.legs)}-leg chain for **{ship_vehicle.get('name', ship_query)}** · "
                    f"ranked by total profit{' · space stations only' if space_only else ''}"
                ),
                color=discord.Color.green(),
            )
            warnings: list[str] = []
            leg_confidences = []
            total_distance_gm = 0.0
            distance_partial = False
            for leg_index, leg in enumerate(route.legs, 1):
                origin_health = (
                    classify_terminal_health(health_rows[leg.origin_id])
                    if leg.origin_id in health_rows else None
                )
                destination_health = (
                    classify_terminal_health(health_rows[leg.destination_id])
                    if leg.destination_id in health_rows else None
                )
                try:
                    distance_row = await self.bot.uex.get_terminal_distance(leg.origin_id, leg.destination_id)
                except UexApiError:
                    distance_row = None
                if distance_row and distance_row.get("distance") is not None:
                    total_distance_gm += float(distance_row["distance"])
                    distance_note = f"{float(distance_row['distance']):,.1f} Gm"
                else:
                    distance_partial = True
                    distance_note = "distance unavailable"
                cargo_lines = [
                    f"• **{item.commodity_name}:** {item.quantity_scu:,.0f} SCU · "
                    f"+{item.profit_per_scu:,.0f}/SCU · **{item.profit:,.0f} profit**"
                    for item in leg.cargo
                ]
                leg_lines = [
                    *cargo_lines,
                    f"Investment: **{leg.investment:,.0f}** · Revenue: **{leg.revenue:,.0f} aUEC** · "
                    f"Profit: **{leg.profit:,.0f} aUEC** · {distance_note}",
                ]
                _add_chunked_fields(
                    route_embed,
                    name=f"Leg {leg_index}: {leg.origin_name} → {leg.destination_name}",
                    lines=leg_lines,
                )
                for side, health in (("Origin", origin_health), ("Destination", destination_health)):
                    if note := format_health_note(health):
                        warnings.append(f"Leg {leg_index} {side}: {note}")
                for item in leg.cargo:
                    if risk := format_commodity_risk(item.source):
                        warnings.append(f"Leg {leg_index} {item.commodity_name}: {risk}")
                    source_stock = float(item.source.get("scu_buy") or 0)
                    destination_demand = float(item.destination.get("scu_sell") or 0)
                    if item.available_scu < float(ship_vehicle["scu"]):
                        if source_stock <= destination_demand:
                            warnings.append(
                                f"⚠️ Leg {leg_index} {item.commodity_name}: origin stock limits this load to "
                                f"{item.available_scu:,.0f} SCU"
                            )
                        else:
                            warnings.append(
                                f"⚠️ Leg {leg_index} {item.commodity_name}: destination demand limits this load to "
                                f"{item.available_scu:,.0f} SCU"
                            )
                    buy_status = resolve_status_label(status_lookup, "buy", item.source.get("status_buy"))
                    sell_status = resolve_status_label(status_lookup, "sell", item.destination.get("status_sell"))
                    if buy_status or sell_status:
                        status_bits = []
                        if buy_status:
                            status_bits.append(f"origin {buy_status}")
                        if sell_status:
                            status_bits.append(f"destination {sell_status}")
                        warnings.append(
                            f"Leg {leg_index} {item.commodity_name} market status: {' · '.join(status_bits)}"
                        )
                warnings.extend(
                    f"Leg {leg_index} {note}"
                    for note in route_practical_notes(leg.cargo[0].source, leg.cargo[0].destination)
                )
                origin_system = leg.cargo[0].source.get("star_system_name")
                destination_system = leg.cargo[0].destination.get("star_system_name")
                if origin_system and destination_system and origin_system != destination_system:
                    warnings.append(f"⚠️ Leg {leg_index} crosses systems: {origin_system} → {destination_system}")
                leg_confidences.extend(
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
                    for item in leg.cargo
                )
            if capital_access_only:
                warnings.append(
                    "Capital-ship access confirmed: XL hangar or external cargo loading dock at every stop"
                )
            confidence = min(leg_confidences, key=lambda value: value.score)
            distance_summary = (
                f"~{total_distance_gm:,.1f} Gm (partial - one or more legs' distance unavailable)"
                if distance_partial
                else f"{total_distance_gm:,.1f} Gm total"
            )
            summary_lines = [
                f"Investment: **{route.investment:,.0f}** · Revenue: **{route.revenue:,.0f} aUEC**",
                f"Profit: **{route.profit:,.0f} aUEC** · ROI: **{route.roi_pct:.1f}%**",
                f"Distance: {distance_summary}",
                f"Confidence: **{confidence.label} ({confidence.score}/100)**",
            ]
            route_embed.add_field(name="Route summary", value="\n".join(summary_lines), inline=False)
            unique_warnings = list(dict.fromkeys(warnings))
            _add_chunked_fields(route_embed, name="Warnings & practical checks", lines=unique_warnings)
            footer = (
                "Collected UEX data + live UEX distance · prices can change before arrival · "
                "warnings do not change profit ranking"
            )
            if budget is not None:
                footer += f" · starting budget {float(budget):,.0f} aUEC"
            if space_only:
                footer += " · surface terminals excluded"
            if capital_access_only:
                footer += " · capital access confirmed at every stop"
            if not route.is_exact:
                footer += " · per-leg cargo allocation for this route is approximate, not proven-optimal"
            route_embed.set_footer(text=footer)
            # Sent one route per message, not batched like /mixed-routes: a multi-leg
            # route's per-leg cargo/warning fields can push a single embed close to
            # Discord's combined 6,000-character-per-message embed limit on their own,
            # and bundling up to 5 of them (as one message with multiple embeds) hit that
            # limit in testing - with nothing catching the send failure, Discord never
            # got a followup at all and the interaction looked permanently "thinking."
            try:
                await interaction.followup.send(embed=route_embed)
            except discord.HTTPException:
                # Plain-message fallback for an embed too large to send - warnings
                # (risk flags, stock/demand limits, practical notes) must survive here
                # too, not just the profit figures, so this goes through the same
                # chunking helper the embed fields use (with Discord's plain-message cap
                # of 2000 chars, not the embed field's 1024) and sends as many messages
                # as it takes rather than silently dropping anything.
                fallback_lines = [
                    f"**#{index} {path_label}**",
                    *summary_lines,
                    "⚠️ Full leg-by-leg cargo/distance details omitted - too large for one Discord message.",
                    *([] if route.is_exact else [
                        "⚠️ Per-leg cargo allocation for this route is approximate, not proven-optimal"
                    ]),
                    *unique_warnings,
                ]
                for chunk in _chunk_lines(fallback_lines, max_length=1900):
                    await interaction.followup.send(content=chunk)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Prices(bot))
