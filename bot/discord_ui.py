"""Shared Discord UI components used across more than one cog.

- BotView / BotModal: the base class for every view and modal in this bot. When a button,
  menu or form handler raises, they tell the player it failed instead of leaving them on
  "thinking..." or a click that seems to do nothing, and a view greys out its controls when
  it times out. BotLayoutView is BotView for a reply built from Discord's layout components
  (a container of text, dividers and sections; discord.py's LayoutView). `on_app_command_error` does the same for
  slash commands; bot/main.py registers it on the command tree.
- AlertRemovePickerView: a paginated dropdown for removing an alert by picking it from a
  menu instead of having to already know (and type) its numeric id. Used by every alert
  family in this bot (bot/cogs/alerts.py, bot/cogs/marketplace_alerts.py,
  bot/cogs/stock_alerts.py) so the interaction is consistent no matter which kind of alert
  you're removing.
"""
from __future__ import annotations

import logging
import math
from typing import Any, Awaitable, Callable

import discord
from discord import app_commands

logger = logging.getLogger("uexbot.discord_ui")

# Discord technically allows up to 25 options in one select menu, but a page this size gets
# hard to scan at a glance - paging at 10 keeps each menu screen-sized, per how this was
# specced (arrows to navigate once you're past ten).
PAGE_SIZE = 10

RemoveCallback = Callable[[discord.Interaction, Any], Awaitable[str]]

UNEXPECTED_ERROR_MESSAGE = (
    "Something went wrong on my end with that. Please try again in a moment - if it keeps "
    "happening, let the bot owner know."
)
# Discord's own names for the permissions a command checks, as players see them in settings.
_PERMISSION_NAMES = {"manage_guild": "Manage Server"}

# How long an interaction's token can edit its messages. Documented by Discord.
INTERACTION_TOKEN_SECONDS = 15 * 60
# A click answered one of these ways has the view's own message as its reply, so its token
# edits that message. Any other answer (a new reply, "thinking...") is a different message.
_UPDATES_ITS_MESSAGE = (
    discord.InteractionResponseType.message_update, discord.InteractionResponseType.deferred_message_update,
)
MAX_REMEMBERED_CLICKS = 5
# Discord's limit on the text in one layout-components message (BotLayoutView); check
# content_length() against it.
LAYOUT_TEXT_LIMIT = 4000


async def tell_player_it_failed(interaction: discord.Interaction, message: str = UNEXPECTED_ERROR_MESSAGE) -> None:
    """Answer an interaction whose handler raised (audit REL-13). Without this, a deferred
    command stays on "thinking..." forever and an unanswered click shows Discord's own
    "interaction failed", with nothing saying what to do next.

    A deferred interaction gets a followup (the first one replaces "thinking..."); an
    unanswered one gets a direct reply. Never raises: if Discord refuses this too (the
    interaction expired), that's only logged."""
    try:
        if interaction.response.is_done():
            await interaction.followup.send(message, ephemeral=True)
        else:
            await interaction.response.send_message(message, ephemeral=True)
    except discord.HTTPException as exc:
        logger.warning("Couldn't tell the player an interaction failed: %s", exc)


async def on_app_command_error(interaction: discord.Interaction, error: app_commands.AppCommandError) -> None:
    """The command tree's error handler, so every slash command answers when it fails.

    Always answers, so a cog shouldn't add its own `cog_app_command_error` that replies too:
    the player would get two messages. A failure a command expects (UEX down, a bad name)
    should still be caught in the command with a specific message; this is the backstop."""
    command = interaction.command
    name = f"/{command.qualified_name}" if command is not None else "that command"
    if isinstance(error, app_commands.MissingPermissions):
        needed = ", ".join(_PERMISSION_NAMES.get(p, p.replace("_", " ").title()) for p in error.missing_permissions)
        message = f"You need the {needed} permission to use `{name}`."
    elif isinstance(error, app_commands.CheckFailure):
        message = f"You can't use `{name}` here."
    else:
        logger.error("Unhandled error in %s", name, exc_info=error)
        message = UNEXPECTED_ERROR_MESSAGE
    await tell_player_it_failed(interaction, message)


