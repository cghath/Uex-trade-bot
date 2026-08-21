"""Per-user want-to-sell list: /items-to-sell, /items-to-sell-list, /items-to-sell-remove.

Each member keeps a private list of items they want to offload and the asking price (aUEC)
they'd part with each at, stored locally in SQLite (bot/db/database.py: user_sell_list) -
nothing here posts to UEX or is visible to anyone else.

Why the add command is shaped as ten (item, price) option pairs rather than a dropdown:
Discord's only search-as-you-type primitive is slash command option autocomplete - a
select-menu component can only show a fixed 25 choices, with no typing. Since the whole
point is searching the bot's traded-items index (the same one /marketplace-index-status
reports on), each item slot is a text option with that autocomplete attached, and up to
ten items can be added in one submission by filling more slots. 10 pairs = 20 options,
under Discord's 25-options-per-command cap.
"""
from __future__ import annotations

import discord
from discord import app_commands
from discord.ext import commands

from bot.cogs.marketplace import traded_item_autocomplete
from bot.discord_ui import send_alert_remove_picker
from bot.sell_list import pair_sell_list_inputs
from bot.uex.exceptions import UexApiError
from bot.uex.marketplace import find_item_id_by_name

# An asking price of at least 1 aUEC; Discord enforces the floor client-side via Range.
AskingPrice = app_commands.Range[float, 1]

MAX_LIST_LINES = 40  # embed description cap is 4096 chars; 40 of these lines fits easily


