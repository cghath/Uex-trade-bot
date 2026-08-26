# Hi
# Hi
"""Entrypoint: python -m bot.main"""
from __future__ import annotations

import asyncio
import logging

import discord
from discord.ext import commands

from bot.config import Config
from bot.db.crypto import load_or_create_key
from bot.db.database import Database
from bot.uex.client import UexClient

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("uexbot")

INITIAL_COGS = (
    "bot.cogs.account",
    "bot.cogs.prices",
    "bot.cogs.alerts",
    "bot.cogs.trades",
    "bot.cogs.trends",
    "bot.cogs.marketplace",
    "bot.cogs.intelligence",
    "bot.cogs.intelligence_brief",
    "bot.cogs.marketplace_alerts",
    "bot.cogs.stock_alerts",
    "bot.cogs.ships",
    "bot.cogs.digest",
    "bot.cogs.diagnostics",
    "bot.cogs.help",
    "bot.cogs.scanner",
    "bot.cogs.liquidity",
    "bot.cogs.personal_inventory",
)


class UexBot(commands.Bot):
    def __init__(self, config: Config) -> None:
        intents = discord.Intents.default()
        intents.message_content = True
        super().__init__(command_prefix="!uex-unused-", intents=intents)
        self.config = config

        # Fernet key for encrypting per-user UEX secret keys at rest, stored next to the DB.
        credentials_key_path = config.database_path.parent / "credentials.key"
        fernet = load_or_create_key(credentials_key_path)

        self.db = Database(config.database_path, fernet)
        self.uex = UexClient(
            app_token=config.uex_app_token,
            # Optional: only used as a fallback if a caller doesn't pass their own key
            # (kept for the bot owner's own convenience/testing). Real multi-user access
            # goes through each member's own linked account (bot/cogs/account.py).
            default_secret_key=config.uex_secret_key,
        )

    async def setup_hook(self) -> None:
        await self.db.init()

        for extension in INITIAL_COGS:
            try:
                await self.load_extension(extension)
                logger.info("Loaded extension %s", extension)
            except Exception as e:
                logger.error("Failed to load extension %s: %s", extension, e)

        if self.config.discord_dev_guild_id:
            guild = discord.Object(id=self.config.discord_dev_guild_id)
            self.tree.copy_global_to(guild=guild)
            # Sync to the specific dev guild for immediate availability
            synced = await self.tree.sync(guild=guild)
            logger.info("Synced %d commands to dev guild %s", len(synced), guild.id)
        else:
            # Fallback to global sync if no dev guild is provided
            synced = await self.tree.sync()
            logger.info("Synced %d global commands", len(synced))

    async def close(self) -> None:
        await self.uex.aclose()
        await super().close()

    async def on_ready(self) -> None:
        logger.info("Logged in as %s (id=%s)", self.user, self.user.id if self.user else "?")

    @commands.command()
    @commands.is_owner()
    async def sync(self, ctx: commands.Context, spec: str | None = None) -> None:
        """Syncs slash commands on demand.
        Usage:
          !sync        -> Syncs commands to the CURRENT guild instantly
          !sync global -> Syncs commands globally (takes up to 1 hour)
          !sync clear  -> Clears commands from the current guild
        """
        if spec == "global":
            synced = await self.tree.sync()
            await ctx.send(f"🌍 Synced {len(synced)} commands globally.")
        elif spec == "clear":
            self.tree.clear_commands(guild=ctx.guild)
            synced = await self.tree.sync(guild=ctx.guild)
            await ctx.send("🧹 Cleared all commands from this guild.")
        else:
            guild = discord.Object(id=ctx.guild.id)
            self.tree.copy_global_to(guild=guild)
            synced = await self.tree.sync(guild=guild)
            await ctx.send(
                f"✅ Synced {len(synced)} commands instantly to **{ctx.guild.name}**."
            )

async def run() -> None:
    config = Config.from_env()
    bot = UexBot(config)
    async with bot:
        await bot.start(config.discord_bot_token)


def main() -> None:
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        logger.info("Shutting down.")


if __name__ == "__main__":
    main()
