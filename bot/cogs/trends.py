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
from datetime import datetime, timedelta, timezone

import discord
from discord import app_commands
from discord.ext import commands, tasks

from bot.cogs.prices import (
    SYSTEM_CHOICES,
    _add_chunked_fields,
    ambiguous_commodity_text,
    commodity_name_autocomplete,
    terminal_name_autocomplete,
)
from bot.cogs.route_progression import RouteLegInput, TrackableRoute
from bot.cogs.ships import ship_name_autocomplete
from bot.route_pages import RoutePage, send_route_pages
from bot.uex.charts import render_price_history_chart
from bot.uex.commodity_risk import format_commodity_risk
from bot.uex.data_health import classify_terminal_health, format_health_note
from bot.uex.exceptions import UexApiError, describe_uex_api_error
from bot.uex.mixed_routes import find_hedge_cargo
from bot.uex.practical_routes import (
    route_in_system,
    route_practical_notes,
    route_supports_auto_load,
)
from bot.uex.route_confidence import compute_route_confidence, track_record_modifier
from bot.uex.route_presentation import (
    cargo_item_line,
    format_evidence_note,
    hedge_room,
    missing_ship_cargo_line,
    missing_ship_note,
    stock_headroom_warning,
    travel_warning,
)
from bot.uex.route_progression import SUPPRESSION_HOURS
from bot.uex.ships import estimate_route_cargo, resolve_ship
from bot.uex.status import build_status_lookup, resolve_status_label
from bot.uex.supply_demand import (
    SELL_SIDE_STATUS_CLARIFIER,
    EvidenceLevel,
    analyze_terminal_market_history,
    classify_supply_evidence,
    has_sell_side_demand,
)
from bot.uex.trading import rows_for_commodity, rows_for_known_commodity
from bot.uex.trading_preferences import (
    describe_active_preferences,
    saved_filter_labels,
    saved_filters_hint,
)
from bot.uex.trends import (
    RefreshGap,
    ScoredRouteEntry,
    TrendingEntry,
    aggregate_commodity_trips,
    compute_movers,
    partial_refresh_hint,
    partial_refresh_note,
    rank_by_achievable_profit,
    rank_top_scored_routes,
    rank_trending,
    select_available_routes,
    select_in_stock_routes,
    should_replace_snapshot,
)

logger = logging.getLogger("uexbot.trends")


def _route_cargo_estimate(r: ScoredRouteEntry, ship_cargo_scu: float | None, budget: float | None):
    """The one place a ranked-list route's haulable cargo is estimated, so the field builder
    and the hedge pre-pass in _send_ranked_routes can never disagree about whether a route
    is stock-limited."""
    return estimate_route_cargo(
        per_unit_profit=r.price_destination - r.price_origin,
        origin_scu_available=r.scu_origin,
        destination_scu_wanted=r.scu_destination,
        ship_cargo_scu=ship_cargo_scu,
        price_origin=r.price_origin,
        budget=budget,
    )


def _build_route_field(
    i: int,
    r: ScoredRouteEntry,
    ship_vehicle: dict | None,
    ship_cargo_scu: float | None,
    status_lookup: dict,
    origin_evidence: EvidenceLevel,
    destination_evidence: EvidenceLevel,
    budget: float | None = None,
    hedge_items: list | None = None,
    missing_ship_line: str | None = None,
) -> tuple[str, str]:
    """Build one route field for /top-routes. hedge_items are find_hedge_cargo's suggestions for a
    stock-limited route, already looked up by the caller - this function does no I/O."""
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

    cargo = _route_cargo_estimate(r, ship_cargo_scu, budget)
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
        if headroom_note := stock_headroom_warning(cargo.limited_by):
            value_lines.append(f"⚠️ {headroom_note}")
            for hedge_item in hedge_items or ():
                value_lines.append(f"Hedge: {cargo_item_line(hedge_item, risk=True)}")
    elif not ship_vehicle:
        value_lines.append(missing_ship_line or "Cargo: unknown (set a ship with /set-trading-preferences to see haulable SCU)")

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
        value_lines.append(f"{r.distance:.1f} Gm")

    name = f"{i}. {r.commodity_name}: {r.origin_terminal_name} → {r.destination_terminal_name}"
    return name, "\n".join(value_lines)


