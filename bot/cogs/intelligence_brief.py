"""Personalized, on-demand market intelligence assembled from collected local history."""
from __future__ import annotations

import discord
from discord import app_commands
from discord.ext import commands

from bot.cogs.digest import _format_data_freshness, _format_rating_movers
from bot.cogs.ships import ship_name_autocomplete
from bot.uex.commodity_risk import commodity_risk_labels, has_commodity_risk_metadata
from bot.uex.exceptions import UexApiError
from bot.uex.mixed_routes import build_mixed_routes, requires_capital_cargo_access
from bot.uex.ships import resolve_ship


class IntelligenceBrief(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    @app_commands.command(
        name="intelligence-brief",
        description="Personalized routes, market shifts, risks, and collection health.",
    )
    @app_commands.describe(
        ship="Optional ship; otherwise uses your saved default",
        budget="Optional maximum aUEC to invest in a mixed load",
        space_only="Exclude surface terminals from route recommendations",
    )
    @app_commands.rename(space_only="space-only")
    @app_commands.autocomplete(ship=ship_name_autocomplete)
    async def intelligence_brief(
        self,
        interaction: discord.Interaction,
        ship: str | None = None,
        budget: app_commands.Range[float, 1, 1_000_000_000] | None = None,
        space_only: bool = False,
    ) -> None:
        await interaction.response.defer()
        freshness = await self.bot.db.get_digest_data_freshness()
        gainers = await self.bot.db.get_liquidity_movers(limit=4, direction="up")
        losers = await self.bot.db.get_liquidity_movers(limit=4, direction="down")
        shifts = await self.bot.db.get_terminal_market_shifts()

        embeds = [self._overview_embed(freshness, gainers, losers)]
        embeds.append(self._market_shifts_embed(shifts))

        ship_query = ship or await self.bot.db.get_default_ship(interaction.user.id)
        if ship_query:
            route_embed = await self._routes_embed(ship_query, budget, space_only)
            embeds.insert(1, route_embed)
        else:
            embeds[0].add_field(
                name="Personalized routes",
                value="Set a ship with `/set-default-ship` or pass `ship` to include mixed-route opportunities.",
                inline=False,
            )
        await interaction.followup.send(embeds=embeds)

    def _overview_embed(self, freshness: dict, gainers: list[dict], losers: list[dict]) -> discord.Embed:
        embed = discord.Embed(title="Intelligence Brief", color=discord.Color.blurple())
        signals = []
        if gainers:
            signals.append(f"📈 Strongest sellability gain: **{gainers[0]['item_name']}** ({gainers[0]['score_change']:+.0f})")
        if losers:
            signals.append(f"📉 Strongest sellability drop: **{losers[0]['item_name']}** ({losers[0]['score_change']:+.0f})")
        embed.add_field(name="Executive signals", value="\n".join(signals) or "No major rating changes yet.", inline=False)
        embed.add_field(name="Data health", value=_format_data_freshness(freshness), inline=False)
        embed.add_field(name="Sellability shifts — Up", value="\n".join(_format_rating_movers(gainers, direction="up")), inline=False)
        embed.add_field(name="Sellability shifts — Down", value="\n".join(_format_rating_movers(losers, direction="down")), inline=False)
        return embed

    async def _routes_embed(self, ship_query: str, budget: float | None, space_only: bool) -> discord.Embed:
        embed = discord.Embed(title="Personalized Mixed Routes", color=discord.Color.green())
        try:
            vehicles = await self.bot.uex.get_vehicles()
            vehicle = resolve_ship(vehicles, ship_query)
            if not vehicle or not vehicle.get("scu"):
                raise ValueError("ship cargo capacity unavailable")
            rows = await self.bot.db.get_mixed_route_market_rows()
            capital_gate = requires_capital_cargo_access(vehicle)
            if capital_gate:
                stations = await self.bot.uex.get_space_stations()
                station_map = {int(s["id"]): s for s in stations if s.get("id") and int(s["id"]) > 0}
                for row in rows:
                    station = station_map.get(int(row.get("id_space_station") or 0), {})
                    row["station_pad_types"] = station.get("pad_types")
                    row["station_has_loading_dock"] = station.get("has_loading_dock")
            routes = build_mixed_routes(
                rows, ship_capacity_scu=float(vehicle["scu"]),
                budget=float(budget) if budget is not None else None,
                limit=3, max_commodities=3, space_only=space_only,
                capital_access_only=capital_gate,
            )
        except (UexApiError, ValueError) as exc:
            embed.description = f"Route intelligence unavailable: {exc}"
            return embed

        embed.description = f"Top opportunities for **{vehicle.get('name', ship_query)}**"
        for index, route in enumerate(routes, 1):
            manifest = ", ".join(f"{item.commodity_name} {item.quantity_scu:,.0f} SCU" for item in route.cargo)
            risks = sorted({label for item in route.cargo for label in commodity_risk_labels(item.source)})
            unknown_risks = sorted({
                item.commodity_name
                for item in route.cargo
                if not has_commodity_risk_metadata(item.source)
            })
            notes = [
                manifest,
                f"Profit **{route.profit:,.0f} aUEC** · investment {route.investment:,.0f} · ROI {route.roi_pct:.1f}%",
            ]
            if risks:
                notes.append("⚠️ " + " · ".join(risks))
            if unknown_risks:
                notes.append(f"⚠️ Cargo risk metadata unavailable: {', '.join(unknown_risks)}")
            if capital_gate:
                notes.append("Capital access confirmed at both ends")
            origin_system = route.cargo[0].source.get("star_system_name")
            destination_system = route.cargo[0].destination.get("star_system_name")
            if cross_system_note := _format_cross_system_note(origin_system, destination_system):
                notes.append(cross_system_note)
            embed.add_field(name=f"#{index} {route.origin_name} → {route.destination_name}", value="\n".join(notes), inline=False)
        if not routes:
            embed.description = "No verified mixed routes fit the selected ship, budget, and safety filters."
        return embed

    def _market_shifts_embed(self, shifts: list[dict]) -> discord.Embed:
        embed = discord.Embed(title="24-Hour Supply & Demand Watch", color=discord.Color.gold())
        supply = sorted((r for r in shifts if r["supply_change"]), key=lambda r: abs(r["supply_change"]), reverse=True)[:4]
        demand = sorted((r for r in shifts if r["demand_change"]), key=lambda r: abs(r["demand_change"]), reverse=True)[:4]
        embed.add_field(name="Largest supply changes", value=_format_market_shifts(supply, "supply_change") or "No supply changes recorded.", inline=False)
        embed.add_field(name="Largest demand changes", value=_format_market_shifts(demand, "demand_change") or "No demand changes recorded.", inline=False)
        embed.set_footer(text="Change-only local history · verify current stock before departure")
        return embed


def _format_market_shifts(rows: list[dict], key: str) -> str:
    return "\n".join(
        f"{'📈' if row[key] > 0 else '📉'} **{row['commodity_name']}** at {row['terminal_name']}: {row[key]:+,.0f} SCU"
        for row in rows
    )


def _format_cross_system_note(origin_system: object, destination_system: object) -> str | None:
    origin = str(origin_system).strip() if origin_system is not None else ""
    destination = str(destination_system).strip() if destination_system is not None else ""
    if not origin or not destination:
        return "⚠️ Star-system data incomplete; verify travel distance before departure"
    if origin != destination:
        return f"⚠️ Cross-system: {origin} → {destination}"
    return None


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(IntelligenceBrief(bot))
