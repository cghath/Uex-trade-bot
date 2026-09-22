"""Shared Discord UI components used across more than one cog.

Currently just AlertRemovePickerView: a paginated dropdown for removing an alert by picking
it from a menu instead of having to already know (and type) its numeric id. Used by every
alert family in this bot (bot/cogs/alerts.py, bot/cogs/marketplace_alerts.py,
bot/cogs/stock_alerts.py) so the interaction is consistent no matter which kind of alert
you're removing.
"""
from __future__ import annotations

import math
from typing import Any, Awaitable, Callable

import discord

# Discord technically allows up to 25 options in one select menu, but a page this size gets
# hard to scan at a glance - paging at 10 keeps each menu screen-sized, per how this was
# specced (arrows to navigate once you're past ten).
PAGE_SIZE = 10

RemoveCallback = Callable[[discord.Interaction, Any], Awaitable[str]]


class AlertRemovePickerView(discord.ui.View):
    """Renders a page of alerts as a dropdown (plus Prev/Next buttons once there's more than
    one page) and calls back into the cog to actually delete whichever one is selected.

    `alerts` is a list of dicts, each needing at least an "id" (passed back to
    `remove_callback` unchanged - int, str, whatever the caller's DB key type is) and a
    "label" (what's shown in the menu; truncated to Discord's 100-char option-label limit).
    An optional "description" renders as the option's grey subtext line - handy for a detail
    that doesn't fit in the label itself (target price, quality range, etc).

    `remove_callback(interaction, alert_id)` should perform the actual removal (DB delete)
    and return the confirmation text to show - the view doesn't know or care what "an alert"
    means to the caller, it just orchestrates the pick-one-and-remove-it interaction.
    """

    def __init__(
        self,
        *,
        alerts: list[dict[str, Any]],
        author_id: int,
        remove_callback: RemoveCallback,
        placeholder_noun: str = "alert",
    ) -> None:
        super().__init__(timeout=180)
        self.alerts = alerts
        self.author_id = author_id
        self.remove_callback = remove_callback
        self.placeholder_noun = placeholder_noun
        self.page = 0
        self._render()

    @property
    def total_pages(self) -> int:
        return max(1, math.ceil(len(self.alerts) / PAGE_SIZE))

    def _page_slice(self) -> list[dict[str, Any]]:
        start = self.page * PAGE_SIZE
        return self.alerts[start : start + PAGE_SIZE]

    def _render(self) -> None:
        self.clear_items()
        if not self.alerts:
            return

        page_alerts = self._page_slice()
        select: discord.ui.Select = discord.ui.Select(
            placeholder=f"Select a {self.placeholder_noun} to remove (page {self.page + 1}/{self.total_pages})",
            options=[
                discord.SelectOption(
                    label=str(a["label"])[:100],
                    value=str(a["id"]),
                    description=(str(a["description"])[:100] if a.get("description") else None),
                )
                for a in page_alerts
            ],
        )
        select.callback = self._on_select  # type: ignore[method-assign]
        self.add_item(select)

        if self.total_pages > 1:
            prev_button: discord.ui.Button = discord.ui.Button(
                label="◀ Prev", style=discord.ButtonStyle.grey, disabled=self.page == 0
            )
            next_button: discord.ui.Button = discord.ui.Button(
                label="Next ▶", style=discord.ButtonStyle.grey, disabled=self.page >= self.total_pages - 1
            )
            prev_button.callback = self._on_prev  # type: ignore[method-assign]
            next_button.callback = self._on_next  # type: ignore[method-assign]
            self.add_item(prev_button)
            self.add_item(next_button)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.author_id:
            await interaction.response.send_message(
                "Only the person who ran this command can use this menu.", ephemeral=True
            )
            return False
        return True

    async def on_timeout(self) -> None:
        for item in self.children:
            item.disabled = True  # type: ignore[attr-defined]

    async def _on_prev(self, interaction: discord.Interaction) -> None:
        self.page = max(0, self.page - 1)
        self._render()
        await interaction.response.edit_message(view=self)

    async def _on_next(self, interaction: discord.Interaction) -> None:
        self.page = min(self.total_pages - 1, self.page + 1)
        self._render()
        await interaction.response.edit_message(view=self)

    async def _on_select(self, interaction: discord.Interaction) -> None:
        raw_value = interaction.data["values"][0]  # type: ignore[index]
        selected = next((a for a in self.alerts if str(a["id"]) == raw_value), None)
        alert_id = selected["id"] if selected is not None else raw_value

        confirmation = await self.remove_callback(interaction, alert_id)

        self.alerts = [a for a in self.alerts if str(a["id"]) != raw_value]
        if self.page >= self.total_pages:
            self.page = max(0, self.total_pages - 1)
        self._render()

        if not self.alerts:
            self.stop()
        await interaction.response.edit_message(content=confirmation, view=self)


async def send_alert_remove_picker(
    interaction: discord.Interaction,
    *,
    alerts: list[dict[str, Any]],
    remove_callback: RemoveCallback,
    empty_message: str,
    placeholder_noun: str = "alert",
) -> None:
    """Shared entrypoint for a cog's `*-remove` command body: shows nothing but a "you have
    none" message if the list is empty, otherwise sends the paginated picker."""
    if not alerts:
        await interaction.response.send_message(empty_message, ephemeral=True)
        return
    view = AlertRemovePickerView(
        alerts=alerts, author_id=interaction.user.id, remove_callback=remove_callback, placeholder_noun=placeholder_noun
    )
    await interaction.response.send_message("Pick one to remove:", view=view, ephemeral=True)
