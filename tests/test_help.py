"""/intro lists only the commands every member can use (2026-10-03 audit UX-10): it used to
list the admin-only /set-digest-channel and /digest-disable to everyone, though Discord hides
them from members. Its reply is public, so an admin running it doesn't get them either."""
import asyncio
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock

import discord

from bot.cogs.digest import Digest
from bot.cogs.help import Help
from bot.cogs.scanner import Scanner


def _intro(permissions: discord.Permissions) -> str:
    async def run():
        commands = [Digest.digest_now, Digest.set_digest_channel, Digest.digest_disable,
                    Scanner.scan_now, Scanner.set_scanner_channel]
        cog = Help(NS(tree=NS(get_commands=lambda: commands)))
        interaction = NS(permissions=permissions, response=NS(defer=AsyncMock()), followup=NS(send=AsyncMock()))
        await Help.intro.callback(cog, interaction)
        embed = interaction.followup.send.await_args.kwargs["embed"]
        return "\n".join(field.value for field in embed.fields)

    return asyncio.run(run())


def test_a_member_isnt_shown_admin_commands():
    text = _intro(discord.Permissions(send_messages=True))
    assert "`/digest-now`" in text and "`/scan-now`" in text
    for admin_only in ("/set-digest-channel", "/digest-disable", "/set-scanner-channel"):
        assert admin_only not in text


def test_an_admin_running_it_doesnt_post_them_for_everyone_to_read():
    text = _intro(discord.Permissions(manage_guild=True, manage_channels=True))
    assert "`/digest-now`" in text and "`/scan-now`" in text
    for admin_only in ("/set-digest-channel", "/digest-disable", "/set-scanner-channel"):
        assert admin_only not in text
