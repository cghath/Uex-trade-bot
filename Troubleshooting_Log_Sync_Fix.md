# Debugging Log: Slash Command Sync Issues

## Problem Summary
The bot was successfully loading cogs and logging in, but the slash commands (specifically `/liquidity-rank`) were not appearing in the Discord client. The logs consistently reported:
`Synced 0 commands to dev guild 1132378045232197694`

## The Root Cause: Global vs. Guild Command Trees
The issue was a misunderstanding of how `discord.py` handles command registration.

### The Mechanics
1.  **Global Commands:** When using `@app_commands.command()`, the commands are added to the bot's **Global Command Tree**.
2.  **Guild Syncing:** When calling `self.tree.sync(guild=guild_id)`, the bot only synchronizes commands that are explicitly registered to that specific guild's tree.
3.  **The Gap:** Because the `LiquidityCog` used standard decorators, the commands were sitting in the **Global Tree**. The `sync(guild=...)` call ignored them because they weren't in the **Guild Tree**.

### The Solution
To fix this, we implemented a "Copy-and-Sync" pattern in `bot.main.py`:

```python
if self.config.discord_dev_guild_id:
    guild = discord.Object(id=self.config.discord_dev_guild_id)
    # Copy the global commands into the specific guild's tree
    self.tree.copy_global_to(guild=guild)
    # Now sync the guild-specific tree
    synced = await self.tree.sync(guild=guild)
```

### Key Takeaways
- **`Synced 0 commands`** is a primary indicator that the commands are not registered in the tree being synced.
- **`copy_global_to()`** is the correct method to bridge the gap between global definitions and local guild testing.
- **Library Shadowing:** We also identified that environment-specific library resolution (shadowing) was causing initial `ImportErrors`, which were resolved by ensuring a clean `discord.py` installation.

## Verification
- **Bot Status:** Bot loads and logs in successfully.
- **Command Status:** `/liquidity-rank` and `/scan-now` are now visible in the dev server.
- **Architecture:** The bot maintains a modular structure where cogs are loaded via `INITIAL_COGS` and registered via standard decorators.