class _BotViewBehaviour:
    """What every view in this bot does, whether it's a classic view (BotView) or a layout
    (BotLayoutView):

    - A failing button or menu tells the player, instead of discord.py's default of only
      logging it.
    - When it times out, its controls grey out on the message (audit UX-7). Before, they
      looked usable after they'd stopped working, and a click just showed "This interaction
      failed".

    To edit its message at timeout the view needs whoever sends it to set one of:
    - `origin`: the interaction whose own reply is this view's message, i.e. it was sent with
      `interaction.response.send_message` or `edit_message`;
    - `message`: the sent message, e.g. `await interaction.followup.send(..., wait=True)`.

    A click that updated the message is tried first, since its token is the freshest.
    Interaction tokens last INTERACTION_TOKEN_SECONDS, and they're the only way to edit an
    ephemeral message, so an ephemeral view should time out well inside that. A public
    message also falls back to a plain channel edit, which doesn't expire."""

    origin: discord.Interaction | None = None
    message: discord.Message | None = None

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._clicks: list[discord.Interaction] = []

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        """Remembers the click so a timeout can edit the message through it. A subclass that
        overrides this must call super() first."""
        self._clicks = [*self._clicks[-(MAX_REMEMBERED_CLICKS - 1):], interaction]
        return True

    async def on_timeout(self) -> None:
        await self.grey_out()

    async def grey_out(self, *, keep: tuple[type, ...] = (), **edit_kwargs: Any) -> bool:
        """Disable every control - but any of a type in `keep`, one that works without the view
        (a DynamicItem's stub) - and show that on the message, along with any other message
        fields passed (e.g. a note in `content`). True if the message was edited. Every control
        counts, including one nested in a layout's container or section."""
        for child in self.walk_children():
            if hasattr(child, "disabled") and not isinstance(child, keep):
                child.disabled = True
        for edit in self._message_editors():
            try:
                await edit(view=self, **edit_kwargs)
                return True
            except discord.HTTPException:
                continue
        logger.info("Couldn't grey out the controls of an expired %s", type(self).__name__)
        return False

    def _message_editors(self) -> list[Callable[..., Awaitable[Any]]]:
        editors = [click.edit_original_response for click in reversed(self._clicks)
                   if click.response.type in _UPDATES_ITS_MESSAGE]
        if self.origin is not None:
            editors.append(self.origin.edit_original_response)
        if self.message is not None:
            editors.append(self.message.edit)
            if not self.message.flags.ephemeral:
                editors.append(self.message.channel.get_partial_message(self.message.id).edit)
        return editors

    async def on_error(self, interaction: discord.Interaction, error: Exception, item: discord.ui.Item[Any], /) -> None:
        logger.error("Unhandled error in %s for %r", type(self).__name__, item, exc_info=error)
        await tell_player_it_failed(interaction)


class BotView(_BotViewBehaviour, discord.ui.View):
    """Base class for every classic view (buttons and menus under a message) in this bot."""


class BotLayoutView(_BotViewBehaviour, discord.ui.LayoutView):
    """Base class for every reply built from layout components: the whole message is the view,
    so it can't also carry `content` or an embed. Discord allows 40 components and 4,000
    characters of text in one (discord.py raises ValueError past 40; check content_length()
    against LAYOUT_TEXT_LIMIT)."""


class BotModal(discord.ui.Modal):
    """Base class for every modal in this bot: a failing form submit tells the player instead
    of discord.py's default of only logging it."""

    async def on_error(self, interaction: discord.Interaction, error: Exception, /) -> None:
        logger.error("Unhandled error in %s", type(self).__name__, exc_info=error)
        await tell_player_it_failed(interaction)


class AlertRemovePickerView(BotView):
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
        await super().interaction_check(interaction)
        if interaction.user.id != self.author_id:
            await interaction.response.send_message(
                "Only the person who ran this command can use this menu.", ephemeral=True
            )
            return False
        return True

    async def _on_prev(self, interaction: discord.Interaction) -> None:
        self.page = max(0, self.page - 1)
        self._render()
        await interaction.response.edit_message(view=self)

    async def _on_next(self, interaction: discord.Interaction) -> None:
        self.page = min(self.total_pages - 1, self.page + 1)
        self._render()
        await interaction.response.edit_message(view=self)

    async def _on_select(self, interaction: discord.Interaction) -> None:
        # Acknowledged before remove_callback's DB write, which can wait on a lock past
        # Discord's 3-second window; the player would see "did not respond" and pick again.
        # REL-8's sweep (entry 100) missed this callback.
        await interaction.response.defer()
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
        await interaction.edit_original_response(content=confirmation, view=self)


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
    view.origin = interaction
