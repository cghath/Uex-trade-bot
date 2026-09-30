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

from bot.discord_ui import BotModal
from bot.uex.exceptions import UexApiError, UexAuthError

# Said wherever a player links or checks their account (audit UX-11): linking is what puts
# them on /leaderboard, which anyone in the server can run.
LEADERBOARD_NOTE = (
    "Linking also puts you on this server's /leaderboard, which shows your verified UEX sell revenue "
    "to anyone who runs it. /unlink-uex-account takes you off."
)


class LinkUexModal(BotModal, title="Link your UEX account"):
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
        # Deferred before any DB write: a write can wait on a lock past Discord's
        # 3-second window, and a player who sees "did not respond" retries into a
        # duplicate (audit REL-8).
        await interaction.response.defer(ephemeral=True, thinking=True)
        secret_key = str(self.secret_key_input.value).strip()
        # Checked with UEX before it's saved (audit UX-11): a wrong key used to link
        # "successfully" and only fail later, in whichever command first used it.
        try:
            profile = await self.bot.uex.get_user_profile(secret_key)
        except UexAuthError:
            await interaction.followup.send(
                "UEX didn't accept that key, so nothing was linked. Copy your secret key again from your "
                "UEX account page and run /link-uex-account.",
                ephemeral=True,
            )
            return
        except UexApiError:
            # UEX being down isn't the key's fault: link it, and say it wasn't checked.
            profile, checked = None, False
        else:
            checked = True
        await self.bot.db.set_user_secret_key(interaction.user.id, secret_key)
        name = (profile or {}).get("username") or (profile or {}).get("name")
        if not checked:
            linked = ("Your UEX account is linked, but UEX couldn't be reached to check the key. If a command "
                      "later says UEX rejected it, run /link-uex-account again.")
        elif name:
            linked = f"Your UEX account is linked as **{discord.utils.escape_markdown(str(name))}**."
        else:
            linked = "Your UEX account is linked."
        await interaction.followup.send(
            f"{linked} It's stored encrypted and only used for your "
            "own requests (e.g. /uex-trades). Use /unlink-uex-account any time to remove it.\n"
            f"{LEADERBOARD_NOTE}\n"
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
        # Deferred before any DB write: a write can wait on a lock past Discord's
        # 3-second window, and a player who sees "did not respond" retries into a
        # duplicate (audit REL-8).
        await interaction.response.defer(ephemeral=True)
        removed = await self.bot.db.remove_user_secret_key(interaction.user.id)
        if removed:
            await interaction.followup.send("Your UEX account has been unlinked.", ephemeral=True)
        else:
            await interaction.followup.send("You don't have a linked UEX account.", ephemeral=True)

    @app_commands.command(name="uex-account-status", description="Check whether you've linked a UEX account.")
    async def uex_account_status(self, interaction: discord.Interaction) -> None:
        linked = await self.bot.db.has_linked_uex_account(interaction.user.id)
        msg = f"Your UEX account is linked. {LEADERBOARD_NOTE}" if linked else "No UEX account linked. Use /link-uex-account."
        await interaction.response.send_message(msg, ephemeral=True)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Account(bot))
