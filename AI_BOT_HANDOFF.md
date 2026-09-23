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

The "Backfilled from 2026-09-22" block (PRs #27-33, #35, #36) was fully ported to aiv2
(`master` @ `28e5df3`) the same day and cleared per this file's own convention above - see
`git log -p -- AI_BOT_HANDOFF.md` for the checked-off detail if it's ever needed again,
including which items were genuine no-ops (#27, since aiv2 never had the Backup route
button) versus deliberately not mirrored (#30's patch-note version numbers, since aiv2's
own notes format already carries a `_Ref:` commit hash per entry). PR #39 (porting 7
aiv2-side audit fixes back to production) is deliberately NOT listed here - that PR is the
reverse direction (aiv2 -> here), so aiv2 already has those fixes; nothing to port.

- [ ] PR #40 - Add `/ingame-item-finder`: which shops sell a weapon/armor/ammo/other item,
      closest to a given location first (`bot/cogs/item_finder.py`, `bot/uex/item_finder.py`,
      `bot/uex/client.py`'s new `get_items_prices`, `bot/cogs/help.py`, `bot/main.py`)
