"""Refinery Advisor (/refinery-advisor): for up to three raw/refinable commodities mined
from the same rock/asteroid, ranks refinery terminals by yield bonus, lists the high-yield
refining methods, and shows each refined commodity's current best sell price.
"""
from __future__ import annotations

import discord
from discord import app_commands
from discord.ext import commands

from bot.uex.exceptions import UexApiError, describe_uex_api_error
from bot.uex.mining_locations import names_for_ids
from bot.uex.refinery import (
    COST_LABELS,
    SPEED_LABELS,
    combine_mining_systems,
    display_terminal_name,
    high_yield_refining_methods,
    rank_refinery_terminals,
    resolve_raw_commodity,
    select_terminals_to_show,
)
from bot.uex.route_presentation import add_chunked_fields
from bot.uex.trading import best_sell_locations

MAX_SELL_LOCATIONS = 3
# Minimum refineries shown; every refinery in the ore's own mining system is shown even
# past this (up to MAX_IN_SYSTEM_TERMINALS) - see select_terminals_to_show.
MAX_TERMINALS = 5
MAX_IN_SYSTEM_TERMINALS = 12


async def raw_commodity_autocomplete(interaction: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
    """Same pattern as commodity_name_autocomplete (bot/cogs/prices.py), scoped to
    commodities flagged both is_raw and is_refinable - the set /refinery-advisor actually
    knows how to look up."""
    try:
        commodities = await interaction.client.uex.get_commodities()
    except UexApiError:
        return []
    refinable = [c for c in commodities if c.get("is_raw") and c.get("is_refinable")]
    current_lower = current.lower()
    matches = [c for c in refinable if current_lower in (c.get("name") or "").lower()][:25]
    return [app_commands.Choice(name=(c.get("name") or "")[:100], value=c.get("name") or "") for c in matches]


def _rating_line(method: dict) -> str:
    cost = COST_LABELS.get(method.get("rating_cost"), "unknown")
    speed = SPEED_LABELS.get(method.get("rating_speed"), "unknown")
    return f"**{method.get('name', 'Unknown')}** — cost: {cost}, speed: {speed}"


class Refinery(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    @app_commands.command(
        name="refinery-advisor",
        description="Best refinery, high-yield methods, and sell price for up to 3 raw ores mined together.",
    )
    @app_commands.describe(
        ore_1="Raw/refinable commodity, e.g. 'Quantainium (Raw)'",
        ore_2="Optional: a second ore from the same haul",
        ore_3="Optional: a third ore from the same haul",
    )
    @app_commands.rename(ore_1="ore-1", ore_2="ore-2", ore_3="ore-3")
    @app_commands.autocomplete(
        ore_1=raw_commodity_autocomplete, ore_2=raw_commodity_autocomplete, ore_3=raw_commodity_autocomplete
    )
    async def refinery_advisor(
        self,
        interaction: discord.Interaction,
        ore_1: str,
        ore_2: str | None = None,
        ore_3: str | None = None,
    ) -> None:
        await interaction.response.defer()
        queries = [q for q in (ore_1, ore_2, ore_3) if q]
        try:
            commodities = await self.bot.uex.get_commodities()
        except UexApiError as exc:
            await interaction.followup.send(describe_uex_api_error(exc))
            return

        resolved: list[dict] = []
        not_found: list[str] = []
        seen_ids: set[int] = set()
        for query in queries:
            match = resolve_raw_commodity(commodities, query)
            if match is None:
                not_found.append(query)
                continue
            if match["id"] in seen_ids:
                continue
            seen_ids.add(match["id"])
            resolved.append(match)

        if not resolved:
            quoted = ", ".join(f"'{q}'" for q in queries)
            await interaction.followup.send(
                f"Couldn't find a refinable raw material matching {quoted} - "
                "pick from the autocomplete suggestions."
            )
            return

        yield_rows_by_commodity: dict[str, list[dict]] = {}
        for commodity in resolved:
            rows = await self.bot.db.get_latest_refinery_yields_for_commodity(commodity["id"])
            yield_rows_by_commodity[commodity["name"]] = rows

        # A refinery's own highest yield bonus for an ore can be in a star system where that
        # ore isn't even mineable (Quantainium's top yield is a Nyx refinery, but Quantainium
        # is Stanton-only) - a pure yield-bonus ranking can send a player planning a
        # multi-system flight for cargo they could only ever have picked up somewhere else.
        # Same ids_star_systems data /where-to-mine already reads, not a second lookup - a
        # failed fetch here just degrades to the original yield-only ordering, not a broken
        # command.
        systems_by_ore: dict[str, set[str]] = {}
        try:
            star_systems = await self.bot.uex.get_star_systems()
            star_systems_by_id = {s["id"]: s["name"] for s in star_systems}
            for commodity in resolved:
                systems_by_ore[commodity["name"]] = set(
                    names_for_ids(commodity.get("ids_star_systems"), star_systems_by_id)
                )
        except UexApiError:
            pass  # combine_mining_systems gets {} and ranking stays by yield alone
        # Several ores are judged against the systems where ALL of them are mined, not the
        # union (see combine_mining_systems) - a union let a refinery near ore B's system
        # pass unflagged for ore A.
        haul_systems = combine_mining_systems(systems_by_ore)
        mining_star_systems = set(haul_systems.systems)

        # Ranked untruncated, then trimmed by select_terminals_to_show - a flat top-N cut
        # dropped real in-system refineries (Quantainium has 6 in Stanton, Corundum 11).
        all_ranked = rank_refinery_terminals(
            yield_rows_by_commodity, limit=None, mining_star_systems=mining_star_systems or None,
        )
        ranked_terminals = select_terminals_to_show(
            all_ranked, min_shown=MAX_TERMINALS, max_in_system=MAX_IN_SYSTEM_TERMINALS,
        )

        try:
            methods = await self.bot.uex.get_refineries_methods()
        except UexApiError:
            methods = []
        methods_high_yield = high_yield_refining_methods(methods)

        title = " + ".join(c["name"] for c in resolved)
        embed = discord.Embed(title=f"{title} — Refinery Advisor", color=discord.Color.blurple())
        if not_found:
            embed.description = f"Couldn't match: {', '.join(not_found)}"

        # Footer set BEFORE any field is added, not after - add_chunked_fields' own
        # len(embed) budget check needs the real footer already counted, matching this
        # codebase's established ordering (see /price's identical fix). The cross-system
        # disclosure is computed here too, for the same reason - it's a genuinely different
        # notice than "no data for this ore" (below), so it's spelled out once rather than
        # repeated per flagged terminal.
        footer_text = (
            "Refinery yield bonus collected periodically · sell prices live from UEX · "
            "methods apply at any refinery, not tied to a specific terminal."
        )
        if any(t.in_mining_system is False for t in ranked_terminals):
            footer_text += (
                " · ⚠️ marks a terminal outside where this ore is actually mined - still "
                "usable once the ore is in your cargo hold, just ranked behind reachable options."
            )
        if haul_systems.note:
            footer_text += f" · {haul_systems.note}"
        if len(all_ranked) > len(ranked_terminals):
            footer_text += (
                f" · Showing {len(ranked_terminals)} of {len(all_ranked)} refineries with yield data."
            )
        embed.set_footer(text=footer_text)
        omitted_sections: list[str] = []

        if ranked_terminals:
            lines = []
            for terminal in ranked_terminals:
                per_commodity = ", ".join(
                    f"{name} +{bonus}%" for name, bonus in terminal.per_commodity.items()
                )
                missing = [c["name"] for c in resolved if c["name"] not in terminal.per_commodity]
                missing_note = f" (no data: {', '.join(missing)})" if missing else ""
                cross_system_note = " ⚠️" if terminal.in_mining_system is False else ""
                name = display_terminal_name(terminal.terminal_name, terminal.star_system_name)
                lines.append(f"**{name}**{cross_system_note} — {per_commodity}{missing_note}")
            if not add_chunked_fields(embed, name="Best refineries by yield bonus", lines=lines):
                omitted_sections.append("refinery list")
        else:
            embed.add_field(
                name="Best refineries by yield bonus",
                value="No refinery yield data collected yet for the selected ore(s).",
                inline=False,
            )

        if methods_high_yield:
            lines = [_rating_line(m) for m in methods_high_yield]
            if not add_chunked_fields(embed, name="High-yield refining methods", lines=lines):
                omitted_sections.append("refining methods")

        seen_parent_ids: set[int] = set()
        for commodity in resolved:
            id_parent = commodity.get("id_parent")
            if not id_parent or id_parent in seen_parent_ids:
                continue
            seen_parent_ids.add(id_parent)
            refined = next((c for c in commodities if c.get("id") == id_parent), None)
            if refined is None:
                continue
            try:
                price_rows = await self.bot.uex.get_commodities_prices(commodity_name=refined["name"])
            except UexApiError:
                price_rows = []
            top_sell = best_sell_locations(price_rows, limit=MAX_SELL_LOCATIONS)
            if top_sell:
                lines = [f"**{r['terminal_name']}** — {r['price_sell']:.2f} aUEC/unit" for r in top_sell]
            else:
                lines = ["No current sell price data."]
            # inline=True to keep this command's existing side-by-side layout for up to 3
            # refined commodities - add_chunked_fields defaults to False everywhere else.
            if not add_chunked_fields(embed, name=f"{refined['name']} — best sell price", lines=lines, inline=True):
                omitted_sections.append(f"{refined['name']} sell price")

        if omitted_sections:
            embed.set_footer(text=f"{embed.footer.text}\n{', '.join(omitted_sections)} omitted - message size limit")

        await interaction.followup.send(embed=embed)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Refinery(bot))