TRENDING_REFRESH_MINUTES = 45
TRENDING_KEEP_TOP = 25
TOP_SCORED_ROUTES_KEEP = 10
TOP_IN_STOCK_ROUTES_KEEP = 10

# Pace background calls well under the 120/min UEX limit, leaving headroom for
# whatever real user commands are running concurrently.
_TRENDING_CALL_DELAY = 0.6

# A refresh that couldn't fetch more than this share of commodities keeps the previous
# snapshot, while that one is more complete and younger than REFRESH_KEEP_PREVIOUS_MAX_AGE
# (audit REL-5). Past that age, the fresher partial one is shown, labelled as partial.
REFRESH_MAX_FAILED_SHARE = 0.10
REFRESH_KEEP_PREVIOUS_MAX_AGE = timedelta(hours=2)


def _gap_log(gap: RefreshGap) -> str:
    return f" (couldn't fetch {gap.missing} of {gap.attempted} commodities)" if gap.missing else ""


class Trends(commands.Cog):
    # What each cached snapshot's refresh couldn't fetch. Class-level defaults as well as
    # instance ones (RefreshGap is immutable), so a snapshot set without a refresh counts
    # as complete.
    _trending_gap = RefreshGap()
    _top_scored_routes_gap = RefreshGap()
    _top_in_stock_routes_gap = RefreshGap()

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self._trending: list[TrendingEntry] = []
        self._trending_updated_at: datetime | None = None
        self._trending_gap = RefreshGap()
        self._trending_lock = asyncio.Lock()
        self._top_scored_routes: list[ScoredRouteEntry] = []
        self._top_scored_routes_updated_at: datetime | None = None
        self._top_scored_routes_gap = RefreshGap()
        self._top_scored_routes_lock = asyncio.Lock()
        self._top_in_stock_routes: list[ScoredRouteEntry] = []
        self._top_in_stock_routes_updated_at: datetime | None = None
        self._top_in_stock_routes_gap = RefreshGap()
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
            gap = self._trending_gap

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
        if partial_refresh_note(gap):
            footer += f" · {partial_refresh_note(gap)}"
        embed.set_footer(text=footer)
        await interaction.response.send_message(embed=embed)

    @tasks.loop(minutes=TRENDING_REFRESH_MINUTES)
    async def refresh_trending(self) -> None:
        # Nothing may escape a tasks.loop body: it only restarts itself after a narrow set
        # of network errors, so anything else would freeze /top-routes and /movers until a
        # restart.
        try:
            await self._refresh_trending_once()
        except Exception:
            logger.exception("Trending refresh failed; keeping the previous snapshot until next cycle")

    async def _refresh_trending_once(self) -> None:
        try:
            commodities = await self.bot.uex.get_commodities()
        except UexApiError as exc:
            logger.warning("Failed to list commodities for trending refresh: %s", exc)
            return

        tradeable = [c for c in commodities if c.get("is_buyable") or c.get("is_sellable")]
        entries: list[TrendingEntry] = []
        route_candidates: list[ScoredRouteEntry] = []
        in_stock_route_candidates: list[ScoredRouteEntry] = []
        # Commodities this refresh couldn't fetch, per cache: a failed price fetch loses a
        # commodity from both, a failed route fetch only from the route lists.
        attempted = 0
        trending_missing: set[str] = set()
        routes_missing: set[str] = set()

        for commodity in tradeable:
            name = commodity.get("name")
            if not name:
                continue
            attempted += 1
            try:
                rows = await self.bot.uex.get_commodities_prices(commodity_name=name)
            except UexApiError as exc:
                logger.info("Skipping %s in trending refresh: %s", name, exc)
                trending_missing.add(name)
                routes_missing.add(name)
                await asyncio.sleep(_TRENDING_CALL_DELAY)
                continue

            await asyncio.sleep(_TRENDING_CALL_DELAY)

            if not rows:
                continue

            # Per commodity, so one commodity's odd rows can't sink the whole refresh.
            try:
                # UEX matches commodity_name as a SUBSTRING: 'Gold' also returned Golden
                # Medmon (its 71,000 became Gold's trending best sell), and 'Tin' listed
                # Astatine first, so rows[0]'s id gathered Astatine's routes a second time
                # under Tin's name. Only this catalog commodity's own rows, by its id.
                rows = rows_for_known_commodity(rows, name, commodity.get("id"))
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
                        routes_missing.add(name)
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
            except Exception:
                logger.exception("Skipping %s in trending refresh: unexpected data", name)
                trending_missing.add(name)
                routes_missing.add(name)

        now = datetime.now(timezone.utc)
        trending_gap = RefreshGap(len(trending_missing), attempted)
        routes_gap = RefreshGap(len(routes_missing), attempted)

        ranked = rank_trending(entries, limit=TRENDING_KEEP_TOP)
        async with self._trending_lock:
            if self._replaces(trending_gap, self._trending_gap, self._trending_updated_at, now):
                self._trending = ranked
                self._trending_updated_at = now
                self._trending_gap = trending_gap
                logger.info("Trending refresh complete: %d commodities ranked%s",
                            len(ranked), _gap_log(trending_gap))
            else:
                logger.warning("Trending refresh kept the previous snapshot: couldn't fetch %d of %d commodities",
                               trending_gap.missing, trending_gap.attempted)

        # Keep every candidate the loop already computed, not just the top
        # TOP_SCORED_ROUTES_KEEP by score - a user's auto-load-only/system filter runs
        # later, at command time, and can only work with what's still here. Discarding
        # the rest now would make a route that fails on score alone but would pass the
        # filter unrecoverable, since this refresh cycle's candidates aren't kept
        # anywhere else. TOP_SCORED_ROUTES_KEEP is applied as a *display* cap instead,
        # after filtering, in _send_ranked_routes.
        ranked_routes = rank_top_scored_routes(route_candidates, limit=len(route_candidates))
        async with self._top_scored_routes_lock:
            if self._replaces(routes_gap, self._top_scored_routes_gap, self._top_scored_routes_updated_at, now):
                self._top_scored_routes = ranked_routes
                self._top_scored_routes_updated_at = now
                self._top_scored_routes_gap = routes_gap
                logger.info("Top-routes refresh complete: %d candidates, %d kept%s",
                            len(route_candidates), len(ranked_routes), _gap_log(routes_gap))
            else:
                logger.warning("Top-routes refresh kept the previous snapshot: couldn't fetch %d of %d commodities",
                               routes_gap.missing, routes_gap.attempted)

        ranked_in_stock_routes = rank_top_scored_routes(in_stock_route_candidates, limit=len(in_stock_route_candidates))
        async with self._top_in_stock_routes_lock:
            if self._replaces(routes_gap, self._top_in_stock_routes_gap, self._top_in_stock_routes_updated_at, now):
                self._top_in_stock_routes = ranked_in_stock_routes
                self._top_in_stock_routes_updated_at = now
                self._top_in_stock_routes_gap = routes_gap
                logger.info("Strict top-routes refresh complete: %d candidates, %d kept%s",
                            len(in_stock_route_candidates), len(ranked_in_stock_routes), _gap_log(routes_gap))
            else:
                logger.warning("Strict top-routes refresh kept the previous snapshot: couldn't fetch %d of %d "
                               "commodities", routes_gap.missing, routes_gap.attempted)

    @staticmethod
    def _replaces(new: RefreshGap, previous: RefreshGap, previous_at: datetime | None, now: datetime) -> bool:
        return should_replace_snapshot(
            new, previous if previous_at is not None else None,
            now - previous_at if previous_at is not None else None,
            max_failed_share=REFRESH_MAX_FAILED_SHARE, max_keep_age=REFRESH_KEEP_PREVIOUS_MAX_AGE,
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
        gap: RefreshGap = RefreshGap(),
        title: str,
        footer_note: str,
        log_label: str,
        display_limit: int,
        auto_load_only: bool = False,
        system: str | None = None,
        budget: float | None = None,
        already_deferred: bool = False,
        auto_load_saved: bool = False,
        system_saved: bool = False,
    ) -> None:
        # auto_load_saved/system_saved: that filter came from the player's saved
        # preferences, not this command - an empty result then says so (audit UX-2).
        # already_deferred: the caller deferred itself before its own DB work (resolving a
        # terminal, reading preferences) - deferring again here would raise
        # discord.InteractionResponded.
        if not already_deferred:
            await interaction.response.defer()

        ship_query = ship or await self.bot.db.get_default_ship(interaction.user.id)
        ship_vehicle = None
        ship_lookup_failed = False
        if ship_query:
            try:
                vehicles = await self.bot.uex.get_vehicles()
                ship_vehicle = resolve_ship(vehicles, ship_query)
            except UexApiError as exc:
                ship_lookup_failed = True
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
                + partial_refresh_hint(gap)
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
                    "No routes with auto-load at both ends found right now."
                    + saved_filters_hint(saved_filter_labels(auto_load_only=auto_load_saved))
                    + partial_refresh_hint(gap)
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
                    + saved_filters_hint(saved_filter_labels(system=system if system_saved else None))
                    + partial_refresh_hint(gap)
                )
                return
        # Re-rank by what THIS player can actually haul/afford before dedup/truncation,
        # not UEX's own unlimited-cargo/budget 'profit' figure the candidates arrived
        # sorted by - see rank_by_achievable_profit's docstring for the real Waste-vs-
        # Corundum numbers that motivated this. A no-op until a ship and/or budget is
        # known for this player.
        entries = rank_by_achievable_profit(entries, ship_cargo_scu=ship_cargo_scu, budget=budget)
        # Dedupe back to one route per commodity - entries can now carry several
        # candidates per commodity (see select_available_routes), so a same-commodity
        # alternative survives being filtered here instead of the whole commodity
        # disappearing when only its top-scored route is checked. entries is now sorted
        # by achievable profit (or still UEX profit, if neither ship nor budget is set) at
        # this point, so keeping the first occurrence per commodity keeps the best
        # surviving one either way.
        seen_commodities: set[int] = set()
        deduped_entries: list[ScoredRouteEntry] = []
        for route in entries:
            if route.id_commodity in seen_commodities:
                continue
            seen_commodities.add(route.id_commodity)
            deduped_entries.append(route)
        entries = deduped_entries
        # Captured before truncation - distinguishes "fewer routes exist right now" (this
        # count) from "more exist but didn't fit the display size" (display_limit itself),
        # so the footer below can tell a player which one they're looking at instead of
        # silently showing fewer routes than the usual list with no explanation.
        qualifying_count = len(entries)
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
        if partial_refresh_note(gap):
            footer += f" · {partial_refresh_note(gap)}"
        if ship_vehicle and ship_cargo_scu is not None:
            # Consistency fix: a resolved ship used to only get named inside a per-route
            # cargo line, and only for a route that happened to be ship-limited
            # specifically (not stock- or budget-limited) - so the exact same ship, used
            # to compute cargo/profit for every route shown, could go completely
            # unnamed. Named here unconditionally instead, matching how /mixed-routes and
            # /multi-stop-route already name theirs up front.
            footer += f" · cargo/run-profit numbers use {ship_vehicle.get('name', ship_query)}'s {ship_cargo_scu:,.0f} SCU hold"
        elif ship_vehicle:
            footer += f" · using {ship_vehicle.get('name', ship_query)} (no cargo capacity on record)"
        else:
            footer += " · " + missing_ship_note(ship_query, lookup_failed=ship_lookup_failed)
        if budget is not None:
            footer += f" · budget {budget:,.0f} aUEC"
        preferences_note = describe_active_preferences(
            auto_load_only=auto_load_only, system=system,
            saved={name for name, on in (("auto_load_only", auto_load_saved), ("system", system_saved)) if on},
        )
        if preferences_note:
            footer += " · " + preferences_note
        if qualifying_count < display_limit:
            footer += (
                f" · only {qualifying_count} route{'s' if qualifying_count != 1 else ''} "
                f"currently {'qualifies' if qualifying_count == 1 else 'qualify'} "
                f"(this list shows up to {display_limit}) - more may appear as route data keeps refreshing"
            )

        # The intro is the text above every page of the one results message (audit UX-6).
        header = f"**{title}**\n-# {footer}"

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

        # Hedge protection (route_presentation.hedge_room): only a stock-limited haul leaves
        # cargo space idle, so the market snapshot is loaded at most once, and only if some
        # shown route actually needs it. Additive - a failure here costs the player the
        # hedge suggestions, never their routes.
        hedge_items_by_route: dict[int, list] = {}
        try:
            market_rows = None
            for i, r in enumerate(entries, start=1):
                if r.origin_terminal_id is None or r.destination_terminal_id is None:
                    continue
                cargo = _route_cargo_estimate(r, ship_cargo_scu, budget)
                room = hedge_room(cargo, ship_cargo_scu=ship_cargo_scu, budget=budget) if cargo is not None else None
                if room is None:
                    continue
                if market_rows is None:
                    market_rows = await self.bot.db.get_mixed_route_market_rows()
                hedge_items_by_route[i] = find_hedge_cargo(
                    market_rows, origin_terminal_id=r.origin_terminal_id,
                    destination_terminal_id=r.destination_terminal_id, exclude_commodity_id=r.id_commodity,
                    remaining_capacity_scu=room.capacity_scu, remaining_budget=room.budget,
                )
        except Exception:
            logger.warning("Hedge suggestions unavailable for %s", log_label, exc_info=True)
            hedge_items_by_route = {}

        pages: list[RoutePage] = []
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
                budget=budget, hedge_items=hedge_items_by_route.get(i),
                missing_ship_line=missing_ship_cargo_line(ship_query, lookup_failed=ship_lookup_failed),
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
            # Per-route embed, budget-checked on its own - a route's own detail lines
            # overflowing a single Discord embed is unlikely but not impossible, and this
            # stops and discloses instead of silently dropping it (see /best-route's
            # identical pattern in prices.py).
            if not _add_chunked_fields(route_embed, name="Details", lines=value.splitlines()):
                continue

            trackable_route = None
            if tracking_cog and r.origin_terminal_id is not None and r.destination_terminal_id is not None:
                trackable_route = TrackableRoute(
                    route_kind="top_routes",
                    title=f"{r.commodity_name}: {r.origin_terminal_name} → {r.destination_terminal_name}",
                    auto_load_only=auto_load_only, system=system,
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
            pages.append(RoutePage(route_embed, f"**{name}**\n{value}", trackable_route))

        await send_route_pages(interaction, pages, tracking_cog=tracking_cog, header=header,
                               omitted=len(entries) - len(pages))

    @app_commands.command(name="top-routes", description="Top trade routes by profit, with live-stock filtering.")
    @app_commands.describe(
        origin="Optional: only routes starting at this terminal, e.g. where you are now",
        destination="Optional: only routes ending at this terminal, e.g. where you're heading",
        ship="Optional: check cargo/profit for a specific ship instead of your saved default",
        strict="Require live stock at the origin and live demand at the destination (safer).",
        auto_load_only="Only show routes where both the origin and destination terminal offer UEX's auto-load",
        system="Optional: require both ends of the route to be in this star system",
        budget="Optional: cap the cargo shown by how much you can actually afford to spend",
    )
    @app_commands.rename(auto_load_only="auto-load-only")
    @app_commands.choices(system=SYSTEM_CHOICES)
    @app_commands.autocomplete(
        ship=ship_name_autocomplete, origin=terminal_name_autocomplete, destination=terminal_name_autocomplete,
    )
    async def top_routes(
        self,
        interaction: discord.Interaction,
        origin: str | None = None,
        destination: str | None = None,
        strict: bool = False,
        ship: str | None = None,
        auto_load_only: bool | None = None,
        system: app_commands.Choice[str] | None = None,
        budget: app_commands.Range[float, 1, 1_000_000_000] | None = None,
    ) -> None:
        """Also answers what /routes-from (origin) and /route-on-the-way (origin and
        destination) did before they were folded in here (audit UX-8): the same ranked
        list, filtered to the named terminals - no separate ranking, no extra UEX calls."""
        # Deferred first: naming a terminal means resolving it, and preferences are a DB
        # read too - both before Discord's ~3s deadline would otherwise be at risk.
        await interaction.response.defer()
        pins: dict[str, tuple[int, str]] = {}
        for key, query in (("origin", origin), ("destination", destination)):
            if not query:
                continue
            resolved = await self.bot.db.resolve_terminal_id_by_name(query)
            if resolved is None:
                await interaction.followup.send(
                    f"Couldn't find a single terminal matching '{query}' - pick one from the "
                    "autocomplete list to make sure it's unambiguous."
                )
                return
            pins[key] = resolved
        origin_id, origin_name = pins.get("origin", (None, None))
        destination_id, destination_name = pins.get("destination", (None, None))
        if origin_id is not None and origin_id == destination_id:
            await interaction.followup.send("Origin and destination can't be the same terminal.")
            return
        # With both ends named there's nothing left for a star-system filter to restrict.
        both_ends = origin_id is not None and destination_id is not None

        prefs = await self.bot.db.get_trading_preferences(interaction.user.id)
        auto_load_saved = auto_load_only is None and bool(prefs["auto_load_only"])
        system_saved = not both_ends and system is None and bool(prefs["preferred_system"])
        if auto_load_only is None:
            auto_load_only = prefs["auto_load_only"]
        system_value = None if both_ends else (system.value if system else prefs["preferred_system"])
        # Consistency fix: /top-routes shared the exact same cargo/budget machinery as the
        # mixed-cargo commands (_build_route_field, estimate_route_cargo) but never plumbed
        # a budget through to it, so a user's saved budget silently had no effect here.
        if budget is None:
            budget = prefs["budget"]
        if strict:
            async with self._top_in_stock_routes_lock:
                pool = list(self._top_in_stock_routes)
                updated_at = self._top_in_stock_routes_updated_at
                gap = self._top_in_stock_routes_gap
        else:
            async with self._top_scored_routes_lock:
                pool = list(self._top_scored_routes)
                updated_at = self._top_scored_routes_updated_at
                gap = self._top_scored_routes_gap

        # Direction-specific: a route the other way round is found by swapping the two.
        entries = [
            r for r in pool
            if (origin_id is None or r.origin_terminal_id == origin_id)
            and (destination_id is None or r.destination_terminal_id == destination_id)
        ]
        if both_ends:
            where = f"from **{origin_name}** to **{destination_name}**"
            title = f"Best Routes: {origin_name} → {destination_name}"
        elif origin_id is not None:
            where, title = f"starting from **{origin_name}**", f"Best Routes from {origin_name}"
        elif destination_id is not None:
            where, title = f"ending at **{destination_name}**", f"Best Routes to {destination_name}"
        else:
            where, title = None, "Top Trade Routes — Strict Live Availability" if strict else "Top Trade Routes"
        if not entries:
            if where is None:
                await interaction.followup.send(
                    "Still gathering route data (this refreshes on a timer after startup) - try again in a few minutes."
                )
                return
            still_gathering = " (still gathering route data - try again in a few minutes)" if not pool else ""
            await interaction.followup.send(
                f"No profitable routes found {where} right now{still_gathering}." + partial_refresh_hint(gap)
            )
            return

        per_commodity = " · one route per commodity" if where is None else ""
        footer_note = (
            f"Ranked by profit (ROI% as a tie-breaker){per_commodity} · requires real stock at the origin and "
            "real demand at the destination right now" if strict else
            f"Ranked by profit (ROI% as a tie-breaker){per_commodity} · filtered to real buy-side stock at the "
            "origin right now · use strict:True for live demand too"
        )
        await self._send_ranked_routes(
            interaction,
            entries=entries,
            updated_at=updated_at,
            gap=gap,
            ship=ship,
            title=title,
            footer_note=footer_note,
            log_label="/top-routes",
            display_limit=TOP_IN_STOCK_ROUTES_KEEP if strict else TOP_SCORED_ROUTES_KEEP,
            auto_load_only=auto_load_only,
            system=system_value,
            auto_load_saved=auto_load_saved,
            system_saved=system_saved,
            budget=float(budget) if budget is not None else None,
            already_deferred=True,
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

        # UEX matches commodity_name as a SUBSTRING - 'Gold' also returned Golden Medmon,
        # whose terminals could win the "most traded" default below and chart the wrong
        # market. Only the commodity asked for.
        price_rows, others = rows_for_commodity(price_rows, commodity)
        if others:
            await interaction.followup.send(ambiguous_commodity_text(commodity, others))
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

        # Off the event loop: drawing a chart is CPU-bound (audit REL-14).
        chart_buffer = await asyncio.to_thread(
            render_price_history_chart,
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