class SellList(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    @app_commands.command(
        name="items-to-sell",
        description="Add up to 10 items to your personal want-to-sell list, each with an asking price in aUEC.",
    )
    @app_commands.describe(
        item1="Item you want to sell — autocomplete searches items seen trading on the Marketplace",
        price1="Asking price for item1, in aUEC",
        item2="Optional: another item to sell",
        price2="Optional: asking price for item2, in aUEC",
        item3="Optional: another item to sell",
        price3="Optional: asking price for item3, in aUEC",
        item4="Optional: another item to sell",
        price4="Optional: asking price for item4, in aUEC",
        item5="Optional: another item to sell",
        price5="Optional: asking price for item5, in aUEC",
        item6="Optional: another item to sell",
        price6="Optional: asking price for item6, in aUEC",
        item7="Optional: another item to sell",
        price7="Optional: asking price for item7, in aUEC",
        item8="Optional: another item to sell",
        price8="Optional: asking price for item8, in aUEC",
        item9="Optional: another item to sell",
        price9="Optional: asking price for item9, in aUEC",
        item10="Optional: another item to sell",
        price10="Optional: asking price for item10, in aUEC",
    )
    @app_commands.autocomplete(
        item1=traded_item_autocomplete,
        item2=traded_item_autocomplete,
        item3=traded_item_autocomplete,
        item4=traded_item_autocomplete,
        item5=traded_item_autocomplete,
        item6=traded_item_autocomplete,
        item7=traded_item_autocomplete,
        item8=traded_item_autocomplete,
        item9=traded_item_autocomplete,
        item10=traded_item_autocomplete,
    )
    async def items_to_sell(
        self,
        interaction: discord.Interaction,
        item1: str,
        price1: AskingPrice,
        item2: str | None = None,
        price2: AskingPrice | None = None,
        item3: str | None = None,
        price3: AskingPrice | None = None,
        item4: str | None = None,
        price4: AskingPrice | None = None,
        item5: str | None = None,
        price5: AskingPrice | None = None,
        item6: str | None = None,
        price6: AskingPrice | None = None,
        item7: str | None = None,
        price7: AskingPrice | None = None,
        item8: str | None = None,
        price8: AskingPrice | None = None,
        item9: str | None = None,
        price9: AskingPrice | None = None,
        item10: str | None = None,
        price10: AskingPrice | None = None,
    ) -> None:
        slots = [
            (item1, price1), (item2, price2), (item3, price3), (item4, price4), (item5, price5),
            (item6, price6), (item7, price7), (item8, price8), (item9, price9), (item10, price10),
        ]
        entries, errors = pair_sell_list_inputs(slots)
        if errors:
            await interaction.response.send_message(
                "Nothing was saved - fix these and rerun:\n" + "\n".join(f"• {e}" for e in errors),
                ephemeral=True,
            )
            return

        await interaction.response.defer(ephemeral=True)

        # Best-effort catalog id resolution (for future joins against UEX price data) -
        # a name that doesn't resolve is still stored as typed, just without an id.
        try:
            items = await self.bot.uex.get_items()
        except UexApiError:
            items = []
        payload = [
            {
                "item_name": entry.item_name,
                "asking_price": entry.asking_price,
                "id_item": find_item_id_by_name(items, entry.item_name),
            }
            for entry in entries
        ]

        added, updated = await self.bot.db.upsert_sell_list_items(interaction.user.id, payload)
        price_by_name = {entry.item_name: entry.asking_price for entry in entries}

        # [:1024] guards Discord's per-field cap: item names are freeform text (autocomplete
        # allows typing anything), so ten maximal names could exceed it in the worst case.
        embed = discord.Embed(title="Your sell list — saved", color=discord.Color.green())
        if added:
            embed.add_field(
                name=f"Added ({len(added)})",
                value="\n".join(f"**{name}** — asking {price_by_name[name]:,.0f} aUEC" for name in added)[:1024],
                inline=False,
            )
        if updated:
            embed.add_field(
                name=f"Price updated ({len(updated)})",
                value="\n".join(f"**{name}** — now asking {price_by_name[name]:,.0f} aUEC" for name in updated)[:1024],
                inline=False,
            )
        total = len(await self.bot.db.list_user_sell_list(interaction.user.id))
        embed.set_footer(
            text=f"Your sell list has {total} item{'s' if total != 1 else ''} · "
            "/items-to-sell-list to view · /items-to-sell-remove to remove"
        )
        await interaction.followup.send(embed=embed)

    @app_commands.command(name="items-to-sell-list", description="Show your want-to-sell list and asking prices.")
    async def items_to_sell_list(self, interaction: discord.Interaction) -> None:
        rows = await self.bot.db.list_user_sell_list(interaction.user.id)
        if not rows:
            await interaction.response.send_message(
                "Your sell list is empty - add items with /items-to-sell.", ephemeral=True
            )
            return

        lines = [f"**{row['item_name']}** — asking {row['asking_price']:,.0f} aUEC" for row in rows[:MAX_LIST_LINES]]
        if len(rows) > MAX_LIST_LINES:
            lines.append(f"…and {len(rows) - MAX_LIST_LINES} more")
        embed = discord.Embed(
            title=f"{interaction.user.display_name}'s sell list",
            description="\n".join(lines)[:4096],  # freeform names could exceed the description cap
            color=discord.Color.green(),
        )
        embed.set_footer(text=f"{len(rows)} item{'s' if len(rows) != 1 else ''} · asking prices in aUEC · only you can see this")
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @app_commands.command(name="items-to-sell-remove", description="Remove items from your want-to-sell list (pick from a menu).")
    async def items_to_sell_remove(self, interaction: discord.Interaction) -> None:
        rows = await self.bot.db.list_user_sell_list(interaction.user.id)
        picker_items = [
            {
                "id": row["id"],
                "label": row["item_name"],
                "description": f"asking {row['asking_price']:,.0f} aUEC",
            }
            for row in rows
        ]

        async def _remove(picker_interaction: discord.Interaction, entry_id: int) -> str:
            removed = await self.bot.db.remove_sell_list_item(entry_id, picker_interaction.user.id)
            return "Removed from your sell list." if removed else "That item was already removed."

        await send_alert_remove_picker(
            interaction,
            alerts=picker_items,
            remove_callback=_remove,
            empty_message="Your sell list is empty - add items with /items-to-sell.",
            placeholder_noun="sell list item",
        )


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(SellList(bot))
