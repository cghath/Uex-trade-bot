"""Per-user UEX account linking, so each Discord member can use their own UEX secret_key.

Slash command *options* are visible to everyone in the channel ("so-and-so used /command
key:abc123..."), so we never accept a secret key as a plain command argument. Instead
/link-uex-account opens a Discord modal, which is private to the user filling it in and
is not echoed into the channel. The key is then encrypted at rest (bot/db/crypto.py)
before being stored.
"""
from __future__ import annotations

import discord
from discord import app_commands
from discord.ext import commands


class LinkUexModal(discord.ui.Modal, title="Link your UEX account"):
    secret_key_input = discord.ui.TextInput(
        label="UEX secret key",
        placeholder="Paste your UEX secret_key (from your UEX account page)",
        style=discord.TextStyle.short,
        required=True,
        max_length=200,
    )

    def __init__(self, bot: commands.Bot) -> None:
        super().__init__()
        self.bot = bot

    async def on_submit(self, interaction: discord.Interaction) -> None:
        secret_key = str(self.secret_key_input.value).strip()
        await self.bot.db.set_user_secret_key(interaction.user.id, secret_key)
        await interaction.response.send_message(
            "Your UEX account is linked. It's stored encrypted and only used for your "
            "own requests (e.g. /uex-trades). Use /unlink-uex-account any time to remove it.\n"
            "Run /intro to see what else this unlocks - negotiation alerts, daily digests, "
            "stock/marketplace alerts, and automatic inventory posting.",
            ephemeral=True,
        )


class Account(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    @app_commands.command(
        name="link-uex-account",
        description="Privately link your personal UEX secret key (opens a private form, not posted in chat).",
    )
    async def link_uex_account(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_modal(LinkUexModal(self.bot))

    @app_commands.command(name="unlink-uex-account", description="Remove your linked UEX account from this bot.")
    async def unlink_uex_account(self, interaction: discord.Interaction) -> None:
        removed = await self.bot.db.remove_user_secret_key(interaction.user.id)
        if removed:
            await interaction.response.send_message("Your UEX account has been unlinked.", ephemeral=True)
        else:
            await interaction.response.send_message("You don't have a linked UEX account.", ephemeral=True)

    @app_commands.command(name="uex-account-status", description="Check whether you've linked a UEX account.")
    async def uex_account_status(self, interaction: discord.Interaction) -> None:
        linked = await self.bot.db.has_linked_uex_account(interaction.user.id)
        msg = "Your UEX account is linked." if linked else "No UEX account linked. Use /link-uex-account."
        await interaction.response.send_message(msg, ephemeral=True)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Account(bot))
