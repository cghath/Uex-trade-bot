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

## Context discipline

These exist because a measured session showed ~90% of its token cost was the
fixed context floor re-read on every turn, not the files it read. Turn count
multiplies everything, so the cheapest win is doing more per turn.

- **`docs/UEX_API_2.0_reference.md` is ~44,000 tokens — never read it whole.**
  That is roughly an entire session's context floor in one file, and once it
  is in, it is re-read every remaining turn. Find the endpoint first, then
  read only its section:
  `grep -n "commodities_routes" docs/UEX_API_2.0_reference.md`
  then `sed -n '<start>,<end>p'` on the hit. The same applies to the root
  `.txt` API captures (`stileron.txt`, `savrilium.txt`, `dump.txt`).
- **Scope searches — `.venv/` has 2,541 `.py` files against the project's 55.**
  Grep honors `.gitignore` and skips it; Glob and `find` do not, so an
  unscoped `**/*.py` returns overwhelmingly pip internals and buries the real
  files. Prefer Grep, or scope explicitly to `bot/`, `tests/`, `scripts/`.
- **Batch independent tool calls into one turn.** Three sequential
  single-command turns measured ~33,200 weighted tokens where one batched turn
  would cost ~19,500 — same information, ~20% of the session saved.
- **Don't re-read a file to confirm an edit landed.** Edits raise on failure;
  a confirming read pays full price for information you already have.
- **`/compact` is worth it past ~80K context and worthless before it** — it
  compresses accumulated conversation, and cannot touch the fixed floor.
