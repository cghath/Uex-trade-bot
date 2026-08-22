# Project context

A running account of where this project stands and how it got here. `CONTRIBUTING.md`
covers *how* to work on this codebase (patterns, pitfalls); this doc covers *what's
happened* and *why things are the way they are*, so nobody has to re-derive it from
scratch or re-litigate a decision that already got made for a reason.

## What this is

A Discord bot for Star Citizen trading, built on the UEX Corp API 2.0
(https://uexcorp.space/api/documentation/). See `README.md` for the full feature list.

## Branch workflow (important - read this before opening a PR)

- **`main`** — production. Only things verified to actually work land here.
- **`TestBranch`** — staging. All new work targets this branch first, so it can be pulled
  down and run against a real Discord server / live UEX data before touching `main`. The
  user merges `TestBranch` → `main` manually once satisfied.
- **`claude/trade-bot-t44ix7`** — the AI session's working branch. Once its PR into
  `TestBranch` merges, it gets **recreated fresh from the current `TestBranch`** for the
  next piece of work (`git fetch origin TestBranch && git checkout -B
  claude/trade-bot-t44ix7 origin/TestBranch`), never left stacking old merged history.

This split exists because early work (see below) went straight to `main`, and the user
wanted a safer place to land less-proven changes - notably the Undervalued Scanner, which
went through several real correctness bugs found only by testing against live data.

## Timeline so far

1. **Housekeeping** (merged to `main`): secrets (`.env`, `data/`) and `__pycache__` were
   committed to git with no `.gitignore` - untracked them (files stayed on disk), added a
   real `.gitignore`, fixed a stale README section, moved a one-off diagnostic script into
   `scripts/`, added 24 unit tests for previously-untested route/trend math. The tokens
   committed in old history are still technically recoverable from git log; low urgency
   since the repo is private, but worth rotating (Discord bot token, UEX app token) when
   convenient - not done yet.
2. **`TestBranch` workflow set up**: user asked for untested work to stop landing directly
   on `main`. `TestBranch` was fast-forwarded to match `main` and adopted as the new PR
   target for everything since.
3. **Undervalued Scanner built from scratch**: a local AI model's earlier attempt at this
   same feature was reviewed and found completely non-functional (cog never wired into
   `INITIAL_COGS`, invalid discord.py API usage). Rebuilt from scratch following this
   codebase's existing `marketplace_alerts.py`/`stock_alerts.py` pattern - see
   `CONTRIBUTING.md` for the specifics of what went wrong and how to avoid repeating it.
4. **Four real correctness bugs found via live testing**, each traced to actual UEX data
   pulled by the user and fixed in its own PR - see "How the scanner's matching logic
   evolved" below for the detail. This is the part worth reading closely before touching
   `bot/uex/scanner.py` again.
5. **`SCANNER_STEAL_THRESHOLD` default raised 20% → 65%**, once the matching logic was
   fully validated against UEX's own API docs and the remaining "implausible" results
   turned out to be a threshold-calibration issue, not a bug.
6. **`CONTRIBUTING.md` / `CLAUDE.md` added**, distilling the local model's mistakes into a
   checklist so the next offloaded task doesn't repeat them.
7. **Marketplace liquidity tools completed** on **`Local-model-handoff`**: the Marketplace
   trends refresh now saves the same live activity snapshot for a leaderboard instead of
   making another UEX request. `/liquidity-rank` presents a seller-focused **Sellability
   Rating (0-100)** with a direct UEX Marketplace link for every item. It uses completed
   deals, open negotiations at half weight, active sell listings as competing supply, and
   a modest active-buy-posting bonus. A three-listing supply cushion prevents one-listing
   items from dominating the rank. `/liquidity-trends [item]` charts an item's hourly
   rating history; without an item it will show the biggest 24-hour movers once enough
   snapshots have accumulated. The implementation was live-tested in Discord and has 83
   passing automated tests.

## How the scanner's matching logic evolved (read before touching `bot/uex/scanner.py`)

Each version below looked correct in isolation and was wrong for a reason only visible
against real live data - the lesson isn't "the final version is right," it's "assume the
next edge case exists until proven otherwise; verify against real API data, not just unit
tests with synthetic fixtures."

| # | What it did | What was actually wrong | Fixed by |
|---|---|---|---|
| 1 | Matched a listing to an average by `id_item` alone | UEX returns separate average rows per currency (UEC/WIF/MGS) and per unit (unit/scu/crate/pack/stack/...) - comparing across them is comparing unrelated numbers | Key on `(id_item, currency, unit)` |
| 2 | Took the lowest `price_avg_month` across all rows for a match | An average built from 1-2 historical listings can be dominated by a single outlier (troll price, typo) | Require `listings_count >= MIN_LISTINGS_FOR_FAIR_PRICE` (3) before trusting a row |
| 3 | Scanned every Marketplace category | Crafted gear (weapons, armor, ship components) has real value driven by crafting-material quality that UEX **never exposes as structured data** - only as free text a seller typed ("Q970", "-44% dmg", "CRAFTED"). `/items` catalog doesn't even have entries for most of these. No amount of matching logic fixes missing data. | Restrict to `ALLOWED_MARKETPLACE_CATEGORY_IDS = {36, 87}` (Commodities, Harvestables) - the only categories where quality is a real, structured, usable signal |
| 4 | Still took the lowest-price *tier* among an item's averages, ignoring the listing's own reported quality | Even raw ore's price varies enormously by quality tier (one commodity ranged ~150K to 200M+ UEC across tiers 0-7) - comparing against "whichever tier happened to have enough sample data" produced real false positives | Key on `(id_item, quality_tier, currency, unit)`; convert the listing's own `quality` (0-1000) to a tier via `bot/sell_list.py: quality_to_tier` and match **only** that exact tier, skipping if no reported quality or no matching tier exists |

After all four fixes, matching logic was cross-checked against UEX's own field-level docs
(`/marketplace_prices_averages_all`) and confirmed exact on every point: `quality_tier`
boundaries, the 30-day/per-unit definition of `price_avg_month`, and the
`id_item + quality_tier + operation + currency + unit` row-uniqueness key. So if a future
result still looks implausible, **check the threshold and the data first, not the
matching code** - it's doc-verified correct as of this writing.

## Full API reference

`docs/UEX_API_2.0_reference.md` is a complete, machine-readable dump of every documented
endpoint (89 total - request params, response fields, types, cache TTLs) scraped
2026-08-22. Check it before guessing at a field's meaning or writing another
`diagnose.py` round-trip to find out - most of what's below was cross-checked against it.
It's also not infallible (see the `quality` field note below) - real observed data still
wins when the two disagree.

## Useful UEX API facts learned along the way (not all documented obviously)

- Marketplace endpoints return numeric fields as **JSON strings**, not real numbers (e.g.
  `"price": "450000"`). Always go through `parse_uex_number` (`bot/uex/marketplace.py`)
  before comparing or formatting - `commodities_prices`-style endpoints don't have this
  quirk, only Marketplace ones do.
- `quality_tier` buckets (confirmed via official docs, `/marketplace_prices_averages_all`):
  `0 = Q0, 1 = Q1-499, 2 = Q500-599, 3 = Q600-699, 4 = Q700-799, 5 = Q800-899, 6 = Q900-949,
  7 = Q950-1000`. Uneven on purpose, not a bug.
- UEX's own "Pricing Parameters" doc table lists a **"Variation Tolerance"** per category -
  e.g. 60% for Ore Sales, 25% for Commodities, 100% for Items - as *normal, expected* price
  variance, not anomalies. Any future "is this discount real" feature should be calibrated
  against this table, not an arbitrary guess.
- `get_marketplace_listings(operation=...)` returns on the order of ~500 rows in practice.
  Per `docs/UEX_API_2.0_reference.md`, the endpoint is documented as capped at 100 rows by
  default, only unlocked to 1,000 when **both** `id_item` and `operation` are supplied
  together - the scanner currently calls it with `operation` alone, so it likely isn't
  getting full coverage. This is a plausible explanation for something observed
  independently: the exact set of listings returned isn't stable, and a specific
  known-active listing can be absent from one pull and present in the next. Not fixed yet -
  would mean restructuring the scanner's fetch (e.g. per-category or per-item calls) to
  unlock the higher limit; flagged as a "worth revisiting" item, not done unprompted.
- `/marketplace_listings`'s documented `quality` field says `string|null // 0-100` in the
  API reference - **this appears to be wrong or stale**. Every real listing pulled during
  this project showed values well above 100 (322, 636, 947, 972, 1000), consistent with
  `/marketplace_prices_history`'s explicitly documented `quality int // 0-1000` and with
  `quality_tier`'s own 0-1000-based boundaries. The scanner's code treats it as 0-1000 and
  that matches every real observation - trust the data over that one doc line if they ever
  seem to disagree again.
- `/items` (the reference catalog) doesn't have entries for most Marketplace-only gear
  (weapons, armor, backpacks) - verified directly by searching for two different flagged
  items and getting zero matches both times. It's mainly scoped to commodities and a
  narrower set of standard goods.
- `marketplace_averages` / `marketplace_averages_all` (no "prices" in the name) are
  **deprecated** UEX endpoints, being phased out - don't use them. The scanner already
  correctly uses `marketplace_prices_averages_all`, the non-deprecated replacement.
- This sandbox's network policy blocks direct calls to `uexcorp.space`/`api.uexcorp.uk`.
  Live-data investigation in this project happened via short-lived diagnostic scripts (see
  below) run on the user's own machine, which does have access.

## The `diagnose.py` pattern

Several rounds of debugging used a throwaway script (never committed) that reuses the
bot's own `Config`/`UexClient` to pull real data:

```python
import asyncio
from bot.config import Config
from bot.uex.client import UexClient

async def main():
    cfg = Config.from_env()
    client = UexClient(app_token=cfg.uex_app_token)
    try:
        ...  # whatever needs checking - listings, averages, categories, items
    finally:
        await client.aclose()

asyncio.run(main())
```

Worth reusing this pattern rather than reinventing it - it's how the currency/unit,
sample-size, category, and quality-tier bugs were all actually diagnosed, as opposed to
guessed at.

## Current state / what's pending

- **Resolved: PR #10** on `TestBranch` came from `jcocja-commits`, a collaborator the
  user added (confirmed) - not an unexpected account. It deleted the entire
  `/items-to-sell` feature (`bot/cogs/sell_list.py`, `bot/sell_list.py`,
  `user_sell_list` table) and replaced it with a `marketplace_item_tier_stats` table,
  moving `quality_to_tier()` into `bot/uex/marketplace.py` - intentional (confirmed by
  the user), not a mistake. It correctly updated `bot/uex/scanner.py`'s import to match
  the new location (verified: all 68 tests pass, including all 22 scanner tests).
  `bot/uex/scanner.py` currently imports `quality_to_tier` from `bot.uex.marketplace`,
  not `bot.sell_list` - if you're reading older PR descriptions in this repo's history
  that reference `bot/sell_list.py`, that file no longer exists.
- All scanner work (PRs adding the feature and its four fixes, plus the threshold change)
  lives on `TestBranch`, not yet merged to `main`. Confirm with the user before assuming
  it's in production.
- `CONTRIBUTING.md`/`CLAUDE.md` PR may or may not be merged yet - check before assuming.
- Discord bot token and UEX app token committed in old git history have not been rotated.
  Low urgency (private repo) but still outstanding.
- `Harvestables` (category id 87) is in the scanner's allowlist alongside `Commodities`
  (id 36) but wasn't independently verified against live data the way `Commodities` was -
  the user included it provisionally. Worth confirming it actually produces sane results,
  or dropping it, once there's live signal.
- `MIN_LISTINGS_FOR_FAIR_PRICE` (currently 3) and `SCANNER_STEAL_THRESHOLD` (currently
  0.65) are both tuning knobs, not settled constants - revisit if live results still look
  off after the threshold change.
- `bot/cogs/scanner.py` fetches `get_marketplace_listings(operation="sell")` without
  `id_item`, which per `docs/UEX_API_2.0_reference.md` means it's likely capped at 100
  rows server-side rather than unlocking the documented 1,000-row limit (needs `id_item`
  *and* `operation` together) - not fixed, see the API-facts note above for detail.
- The user mentioned possibly creating a separate branch for a local model to work on
  independently - `CONTRIBUTING.md` exists specifically so that work starts from a better
  footing.
- **Liquidity feature:** current work is on `Local-model-handoff`, not `main`. Its
  score is deliberately an indicator, not a predicted percentage chance of sale. It is
  bounded to 0-100 so users can interpret it at a glance. The history/movers view needs
  at least two hourly Marketplace snapshots before it can show a comparison.

