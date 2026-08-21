"""Price lookup and trade-route commands backed by UEX /commodities_prices."""
from __future__ import annotations

import logging

import discord
from discord import app_commands
from discord.ext import commands

from bot.cogs.ships import ship_name_autocomplete
from bot.uex.exceptions import UexApiError
from bot.uex.ships import estimate_route_cargo, resolve_ship
from bot.uex.status import build_status_lookup, resolve_status_label
from bot.uex.trading import best_buy_locations, best_routes, best_sell_locations

logger = logging.getLogger("uexbot.prices")

MAX_FIELD_ROWS = 5


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
            await interaction.followup.send(f"UEX API error: {exc}")
            return

        if not rows:
            await interaction.followup.send(f"No price data found for '{commodity}'. Check the spelling.")
            return

        commodity_display = rows[0].get("commodity_name", commodity)
        embed = discord.Embed(title=f"{commodity_display} — Prices", color=discord.Color.blurple())

        top_sell = best_sell_locations(rows, limit=MAX_FIELD_ROWS)
        top_buy = best_buy_locations(rows, limit=MAX_FIELD_ROWS)
        status_lookup = await self._get_status_lookup()

        if top_sell:
            lines = []
            for r in top_sell:
                label = resolve_status_label(status_lookup, "sell", r.get("status_sell"))
                label_text = f" · {label}" if label else ""
                lines.append(f"**{r['terminal_name']}** — {r['price_sell']:.2f} aUEC/unit{label_text}")
            embed.add_field(name="Best places to SELL", value="\n".join(lines), inline=False)
        if top_buy:
            lines = []
            for r in top_buy:
                label = resolve_status_label(status_lookup, "buy", r.get("status_buy"))
                label_text = f" · {label}" if label else ""
                lines.append(f"**{r['terminal_name']}** — {r['price_buy']:.2f} aUEC/unit{label_text}")
            embed.add_field(name="Best places to BUY", value="\n".join(lines), inline=False)

        embed.set_footer(text="Data from UEX Corp · cached up to 30 min · status = current stock/demand level")
        await interaction.followup.send(embed=embed)

    @app_commands.command(name="best-route", description="Find the most profitable buy->sell terminal pair for a commodity.")
    @app_commands.describe(
        commodity="Commodity name, e.g. 'Gold' or 'Laranite'",
        ship="Optional: check cargo for a specific ship instead of your default (/set-default-ship)",
    )
    @app_commands.autocomplete(ship=ship_name_autocomplete, commodity=commodity_name_autocomplete)
    async def best_route(self, interaction: discord.Interaction, commodity: str, ship: str | None = None) -> None:
        await interaction.response.defer()
        try:
            rows = await self.bot.uex.get_commodities_prices(commodity_name=commodity)
        except UexApiError as exc:
            await interaction.followup.send(f"UEX API error: {exc}")
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

        # Best-effort illegal-cargo check - /commodities already carries is_illegal per
        # commodity (a separate field, not tied to /jurisdictions' faction territories).
        # Never blocks the route lookup itself: a failed/missing lookup just means no
        # warning shown, not an error for the whole command.
        illegal_warning: str | None = None
        if id_commodity is not None:
            try:
                commodity_details = await self.bot.uex.get_commodities(id_commodity=id_commodity)
                if commodity_details and commodity_details[0].get("is_illegal"):
                    illegal_warning = (
                        "⚠️ **Illegal cargo** — this commodity is contraband. Expect scans/fines "
                        "at checkpoints and no legal buyer at most terminals."
                    )
            except UexApiError as exc:
                logger.info("is_illegal lookup failed for %s: %s", commodity_display, exc)

        # Prefer UEX's own precomputed routes (real inter-terminal distance, ROI, profit,
        # and a UEX quality score) over our own buy/sell pairing, which has no distance data.
        uex_routes: list[dict] = []
        if id_commodity is not None:
            try:
                uex_routes = await self.bot.uex.get_commodities_routes(id_commodity=id_commodity)
            except UexApiError as exc:
                logger.info("commodities_routes unavailable for %s, falling back: %s", commodity_display, exc)

        if uex_routes:
            ranked = sorted(uex_routes, key=lambda r: r.get("profit") or 0, reverse=True)[:MAX_FIELD_ROWS]
            embed = discord.Embed(title=f"{commodity_display} — Best Trade Routes", color=discord.Color.green())
            if illegal_warning:
                embed.description = illegal_warning
            for r in ranked:
                origin = r.get("origin_terminal_name", "Unknown")
                dest = r.get("destination_terminal_name", "Unknown")
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
                embed.add_field(name=f"{origin} → {dest}", value="\n".join(value_lines), inline=False)
            footer = "Data from UEX Corp /commodities_routes"
            if not ship_vehicle:
                footer += " · set a default ship with /set-default-ship for cargo/run-profit numbers"
            embed.set_footer(text=footer)
            await interaction.followup.send(embed=embed)
            return

        # Fallback: derive routes ourselves from raw price rows (no distance data available).
        routes = best_routes(rows, limit=MAX_FIELD_ROWS)
        if not routes:
            await interaction.followup.send(f"No profitable buy/sell pair found for '{commodity}' right now.")
            return

        embed = discord.Embed(
            title=f"{routes[0].commodity_name} — Best Trade Routes",
            color=discord.Color.green(),
        )
        if illegal_warning:
            embed.description = illegal_warning
        for route in routes:
            value_lines = [
                f"Buy {route.buy_price:.2f} / Sell {route.sell_price:.2f}\n"
                f"Profit: **{route.profit_per_unit:.2f} aUEC/unit** ({route.margin_pct}%)"
            ]

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

            embed.add_field(name=f"{route.buy_terminal} → {route.sell_terminal}", value="\n".join(value_lines), inline=False)

        footer = "Data from UEX Corp · does not account for travel time between terminals"
        if not ship_vehicle:
            footer += " · set a default ship with /set-default-ship for cargo/run-profit numbers"
        embed.set_footer(text=footer)
        await interaction.followup.send(embed=embed)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Prices(bot))
