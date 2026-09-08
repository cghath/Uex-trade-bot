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
from bot.uex.route_confidence import coalesce_report_count, compute_route_confidence, track_record_modifier
from bot.uex.practical_routes import route_in_system, route_practical_notes, route_supports_auto_load
from bot.uex.commodity_risk import format_commodity_risk
from bot.uex.supply_demand import analyze_terminal_market_history, classify_supply_evidence, has_sell_side_demand
from bot.uex.ships import estimate_route_cargo, resolve_ship
from bot.uex.status import build_status_lookup, resolve_status_label
from bot.uex.trading import best_buy_locations, best_routes, best_sell_locations
from bot.uex.mixed_routes import build_mixed_routes, requires_capital_cargo_access
from bot.uex.multi_stop_routes import build_multi_stop_routes, find_diminishing_returns_budget, sweep_budget_curve
from bot.uex.charts import render_budget_curve_chart
from bot.uex.trading_preferences import describe_active_preferences
from bot.cogs.route_progression import RouteLegInput, RouteTrackingView, TrackableRoute
from bot.uex.route_presentation import (
    add_chunked_fields,
    approximation_note,
    capital_access_note,
    cargo_confidences,
    cargo_item_line,
    cargo_item_warnings,
    chunk_lines,
    format_evidence_note,
    side_health_warnings,
    travel_warning,
    worst_confidence,
)

logger = logging.getLogger("uexbot.prices")

MAX_FIELD_ROWS = 5

# Confirmed live via UEX /terminals: exactly these three values exist for star_system_name.
SYSTEM_CHOICES = [
    app_commands.Choice(name="Stanton", value="Stanton"),
    app_commands.Choice(name="Pyro", value="Pyro"),
    app_commands.Choice(name="Nyx", value="Nyx"),
]

# Re-exported under their historical names: bot/cogs/trends.py imports these from here,
# and several tests monkeypatch bot.cogs.prices._add_chunked_fields/_chunk_lines directly -
# the real implementation now lives in bot/uex/route_presentation.py (shared with
# trends.py and intelligence_brief.py) so it isn't copy-pasted per command surface.
_chunk_lines = chunk_lines
_add_chunked_fields = add_chunked_fields


