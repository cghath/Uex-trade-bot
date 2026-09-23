# Bot Handoff

A running, bidirectional queue of changes made in one bot's repo that still need porting to
the other: production `uex-trading-bot-v2` (deployed on the Pi as `uex-trade-bot.service`)
and its AI-chat fork `aiv2` (started as a carbon copy of this one, so most changes in either
are relevant to both - command names differ only by aiv2's `ai-` prefix, e.g. `/price` ->
`/ai-price`). Previously scoped production -> aiv2 only, with reverse-direction changes
deliberately left untracked; widened to cover both directions once work started happening
directly in aiv2 rather than always starting in production first.

This is a live checklist, not a point-in-time log - unlike `PROJECT_CONTEXT.md`/
`CONTRIBUTING.md` (see their own "no new standalone log files" convention under
`PROJECT_CONTEXT.md`'s "Where to look for what"), entries here get checked off as they're
actually ported, not kept as permanent history. Once every entry in a section above a point
is checked, feel free to delete the checked block rather than let it grow forever - `git log`
on this file preserves the record if it's ever needed again.

**How to use this:** after landing a change in EITHER repo that's relevant to the other, add
an entry under that direction's section below before moving on. When you port a change,
check its box. If you skip an entry because it doesn't apply on the other side (e.g. it's
Discord-bot-only glue, or the other bot never had the feature being touched), check it anyway
and say why in a parenthetical - a skipped-and-explained box is worth more than an unchecked
one you can no longer remember the reason for.

**Format:** `- [ ] <repo> <PR #N or commit hash> - one-line summary (key files touched)`

---

## To port: production -> aiv2

The "Backfilled from 2026-09-22" block (PRs #27-33, #35, #36) was fully ported to aiv2
(`master` @ `28e5df3`) the same day and cleared per this file's own convention above - see
`git log -p -- AI_BOT_HANDOFF.md` for the checked-off detail if it's ever needed again,
including which items were genuine no-ops (#27, since aiv2 never had the Backup route
button) versus deliberately not mirrored (#30's patch-note version numbers, since aiv2's own
notes format already carries a `_Ref:` commit hash per entry). PR #39 (porting 7 aiv2-side
audit fixes back to production) was the reverse direction and was already done as of that
date - no open item, nothing to record here.

- [ ] PR #40 - Add `/ingame-item-finder`: which shops sell a weapon/armor/ammo/other item,
      closest to a given location first (`bot/cogs/item_finder.py`, `bot/uex/item_finder.py`,
      `bot/uex/client.py`'s new `get_items_prices`, `bot/cogs/help.py`, `bot/main.py`)
- [ ] PR #42 - `/ingame-item-finder`: same-system fallback sort tier for when
      `/terminals_distances` can't price a same-system pair, results grouped into one embed
      field per star system (`bot/uex/item_finder.py`, `bot/cogs/item_finder.py`,
      `bot/db/database.py`'s new `get_terminal_star_system`)
- [ ] PR #43 - `/ingame-item-finder`: results render as a place+vendor table (split from the
      terminal name's "Vendor - Place" convention, e.g. "GrimHEX" instead of the formal
      station name) instead of one bullet line per shop (`bot/uex/item_finder.py`,
      `bot/cogs/item_finder.py`)

## To port: aiv2 -> production

- [ ] aiv2 commit `a6bd024` - Cross-terminal price-outlier warnings: a commodity's buy/sell
      price checked against every other terminal trading it in the same snapshot, flagged
      when 4x-or-more off the median (`bot/uex/price_outliers.py`,
      `bot/uex/route_presentation.py`, `bot/cogs/prices.py`, `bot/cogs/intelligence_brief.py`,
      `bot/cogs/ai_chat.py`). Wired into `/ai-mixed-routes`, `/ai-multi-stop-route`,
      `/ai-route-from-multi`, `/ai-best-route` (both branches), `/ai-intelligence-brief` -
      `/ai-top-routes` and siblings intentionally left uncovered (see the commit message for
      why). Production already has its own copy of this same feature, built there first, on
      the unmerged `feature/price-outlier-detection` branch - porting this entry likely
      means merging that branch rather than re-implementing from scratch, but check it's
      still current (and still unmerged) before assuming a fresh port is needed.
