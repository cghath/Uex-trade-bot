"""Trend-finding commands: most-traded commodities, price movers, price history charts, and
top trade routes across the whole catalog.

/trending needs one API call per tradeable commodity (UEX only exposes real trade-trip
counts scoped to a single commodity at a time), so it's computed by a background task on
a slow interval and served from an in-memory cache - instant for users, gentle on the
120 req/min rate limit. /movers and /commodity-history are single bulk calls each and run
on demand.

/top-routes has the same "needs one call per commodity" constraint, for a different
reason: /commodities_routes (UEX's own precomputed buy->sell routes, with a proprietary
"score" field) requires at least one filter - there's no "give me every route for every
commodity" call. Rather than run a second full-catalog scan on its own schedule, this piggybacks
on refresh_trending's existing per-commodity loop: it already fetches /commodities_prices for
every tradeable commodity and already has that commodity's id from the returned rows, so this
just adds one more paced /commodities_routes call per commodity in the same iteration.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

import discord
from discord import app_commands
from discord.ext import commands, tasks

from bot.cogs.prices import (
    SYSTEM_CHOICES,
    _add_chunked_fields,
    commodity_name_autocomplete,
    terminal_name_autocomplete,
)
from bot.cogs.route_progression import RouteLegInput, RouteTrackingView, TrackableRoute
from bot.cogs.ships import ship_name_autocomplete
from bot.uex.charts import render_price_history_chart
from bot.uex.exceptions import UexApiError, describe_uex_api_error
from bot.uex.data_health import classify_terminal_health, format_health_note
from bot.uex.route_confidence import compute_route_confidence, track_record_modifier
from bot.uex.route_progression import SUPPRESSION_HOURS
from bot.uex.practical_routes import route_in_system, route_practical_notes, route_supports_auto_load
from bot.uex.commodity_risk import format_commodity_risk
from bot.uex.route_presentation import format_evidence_note, travel_warning
from bot.uex.ships import estimate_route_cargo, resolve_ship
from bot.uex.status import build_status_lookup, resolve_status_label
from bot.uex.supply_demand import (
    SELL_SIDE_STATUS_CLARIFIER,
    EvidenceLevel,
    analyze_terminal_market_history,
    classify_supply_evidence,
    has_sell_side_demand,
)
from bot.uex.trading_preferences import describe_active_preferences
from bot.uex.trends import (
    ScoredRouteEntry,
    TrendingEntry,
    aggregate_commodity_trips,
    compute_movers,
    rank_top_scored_routes,
    rank_trending,
    select_available_routes,
    select_in_stock_routes,
)

logger = logging.getLogger("uexbot.trends")


def _build_route_field(
    i: int,
    r: ScoredRouteEntry,
    ship_vehicle: dict | None,
    ship_cargo_scu: float | None,
    status_lookup: dict,
    origin_evidence: EvidenceLevel,
    destination_evidence: EvidenceLevel,
    budget: float | None = None,
) -> tuple[str, str]:
    """Build one route field for /top-routes."""
    per_unit_profit = r.price_destination - r.price_origin
    value_lines = [f"Buy {r.price_origin:.2f} / Sell {r.price_destination:.2f} (+{per_unit_profit:.2f} aUEC/unit)"]

    buy_status = resolve_status_label(status_lookup, "buy", r.status_origin)
    sell_status = resolve_status_label(status_lookup, "sell", r.status_destination)
    if buy_status or sell_status:
        status_bits = []
        if buy_status:
            status_bits.append(f"buy side: {buy_status}")
        if sell_status:
            status_bits.append(f"sell side: {sell_status}")
        value_lines.append(" · ".join(status_bits))

    # Evidence-Level Labels: a missing SCU figure (None) used to render nothing at all,
    # visually identical to a confirmed-zero figure that just didn't get shown - this
    # always shows something, and a genuinely unknown figure now reads differently from
    # both a fresh report and a confirmed zero (see format_evidence_note's docstring).
    value_lines.append(format_evidence_note(origin_evidence, label="Stock"))
    value_lines.append(format_evidence_note(destination_evidence, label="Demand"))

    cargo = estimate_route_cargo(
        per_unit_profit=per_unit_profit,
        origin_scu_available=r.scu_origin,
        destination_scu_wanted=r.scu_destination,
        ship_cargo_scu=ship_cargo_scu,
        price_origin=r.price_origin,
        budget=budget,
    )
    if cargo is not None:
        limit_note = {
            "ship": f"limited by {ship_vehicle.get('name')}'s cargo hold" if ship_vehicle else "limited by ship capacity",
            "stock": "limited by available stock, not your ship",
            "budget": "limited by your budget, not cargo space",
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
    if r.price_margin is not None:
        pct_bits.append(f"margin {r.price_margin:.1f}%")
    if r.price_roi is not None:
        pct_bits.append(f"ROI {r.price_roi:.1f}%")
    if pct_bits:
        value_lines.append(" · ".join(pct_bits))

    # r.profit (UEX's own route-level figure, used for ranking) is deliberately NOT shown
    # here - it's computed off the full stock/demand volume, not this player's actual ship
    # capacity, and displaying it next to Run profit above (which IS ship-scaled) produced
    # two differently-scaled "profit" numbers in the same field with nothing to tell them
    # apart - a real report ("Profit: 5,136,000" vs. a correct "Run profit: 308,160 for
    # this haul" two lines up, ~17x apart). Per-unit margin is already shown in the first
    # line above, matching /best-route's own established pattern of never displaying a
    # second, differently-scaled lump-sum profit figure.
    if r.distance is not None:
        value_lines.append(f"{r.distance:.1f} GM")

    name = f"{i}. {r.commodity_name}: {r.origin_terminal_name} → {r.destination_terminal_name}"
    return name, "\n".join(value_lines)


TRENDING_REFRESH_MINUTES = 45
TRENDING_KEEP_TOP = 25
TOP_SCORED_ROUTES_KEEP = 10
TOP_IN_STOCK_ROUTES_KEEP = 10

# Pace background calls well under the 120/min UEX limit, leaving headroom for
# whatever real user commands are running concurrently.
_TRENDING_CALL_DELAY = 0.6


class Trends(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self._trending: list[TrendingEntry] = []
        self._trending_updated_at: datetime | None = None
        self._trending_lock = asyncio.Lock()
        self._top_scored_routes: list[ScoredRouteEntry] = []
        self._top_scored_routes_updated_at: datetime | None = None
        self._top_scored_routes_lock = asyncio.Lock()
        self._top_in_stock_routes: list[ScoredRouteEntry] = []
        self._top_in_stock_routes_updated_at: datetime | None = None
        self._top_in_stock_routes_lock = asyncio.Lock()
        self.refresh_trending.start()

    def cog_unload(self) -> None:
        self.refresh_trending.cancel()

    def get_trending_snapshot(self) -> list[TrendingEntry]:
        """Read-only access to the current cached ranking, for other cogs (e.g. the daily
        digest) that want to reuse it without re-triggering a fresh scan of every commodity."""
        return list(self._trending)

    async def _get_status_lookup(self) -> dict:
        """Best-effort readable status labels for /top-routes."""
        try:
            status_data = await self.bot.uex.get_commodities_status()
        except UexApiError as exc:
            logger.info("Status labels unavailable for /top-routes: %s", exc)
            return {"buy": {}, "sell": {}}
        return build_status_lookup(status_data)

    # -- /trending: served from cache, refreshed by the background loop -----

    @app_commands.command(name="trending", description="Most actively traded commodities right now, by real player trade volume.")
    async def trending(self, interaction: discord.Interaction) -> None:
        async with self._trending_lock:
            entries = list(self._trending)
            updated_at = self._trending_updated_at

        if not entries:
            await interaction.response.send_message(
                "Still gathering trend data (this refreshes on a timer after startup) - try again in a few minutes."
            )
            return

        embed = discord.Embed(title="Most Actively Traded Commodities", color=discord.Color.gold())
        lines = []
        for i, e in enumerate(entries[:10], start=1):
            volatility = f"{e.avg_volatility:.2f}" if e.avg_volatility is not None else "n/a"
            lines.append(
                f"**{i}. {e.commodity_name}** — {e.total_trips_15d} trips (15d) · "
                f"sell {e.best_sell_price:,.0f} aUEC · volatility {volatility}"
            )
        embed.description = "\n".join(lines)
        footer = "Trade volume = real player-submitted trips, last 15 days (UEX data)"
        if updated_at:
            footer += f" · refreshed {updated_at.strftime('%Y-%m-%d %H:%M UTC')}"
        embed.set_footer(text=footer)
        await interaction.response.send_message(embed=embed)

    @tasks.loop(minutes=TRENDING_REFRESH_MINUTES)
    async def refresh_trending(self) -> None:
        try:
            commodities = await self.bot.uex.get_commodities()
        except UexApiError as exc:
            logger.warning("Failed to list commodities for trending refresh: %s", exc)
            return

        tradeable = [c for c in commodities if c.get("is_buyable") or c.get("is_sellable")]
        entries: list[TrendingEntry] = []
        route_candidates: list[ScoredRouteEntry] = []
        in_stock_route_candidates: list[ScoredRouteEntry] = []

        for commodity in tradeable:
            name = commodity.get("name")
            if not name:
                continue
            try:
                rows = await self.bot.uex.get_commodities_prices(commodity_name=name)
            except UexApiError as exc:
                logger.info("Skipping %s in trending refresh: %s", name, exc)
                await asyncio.sleep(_TRENDING_CALL_DELAY)
                continue

            await asyncio.sleep(_TRENDING_CALL_DELAY)

            if not rows:
                continue

            total_trips, avg_volatility = aggregate_commodity_trips(rows)
            if total_trips > 0:
                best_sell = max((r.get("price_sell") or 0 for r in rows), default=0)
                buy_candidates = [r.get("price_buy") or 0 for r in rows if (r.get("price_buy") or 0) > 0]
                best_buy = min(buy_candidates) if buy_candidates else None

                entries.append(
                    TrendingEntry(
                        commodity_name=name,
                        total_trips_15d=total_trips,
                        avg_volatility=avg_volatility,
                        best_sell_price=best_sell,
                        best_buy_price=best_buy,
                    )
                )

            # Top-routes gathering, independent of trending trip volume - a commodity
            # can have zero recent trade trips and still have real stock and a good score.
            id_commodity = rows[0].get("id_commodity")
            if id_commodity is not None:
                try:
                    route_rows = await self.bot.uex.get_commodities_routes(id_commodity=id_commodity)
                except UexApiError as exc:
                    logger.info("Skipping %s in top-routes refresh: %s", name, exc)
                    route_rows = []
                else:
                    # Every qualifying route for this commodity, not just the top-scored
                    # one - so a later auto-load-only/system filter has a same-commodity
                    # alternative to fall back to instead of the commodity vanishing.
                    route_candidates.extend(select_available_routes(name, id_commodity, route_rows))
                    # Same route_rows, no extra API call - just a stricter filter requiring
                    # real demand at the destination too, not just stock at the origin.
                    in_stock_route_candidates.extend(select_in_stock_routes(name, id_commodity, route_rows))
                await asyncio.sleep(_TRENDING_CALL_DELAY)

        ranked = rank_trending(entries, limit=TRENDING_KEEP_TOP)
        async with self._trending_lock:
            self._trending = ranked
            self._trending_updated_at = datetime.now(timezone.utc)
        logger.info("Trending refresh complete: %d commodities ranked", len(ranked))

        # Keep every candidate the loop already computed, not just the top
        # TOP_SCORED_ROUTES_KEEP by score - a user's auto-load-only/system filter runs
        # later, at command time, and can only work with what's still here. Discarding
        # the rest now would make a route that fails on score alone but would pass the
        # filter unrecoverable, since this refresh cycle's candidates aren't kept
        # anywhere else. TOP_SCORED_ROUTES_KEEP is applied as a *display* cap instead,
        # after filtering, in _send_ranked_routes.
        ranked_routes = rank_top_scored_routes(route_candidates, limit=len(route_candidates))
        async with self._top_scored_routes_lock:
            self._top_scored_routes = ranked_routes
            self._top_scored_routes_updated_at = datetime.now(timezone.utc)
        logger.info(
            "Top-routes refresh complete: %d candidates, %d kept", len(route_candidates), len(ranked_routes)
        )

        ranked_in_stock_routes = rank_top_scored_routes(in_stock_route_candidates, limit=len(in_stock_route_candidates))
        async with self._top_in_stock_routes_lock:
            self._top_in_stock_routes = ranked_in_stock_routes
            self._top_in_stock_routes_updated_at = datetime.now(timezone.utc)
        logger.info(
            "Strict top-routes refresh complete: %d candidates, %d kept",
            len(in_stock_route_candidates), len(ranked_in_stock_routes),
        )

    @refresh_trending.before_loop
    async def before_refresh_trending(self) -> None:
        await self.bot.wait_until_ready()

    # -- /top-routes: served from one of two caches refreshed by the same background loop. --

    async def _send_ranked_routes(
        self,
        interaction: discord.Interaction,
        *,
        entries: list[ScoredRouteEntry],
        updated_at: datetime | None,
        ship: str | None,
        title: str,
        footer_note: str,
        log_label: str,
        display_limit: int,
        auto_load_only: bool = False,
        system: str | None = None,
        risk_tolerance: str | None = None,
        budget: float | None = None,
    ) -> None:
        await interaction.response.defer()

        ship_query = ship or await self.bot.db.get_default_ship(interaction.user.id)
        ship_vehicle = None
        if ship_query:
            try:
                vehicles = await self.bot.uex.get_vehicles()
                ship_vehicle = resolve_ship(vehicles, ship_query)
            except UexApiError as exc:
                logger.info("Vehicle lookup failed for '%s' in %s: %s", ship_query, log_label, exc)
        ship_cargo_scu = ship_vehicle.get("scu") if ship_vehicle else None
        status_lookup = await self._get_status_lookup()
        terminal_ids = [
            terminal_id
            for route in entries
            for terminal_id in (route.origin_terminal_id, route.destination_terminal_id)
            if terminal_id is not None
        ]
        terminal_references = await self.bot.db.get_terminal_references_by_ids(terminal_ids)
        # Suppression window: a pair a player recently confirmed genuinely empty (see
        # update_confirms_depletion) is excluded here, on the FULL candidate pool - same
        # "filter before truncating" discipline as auto_load_only/system below, not the
        # smaller post-truncation set _send_ranked_routes fetches market_signals for later.
        now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        suppressed_pairs = await self.bot.db.get_suppressed_sides_by_ids(
            [
                (route.id_commodity, terminal_id)
                for route in entries
                for terminal_id in (route.origin_terminal_id, route.destination_terminal_id)
                if terminal_id is not None
            ],
            now=now_str,
        )
        entries = [
            route for route in entries
            if not suppressed_pairs.get((route.id_commodity, route.origin_terminal_id), {}).get("buy")
            and not suppressed_pairs.get((route.id_commodity, route.destination_terminal_id), {}).get("sell")
        ]
        if not entries:
            await interaction.followup.send(
                "No routes found right now - the ones that would otherwise qualify were recently "
                f"reported empty and are given up to {SUPPRESSION_HOURS}h to refresh before showing again."
            )
            return
        if auto_load_only:
            entries = [
                route for route in entries
                if route_supports_auto_load(
                    terminal_references.get(route.origin_terminal_id),
                    terminal_references.get(route.destination_terminal_id),
                )
            ]
            if not entries:
                await interaction.followup.send(
                    "No auto-load-capable routes found right now - try again once more route data has been collected."
                )
                return
        if system is not None:
            entries = [
                route for route in entries
                if route_in_system(
                    terminal_references.get(route.origin_terminal_id),
                    terminal_references.get(route.destination_terminal_id),
                    system,
                )
            ]
            if not entries:
                await interaction.followup.send(
                    f"No routes confirmed entirely within {system} found right now."
                )
                return
        # Dedupe back to one route per commodity - entries can now carry several
        # candidates per commodity (see select_available_routes), so a same-commodity
        # alternative survives being filtered here instead of the whole commodity
        # disappearing when only its top-scored route is checked. entries is still
        # score-sorted overall at this point, so keeping the first occurrence per
        # commodity keeps the highest-scoring surviving one.
        seen_commodities: set[int] = set()
        deduped_entries: list[ScoredRouteEntry] = []
        for route in entries:
            if route.id_commodity in seen_commodities:
                continue
            seen_commodities.add(route.id_commodity)
            deduped_entries.append(route)
        entries = deduped_entries
        # Truncate for display only after filtering, not before - the background refresh
        # loop now keeps every candidate it computed specifically so this filter has a
        # real pool to work with (see refresh_trending).
        entries = entries[:display_limit]
        terminal_ids = [
            terminal_id
            for route in entries
            for terminal_id in (route.origin_terminal_id, route.destination_terminal_id)
            if terminal_id is not None
        ]
        health_rows = await self.bot.db.get_terminal_data_health_by_ids(terminal_ids)
        health_notes = {
            terminal_id: note
            for terminal_id, row in health_rows.items()
            if (note := format_health_note(classify_terminal_health(row)))
        }
        market_signals = await self.bot.db.get_route_market_signals_by_ids(
            [
                (route.id_commodity, terminal_id)
                for route in entries
                for terminal_id in (route.origin_terminal_id, route.destination_terminal_id)
                if terminal_id is not None
            ],
        )
        commodity_references = await self.bot.db.get_commodity_references(
            [route.id_commodity for route in entries]
        )
        # Evidence-Level Labels' "inferred trend" fallback: when a route has no live
        # scu_origin/scu_destination figure, fall back to how often this (commodity,
        # terminal) pair has historically had supply/demand, from the same change-only
        # observation history /terminal-history already analyzes for one pair at a time.
        observations_by_pair = await self.bot.db.get_terminal_market_observations_by_ids([
            (route.id_commodity, terminal_id)
            for route in entries
            for terminal_id in (route.origin_terminal_id, route.destination_terminal_id)
            if terminal_id is not None
        ])
        # Anchor each pair's coverage to the COLLECTOR's own last confirmed check
        # (terminal_market_state.last_seen, already fetched above as market_signals),
        # matching /terminal-history's existing, correct anchor - not wall-clock now(),
        # which would silently count any gap since the collector actually last saw this
        # pair as continued, confirmed observation. See the matching comment in
        # Prices._history_by_pair (bot/cogs/prices.py) for the full rationale.
        history_by_pair = {
            key: analyze_terminal_market_history(
                observations,
                observed_until=(
                    market_signals.get(key, {}).get("last_seen")
                    or max(str(row["observed_at"]) for row in observations)
                ),
            )
            for key, observations in observations_by_pair.items()
        }

        # Built and attached BEFORE the field loop below, not after: _add_chunked_fields'
        # budget check measures the embed's real total via len(embed), which only includes
        # the footer once it's actually been set - setting it afterward meant the loop
        # under-reserved for real footer text (explanation + refresh timestamp + ship note),
        # confirmed to let the final assembled embed land at 6,009 characters despite the
        # loop's own bookkeeping. The omission-count suffix is appended afterward, once
        # routes_shown is known - it's a short, bounded-length addition that the reserve
        # margin below already accounts for.
        footer = footer_note + " · " + SELL_SIDE_STATUS_CLARIFIER
        if updated_at:
            footer += f" · refreshed {updated_at.strftime('%Y-%m-%d %H:%M UTC')}"
        if not ship_vehicle:
            footer += " · set a default ship with /set-default-ship for cargo/run-profit numbers"
        if budget is not None:
            footer += f" · budget {budget:,.0f} aUEC"
        preferences_note = describe_active_preferences(
            auto_load_only=auto_load_only, system=system, risk_tolerance=risk_tolerance
        )
        if preferences_note:
            footer += " · " + preferences_note

        intro_embed = discord.Embed(title=title, color=discord.Color.green())
        intro_embed.set_footer(text=footer)
        await interaction.followup.send(embed=intro_embed)

        track_record_pairs = [
            pair
            for route in entries
            for pair in (
                (route.id_commodity, route.origin_terminal_id, "buy"),
                (route.id_commodity, route.destination_terminal_id, "sell"),
            )
            if pair[1] is not None
        ]
        track_record = await self.bot.db.get_route_progression_track_record(track_record_pairs)
        # RouteProgression may not be loaded (a cog load failure elsewhere shouldn't break
        # /top-routes) - tracking buttons are additive, never required for the command's
        # own result.
        tracking_cog = self.bot.get_cog("RouteProgression")

        routes_shown = 0
        for i, r in enumerate(entries, start=1):
            origin_health = classify_terminal_health(health_rows[r.origin_terminal_id]) if r.origin_terminal_id in health_rows else None
            destination_health = (
                classify_terminal_health(health_rows[r.destination_terminal_id])
                if r.destination_terminal_id in health_rows else None
            )
            origin_evidence = classify_supply_evidence(
                scu=r.scu_origin, health=origin_health,
                history=history_by_pair.get((r.id_commodity, r.origin_terminal_id)), side="supply",
            )
            destination_evidence = classify_supply_evidence(
                scu=r.scu_destination, health=destination_health,
                history=history_by_pair.get((r.id_commodity, r.destination_terminal_id)), side="demand",
                status_sell=r.status_destination,
            )
            name, value = _build_route_field(
                i, r, ship_vehicle, ship_cargo_scu, status_lookup, origin_evidence, destination_evidence,
                budget=budget,
            )
            warnings = []
            for side, terminal_id in (
                ("Origin", r.origin_terminal_id),
                ("Destination", r.destination_terminal_id),
            ):
                note = health_notes.get(terminal_id)
                if note:
                    warnings.append(f"{side}: {note}")
            if warnings:
                value += "\n" + "\n".join(warnings)
            origin_health_row = health_rows.get(r.origin_terminal_id)
            destination_health_row = health_rows.get(r.destination_terminal_id)
            origin_signal = market_signals.get((r.id_commodity, r.origin_terminal_id), {})
            destination_signal = market_signals.get((r.id_commodity, r.destination_terminal_id), {})
            origin_matched, origin_total = track_record.get((r.id_commodity, r.origin_terminal_id, "buy"), (0, 0))
            destination_matched, destination_total = track_record.get(
                (r.id_commodity, r.destination_terminal_id, "sell"), (0, 0)
            )
            confidence = compute_route_confidence(
                origin_health=classify_terminal_health(origin_health_row) if origin_health_row else None,
                destination_health=classify_terminal_health(destination_health_row) if destination_health_row else None,
                origin_report_count=origin_signal.get("buy_report_count"),
                destination_report_count=destination_signal.get("sell_report_count"),
                volatility_origin=r.volatility_origin,
                volatility_destination=r.volatility_destination,
                origin_available=bool(r.scu_origin and r.scu_origin > 0),
                destination_available=has_sell_side_demand(
                    r.scu_destination, r.status_destination
                ),
                track_record_modifier=track_record_modifier(
                    origin_matched + destination_matched, origin_total + destination_total
                ),
            )
            value += f"\nConfidence: **{confidence.label} ({confidence.score}/100)**"
            practical_notes = route_practical_notes(
                terminal_references.get(r.origin_terminal_id),
                terminal_references.get(r.destination_terminal_id),
            )
            if practical_notes:
                value += "\n" + "\n".join(practical_notes)
            origin_system = (terminal_references.get(r.origin_terminal_id) or {}).get("star_system_name")
            destination_system = (terminal_references.get(r.destination_terminal_id) or {}).get("star_system_name")
            # has_real_distance reflects THIS route's own distance field, not the branch
            # as a whole - see the matching comment in Prices.best_route's primary branch
            # (bot/cogs/prices.py) for why a per-route check is needed here.
            if travel_note := travel_warning(origin_system, destination_system, has_real_distance=r.distance is not None):
                value += f"\n{travel_note}"
            risk_note = format_commodity_risk(commodity_references.get(r.id_commodity))
            if risk_note:
                value += f"\n{risk_note}"
            route_embed = discord.Embed(title=name, color=discord.Color.green())
            route_embed.set_footer(text=f"Route {i} of {len(entries)}")
            # Per-route embed, budget-checked on its own - a route's own detail lines
            # overflowing a single Discord embed is unlikely but not impossible, and this
            # stops and discloses instead of silently dropping it (see /best-route's
            # identical pattern in prices.py).
            if not _add_chunked_fields(route_embed, name="Details", lines=value.splitlines()):
                continue
            routes_shown += 1

            view = None
            if tracking_cog and r.origin_terminal_id is not None and r.destination_terminal_id is not None:
                trackable_route = TrackableRoute(
                    route_kind="top_routes",
                    title=f"{r.commodity_name}: {r.origin_terminal_name} → {r.destination_terminal_name}",
                    legs=[
                        RouteLegInput(
                            side="buy", id_terminal=r.origin_terminal_id, id_commodity=r.id_commodity,
                            terminal_name=r.origin_terminal_name, commodity_name=r.commodity_name,
                            display_label=f"Buy {r.commodity_name} at {r.origin_terminal_name}",
                            quoted_price=r.price_origin, quoted_scu=r.scu_origin,
                            quoted_status=r.status_origin,
                        ),
                        RouteLegInput(
                            side="sell", id_terminal=r.destination_terminal_id, id_commodity=r.id_commodity,
                            terminal_name=r.destination_terminal_name, commodity_name=r.commodity_name,
                            display_label=f"Sell {r.commodity_name} at {r.destination_terminal_name}",
                            quoted_price=r.price_destination, quoted_scu=r.scu_destination,
                            quoted_status=r.status_destination,
                        ),
                    ],
                )
                view = RouteTrackingView(tracking_cog, [trackable_route])

            if view is not None:
                await interaction.followup.send(embed=route_embed, view=view)
            else:
                await interaction.followup.send(embed=route_embed)

        omitted = len(entries) - routes_shown
        if omitted > 0:
            await interaction.followup.send(f"{omitted} more route(s) omitted - too large to display.")

    @app_commands.command(name="top-routes", description="Top trade routes by profit, with live-stock filtering.")
    @app_commands.describe(
        ship="Optional: check cargo/profit for a specific ship instead of your default (/set-default-ship)",
        strict="Require live stock at the origin and live demand at the destination (safer).",
        auto_load_only="Only show routes where both the origin and destination terminal offer UEX's auto-load",
        system="Optional: require both ends of the route to be in this star system",
    )
    @app_commands.rename(auto_load_only="auto-load-only")
    @app_commands.choices(system=SYSTEM_CHOICES)
    @app_commands.autocomplete(ship=ship_name_autocomplete)
    async def top_routes(
        self,
        interaction: discord.Interaction,
        strict: bool = False,
        ship: str | None = None,
        auto_load_only: bool | None = None,
        system: app_commands.Choice[str] | None = None,
    ) -> None:
        prefs = await self.bot.db.get_trading_preferences(interaction.user.id)
        if auto_load_only is None:
            auto_load_only = prefs["auto_load_only"]
        system_value = system.value if system else prefs["preferred_system"]
        if strict:
            async with self._top_in_stock_routes_lock:
                entries = list(self._top_in_stock_routes)
                updated_at = self._top_in_stock_routes_updated_at
            title = "Top Trade Routes — Strict Live Availability"
            footer_note = (
                "Ranked by profit (ROI% as a tie-breaker) · one route per commodity · requires "
                "real stock at the origin and real demand at the destination right now"
            )
        else:
            async with self._top_scored_routes_lock:
                entries = list(self._top_scored_routes)
                updated_at = self._top_scored_routes_updated_at
            title = "Top Trade Routes"
            footer_note = (
                "Ranked by profit (ROI% as a tie-breaker) · one route per commodity · filtered "
                "to real buy-side stock at the origin right now · use strict:True for live demand too"
            )

        if not entries:
            await interaction.response.send_message(
                "Still gathering route data (this refreshes on a timer after startup) - try again in a few minutes."
            )
            return

        await self._send_ranked_routes(
            interaction,
            entries=entries,
            updated_at=updated_at,
            ship=ship,
            title=title,
            footer_note=footer_note,
            log_label="/top-routes",
            display_limit=TOP_IN_STOCK_ROUTES_KEEP if strict else TOP_SCORED_ROUTES_KEEP,
            auto_load_only=auto_load_only,
            system=system_value,
            risk_tolerance=prefs["risk_tolerance"],
        )

    @app_commands.command(name="routes-from", description="Best trade routes starting from wherever you currently are.")
    @app_commands.describe(
        location="Terminal you're currently at, e.g. 'Area18' or 'Port Tressler'",
        ship="Optional: check cargo/profit for a specific ship instead of your default (/set-default-ship)",
        strict="Require live stock at the origin and live demand at the destination (safer).",
        auto_load_only="Only show routes where both the origin and destination terminal offer UEX's auto-load",
        system="Optional: require the destination to be in this star system too",
    )
    @app_commands.rename(auto_load_only="auto-load-only")
    @app_commands.choices(system=SYSTEM_CHOICES)
    @app_commands.autocomplete(ship=ship_name_autocomplete, location=terminal_name_autocomplete)
    async def routes_from(
        self,
        interaction: discord.Interaction,
        location: str,
        strict: bool = False,
        ship: str | None = None,
        auto_load_only: bool | None = None,
        system: app_commands.Choice[str] | None = None,
    ) -> None:
        resolved = await self.bot.db.resolve_terminal_id_by_name(location)
        if resolved is None:
            await interaction.response.send_message(
                f"Couldn't find a single terminal matching '{location}' - pick one from the "
                "autocomplete list to make sure it's unambiguous."
            )
            return
        origin_id, origin_name = resolved

        prefs = await self.bot.db.get_trading_preferences(interaction.user.id)
        if auto_load_only is None:
            auto_load_only = prefs["auto_load_only"]
        system_value = system.value if system else prefs["preferred_system"]

        # Reuses the SAME background-refreshed candidate pool /top-routes reads from
        # (comprehensive across every commodity UEX has route data for, not truncated),
        # just filtered down to routes departing from the resolved origin - no separate
        # ranking logic, no extra UEX calls.
        if strict:
            async with self._top_in_stock_routes_lock:
                pool = list(self._top_in_stock_routes)
                updated_at = self._top_in_stock_routes_updated_at
        else:
            async with self._top_scored_routes_lock:
                pool = list(self._top_scored_routes)
                updated_at = self._top_scored_routes_updated_at

        entries = [r for r in pool if r.origin_terminal_id == origin_id]
        if not entries:
            still_gathering = " (still gathering route data - try again in a few minutes)" if not pool else ""
            await interaction.response.send_message(
                f"No profitable routes found starting from **{origin_name}** right now{still_gathering}."
            )
            return

        title = f"Best Routes from {origin_name}"
        footer_note = (
            "Ranked by profit (ROI% as a tie-breaker) · requires real stock at the origin and "
            "real demand at the destination right now" if strict else
            "Ranked by profit (ROI% as a tie-breaker) · filtered to real buy-side stock at the "
            "origin right now · use strict:True for live demand too"
        )
        await self._send_ranked_routes(
            interaction,
            entries=entries,
            updated_at=updated_at,
            ship=ship,
            title=title,
            footer_note=footer_note,
            log_label="/routes-from",
            display_limit=TOP_IN_STOCK_ROUTES_KEEP if strict else TOP_SCORED_ROUTES_KEEP,
            auto_load_only=auto_load_only,
            system=system_value,
            risk_tolerance=prefs["risk_tolerance"],
        )

    @app_commands.command(
        name="route-on-the-way",
        description="Check for a profitable route between two specific terminals you're already traveling between.",
    )
    @app_commands.describe(
        origin="Terminal you're currently at, e.g. 'Area18' or 'Port Tressler'",
        destination="Terminal you're heading to",
        ship="Optional: check cargo/profit for a specific ship instead of your default (/set-default-ship)",
        budget="Optional: cap how much of the commodity you buy by this starting capital, in aUEC",
        strict="Require live stock at the origin and live demand at the destination (safer).",
        auto_load_only="Only show routes where both the origin and destination terminal offer UEX's auto-load",
    )
    @app_commands.rename(auto_load_only="auto-load-only")
    @app_commands.autocomplete(
        ship=ship_name_autocomplete, origin=terminal_name_autocomplete, destination=terminal_name_autocomplete
    )
    async def route_on_the_way(
        self,
        interaction: discord.Interaction,
        origin: str,
        destination: str,
        strict: bool = False,
        ship: str | None = None,
        budget: app_commands.Range[float, 1, 1_000_000_000] | None = None,
        auto_load_only: bool | None = None,
    ) -> None:
        resolved_origin = await self.bot.db.resolve_terminal_id_by_name(origin)
        if resolved_origin is None:
            await interaction.response.send_message(
                f"Couldn't find a single terminal matching '{origin}' - pick one from the "
                "autocomplete list to make sure it's unambiguous."
            )
            return
        origin_id, origin_name = resolved_origin

        resolved_destination = await self.bot.db.resolve_terminal_id_by_name(destination)
        if resolved_destination is None:
            await interaction.response.send_message(
                f"Couldn't find a single terminal matching '{destination}' - pick one from the "
                "autocomplete list to make sure it's unambiguous."
            )
            return
        destination_id, destination_name = resolved_destination

        if origin_id == destination_id:
            await interaction.response.send_message("Origin and destination can't be the same terminal.")
            return

        prefs = await self.bot.db.get_trading_preferences(interaction.user.id)
        if auto_load_only is None:
            auto_load_only = prefs["auto_load_only"]
        if budget is None:
            budget = prefs["budget"]

        # Same reuse pattern as /routes-from: filter the SAME background-refreshed
        # candidate pool /top-routes reads from, this time down to routes matching BOTH
        # the resolved origin AND destination - no separate ranking logic, no extra UEX
        # calls. Direction-specific (origin -> destination only, matching how the player
        # actually asked the question) - a route in the reverse direction, if one exists,
        # is what /route-on-the-way with the two locations swapped would find.
        if strict:
            async with self._top_in_stock_routes_lock:
                pool = list(self._top_in_stock_routes)
                updated_at = self._top_in_stock_routes_updated_at
        else:
            async with self._top_scored_routes_lock:
                pool = list(self._top_scored_routes)
                updated_at = self._top_scored_routes_updated_at

        entries = [
            r for r in pool if r.origin_terminal_id == origin_id and r.destination_terminal_id == destination_id
        ]
        if not entries:
            still_gathering = " (still gathering route data - try again in a few minutes)" if not pool else ""
            await interaction.response.send_message(
                f"No profitable routes found from **{origin_name}** to **{destination_name}** "
                f"right now{still_gathering}."
            )
            return

        title = f"Best Routes: {origin_name} → {destination_name}"
        footer_note = (
            "Ranked by profit (ROI% as a tie-breaker) · requires real stock at the origin and "
            "real demand at the destination right now" if strict else
            "Ranked by profit (ROI% as a tie-breaker) · filtered to real buy-side stock at the "
            "origin right now · use strict:True for live demand too"
        )
        await self._send_ranked_routes(
            interaction,
            entries=entries,
            updated_at=updated_at,
            ship=ship,
            title=title,
            footer_note=footer_note,
            log_label="/route-on-the-way",
            display_limit=TOP_IN_STOCK_ROUTES_KEEP if strict else TOP_SCORED_ROUTES_KEEP,
            auto_load_only=auto_load_only,
            system=None,
            risk_tolerance=prefs["risk_tolerance"],
            budget=float(budget) if budget is not None else None,
        )

    # -- /movers: single bulk call, computed on demand -----------------------

    @app_commands.command(name="movers", description="Commodities with the biggest sell-price swing vs their recent average.")
    async def movers(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer()
        try:
            rows = await self.bot.uex.get_commodities_prices_all()
        except UexApiError as exc:
            await interaction.followup.send(describe_uex_api_error(exc))
            return

        gainers, losers = compute_movers(rows, limit=5)
        if not gainers and not losers:
            await interaction.followup.send("No notable price movers right now.")
            return

        embed = discord.Embed(title="Commodity Price Movers", color=discord.Color.purple())
        if gainers:
            embed.add_field(
                name="Trending up",
                value="\n".join(
                    f"**{m.commodity_name}** +{m.pct_change:.1f}% ({m.current_avg_sell:,.0f} aUEC)" for m in gainers
                ),
                inline=False,
            )
        if losers:
            embed.add_field(
                name="Trending down",
                value="\n".join(
                    f"**{m.commodity_name}** {m.pct_change:.1f}% ({m.current_avg_sell:,.0f} aUEC)" for m in losers
                ),
                inline=False,
            )
        embed.set_footer(text="Sell price vs each commodity's own recent average, across all terminals · UEX data")
        await interaction.followup.send(embed=embed)

    # -- /commodity-history: chart, on demand ---------------------------------

    @app_commands.command(name="commodity-history", description="Price history chart for a commodity (optionally at a specific terminal).")
    @app_commands.describe(
        commodity="Commodity name, e.g. 'Gold'",
        terminal="Optional: terminal name to focus on (defaults to the most actively traded one)",
    )
    @app_commands.autocomplete(commodity=commodity_name_autocomplete)
    async def commodity_history(
        self, interaction: discord.Interaction, commodity: str, terminal: str | None = None
    ) -> None:
        await interaction.response.defer()

        try:
            price_rows = await self.bot.uex.get_commodities_prices(commodity_name=commodity)
        except UexApiError as exc:
            await interaction.followup.send(describe_uex_api_error(exc))
            return

        if not price_rows:
            await interaction.followup.send(f"No data found for '{commodity}'. Check the spelling.")
            return

        id_commodity = price_rows[0].get("id_commodity")
        commodity_display = price_rows[0].get("commodity_name", commodity)

        chosen_row = None
        if terminal:
            needle = terminal.lower()
            chosen_row = next((r for r in price_rows if needle in (r.get("terminal_name") or "").lower()), None)
            if chosen_row is None:
                await interaction.followup.send(
                    f"No terminal matching '{terminal}' sells/buys {commodity_display}. "
                    "Try /price to see the full terminal list."
                )
                return
        else:
            # Default: the terminal with the most real player trade activity for this commodity.
            chosen_row = max(
                price_rows,
                key=lambda r: (r.get("scu_buy_users_rows") or 0) + (r.get("scu_sell_users_rows") or 0),
            )

        id_terminal = chosen_row.get("id_terminal")
        terminal_display = chosen_row.get("terminal_name", "Unknown")

        try:
            history_rows = await self.bot.uex.get_commodities_prices_history(
                id_terminal=id_terminal, id_commodity=id_commodity
            )
        except UexApiError as exc:
            await interaction.followup.send(describe_uex_api_error(exc))
            return

        if not history_rows:
            await interaction.followup.send(f"No historical price data for {commodity_display} at {terminal_display} yet.")
            return

        chart_buffer = render_price_history_chart(
            commodity_name=commodity_display, terminal_name=terminal_display, history_rows=history_rows
        )
        if chart_buffer is None:
            await interaction.followup.send(f"No plottable price data for {commodity_display} at {terminal_display}.")
            return

        file = discord.File(chart_buffer, filename="price_history.png")
        embed = discord.Embed(title=f"{commodity_display} — Price History", color=discord.Color.blurple())
        embed.set_image(url="attachment://price_history.png")
        embed.set_footer(text=f"{terminal_display} · UEX data, up to 500 most recent snapshots")
        await interaction.followup.send(embed=embed, file=file)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Trends(bot))
