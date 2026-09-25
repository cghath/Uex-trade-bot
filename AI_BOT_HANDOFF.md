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
- [ ] PR #43/#45/#46 - `/ingame-item-finder`: shows place split from the terminal name's
      "Vendor - Place" convention (e.g. "GrimHEX" instead of the formal station name) plus
      vendor, one plain-text line per shop - `"**Place** (Vendor) — Price aUEC · Distance"`.
      Port the NET result of these three, not #43 alone: #43 shipped a fixed-width monospace
      table first, #45 widened its columns to fix a real truncation-collision bug, #46 then
      replaced the table entirely with plain text after the wider columns turned out to make
      Discord wrap the rows and break alignment anyway (see PROJECT_CONTEXT.md entry 79 for
      the full story). `bot/uex/item_finder.py`'s `format_item_listing_line` is the only
      formatter that matters now - `build_item_listing_table`/`format_item_listing_header`/
      `format_item_listing_row` and the PR #45 column-width constants were deleted, don't
      port those (`bot/uex/item_finder.py`, `bot/cogs/item_finder.py`)
- [ ] PR #50 - `/ingame-item-finder`'s `item` autocomplete now scopes to items UEX reports at
      least one real shop price for (`/items_prices_all`), not the full catalog - confirmed
      live that ~5,000 of 7,769 catalogued items (cosmetics, ship paint, and similar) have no
      shop listing at all and were suggesting dead ends ("No shop currently lists X for
      sale") every time. New `UexClient.get_items_prices_all()` +
      `sold_item_name_autocomplete` (`bot/uex/client.py`, `bot/cogs/item_finder.py`) -
      deliberately does NOT fall back to the full catalog the way Marketplace's
      `traded_item_autocomplete` does, since an item missing here means it's genuinely not
      sold anywhere, not just a gap in the bot's own tracking
- [ ] PR #TBD (branch `feature/where-to-buy-ship` - fill in the number once opened) - Add
      `/where-to-buy-ship`: every in-game terminal that sells or rents one ship, aUEC prices
      cheapest first, rentals grouped per star system and labelled as the 1-day rate. No
      location option or distance sort, deliberately (only 7 terminals sell ships). A buy or
      rent row UEX sends with no star system takes its terminal's system from
      `terminal_reference` via the existing `Database.get_terminal_star_system`. Autocomplete
      only offers ships with at least one buy or rent row (same lesson as PR #50). See
      PROJECT_CONTEXT.md entry 81. Four new `UexClient` methods, `get_vehicle_purchase_prices`/
      `get_vehicle_rental_prices`/`get_vehicle_purchase_prices_all`/
      `get_vehicle_rental_prices_all`, each with a 12h `_ENDPOINT_CACHE_TTL` entry
      (`bot/uex/client.py`, new `bot/uex/ship_shops.py`, new `bot/cogs/ship_shops.py`,
      `bot/main.py`'s `INITIAL_COGS`, `bot/cogs/help.py`'s `CATEGORIES`, new
      `tests/test_ship_shops.py`)

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