def _positive_int(value: object) -> int | None:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


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

    async def _history_by_pair(
        self, id_commodity: object, terminal_ids: list[int]
    ) -> dict[tuple[int, int], object]:
        """Evidence-Level Labels' 'inferred trend' fallback: bulk change-only observation
        history for one commodity across many terminals, reduced to a TerminalMarketHistory
        per (commodity, terminal) pair - used when a route has no live stock/demand figure
        to fall back to how often that pair has historically had supply/demand. Both
        /best-route branches need this for the same single commodity, hence the shared
        helper rather than repeating the fetch-then-reduce in each branch."""
        if id_commodity is None:
            return {}
        pairs = [(id_commodity, terminal_id) for terminal_id in terminal_ids]
        observations_by_pair = await self.bot.db.get_terminal_market_observations_by_ids(pairs)
        if not observations_by_pair:
            return {}
        # Anchor each pair's coverage to the COLLECTOR's own last confirmed check
        # (terminal_market_state.last_seen), matching /terminal-history's existing,
        # correct anchor - not wall-clock now(), which would silently count any gap since
        # the collector actually last saw this pair (bot downtime, a stalled collector
        # loop, a pair briefly missing from a UEX response) as continued, confirmed
        # observation. Falls back to the last recorded observation's own timestamp (zero
        # fabricated extension) on the pair's current-state row being missing, which
        # shouldn't happen in practice - record_terminal_market_snapshot always upserts
        # terminal_market_state in the same call that can insert an observation row.
        market_signals = await self.bot.db.get_route_market_signals_by_ids(pairs)
        return {
            key: analyze_terminal_market_history(
                observations,
                observed_until=(
                    market_signals.get(key, {}).get("last_seen")
                    or max(str(row["observed_at"]) for row in observations)
                ),
            )
            for key, observations in observations_by_pair.items()
        }

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
        auto_load_only: bool | None = None,
        system: app_commands.Choice[str] | None = None,
    ) -> None:
        await interaction.response.defer()
        prefs = await self.bot.db.get_trading_preferences(interaction.user.id)
        if auto_load_only is None:
            auto_load_only = prefs["auto_load_only"]
        system_value = system.value if system else prefs["preferred_system"]
        preferences_note = describe_active_preferences(
            auto_load_only=auto_load_only, system=system_value, risk_tolerance=prefs["risk_tolerance"]
        )
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
            history_by_pair = await self._history_by_pair(id_commodity, ranked_terminal_ids)
            track_record_pairs = [
                pair
                for route in ranked
                for pair in (
                    (id_commodity, _positive_int(route.get("id_terminal_origin")), "buy"),
                    (id_commodity, _positive_int(route.get("id_terminal_destination")), "sell"),
                )
                if pair[1] is not None
            ]
            track_record = await self.bot.db.get_route_progression_track_record(track_record_pairs)
            intro_embed = discord.Embed(title=f"{commodity_display} — Best Trade Routes", color=discord.Color.green())
            if risk_warning:
                intro_embed.description = risk_warning
            footer = "Data from UEX Corp /commodities_routes"
            if not ship_vehicle:
                footer += " · set a default ship with /set-default-ship for cargo/run-profit numbers"
            if preferences_note:
                footer += " · " + preferences_note
            intro_embed.set_footer(text=footer)
            await interaction.followup.send(embed=intro_embed)

            # Each route gets its OWN message with its OWN "Track this route" button
            # directly beneath it, rather than one combined embed with every route's
            # button bundled at the end - Discord has no way to place a component between
            # two fields of a single embed, only below the whole message.
            tracking_cog = self.bot.get_cog("RouteProgression")
            routes_shown = 0
            for index, r in enumerate(ranked):
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

                origin_health_obj = classify_terminal_health(health_rows[origin_id]) if origin_id in health_rows else None
                destination_health_obj = (
                    classify_terminal_health(health_rows[destination_id]) if destination_id in health_rows else None
                )
                value_lines.append(format_evidence_note(
                    classify_supply_evidence(
                        scu=r.get("scu_origin"), health=origin_health_obj,
                        history=history_by_pair.get((id_commodity, origin_id)), side="supply",
                    ), label="Stock",
                ))
                value_lines.append(format_evidence_note(
                    classify_supply_evidence(
                        scu=r.get("scu_destination"), health=destination_health_obj,
                        history=history_by_pair.get((id_commodity, destination_id)), side="demand",
                        status_sell=r.get("status_destination"),
                    ), label="Demand",
                ))

                cargo = estimate_route_cargo(
                    per_unit_profit=per_unit_diff,
                    origin_scu_available=r.get("scu_origin"),
                    destination_scu_wanted=r.get("scu_destination"),
                    ship_cargo_scu=ship_cargo_scu,
                    price_origin=price_origin,
                )
                if cargo is not None:
                    limit_note = {
                        "ship": f"limited by {ship_vehicle.get('name')}'s cargo hold" if ship_vehicle else "limited by ship capacity",
                        "stock": "limited by available stock, not your ship",
                    }.get(cargo.limited_by, "")
                    cargo_line = f"Cargo: **{cargo.max_scu:,.0f} SCU**"
                    if limit_note:
                        cargo_line += f" ({limit_note})"
                    if cargo.investment is not None:
                        cargo_line += f"\nInvestment: **{cargo.investment:,.0f} aUEC**"
                    if cargo.run_profit is not None:
                        cargo_line += f" · Run profit: **{cargo.run_profit:,.0f} aUEC** for this haul"
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
                # Real player-reported outcomes (Recommendation Outcome Tracking), on top
                # of - not instead of - the evidence-quality scoring below. 0 when there's
                # no/too-little tracking history for this pair yet, which is nearly always.
                origin_matched, origin_total = track_record.get((id_commodity, origin_id, "buy"), (0, 0))
                destination_matched, destination_total = track_record.get(
                    (id_commodity, destination_id, "sell"), (0, 0)
                )
                confidence = compute_route_confidence(
                    origin_health=origin_health_obj,
                    destination_health=destination_health_obj,
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
                    track_record_modifier=track_record_modifier(
                        origin_matched + destination_matched, origin_total + destination_total
                    ),
                )
                value_lines.append(f"Confidence: **{confidence.label} ({confidence.score}/100)**")
                practical_notes = route_practical_notes(
                    terminal_references.get(origin_id),
                    terminal_references.get(destination_id),
                )
                value_lines.extend(practical_notes)
                origin_system = (terminal_references.get(origin_id) or {}).get("star_system_name")
                destination_system = (terminal_references.get(destination_id) or {}).get("star_system_name")
                # has_real_distance reflects THIS route's own row, not the branch as a
                # whole - UEX documents commodities_routes.distance as non-nullable, but
                # this codebase has precedent of similar "documented non-null" fields
                # (scu_origin/scu_destination) being null in real data, so a missing
                # distance here must still get a travel-time disclaimer, not silent
                # omission of both the figure and the warning.
                if note := travel_warning(origin_system, destination_system, has_real_distance=distance is not None):
                    value_lines.append(note)
                route_embed = discord.Embed(title=f"{origin} → {dest}", color=discord.Color.green())
                route_embed.set_footer(text=f"Route {index + 1} of {len(ranked)}")
                # Per-route embed, budget-checked on its own now rather than shared across
                # all 5 - stop and disclose instead of silently dropping a route that can't
                # fit (see /top-routes' identical pattern in trends.py).
                if not _add_chunked_fields(route_embed, name="Details", lines=value_lines):
                    continue
                routes_shown += 1

                view = None
                # RouteProgression may not be loaded (a cog load failure elsewhere shouldn't
                # break /best-route) - tracking buttons are additive, never required for the
                # command's own result.
                if tracking_cog and origin_id is not None and destination_id is not None:
                    trackable_route = TrackableRoute(
                        route_kind="best_route",
                        title=f"{commodity_display}: {origin} → {dest}",
                        legs=[
                            RouteLegInput(
                                side="buy", id_terminal=origin_id, id_commodity=id_commodity,
                                terminal_name=origin, commodity_name=commodity_display,
                                display_label=f"Buy at {origin}",
                                quoted_price=r.get("price_origin"), quoted_scu=r.get("scu_origin"),
                                quoted_status=r.get("status_origin"),
                            ),
                            RouteLegInput(
                                side="sell", id_terminal=destination_id, id_commodity=id_commodity,
                                terminal_name=dest, commodity_name=commodity_display,
                                display_label=f"Sell at {dest}",
                                quoted_price=r.get("price_destination"), quoted_scu=r.get("scu_destination"),
                                quoted_status=r.get("status_destination"),
                            ),
                        ],
                    )
                    view = RouteTrackingView(tracking_cog, [trackable_route])

                if view is not None:
                    await interaction.followup.send(embed=route_embed, view=view)
                else:
                    await interaction.followup.send(embed=route_embed)

            omitted = len(ranked) - routes_shown
            if omitted > 0:
                await interaction.followup.send(f"{omitted} more route(s) omitted - too large to display.")
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
        history_by_pair = await self._history_by_pair(id_commodity, ranked_terminal_ids)

        embed = discord.Embed(
            title=f"{routes[0].commodity_name} — Best Trade Routes",
            color=discord.Color.green(),
        )
        if risk_warning:
            embed.description = risk_warning
        # Set before the field loop, not after - see the matching comment above.
        footer = "Data from UEX Corp · does not account for travel time between terminals"
        if not ship_vehicle:
            footer += " · set a default ship with /set-default-ship for cargo/run-profit numbers"
        if preferences_note:
            footer += " · " + preferences_note
        embed.set_footer(text=footer)
        routes_shown = 0
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

            origin_health_obj = (
                classify_terminal_health(route_health_rows[route.buy_terminal_id])
                if route.buy_terminal_id in route_health_rows else None
            )
            destination_health_obj = (
                classify_terminal_health(route_health_rows[route.sell_terminal_id])
                if route.sell_terminal_id in route_health_rows else None
            )
            value_lines.append(format_evidence_note(
                classify_supply_evidence(
                    scu=route.scu_buy_available, health=origin_health_obj,
                    history=history_by_pair.get((id_commodity, route.buy_terminal_id)), side="supply",
                ), label="Stock",
            ))
            value_lines.append(format_evidence_note(
                classify_supply_evidence(
                    scu=route.scu_sell_wanted, health=destination_health_obj,
                    history=history_by_pair.get((id_commodity, route.sell_terminal_id)), side="demand",
                    status_sell=route.status_sell_code,
                ), label="Demand",
            ))

            cargo = estimate_route_cargo(
                per_unit_profit=route.profit_per_unit,
                origin_scu_available=route.scu_buy_available,
                destination_scu_wanted=route.scu_sell_wanted,
                ship_cargo_scu=ship_cargo_scu,
                price_origin=route.buy_price,
            )
            if cargo is not None:
                limit_note = {
                    "ship": f"limited by {ship_vehicle.get('name')}'s cargo hold" if ship_vehicle else "limited by ship capacity",
                    "stock": "limited by available stock, not your ship",
                }.get(cargo.limited_by, "")
                cargo_line = f"Cargo: **{cargo.max_scu:,.0f} SCU**"
                if limit_note:
                    cargo_line += f" ({limit_note})"
                if cargo.investment is not None:
                    cargo_line += f"\nInvestment: **{cargo.investment:,.0f} aUEC**"
                if cargo.run_profit is not None:
                    cargo_line += f" · Run profit: **{cargo.run_profit:,.0f} aUEC** for this haul"
                value_lines.append(cargo_line)
            elif not ship_vehicle:
                value_lines.append("Cargo: unknown (set a ship with /set-default-ship to see haulable SCU)")

            origin_signal = live_signals.get(route.buy_terminal_id, {})
            destination_signal = live_signals.get(route.sell_terminal_id, {})
            confidence = compute_route_confidence(
                origin_health=origin_health_obj,
                destination_health=destination_health_obj,
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
            value_lines.append(travel_warning(origin_system, destination_system, has_real_distance=False))

            if not _add_chunked_fields(
                embed,
                name=f"{route.buy_terminal} → {route.sell_terminal}",
                lines=value_lines,
            ):
                break
            routes_shown += 1

        omitted = len(routes) - routes_shown
        if omitted > 0:
            embed.set_footer(text=footer + f" · {omitted} more route(s) omitted - message size limit")
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
        space_only: bool | None = None,
        auto_load_only: bool | None = None,
        system: app_commands.Choice[str] | None = None,
    ) -> None:
        await interaction.response.defer()
        prefs = await self.bot.db.get_trading_preferences(interaction.user.id)
        if space_only is None:
            space_only = prefs["space_only"]
        if auto_load_only is None:
            auto_load_only = prefs["auto_load_only"]
        system_value = system.value if system else prefs["preferred_system"]

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
        # OR'd with the saved preference, not replaced by it - either a genuine capital
        # ship or an explicit "always require capital-ship access" preference should force
        # this filter on; the ship-derived signal never gets to silently disable it.
        capital_access_only = requires_capital_cargo_access(ship_vehicle) or prefs["capital_ship_access"]
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
        # RouteProgression may not be loaded (a cog load failure elsewhere shouldn't break
        # /mixed-routes) - tracking buttons are additive, never required for the command's
        # own result.
        tracking_cog = self.bot.get_cog("RouteProgression")
        for index, route in enumerate(routes, 1):
            origin_health = (
                classify_terminal_health(health_rows[route.origin_id])
                if route.origin_id in health_rows else None
            )
            destination_health = (
                classify_terminal_health(health_rows[route.destination_id])
                if route.destination_id in health_rows else None
            )
            cargo_lines = [cargo_item_line(item) for item in route.cargo]
            warnings: list[str] = side_health_warnings(
                origin_health=origin_health, destination_health=destination_health
            )
            for item in route.cargo:
                warnings.extend(cargo_item_warnings(item, status_lookup=status_lookup))
            warnings.extend(route_practical_notes(route.cargo[0].source, route.cargo[0].destination))
            if capital_access_only:
                warnings.append(capital_access_note("both ends"))
            origin_system = route.cargo[0].source.get("star_system_name")
            destination_system = route.cargo[0].destination.get("star_system_name")
            warnings.append(travel_warning(origin_system, destination_system, has_real_distance=False))

            confidence = worst_confidence(
                cargo_confidences(route.cargo, origin_health=origin_health, destination_health=destination_health)
            )
            value_lines = [
                *cargo_lines,
                f"Cargo: **{route.cargo_scu:,.0f}/{float(ship_vehicle['scu']):,.0f} SCU**",
                f"Investment: **{route.investment:,.0f}** · Revenue: **{route.revenue:,.0f} aUEC**",
                f"Profit: **{route.profit:,.0f} aUEC** · ROI: **{route.roi_pct:.1f}%**",
                f"Confidence: **{confidence.label} ({confidence.score}/100)**",
            ]
            footer = "Collected UEX data · prices can change before arrival · warnings do not change profit ranking"
            if budget is not None:
                footer += f" · budget {float(budget):,.0f} aUEC"
            if space_only:
                footer += " · surface terminals excluded"
            if capital_access_only:
                footer += " · capital access confirmed at both ends"
            if note := approximation_note(route.is_exact):
                footer += f" · {note}"

            route_embed = discord.Embed(
                title=f"#{index} {route.origin_name} → {route.destination_name}",
                description=(
                    f"Mixed load for **{ship_vehicle.get('name', ship_query)}** · "
                    f"ranked by estimated haul profit{' · space stations only' if space_only else ''}"
                ),
                color=discord.Color.green(),
            )
            # Footer set before any budget-checked field is added, so _add_chunked_fields'
            # len(embed) check below already accounts for it.
            route_embed.set_footer(text=footer)
            route_embed.add_field(
                name="Cargo plan",
                value="\n".join(value_lines),
                inline=False,
            )
            unique_warnings = list(dict.fromkeys(warnings))
            # Atomic, budget-checked - never leaves this route's embed with its first
            # warning chunk shown and a later one silently missing (the exact class of bug
            # already fixed for /multi-stop-route's own warnings section).
            warnings_fit = _add_chunked_fields(route_embed, name="Warnings & practical checks", lines=unique_warnings)

            view = None
            if tracking_cog:
                # All buys first, then all sells - matches how a player actually executes
                # this (buy everything at the one origin stop, travel, sell everything at
                # the destination), same order /multi-stop-route's per-hop flattening uses.
                trackable_route = TrackableRoute(
                    route_kind="mixed_routes",
                    title=f"#{index} {route.origin_name} → {route.destination_name}",
                    legs=[
                        RouteLegInput(
                            side="buy", id_terminal=route.origin_id, id_commodity=item.id_commodity,
                            terminal_name=route.origin_name, commodity_name=item.commodity_name,
                            display_label=f"Buy {item.commodity_name} at {route.origin_name}",
                            quoted_price=item.buy_price, quoted_scu=item.quantity_scu,
                            quoted_status=item.source.get("status_buy"),
                        )
                        for item in route.cargo
                    ] + [
                        RouteLegInput(
                            side="sell", id_terminal=route.destination_id, id_commodity=item.id_commodity,
                            terminal_name=route.destination_name, commodity_name=item.commodity_name,
                            display_label=f"Sell {item.commodity_name} at {route.destination_name}",
                            quoted_price=item.sell_price, quoted_scu=item.quantity_scu,
                            quoted_status=item.destination.get("status_sell"),
                        )
                        for item in route.cargo
                    ],
                )
                view = RouteTrackingView(tracking_cog, [trackable_route])

            # Sent one route per message (matching /best-route, /top-routes, and
            # /multi-stop-route) - each embed is independently budget-checked now, not
            # bundled with up to 4 others into Discord's shared combined-embed-text limit,
            # so a send failure here means only THIS route's own content is too large.
            embed_too_large = not warnings_fit
            if not embed_too_large:
                try:
                    if view is not None:
                        await interaction.followup.send(embed=route_embed, view=view)
                    else:
                        await interaction.followup.send(embed=route_embed)
                except discord.HTTPException:
                    embed_too_large = True
            if embed_too_large:
                # Includes footer last - it carries the route.is_exact approximation
                # disclosure plus the budget/space-only/capital-access notes, none of
                # which the embed path would ever drop, so the fallback must not
                # silently lose them either.
                fallback_text = "\n".join([
                    f"**#{index} {route.origin_name} → {route.destination_name}**",
                    *value_lines, *unique_warnings, footer,
                ])
                for chunk in _chunk_lines([fallback_text], max_length=1900):
                    await interaction.followup.send(content=chunk)

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
        space_only: bool | None = None,
        auto_load_only: bool | None = None,
        system: app_commands.Choice[str] | None = None,
    ) -> None:
        await interaction.response.defer()
        prefs = await self.bot.db.get_trading_preferences(interaction.user.id)
        if space_only is None:
            space_only = prefs["space_only"]
        if auto_load_only is None:
            auto_load_only = prefs["auto_load_only"]
        system_value = system.value if system else prefs["preferred_system"]

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
        # OR'd with the saved preference, not replaced by it - either a genuine capital
        # ship or an explicit "always require capital-ship access" preference should force
        # this filter on; the ship-derived signal never gets to silently disable it.
        capital_access_only = requires_capital_cargo_access(ship_vehicle) or prefs["capital_ship_access"]
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
        # RouteProgression may not be loaded (a cog load failure elsewhere shouldn't break
        # /multi-stop-route) - tracking buttons are additive, never required for the
        # command's own result.
        tracking_cog = self.bot.get_cog("RouteProgression")
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
            # Set before the per-leg field loop, not after - see the matching comment in
            # /best-route above. This route's footer depends only on already-known
            # per-command options and route.is_exact, all available before the loop runs.
            route_footer = (
                "Collected UEX data + live UEX distance · prices can change before arrival · "
                "warnings do not change profit ranking"
            )
            if budget is not None:
                route_footer += f" · starting budget {float(budget):,.0f} aUEC"
            if space_only:
                route_footer += " · surface terminals excluded"
            if capital_access_only:
                route_footer += " · capital access confirmed at every stop"
            if note := approximation_note(route.is_exact, per_leg=True):
                route_footer += f" · {note}"
            route_embed.set_footer(text=route_footer)
            warnings: list[str] = []
            leg_confidences = []
            total_distance_gm = 0.0
            distance_partial = False
            all_legs_fit = True
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
                cargo_lines = [cargo_item_line(item) for item in leg.cargo]
                leg_lines = [
                    *cargo_lines,
                    f"Investment: **{leg.investment:,.0f}** · Revenue: **{leg.revenue:,.0f} aUEC** · "
                    f"Profit: **{leg.profit:,.0f} aUEC** · {distance_note}",
                ]
                # Unlike /top-routes (where one route missing is just one omitted route),
                # a route embed's title and "Route summary" field both unconditionally
                # describe ALL of route.legs - if a leg's own field silently failed to
                # fit, the embed would claim (and still total the profit/investment for)
                # a leg it never actually shows. Tracked here and folded into
                # embed_too_large below so that case routes into the same full-fidelity
                # plain-text fallback as a real send failure, rather than sending a
                # self-contradictory embed.
                if not _add_chunked_fields(
                    route_embed,
                    name=f"Leg {leg_index}: {leg.origin_name} → {leg.destination_name}",
                    lines=leg_lines,
                ):
                    all_legs_fit = False
                leg_prefix = f"Leg {leg_index} "
                warnings.extend(side_health_warnings(
                    origin_health=origin_health, destination_health=destination_health,
                    origin_label=f"{leg_prefix}Origin", destination_label=f"{leg_prefix}Destination",
                ))
                for item in leg.cargo:
                    warnings.extend(cargo_item_warnings(item, status_lookup=status_lookup, prefix=leg_prefix))
                warnings.extend(
                    f"{leg_prefix}{note}"
                    for note in route_practical_notes(leg.cargo[0].source, leg.cargo[0].destination)
                )
                origin_system = leg.cargo[0].source.get("star_system_name")
                destination_system = leg.cargo[0].destination.get("star_system_name")
                if note := travel_warning(
                    origin_system, destination_system, has_real_distance=True, prefix=leg_prefix
                ):
                    warnings.append(note)
                leg_confidences.extend(
                    cargo_confidences(leg.cargo, origin_health=origin_health, destination_health=destination_health)
                )
            if capital_access_only:
                warnings.append(capital_access_note("every stop"))
            confidence = worst_confidence(leg_confidences)
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
            # _add_chunked_fields is atomic (see prices.py's own docstring) - for a route
            # embed's leg fields, that's exactly what's wanted (never show a leg with its
            # warning silently missing). But here the "logical field" being added is the
            # WHOLE warnings section, not a single route - if it doesn't fit, atomicity
            # means it adds NOTHING, silently dropping every cargo-risk/cross-system/stale-
            # health warning while the smaller, warning-free embed still sends successfully
            # (no discord.HTTPException, so the existing too-large fallback below never
            # triggers). Its return value must be checked and treated the same as a real
            # send failure - entering the same full-fidelity plain-text fallback - rather
            # than silently accepting an embed that looks complete but isn't.
            warnings_fit = _add_chunked_fields(
                route_embed, name="Warnings & practical checks", lines=unique_warnings
            )
            # Sent one route per message, not batched like /mixed-routes: a multi-leg
            # route's per-leg cargo/warning fields can push a single embed close to
            # Discord's combined 6,000-character-per-message embed limit on their own,
            # and bundling up to 5 of them (as one message with multiple embeds) hit that
            # limit in testing - with nothing catching the send failure, Discord never
            # got a followup at all and the interaction looked permanently "thinking."
            embed_too_large = not warnings_fit or not all_legs_fit
            if not embed_too_large:
                # A multi-stop leg carries several commodities at once (allocate_pair_cargo's
                # mixed load), not one - flattened here into one buy + one sell progression-
                # leg per commodity per hop, in order, so the existing leg-by-leg cog can walk
                # a chain exactly the same way it already walks /best-route's simple 2-leg case.
                view = None
                if tracking_cog:
                    progression_legs: list[RouteLegInput] = []
                    for chain_leg in route.legs:
                        for item in chain_leg.cargo:
                            progression_legs.append(RouteLegInput(
                                side="buy", id_terminal=chain_leg.origin_id, id_commodity=item.id_commodity,
                                terminal_name=chain_leg.origin_name, commodity_name=item.commodity_name,
                                display_label=f"Buy {item.commodity_name} at {chain_leg.origin_name}",
                                quoted_price=item.buy_price, quoted_scu=item.quantity_scu,
                                quoted_status=item.source.get("status_buy"),
                            ))
                        for item in chain_leg.cargo:
                            progression_legs.append(RouteLegInput(
                                side="sell", id_terminal=chain_leg.destination_id, id_commodity=item.id_commodity,
                                terminal_name=chain_leg.destination_name, commodity_name=item.commodity_name,
                                display_label=f"Sell {item.commodity_name} at {chain_leg.destination_name}",
                                quoted_price=item.sell_price, quoted_scu=item.quantity_scu,
                                quoted_status=item.destination.get("status_sell"),
                            ))
                    if progression_legs:
                        view = RouteTrackingView(tracking_cog, [TrackableRoute(
                            route_kind="multi_stop_route", title=f"#{index} {path_label}", legs=progression_legs,
                        )])
                try:
                    if view is not None:
                        await interaction.followup.send(embed=route_embed, view=view)
                    else:
                        await interaction.followup.send(embed=route_embed)
                except discord.HTTPException:
                    embed_too_large = True
            if embed_too_large:
                # Plain-message fallback for an embed too large to send (or whose warnings
                # section didn't fit) - warnings (risk flags, stock/demand limits, practical
                # notes) must survive here too, not just the profit figures, so this goes
                # through the same chunking helper the embed fields use (with Discord's
                # plain-message cap of 2000 chars, not the embed field's 1024) and sends as
                # many messages as it takes rather than silently dropping anything.
                per_leg_note = approximation_note(route.is_exact, per_leg=True)
                fallback_lines = [
                    f"**#{index} {path_label}**",
                    *summary_lines,
                    "⚠️ Full leg-by-leg cargo/distance details omitted - too large for one Discord message.",
                    *([] if per_leg_note is None else [f"⚠️ {per_leg_note[0].upper()}{per_leg_note[1:]}"]),
                    *unique_warnings,
                ]
                for chunk in _chunk_lines(fallback_lines, max_length=1900):
                    await interaction.followup.send(content=chunk)

    @app_commands.command(
        name="diminishing-returns",
        description="Chart how a multi-stop chain's ROI changes as your starting budget grows.",
    )
    @app_commands.describe(
        ship="Optional: use a specific ship instead of your saved default",
        space_only="Exclude surface terminals; require every stop to be a confirmed space station",
        auto_load_only="Only consider chains where every stop offers UEX's auto-load",
        system="Optional: require every stop in the chain to be in this star system",
    )
    @app_commands.rename(space_only="space-only", auto_load_only="auto-load-only")
    @app_commands.choices(system=SYSTEM_CHOICES)
    @app_commands.autocomplete(ship=ship_name_autocomplete)
    async def diminishing_returns(
        self,
        interaction: discord.Interaction,
        ship: str | None = None,
        space_only: bool | None = None,
        auto_load_only: bool | None = None,
        system: app_commands.Choice[str] | None = None,
    ) -> None:
        await interaction.response.defer()
        prefs = await self.bot.db.get_trading_preferences(interaction.user.id)
        if space_only is None:
            space_only = prefs["space_only"]
        if auto_load_only is None:
            auto_load_only = prefs["auto_load_only"]
        system_value = system.value if system else prefs["preferred_system"]

        ship_query = ship or await self.bot.db.get_default_ship(interaction.user.id)
        if not ship_query:
            await interaction.followup.send(
                "Set a default ship with `/set-default-ship`, or provide the `ship` option, "
                "so this can be measured against a real cargo limit."
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
        capital_access_only = requires_capital_cargo_access(ship_vehicle) or prefs["capital_ship_access"]
        if capital_access_only:
            try:
                stations = await self.bot.uex.get_space_stations()
            except UexApiError as exc:
                await interaction.followup.send(
                    "I couldn't verify XL-hangar/loading-dock access for this capital ship, "
                    f"so I won't measure against potentially unusable routes: {exc}"
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

        await interaction.followup.send(
            "Running a budget sweep against the current market snapshot - this runs the "
            "full route search several times over, so it can take up to a minute..."
        )
        # Same CPU-bound-offload reasoning as /mixed-routes and /multi-stop-route, but more
        # pronounced here: this calls build_multi_stop_routes up to a dozen times in a row.
        points = await asyncio.to_thread(
            sweep_budget_curve,
            market_rows,
            ship_capacity_scu=float(ship_vehicle["scu"]),
            space_only=space_only,
            capital_access_only=capital_access_only,
            auto_load_only=auto_load_only,
            system=system_value,
        )
        plottable = [p for p in points if p.investment > 0]
        if len(plottable) < 2:
            await interaction.followup.send(
                f"Couldn't find enough profitable multi-stop chains for "
                f"**{ship_vehicle.get('name', ship_query)}** to chart a budget curve right now."
            )
            return

        diminishing_returns_budget = find_diminishing_returns_budget(plottable)
        chart_buffer = render_budget_curve_chart(
            ship_name=ship_vehicle.get("name", ship_query),
            points=plottable,
            diminishing_returns_budget=diminishing_returns_budget,
        )
        if chart_buffer is None:
            await interaction.followup.send("Couldn't render a chart from this ship's budget sweep.")
            return

        file = discord.File(chart_buffer, filename="budget_curve.png")
        embed = discord.Embed(
            title=f"{ship_vehicle.get('name', ship_query)} — Diminishing returns",
            color=discord.Color.blurple(),
        )
        embed.set_image(url="attachment://budget_curve.png")
        first, last = plottable[0], plottable[-1]
        embed.add_field(
            name=f"At {first.budget:,.0f} aUEC",
            value=f"Profit: **{first.profit:,.0f}** · ROI: **{first.roi_pct:.1f}%**",
            inline=True,
        )
        embed.add_field(
            name=f"At {last.budget:,.0f} aUEC",
            value=f"Profit: **{last.profit:,.0f}** · ROI: **{last.roi_pct:.1f}%**",
            inline=True,
        )
        if diminishing_returns_budget is not None:
            note = (
                f"Diminishing returns begin around **{diminishing_returns_budget:,.0f} aUEC** - "
                "beyond that, real stock, demand, or cargo space limits the same chain "
                "regardless of how much more you bring."
            )
        else:
            note = (
                "Still improving at the largest budget swept - real market limits may sit "
                "beyond this range, or this ship/route combination has unusually deep opportunities."
            )
        embed.description = note
        footer = "Collected UEX data · one route search per budget checkpoint, stops early once it plateaus"
        preferences_note = describe_active_preferences(
            space_only=space_only, capital_ship_access=capital_access_only,
            auto_load_only=auto_load_only, system=system_value,
        )
        if preferences_note:
            footer += " · " + preferences_note
        embed.set_footer(text=footer)
        await interaction.followup.send(embed=embed, file=file)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Prices(bot))
