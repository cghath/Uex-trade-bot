# CLAUDE.md

Guidance for Claude Code (or any AI coding session) working in this repo.

**Read `CONTRIBUTING.md` before adding any feature.** It exists because of a real,
expensive mistake (a feature that silently never worked, hours of debugging, a fabricated
root cause) and lays out this codebase's non-negotiable patterns plus a pre-flight
checklist. The two mistakes that mattered most, in short:

1. A new cog in `bot/cogs/` does nothing until its module path is added to `INITIAL_COGS`
   in `bot/main.py`. No error is raised if you forget — the command just silently never
   appears in Discord.
2. Slash commands use `@app_commands.command(...)` decorators directly on Cog methods,
   full stop. Never `bot.tree.command(callback=...)` or `func=...` — neither is a valid
   discord.py API and both raise `TypeError`.

## Quick facts

- Run tests: `python -m pytest -q` (from repo root)
- Run the bot: `python -m bot.main`
- Pure logic lives in `bot/uex/*.py` (dependency-free, unit tested); Discord-facing glue
  lives in `bot/cogs/*.py`
- DB schema is a single `SCHEMA` string in `bot/db/database.py` — new tables need a real
  `CREATE TABLE IF NOT EXISTS` there, not just a query that assumes the table exists
- This session's branch workflow: PRs target `TestBranch`, not `main` — see conversation
  history / the user for current specifics if picking this up fresh
