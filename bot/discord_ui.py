"""Shared Discord UI components used across more than one cog.

AlertRemovePickerView: a paginated dropdown for removing an alert by picking it from a menu
instead of having to already know (and type) its numeric id. Used by every alert family in this
bot (bot/cogs/alerts.py, bot/cogs/marketplace_alerts.py, bot/cogs/stock_alerts.py) so the
interaction is consistent no matter which kind of alert you're removing.

BackupRouteButton / add_backup_button: the "Backup route" button on a stock-limited route
message (bot/cogs/prices.py's /best-route, bot/cogs/trends.py's ranked lists).
"""
from __future__ import annotations

import asyncio
import logging
import math
from typing import Any, Awaitable, Callable

import discord

from bot.uex.backup_routes import BackupContext, build_backup_message, run_backup_search
from bot.uex.route_presentation import add_chunked_fields

logger = logging.getLogger("uexbot.discord_ui")

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



# Matches RouteTrackingView's timeout, so a route message's buttons all expire together.
BACKUP_VIEW_TIMEOUT_SECONDS = 900


class BackupRouteButton(discord.ui.Button):
    """A stock-limited route plans to use ALL the stock on record, so it is fragile and leaves
    spare hold space. Pressing this works out a plan B on demand (bot.uex.backup_routes) that
    KEEPS the commodity the player may already have bought, and answers privately.

    Only the player who ran the command can press it: the answer depends on their ship, budget
    and filters, which live in this button's context, not the presser's. Like every non-
    persistent view here it stops working after its timeout or a bot restart."""

    def __init__(self, *, owner_id: int, db: Any, context: BackupContext, row: int = 1) -> None:
        super().__init__(label="Backup route", style=discord.ButtonStyle.secondary, row=row)
        self.owner_id = owner_id
        self.db = db
        self.context = context
        self._working = False

    async def callback(self, interaction: discord.Interaction) -> None:
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message(
                "This button belongs to whoever ran the command - run it yourself and the backup route "
                "will use your own ship and budget.",
                ephemeral=True,
            )
            return
        if self._working:
            await interaction.response.send_message("Still working on it - one moment.", ephemeral=True)
            return
        self._working = True
        try:
            await interaction.response.defer(ephemeral=True, thinking=True)
            try:
                # Fresh market rows at press time, and the search off the event loop like every other
                # cargo allocation in this bot (bot/cogs/prices.py).
                rows = await self.db.get_mixed_route_market_rows()
                result = await asyncio.to_thread(run_backup_search, rows, self.context)
                message = build_backup_message(result, self.context)
                embed = discord.Embed(
                    title=message.title, description=message.description, color=discord.Color.blurple(),
                )
                embed.set_footer(text=message.footer)
                omitted = sum(
                    not add_chunked_fields(embed, name=name, lines=list(lines)) for name, lines in message.sections
                )
                if omitted:
                    embed.description += f"\n({omitted} option(s) omitted - too large to display.)"
                await interaction.followup.send(embed=embed, ephemeral=True)
            except Exception:
                logger.warning("Backup route failed for %s", self.context.anchor_name, exc_info=True)
                try:
                    await interaction.followup.send(
                        "I couldn't work out a backup route just now - try again in a moment. "
                        "Your original route is unchanged.",
                        ephemeral=True,
                    )
                except discord.HTTPException:
                    logger.info("Could not deliver the backup-route failure notice either", exc_info=True)
        finally:
            self._working = False


def add_backup_button(
    view: discord.ui.View | None, *, owner_id: int, db: Any, context: BackupContext
) -> discord.ui.View:
    """Add a Backup route button to a route message's view, creating a bare view when the
    message has none (route tracking may not be loaded, or the terminal ids may be unknown -
    the backup button doesn't depend on either). Row 1, below the Track button on row 0."""
    if view is None:
        view = discord.ui.View(timeout=BACKUP_VIEW_TIMEOUT_SECONDS)
    view.add_item(BackupRouteButton(owner_id=owner_id, db=db, context=context))
    return view

