"""Diagnostic commands - currently just /test-dm.

Motivated by a real, recurring problem with Discord bots in general: whether a bot's DM
actually reaches a given user depends on THEIR Discord settings (a per-server "Allow direct
messages from server members" toggle, or having blocked the bot outright), not anything the
bot does - and the failure looks identical from the bot's side either way (Discord just
returns a 403 Forbidden). This bot now has two DM-only delivery paths (Marketplace alerts, and
a Personal-scope /stock-alert-add) where a silently-failing DM means the user never finds out
their alert is dead. /test-dm lets someone check that channel actually works *before*
depending on it, rather than discovering it during a real alert.
"""
from __future__ import annotations

import discord
from discord import app_commands
from discord.ext import commands


class Diagnostics(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    @app_commands.command(
        name="test-dm",
        description="Send yourself a test DM to check whether this bot can actually reach you that way.",
    )
    async def test_dm(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        try:
            # interaction.user.send() creates/uses the same DM channel and hits the same
            # Discord permission checks as bot.fetch_user(user_id).send() - what every
            # background poller here uses to deliver a DM - so a pass/fail here is a direct
            # answer for whether those will reach this user too.
            await interaction.user.send(
                "This is a test DM from the UEX Trading Bot. If you're reading this, DMs from "
                "this bot reach you - any Personal-scope alert (/stock-alert-add ... scope: "
                "Personal) or Marketplace alert (/marketplace-alert-add) will get through too."
            )
        except discord.Forbidden:
            await interaction.followup.send(
                "Couldn't DM you - Discord blocked it (a 403 Forbidden). This almost always "
                "means one of two things: your Discord privacy settings have \"Allow direct "
                "messages from server members\" turned off for this server (right-click the "
                "server icon → Privacy Settings, or Server Settings → Privacy Settings depending "
                "on your client), or you've blocked this bot specifically. Fix whichever applies "
                "and run /test-dm again - anything DM-only (Personal-scope stock alerts, "
                "Marketplace alerts) needs this to actually work for you.",
                ephemeral=True,
            )
            return
        except discord.HTTPException as exc:
            await interaction.followup.send(
                f"Couldn't DM you - unexpected Discord error, not the usual privacy-settings block: {exc}",
                ephemeral=True,
            )
            return

        await interaction.followup.send(
            "Sent! If it showed up in your DMs, this bot can reach you there - Personal-scope "
            "stock alerts and Marketplace alerts will work fine.",
            ephemeral=True,
        )


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Diagnostics(bot))
