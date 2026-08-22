# Contributing to this bot

This doc exists because of a real, expensive mistake: an earlier AI coding session spent
hours debugging why `/scan-now` "wasn't syncing," invented a fake root cause ("library
resolution conflict," a "poisoned environment"), reset the branch, and shipped a feature
that still didn't work. The actual bug was one missing line and one line of invalid API
usage — both of which the checklist below would have caught in under a minute. Read this
before adding a feature, and run the checklist before calling one done.

## The #1 rule: a new cog isn't live until it's registered

Adding a file to `bot/cogs/` does **nothing** by itself. `bot/main.py` only loads cogs
listed in its `INITIAL_COGS` tuple:

```python
INITIAL_COGS = (
    "bot.cogs.account",
    "bot.cogs.prices",
    ...
    "bot.cogs.scanner",   # <- your new cog must be added here
)
```

If a cog isn't in this tuple, `setup_hook()` never calls `load_extension()` on it, its
`setup()` function never runs, its commands never get added to `bot.tree`, and
`tree.sync()` has nothing to push for it. **No error is raised.** The bot starts up fine,
logs in fine, and the command just silently never appears in Discord. This exact silent
failure is what actually broke `/scan-now` — not a library bug.

## Slash commands: there is exactly one correct pattern in this codebase

Every cog uses standard decorators directly on the Cog's methods, and `setup()` does
nothing but `await bot.add_cog(...)`:

```python
class MyCog(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    @app_commands.command(name="my-command", description="...")
    async def my_command(self, interaction: discord.Interaction) -> None:
        ...

async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(MyCog(bot))
```

That's it. `bot.add_cog()` automatically finds and registers every `@app_commands.command`
decorated method on the class.

**Never do any of the following** — all three were tried in the broken version of the
scanner and all three are wrong:
- `bot.tree.command(name=..., callback=some_method)` — `CommandTree.command()` is a
  decorator factory (`@bot.tree.command(...)` above a function def). It does not accept a
  `callback=` keyword. Passing one raises `TypeError`.
- `bot.tree.command(..., func=some_method)` — same problem, `func` isn't a valid keyword
  either.
- Calling `bot.add_cog(...)` manually in `setup_hook()` *and* also relying on
  `load_extension()` to load the same cog — this double-registers it and raises a
  `CommandAlreadyRegistered` error. Pick one path: put the cog in `INITIAL_COGS` and let
  `load_extension()` handle it. Don't also add it by hand.

If you ever think you need to register a command outside the decorator (you almost never
do), the real API is `bot.tree.add_command(app_commands.Command(name=..., description=...,
callback=...))` — but check whether an existing cog already solves your problem with plain
decorators first, because one always does.

## New DB tables need an actual `CREATE TABLE`

Referencing a table in a query doesn't create it. Every table your feature reads or writes
must have a `CREATE TABLE IF NOT EXISTS` statement added to the `SCHEMA` string in
`bot/db/database.py`. If you skip this, the code will crash the first time it actually
runs with `sqlite3.OperationalError: no such table: whatever` — and only then, not at
import time, so it's easy to miss if you never actually run the feature.

## Copy the pattern of the most similar existing feature

Before writing a new cog, find the existing cog closest in shape to what you're building
and copy its structure, not just its vibe:

- **Background poll + slash commands + per-user notification**: copy
  `bot/cogs/marketplace_alerts.py` or `bot/cogs/stock_alerts.py`.
- **Pure matching/calculation logic** (no Discord, no I/O): put it in `bot/uex/<name>.py`,
  parallel to `bot/uex/stock_alerts.py` or `bot/uex/trends.py`. Keep it dependency-free so
  it's unit-testable with plain dicts.
- **Discord-facing glue** (commands, embeds, the poll loop itself): put it in
  `bot/cogs/<name>.py`, which imports from the pure module above.

This isn't a style preference — matching the existing pattern exactly is what makes a new
feature immediately reviewable and keeps `INITIAL_COGS`/schema/config wiring from being
forgotten, because you're copying a file that already got all of that right.

## Before declaring a bug "diagnosed" or a feature "done": verify, don't theorize

The single biggest time-sink in this repo's history was an AI session that wrote a
confident, detailed "Deep Dive Analysis" blaming a nonexistent environment/library
conflict, when the real cause (a cog missing from `INITIAL_COGS`) was checkable in one
`grep`. A wrong-but-confident explanation is worse than no explanation, because it sends
the next round of work in the wrong direction entirely.

Before writing up a root cause or claiming something works:
- **Reproduce the exact error message**, don't paraphrase it from memory or guess at what
  "probably" caused it.
- **grep for the thing you think is missing** before concluding it's missing — e.g.
  `grep -n "your_cog_name" bot/main.py` takes two seconds and either confirms or kills the
  theory immediately.
- **Actually start the bot** (`python -m bot.main`) and read the startup log. You are
  looking for two specific lines: `Loaded extension bot.cogs.<yours>` and `Synced N
  commands...` with a plausible N. If either is missing or looks wrong, the feature isn't
  wired up yet, no matter how correct the command code itself looks.
- **Actually run the command in Discord** before calling the feature done. Passing tests
  and clean imports are necessary, not sufficient — this bot's tests only cover pure logic,
  not Discord registration.
- If you're unsure what a UEX API field actually means (quality, quality_tier, pricing
  units, anything with ambiguous semantics), check the live docs at
  https://uexcorp.space/api/documentation/ before assuming — a wrong assumption here
  produced several rounds of a real bug (the Undervalued Scanner's early "steal" detection
  was wrong for days because of an unverified assumption about how `quality_tier` buckets
  map to raw quality values, until someone actually pulled the field-level docs).

## Pre-flight checklist for any new feature

Run through this before considering a feature finished:

- [ ] New cog's module path added to `INITIAL_COGS` in `bot/main.py`
- [ ] New cog's `setup()` does `await bot.add_cog(...)` exactly once, nothing else
- [ ] Every slash command uses `@app_commands.command(...)` directly on a Cog method — no
      manual `bot.tree.command(callback=...)` or `func=...` anywhere
- [ ] Any new DB table has a real `CREATE TABLE IF NOT EXISTS` in `bot/db/database.py`'s
      `SCHEMA` string
- [ ] Any new config value is read in `bot/config.py` and documented in `.env.example`
- [ ] `python -m pytest -q` passes
- [ ] Started the bot locally and saw `Loaded extension bot.cogs.<yours>` and a plausible
      `Synced N commands` line in the log
- [ ] Actually ran the new command(s) in a real Discord server, not just imported the code
