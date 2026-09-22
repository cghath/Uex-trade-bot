# AI Bot Handoff

A running queue of changes merged to `TestBranch` here that still need porting over to the
separate AI-bot project (it started as a carbon copy of this one, so most changes here are
relevant there too - command names differ by the `ai-` prefix, e.g. `/price` -> `/ai-price`).

This is a live checklist, not a point-in-time log - unlike `PROJECT_CONTEXT.md`/
`CONTRIBUTING.md` (see their own "no new standalone log files" convention under
`PROJECT_CONTEXT.md`'s "Where to look for what"), entries here get checked off as they're
actually ported, not kept as permanent history. Once every entry above a point is checked,
feel free to delete the checked block rather than let it grow forever - `git log` on this
file preserves the record if it's ever needed again.

**How to use this:** after merging a PR here, add an entry (see format below) before moving
on. When you port a change to the AI bot, check its box. If you skip an entry because it
doesn't apply there (e.g. it's Discord-bot-only glue, or the AI bot never had the feature
being touched), check it anyway and say why in a parenthetical - a skipped-and-explained box
is worth more than an unchecked one you can no longer remember the reason for.

**Format:** `- [ ] PR #N - one-line summary (key files touched)`

---

## Backfilled from 2026-09-22 (today's session, in case anything before the route work was missed)

- [ ] PR #27 - Removed the Backup route button feature entirely (never reached the Pi, so
      likely a no-op if it was never ported to the AI bot in the first place - check first).
      `bot/discord_ui.py`, `bot/cogs/prices.py`, `bot/cogs/trends.py`,
      `bot/uex/route_presentation.py`, `bot/uex/mixed_routes.py`
- [ ] PR #28 - New: sell-side shortfall reroute suggestion in route-tracking threads (mirrors
      the existing buy-side hedge, reuses `bot/uex/backup_routes.py`'s search) + the buy-side
      hedge now says so explicitly when nothing is found instead of staying silent.
      `bot/cogs/route_progression.py`
- [ ] PR #29 - Fix: the new sell-side reroute search now runs via `asyncio.to_thread` instead
      of blocking the event loop (same bug class as the existing `/mixed-routes`/
      `/multi-stop-route` offload - worth checking the AI bot doesn't have this same gap on
      its own callers of `find_backup_routes`/`build_mixed_routes`/`build_multi_stop_routes`
      if it has any this project doesn't). `bot/cogs/route_progression.py`
- [ ] PR #30 - Docs only: added version numbers to `PATCH_NOTES.md` entries. Not portable to
      code, but worth mirroring in the AI bot's own patch notes if it keeps a parallel file.
- [ ] PR #31 - New: owner-only `/command-usage` (`/ai-command-usage`?) to inform trimming the
      command surface - tracks real per-command, per-user usage, excluding your own testing.
      New table `command_usage_by_user`. `bot/db/database.py`, `bot/main.py`,
      `bot/cogs/diagnostics.py`, `bot/cogs/help.py`
- [ ] PR #32 - Removed `/my-ship`, folded its live SCU-capacity/staleness info into
      `/my-trading-preferences`. `bot/uex/trading_preferences.py`,
      `bot/cogs/trading_preferences.py`, `bot/cogs/ships.py`, `bot/cogs/help.py`
- [ ] PR #33 - Docs only: added this file. Nothing to port, check off once read.
- [ ] PR #35 - `/command-usage` now also tracks and shows real users by display name (not
      just counts), with a new optional `command` option to drill into who ran a specific
      one - for reaching out to real users for feedback. `command_usage_by_user` gained a
      `username` column (additive `ALTER TABLE`, not just `SCHEMA` - the AI bot's own copy
      of this table, if PR #31 was already ported, needs the same migration, not a fresh
      `CREATE TABLE`). `bot/db/database.py`, `bot/main.py`, `bot/cogs/diagnostics.py`
- [ ] PR #36 - Fix: `/command-usage`'s per-user mentions weren't actually clickable - a
      ```code block``` around the whole response silently blocks Discord's `<@id>` mention
      parsing. Only relevant if PR #35 (or an equivalent) was already ported.
      `bot/cogs/diagnostics.py`
