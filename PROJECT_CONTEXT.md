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
- **`Local-model-handoff`** — retained as a historical backup of the earlier local-model work.
  New work happens directly on `TestBranch`; it is not an active development target.

This split exists because early work (see below) went straight to `main`, and the user
wanted a safer place to land less-proven changes - notably the Undervalued Scanner, which
went through several real correctness bugs found only by testing against live data.

Don't assume from this doc whether a given feature has reached `main` yet - that changes
often. Check with `git log --oneline origin/main..origin/TestBranch` (empty output means
they're in sync).

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
7. **Marketplace liquidity tools completed** and merged into **`TestBranch`**: the Marketplace
   trends refresh now saves the same live activity snapshot for a leaderboard instead of
   making another UEX request. `/liquidity-rank` presents a seller-focused **Sellability
   Rating (0-100)** with a direct UEX Marketplace link for every item. It uses completed
   deals, open negotiations at half weight, active sell listings as competing supply, and
   a modest active-buy-posting bonus. A three-listing supply cushion prevents one-listing
   items from dominating the rank. `/liquidity-trends [item]` charts an item's hourly
   rating history; without an item it will show the biggest 24-hour movers once enough
   snapshots have accumulated. The implementation was live-tested in Discord.
8. **Command and scanner polish**: `/top-scored-routes` and `/top-in-stock-routes` became one
   `/top-routes` command with an optional strict live-availability filter. `/intro` is now a
   compact categorized guide with emoji headings rather than a wall of repeated descriptions.
   Marketplace Intelligence distinguishes all-item sellability from the Raw Materials Deal
   Scanner, which is explicitly limited to quality-matched Commodities and Harvestables.
9. **UEX data collection foundation** (`bot/cogs/intelligence.py`): four background collectors
   that periodically snapshot terminal market state, terminal data freshness, fuel prices, and
   reference metadata into 10 new tables, so the forward-looking features in `ROADMAP.md` have
   real accumulated history to work from rather than only whatever a single live API call
   returns. Refinery yields are gathered too, but by the daily reference refresh rather than a
   collector of their own. See "Data collection architecture" below for the storage patterns
   these tables use and when to use which.
10. **Liquidity buy-posting weight corrected** (`ff997e0`): `compute_liquidity_score` weighted
    an active buy posting at `2.0` - twice a completed sale - so an item nobody had actually
    bought could outrank one with five real completed transactions. Backwards for a rating whose
    whole question is "will this sell?". Now `0.25`, behind named constants ordered
    `COMPLETED (1.0) > OPEN (0.5) > BUY_POSTING (0.25)` in `bot/uex/marketplace.py`, with a
    regression test pinning that ordering. Scores for items with no buy postings are unchanged.
    Worth noting *how* this surfaced: item 7 above already described the feature as "a modest
    active-buy-posting bonus" while the code made it the strongest signal in the formula. The
    doc recorded the intent correctly and the code contradicted it - that gap **was** the bug.
    Two lessons: writing intent down pays off, and a prose/code mismatch is a defect report,
    not a stale comment.
11. **Route intelligence completed**: terminal-data health, route-confidence scoring,
    time-weighted supply/demand history, terminal infrastructure checks, and commodity-risk
    labels now feed the existing route commands without mixing safety/confidence into the
    profit score. `/terminal-history` exposes the collected history directly.
12. **Mixed-cargo routing added on `TestBranch`**: `/mixed-routes` combines two or three
    commodities from one origin to one destination and returns the five best ship- and
    budget-adjusted loads. Allocation is bounded by origin stock, destination demand, cargo
    capacity, and investment capital. `space-only:true` fails closed unless both terminals
    have an explicit UEX space-station relationship. XL/loading-dock ships such as the
    Polaris automatically require confirmed external loading docks or XL station access at
    both ends; missing access metadata excludes the route rather than presenting it as safe.
13. **Digest and intelligence brief expanded on `TestBranch`**: the daily digest now keeps
    four upward and four downward Sellability Rating shifts in separate Discord-safe fields,
    includes collector freshness, and correctly commits its once-per-day posting marker.
    `/intelligence-brief` is the deeper on-demand view: executive signals, personalized mixed
    routes, 24-hour supply/demand changes, rating direction, risk notes, and data health.
14. **Route-intelligence audit hardened on `TestBranch`**: freshness now follows UEX's explicit
    TTL age/limit fields instead of the pending-report queue flag; mixed routes reject full or
    unknown sell-side demand; every route view uses consistent confidence inputs and stable
    terminal ids; oversized Discord fields are split without dropping warnings; missing cargo
    risk metadata is disclosed instead of looking safe; and incomplete system names no longer
    render as literal `None` values.
15. **Personal inventory and best-time Marketplace posting built for local validation**:
    `/inventory-add` stores catalogued game-earned stacks by quality and location without an
    acquisition-cost field; `/inventory` shows the existing Sellability Rating and links item
    names to UEX postings; `/best-posting-time` ranks four-hour Eastern windows from positive
    hourly negotiation/listing changes; and `/inventory-sell` provides a paged checklist plus
    a deliberate authorization gate. Automatic UEC sell posts recalculate a balanced price at
    execution time, enforce the user's manual per-unit floor, expire after 48 hours, and relist
    only from explicit remaining-stock evidence. Network-ambiguous or interrupted POSTs are
    quarantined, never blindly retried. Private notes stay private. The implementation also
    corrected the older manual-post confirmation to read UEX's documented `id_listing` result.
16. **Negotiation-message DM alerts, custom pricing, and a listing-lookup command added on
    `TestBranch`**: `/negotiation-alerts` opt-in DMs a user when anyone sends a new message in
    any of their UEX negotiations (not just bot-posted ones), seeding a baseline on enable so
    existing history never floods in as new. `/inventory-post-now` gained a live-data
    "Recommended price" preview (sourced from the same function the real post uses, so preview
    and post can't diverge) plus a custom-price option, which required a SQLite CHECK-constraint
    migration (`marketplace_post_jobs.pricing_strategy`) done as a detect-and-rebuild since
    SQLite can't ALTER a CHECK in place. Added `/marketplace-listing` to look up a listing's
    details by id. Local and the Pi's live data were fully merged after both had run
    independently for stretches - not a simple one-way copy, since both sides had accumulated
    genuinely unique history in places.
17. **Review-fix pass and a 48-hour no-interest relist-discount cycle added on `TestBranch`**:
    seven bugs found by review and confirmed against the live code before fixing (worst: a
    relisted custom-priced job would crash the background poster with `int(None)`, since
    `custom_price` wasn't carried into `expire_and_relist_inventory_post`'s INSERT). Also added:
    an unsold listing with no open negotiation now relists 5% below its previous price every 48
    hours (compounding, never below the manual minimum) instead of sitting at the same
    unsuccessful price; an open negotiation pauses the cycle entirely; hitting the floor with no
    interest pauses it and sends an interactive DM (keep / lower the floor & resume / cancel),
    with `/inventory-resolve-floor` as a fallback since that DM's buttons can't survive a bot
    restart. `/inventory` now shows each active job's real status/timing instead of a bare
    count. Finally, `/best-posting-time` and `/inventory-sell`'s "recommended window" scheduling
    were removed entirely: UEX listings require staff approval before going live on an
    unpredictable timeline, so precisely timing *submission* never actually controlled when a
    listing became visible - the premise the feature was built on didn't hold. Posting is now
    immediate (queued for the next few-minutes cycle) rather than deferred to a computed window.
    Every inventory interaction response is explicitly ephemeral; background worker updates use
    private DMs because Discord cannot make a non-interaction channel post ephemeral.

18. **Cleanup/trim pass on `TestBranch`**: no new features - a user request to reduce bulk for
    first-time users. Dead-code sweep removed two never-called `UexClient` methods
    (`get_items_prices`, `get_companies`), their orphaned cache-TTL entry, and a dead
    `datetime`/`timezone` import in `bot/uex/inventory.py` left over from entry 17's
    best-posting-time removal. Fixed stale text that removal left behind: `/inventory-sell`'s
    own description still promised "best-time automatic UEX posting"; README/ROADMAP/
    `CLAUDE.local.md` still described Eastern-time posting windows and the old two-command
    `/top-scored-routes` + `/top-in-stock-routes` naming (that merge into one `/top-routes` with
    a `strict` flag happened before this session but was never written down - see entry 8).
    Merged the 9-command alerts trio down to 5: `/alert-add`, `/stock-alert-add`, and
    `/marketplace-alert-add` stay separate (their inputs genuinely differ), but the three
    `*-list` and three `*-remove` commands became one shared `/alert-list` and `/alert-remove`,
    living in `bot/cogs/alerts.py` and reaching into all three alert tables via `self.bot.db`.
    `/alert-remove`'s picker needed a composite `"price:<id>"`/`"stock:<id>"`/`"marketplace:<id>"`
    key since the three tables auto-increment independently and could collide on a bare int id -
    `discord_ui.py`'s `AlertRemovePickerView` already treated the id as an opaque value, so it
    needed no changes. Added `tests/test_alerts.py`, the first test coverage any of the three
    alert cogs have had. Separately, kept `/liquidity-rank`/`/liquidity-trends` and
    `/marketplace-trending`/`/marketplace-movers` as four distinct commands (they measure
    genuinely different things - a bot-computed sellability score vs. UEX's raw negotiation/
    price-swing activity) but reworded all four descriptions plus the `/intro` category
    (renamed "Marketplace Intelligence" -> "Sellability Ratings") to say so explicitly, since
    the similar naming read as duplication. Command count: 59 -> 55. Test count: 160 -> 165.

19. **Custom-price parity for `/inventory-sell`'s batch flow**: found by the user testing the
    live bot right after entry 18 - `/inventory-post-now` (single item) already had a "custom"
    pricing option added in entry 16, but `AuthorizeScheduleView` (the multi-stack checklist
    behind `/inventory-sell`) only ever offered balanced/undercut/premium, with no way to enter
    an exact price. `create_inventory_post_jobs` already supported a per-job `custom_price`
    (it re-validates against that inventory entry's own minimum_price from the DB, not
    whatever the caller passes), so this was purely a missing UI path, not a DB gap.
    `CustomPriceModal` was generalized to take `minimum_price` explicitly instead of reading
    `view.entry["minimum_price"]`, so the same modal now works for both `PostNowView` and
    `AuthorizeScheduleView`. An absolute custom price only makes sense for one stack at a time
    (unlike the percentage strategies, which scale per-item automatically), so choosing
    "custom" with more than one stack selected explains that and reverts instead of silently
    misapplying one price across different items. Added direct test coverage for
    `AuthorizeScheduleView` for the first time (previously only `PostNowView` had any).

20. **`/inventory-add` resolves variant/skin-qualified item names**: also found by the user
    testing live - typing a real item they own (e.g. "Arlington Rifle Widowmaker") failed to
    resolve, with the bot suggesting only an exact autocomplete pick would work. Checked UEX's
    live catalog and marketplace listings directly rather than guessing (`id=8069` is the only
    "Arlington"-matching catalog entry; 11 of 12 live "arlington"-titled sell listings post
    against that same id, confirming "Widowmaker"/"Watchpoint"/"Gamekeeper" are seller-chosen
    listing titles for one base item, not separate catalog entries - this was the "genuinely
    uncatalogued item" question from earlier in the session, and the answer turned out to be
    no, so the schema-migration path was never needed). Real listing titles interleave the
    variant word in the middle sometimes ("Arlington \"Watchpoint\" Rifle") rather than only
    appending it, so `find_item_id_by_name` (`bot/uex/marketplace.py`, shared by all of
    inventory/marketplace/marketplace-alerts) gained a third, last-resort tier: word-subset
    match (every word in a catalog name present in the query, not required contiguous),
    still gated to a unique result so it never guesses across two plausible candidates.
    `/inventory-add` also now keeps what was actually typed as the stored/displayed item name
    instead of collapsing it to the catalog's bare name once resolved, so the variant survives
    into `/inventory`, the eventual Marketplace listing title, and the confirmation message.

21. **Auto-load route filter added on `TestBranch`**: `/best-route` and `/top-routes` gained
    an `auto-load-only` option that filters to routes whose origin terminal supports UEX's
    `is_auto_load` - the purchase-time "buy cargo, have it loaded onto my stored ship
    automatically" feature, which UEX exposes as a field distinct from
    `has_loading_dock`/`has_freight_elevator` (physical external cargo infrastructure,
    already used by practical-route notes and mixed-route capital-ship gating) - the bot
    wasn't capturing `is_auto_load` at all before this. `terminal_reference` gained an
    `is_auto_load` column, filled from the same `/terminals` rows the 24h reference refresh
    already fetches - no new API call. Checked at the origin only, not the destination:
    auto-load is about loading purchased cargo onto a stored ship, not the sell side. Not
    yet verified live in Discord - see "Current state" below.

22. **Marketplace item links made consistent on `TestBranch`**: an audit found
    `marketplace_item_url` (the UEX Marketplace page link) applied inconsistently - present
    in `liquidity.py`/`digest.py`/most of `personal_inventory.py`, missing everywhere else
    that displays an item name. Added a shared `marketplace_item_link(name, id_item)`
    helper (falls back to plain text when `id_item` is unavailable) and applied it to
    `scanner.py` (both `/scan-now` and the channel alert - `StealEntry` gained `id_item`,
    already resolved during matching but not stored), `marketplace_alerts.py`'s DM,
    `intelligence_brief.py`'s executive-signals lines, and `marketplace.py`'s
    `/marketplace-trending`/`/marketplace-movers` (`MarketplaceMoverEntry` gained
    `id_item`). All used data already on hand - no new API calls. Deliberately left
    unfixed: `negotiation_alerts.py` and `marketplace.py`'s `/my-negotiations`/
    `/my-favorites` - UEX's `/marketplace_negotiations` and `/marketplace_favorites`
    don't return `id_item` at all, so linking those needs an extra
    `get_marketplace_listings` lookup per row (a real added-cost decision, a recurring
    background poller in negotiation_alerts.py's case, not a free fix).

## Where to look for what

Five docs, deliberately scoped so they don't duplicate each other:

| Doc | Answers |
|---|---|
| `README.md` | What the bot does, how to install/run/deploy it |
| `CLAUDE.md` | Auto-loaded pointer for AI sessions: the two most critical gotchas + quick facts |
| `CONTRIBUTING.md` | *How* to work on this codebase - required patterns, pre-flight checklist |
| `PROJECT_CONTEXT.md` (this doc) | *What happened and why* - history, hard-won API knowledge, current state |
| `ROADMAP.md` | *What's next* - completed features and the backlog of ideas |

Standalone troubleshooting write-ups have been folded into `CONTRIBUTING.md` rather than kept
as separate files - point-in-time incident logs drift out of date and end up contradicting the
maintained guidance. If you debug something worth remembering, add it to `CONTRIBUTING.md`
(mechanics and prevention) or here (history and context), not a new log file.

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
| 4 | Still took the lowest-price *tier* among an item's averages, ignoring the listing's own reported quality | Even raw ore's price varies enormously by quality tier (one commodity ranged ~150K to 200M+ UEC across tiers 0-7) - comparing against "whichever tier happened to have enough sample data" produced real false positives | Key on `(id_item, quality_tier, currency, unit)`; convert the listing's own `quality` (0-1000) to a tier via `bot/uex/marketplace.py: quality_to_tier` and match **only** that exact tier, skipping if no reported quality or no matching tier exists |

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
- `POST /marketplace_advertise` returns the created id as **`id_listing`**, not `id`. Treat a
  missing id or a lost/invalid POST response as ambiguous: the public listing may exist, so a
  blind retry can create a duplicate.
- `/items` currently enforces its documented category/filter requirement: the inherited
  unfiltered `get_items()` pattern returns zero rows with `requires_id_category`. Full-catalog
  callers must use `UexClient.get_item_catalog()`, which loads item categories in bounded batches,
  deduplicates the result, and caches it for 12 hours. Fast autocomplete uses the persisted
  Marketplace activity index and only supplements it from an already-warm catalog.
- `/marketplace_listings` may expose a listing with `is_sold_out=1` while it remains in the
  active-advertisement window. Its asking price is useful supporting evidence, but neither that
  flag nor a disappeared listing proves the final negotiated unit price.
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
- Live API investigation should use the bot's configured environment or a short-lived diagnostic
  script (see below). Some restricted development environments may block direct API access.

## Data collection architecture (the `intelligence` cog)

`bot/cogs/intelligence.py` is shaped differently from every other cog and it's worth knowing
why before extending it: **it has no slash commands at all**, only background `tasks.loop`
collectors. It exists to accumulate history, not to answer questions - the querying happens in
whatever feature later consumes that history.

It runs exactly four loops:

| Loop | Cadence | Writes |
|---|---|---|
| `snapshot_terminal_market` | 2h | `terminal_market_state` + `terminal_market_observations` |
| `snapshot_data_health` | 1h | `terminal_data_health_state` + `terminal_data_health_observations` |
| `snapshot_fuel_prices` | 6h | `fuel_price_state` + `fuel_price_observations` |
| `refresh_reference_data` | 24h | `commodity_reference`, `terminal_reference`, `refinery_yield_observations` |

### Three storage patterns, not one

An earlier version of this section said every table here is a `*_state` / `*_observations`
pair. Six of the ten are, forming three pairs; the other four aren't. If you're adding a
collector, pick the pattern that matches the *shape of the data*, not the one that sounds most
consistent:

- **Change-only pair** - for values that are stable and change unpredictably (market prices,
  data freshness, fuel). The `*_state` table holds one current row per key; the
  `*_observations` table is append-only and **only gets a row when a value actually changes**.
  That's what makes multi-week history affordable - writing a full copy of every terminal every
  hour would balloon the SQLite file for no added signal. Six tables use it, and it's the right
  default for anything polled on a timer.
- **Daily upsert** - for values UEX itself reports at a daily grain.
  `refinery_yield_observations` has no state partner and isn't change-only: it upserts one row
  per `(id_commodity, id_terminal, recorded_day)` (`record_refinery_yield_snapshot` in
  `bot/db/database.py`). Deliberate, not a degraded pair - the source data has a daily grain, so
  one row per day *is* the natural resolution.
- **Reference tables** - `commodity_reference` and `terminal_reference` are slow-moving catalog
  data, refreshed wholesale on the 24h loop. No history is kept because none is useful.

Two traps before you try to count these tables yourself:

- `marketplace_tier_observations` is the tenth table and a real change-only pair, but **nothing
  in this cog writes it** - the writer is `bot/cogs/marketplace.py`. Its state partner is also
  named `marketplace_item_tier_stats` rather than `*_state`, and predates this batch (`29a09f5`).
  Schema location is not a reliable guide to ownership.
- `stock_alert_terminal_state` matches the `*_state` naming but belongs to the stock-alerts
  feature and has nothing to do with data collection.

User-facing intelligence stays outside the collector cog. `bot/cogs/intelligence_brief.py`
queries these durable tables for `/intelligence-brief`, while `bot/uex/mixed_routes.py` owns
the dependency-free mixed-load allocation and terminal-access gates. Keep that split: the
collector records evidence, database methods retrieve it, pure helpers rank it, and cogs only
coordinate Discord/API presentation.

## The `diagnose.py` pattern

Several rounds of debugging used a throwaway script (gitignored, not committed - see
`.gitignore`'s "local investigation notes" block, which also covers `categories.txt`,
`dump.txt`, and friends) that reuses the bot's own `Config`/`UexClient` to pull real data.
Note the split: one-off investigation scripts stay local and ignored, while anything reusable
belongs in `scripts/` and gets committed.

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

- **`bot/sell_list.py` no longer exists.** The per-user `/items-to-sell` feature was
  intentionally dropped as redundant, and `quality_to_tier()` moved to
  `bot/uex/marketplace.py`. Older PR descriptions and commit messages in this repo's history
  still reference the old path - import from `bot.uex.marketplace` instead.
- Discord bot token and UEX app token committed in old git history have not been rotated.
  Low urgency (private repo) but still outstanding.
- **Three Marketplace surfaces still show unlinked item names** (timeline entry 22):
  `negotiation_alerts.py`'s DM alerts, and `marketplace.py`'s `/my-negotiations` and
  `/my-favorites`. Fixable, but needs an explicit decision first since UEX's underlying
  endpoints don't return `id_item` - closing the gap means adding a
  `get_marketplace_listings(id=id_listing)` lookup per row, which is a real cost on
  negotiation_alerts.py's 5-min poller specifically (the two `/my-*` commands are
  on-demand and bounded to 15 rows, so lower-stakes either way).
- **Auto-load-only route filter needs a live check.** `/best-route auto-load-only:True` and
  `/top-routes auto-load-only:True` (timeline entry 21) were built and unit-tested - including
  the schema migration exercised end-to-end against a real SQLite file - from a sandbox with
  no Discord/UEX credentials. Confirm on the next update/deploy: `tree.sync()` actually picks
  up the renamed `auto-load-only` parameter, and real UEX terminal data populates
  `is_auto_load` as expected once the 24h reference refresh has run.
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
- `Local-model-handoff` remains available as a backup, but current development happens on
  `TestBranch`.
- **Liquidity rating** is deliberately an indicator, not a predicted percentage chance of
  sale. It is bounded to 0-100 so users can interpret it at a glance. The history/movers view
  needs at least two hourly Marketplace snapshots before it can show a comparison.
- **Two open liquidity judgment calls**, both deliberate deferrals awaiting live results
  rather than oversights - decide them by looking at a real `/liquidity-rank`, not from first
  principles:
  - *Zero sell listings scores 0.* `compute_liquidity_score` returns `0.0` when `listings <= 0`,
    so an item with real demand and no competing sellers disappears from the leaderboard -
    arguably the single best thing to list. The guard conflates "no competition (opportunity)"
    with "no activity (irrelevant)".
  - *The fallback path over-scores.* When `negotiations_success`/`negotiations_open` are both
    absent, `compute_liquidity_score` falls back to `negotiations_count` and weights it at
    `1.0` - the *completed-sale* weight. So an item with 10 open negotiations and zero sales
    scores 29.41 through the fallback but 17.24 when the same reality is reported in detail
    (1.7x). Same class of defect as the buy-posting weight: a signal weighted as something
    stronger than it is. Fixing it means weighting the fallback nearer `WEIGHT_OPEN_NEGOTIATION`,
    or refusing to score without detail. Not decided.
- **Checked and dismissed:** an earlier review worried the 0-100 scale was so compressed that a
  leaderboard leader would read ~12/100. Recomputed against 503 real `marketplace_item_activity`
  rows, the top score is 84.21, the median nonzero score 26.15, and nothing scores 0. The worry
  came from reasoning about completed-sale counts alone; real items carry far larger *total*
  negotiation counts. The scale is fine - don't re-open this without new data.
- The footer text in `bot/cogs/liquidity.py` ("each buy posting adds a small demand bonus")
  described the *intended* behaviour and only became accurate once the buy-posting weight was
  lowered to 0.25 - see timeline item 10.
- **Terminal Data Health** uses UEX's explicit `last_update_days_limit`, `last_update_days`,
  and `last_update_days_percentage` fields for freshness, while
  `prices_updated_percentage` remains coverage only. `has_recent_reports` means pending,
  unconsolidated report ids exist and is retained only as diagnostic state—it must never
  influence freshness. `/price`, `/best-route`, and `/top-routes` warn on stale, unknown,
  or poorly covered terminals but stay quiet for healthy data.
- **Route Confidence Rating** is separate from profit and UEX's proprietary route score.
  `/best-route` and `/top-routes` show a 0-100 High/Medium/Low confidence rating based on
  terminal freshness, directional player-report depth, live stock/demand, and price volatility.
- **Supply & Demand History** is reconstructed from the change-only terminal observations,
  weighting each state by how long it remained active rather than counting database rows.
  `/terminal-history [commodity] [terminal]` shows supply and buyer-demand availability rates,
  the observed window, and state-change count; windows shorter than 24 hours are preliminary.
- **Route evidence and Practical Route Checks** carry UEX terminal ids through both cached and
  fallback route objects. Health, report-depth, and terminal-reference lookups use those ids,
  not names, because endpoint-specific names can include prefixes such as `Admin -`.
  `/best-route` and `/top-routes` surface container-size limits, missing cargo infrastructure,
  player-owned locations, and confirmed refuel, repair, or cargo-center services without
  changing route ranking.
- **Commodity Risk Labels** use collected UEX flags and remain separate from profit and route
  confidence. Route views label jurisdiction restrictions, explosion risk, quantum/time
  volatility, and recent gameplay bugs. `is_illegal` is worded as restricted in some
  jurisdictions, matching UEX's definition rather than overstating it as universal contraband.
  If the commodity reference row or any risk flag is missing, route views explicitly warn that
  risk metadata is unavailable rather than silently presenting the cargo as safe.
- **Mixed Routes** intentionally rank by estimated profit *after* ship, stock, demand, and
  optional budget limits. They do not include travel time/distance in the score, so the output
  retains cross-system and missing-distance warnings. A qualifying result must allocate at
  least two commodities; a commodity that can fill the ship alone remains a `/best-route`
  concern instead of being disguised as a mixed load.
- **Capital-ship cargo access** uses UEX vehicle `pad_type`/`is_loading_dock`, terminal
  `has_loading_dock`, and parent space-station `pad_types`/`has_loading_dock`. Do not infer
  orbital access from `planet_name`: UEX attaches orbiting stations to nearby planets. Also
  treat foreign-key value `0` as absent—real UEX terminal data uses both zero and null for
  missing relationships.
- **Digest freshness thresholds** follow collector schedules: terminal-market data warns
  after three hours; hourly liquidity and Marketplace data warn after two. The rating-shift
  queries request four gainers and four losers independently so one direction cannot crowd
  out the other, and the fields are separated to stay below Discord's 1,024-character limit.
- **Current staging state (2026-08-27)**: `TestBranch` is deployed and running live on the
  Pi (`uex-trade-bot.service`, host `arkwatcher`) - it is no longer just a local-validation
  branch. Local (PC) and the Pi's databases have been fully merged at least twice now; the
  established practice is to back up both sides before any such merge and pull the Pi's
  backup down to the PC afterward, so nothing valuable lives only on the Pi's disk. The full
  suite has 171 passing tests. Re-check live service and branch state rather than assuming
  this point-in-time operational note is still current.
- The data collectors in `bot/cogs/intelligence.py` only pay off once they've been running a
  while - most of the `ROADMAP.md` intelligence backlog depends on accumulated history, so
  those features will look broken/empty if built and tested against a fresh database.

