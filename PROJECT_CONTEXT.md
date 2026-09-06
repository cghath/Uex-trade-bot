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
    background poller in negotiation_alerts.py's case, not a free fix). See entry 23 -
    this was worked through and closed the same session.

23. **The three deferred Marketplace links from entry 22 added, after a cost check**:
    worked out the actual burst math for negotiation_alerts.py's 5-min poller (bounded to
    once per negotiation with a genuinely new message, not once per poll or per message,
    and `marketplace_listings` has a 60s cache so multiple new messages on one negotiation
    in the same cycle resolve `id_item` once) - concluded it stays well inside the
    120 req/min headroom at any realistic scale for a single-server bot, so implemented
    all three rather than leaving them deferred. `negotiation_alerts.py` resolves lazily
    inside `_check_negotiation`; `/my-negotiations` and `/my-favorites` are on-demand and
    already capped at 15 rows, so they resolve concurrently via `asyncio.gather` through a
    new shared `_resolve_id_item` helper. Every lookup falls back to a plain, unlinked name
    on failure rather than ever blocking the notification/response. Added direct test
    coverage for both new code paths, since neither existing negotiation_alerts fixture
    happened to set `id_listing` and so never actually reached this code before.

24. **CI added on `TestBranch`**: `.github/workflows/tests.yml` runs `pytest -q` on every
    push to `main`/`TestBranch` and every pull request - previously tests only ran when
    someone remembered to run them locally. A second job runs `ruff check --select F`
    (undefined names, unused imports) - deliberately scoped narrower than ruff's own
    unscoped default, which a dry run showed surfaces 45 findings (import sorting,
    quoted-annotation style, etc.) against 2 real ones on this codebase. `--select F` is
    exactly the category that would have caught a real bug from this same session: a
    `replace_all` edit in `digest.py` left one differently-indented call site referencing
    a now-removed import, surfacing only as a runtime `NameError`. Fixing the two
    pre-existing `F401` hits to get CI green on the first run caught a live false positive
    of ruff's own `--fix`: it deleted `trends.py`'s `SELL_SIDE_NO_DEMAND_CODE` import
    because nothing inside that file uses it, not noticing `tests/test_trends.py` imports
    it *from* `trends.py` as a re-export - restored via the `import X as X` explicit
    re-export idiom, which both ruff and mypy recognize as intentional.
    `.github/workflows/dependency-audit.yml` runs `pip-audit` against both requirements
    files weekly plus on any `requirements*.txt` change - a pure vulnerability check, no
    auto-PRs (native Dependabot security-update PRs are a repo Settings toggle, outside
    what a committed file can control).

25. **Discord UX audit on `TestBranch`**: a dedicated pass over the Discord-facing
    behavior (not architecture) across ~13 of 18 cogs found and fixed five things.
    `ships.py`'s `/set-default-ship` and `/my-ship` called the UEX API before
    responding/deferring - on a slow request Discord would show "This interaction
    failed" even though the command actually succeeded; both now defer first.
    `trades.py`'s `/trade-log-add`/`/trade-log` posted personal trade financials publicly
    while `/uex-trades` in the same cog was ephemeral - made consistent. Fixing that
    surfaced a real bug while already in the file: `/uex-trades` deferred
    `ephemeral=True` but its `followup.send()` calls never passed `ephemeral=True`
    themselves - discord.py does not inherit the flag from `defer()`, so trade history
    was actually posting publicly despite the apparent intent, unlike the
    already-correct pattern in `personal_inventory.py` (every `followup.send()` there
    passes `ephemeral=True` explicitly). Added `tests/test_trades.py` to lock in the
    fix. `account.py`'s post-link confirmation now points to `/intro`, since opt-in
    features (negotiation alerts, digest, stock/marketplace alerts, auto-posting) were
    otherwise undiscoverable. Added `describe_uex_api_error()` to
    `bot/uex/exceptions.py`, which gives rate-limit (transient, wait) and auth
    (actionable, relink) failures distinct text instead of the same raw
    `f"UEX API error: {exc}"` string every failure class previously got - applied
    across 17 call sites in `marketplace.py`, `prices.py`, `trends.py`, `scanner.py`,
    `ships.py`.

26. **`/marketplace-delete-listing` gained a Confirm/Cancel gate**: previously a single
    command permanently deleted a real, public UEX listing with no recovery - a
    mistyped `listing_id` (a bare int, easy to confuse with someone else's or an old
    one) had no undo. Now shows a preview embed (title + price, from the same
    `get_marketplace_listings` lookup already used to verify tracked-inventory stock)
    with Delete/Cancel buttons before touching anything, via a new
    `ConfirmDeleteListingView` matching the `ConfirmListingView` pattern
    `/marketplace-post` already used. All the original correctness-critical logic
    (tracked-job stock verification, delete-before-touching-local-state ordering) moved
    into the confirm button unchanged - only the timing shifted, from immediate to
    gated behind an explicit click.

27. **Pi deploy/revert scripts added, replacing the manual-merge fallback for a bad
    upgrade**: `scripts/deploy_and_backup.sh` stops `uex-trade-bot.service`, snapshots
    the current DB (+ `-wal`/`-shm` sidecars if present) and records the current commit
    to `backups/pi/<timestamp>_<commit>/meta.txt`, fast-forwards to the target branch
    (`TestBranch` by default), reinstalls dependencies only if `requirements*.txt`
    actually changed between the old and new commit, then restarts. `scripts/
    revert_last_deploy.sh` undoes it: restores the snapshotted DB, `git checkout`s the
    recorded commit, and restarts - and snapshots the state it's discarding first (a
    `..._pre-revert` backup), so the revert itself is undoable. This is narrower than
    the existing PC<->Pi database-*merge* practice documented below (that's for two
    sides that both accumulated genuinely independent user data over time) - this tool
    is specifically for "about to upgrade the Pi's code, want an easy way back if the
    new commit is bad," which needs a point-in-time snapshot, not a merge. Verified
    end-to-end in an isolated fake git repo with stubbed `sudo`/`systemctl` (backup,
    fast-forward, dependency-change detection, revert, and the pre-revert safety net
    all exercised) - not yet run against the real Pi's systemd unit or its actual
    `data/uexbot.sqlite3`, since this sandbox has no path to `arkwatcher`.
28. **Five high-priority review findings fixed in the 48h relist-discount cycle and the
    two deploy scripts, plus four smaller hardening items**, each verified against the
    live code (not the finding text) before changing anything:
    - A failed negotiation fetch used to return an empty list indistinguishable from
      "verified: no negotiations" - `_fetch_negotiations` now returns `None` on
      `UexApiError`, and the reprice decision `continue`s (retries next cycle) rather
      than proceeding as if confirmed-clear.
    - Picking one "best" negotiation per listing by `(closed, date_modified)` always
      ranked ANY closed negotiation above ANY open one in tuple comparison, regardless
      of recency - an older closed negotiation could hide a genuinely newer open one for
      the same listing. Open-negotiation detection is now a separate per-listing set,
      independent of the "best" pick (which still exists, unchanged, for its own job:
      surfacing `deal_value` on a completed sale).
    - The old listing is deleted before the discounted replacement is posted, so a failed
      replacement post left the item with zero active listings - but `_post_one_job`'s
      return value was never checked, so the bot told the user "relisted as job #N"
      regardless. Now checked; failure gets an honest "no active listing, check
      `/inventory-post-now`" message instead.
    - `deploy_and_backup.sh` had no failure recovery: `set -e` meant any error between
      stop and start (bad fetch, non-fast-forwardable merge, broken pip install) left the
      bot down with no automatic restart. Added a `trap ... ERR` that checks out the old
      commit and restarts, guarded by flags so it only fires once the service has
      actually been stopped and only if the deploy hasn't already succeeded.
    - `revert_last_deploy.sh` validated that `meta.txt` parsed, but never that the backup
      DB file it named actually existed or that its recorded commit still exists in the
      repo - either gap would only surface after the service was already stopped. Both
      are now checked first.
    - Smaller items: `revert_last_deploy.sh` never reinstalled dependencies (unlike the
      forward deploy script), so reverting past a requirements change would run old code
      against mismatched packages - fixed with the same conditional reinstall.
      `/alert-list` could exceed Discord's 2000-char message cap with enough alerts across
      all three types - now truncates at a line boundary with a count-and-hint note.
      The exact 50% terminal-freshness boundary turned out to already be correct (verified
      by hand-checking the math and confirmed with a new test) - the real gap was that no
      test had ever exercised the age/age_limit fallback branch at all, only the
      `last_update_days_percentage` branch; added coverage rather than changing behavior
      that was already right. Checked whether the new auto-load-only filtering added any
      per-route UEX API calls (rate-limiting concern) - it reads terminal data via one
      batched local DB call, not per-route, so no new exposure there.
    - The two script fixes were verified against real failure simulations, not just read
      through: a scratch sandbox (fake git repo, stubbed `sudo`/`systemctl`) confirmed the
      deploy script's rollback trap actually restarts the service and returns to the old
      commit on a forced `git fetch` failure, and confirmed the revert script's new checks
      reject both a missing backup DB file and a bogus commit reference before ever
      touching the service.
    - 186 tests passing (10 new).
    - **Found deploying this very commit**: the sandbox test above used a plain local
      "origin" with no fetch restrictions, so it never caught that `deploy_and_backup.sh`'s
      `git fetch origin "$BRANCH"` silently does nothing on the Pi's actual clone - its
      `remote.origin.fetch` only auto-updates `origin/main` (see the git quirk noted
      elsewhere in this doc), so `origin/TestBranch` never moved and `git merge --ff-only`
      reported "Already up to date" against a stale ref, with exit code 0. The real first
      run of the real script deployed nothing while claiming success. Fixed with an
      explicit destination refspec (`git fetch origin "$BRANCH:refs/remotes/origin/$BRANCH"`)
      and verified in a new sandbox that specifically reproduces the restrictive refspec
      (a bare "remote" repo + a clone configured with the same single-branch fetch rule as
      the Pi), confirming the fix actually advances to the real branch tip rather than
      silently no-oping.
29. **`/inventory-sell` sets a missing minimum price inline instead of dead-ending**:
    selecting a stack with no `minimum_price` used to just bounce you out with "run
    `/inventory-set-minimum` and try again," losing the selection. Now shows a
    "Set minimum: <item>" button per stack that needs one (`SetMinimumPricesView` +
    `SetMinimumModal`); setting the last missing floor turns the same message straight
    into the authorize screen. Required pulling the authorize-embed construction out of
    `InventorySelectionView.review_selected` into `PersonalInventory._build_authorize_screen`
    so both the no-floor-missing path and the just-finished-setting-floors path share it,
    rather than duplicating it. 189 tests passing (3 new).
30. **`/mixed-routes` gained the same `auto-load-only` filter as `/best-route` and
    `/top-routes`**: noticed missing by comparing `/mixed-routes`' options against the
    other two in Discord. The data was already one `SELECT` away -
    `get_mixed_route_market_rows()`'s existing join against `terminal_reference` (the
    same one that already supplies `has_loading_dock` etc. for the capital-ship checks)
    just didn't include `is_auto_load`, so this needed a one-line query change plus a new
    `auto_load_only` parameter on `build_mixed_routes` (filtering only the *origins* list,
    never destinations - auto-load is purchase-time only) and the matching command option.
    191 tests passing (2 new).
31. **Audited the other two route commands for the same kind of gap, on request** - not
    everything that looked asymmetric between `/best-route`, `/top-routes`, and
    `/mixed-routes` turned out to be a real one:
    - `/mixed-routes`' `space-only` filter and its auto-derived capital-ship-access check
      are genuinely mixed-routes-specific by design (entry 12), not an omission elsewhere -
      a hard filter fits a single one-terminal-visit recommendation; `/best-route` and
      `/top-routes` return a ranked list across many terminals instead, where an empty
      result from over-filtering is a worse experience than an informational note.
    - Suspected `/top-routes` was missing the real `distance`/`score` fields `/best-route`
      shows - false alarm, caught on a fuller read of `_build_route_field`: it already
      shows both, identically.
    - Real finding: `/best-route`'s fallback path (no UEX `/commodities_routes` data for
      that commodity - a real, documented, non-rare branch) never disclosed that distance/
      travel-time isn't factored into that ranking, unlike `/mixed-routes`, which is
      *always* in that same situation and always says so. Added the identical
      cross-system-aware disclaimer `/mixed-routes` already uses, reusing
      `fallback_references`' existing `star_system_name` data - no new lookups needed.
      No new cog-level test added (no existing harness tests `/best-route`'s Discord
      rendering at all, and building one from scratch for a one-line text addition would
      cost more than the fix itself); verified by tracing the logic by hand and confirming
      the module still imports cleanly with the full suite green.
32. **`/mixed-routes`' per-commodity cargo line had an unlabeled number**: a user testing
    the bot flagged confusion over a real, arithmetically-correct example (verified against
    live UEX prices down to the exact aUEC - nothing was actually wrong) - the last bolded
    figure on each cargo line is that commodity's *profit* (`item.profit`, i.e. quantity ×
    profit-per-SCU), but the line never said so, so it read as ambiguous next to the
    aggregate Investment/Revenue line further down. Added the word "profit" next to it.
    A reminder that correct math doesn't excuse an unlabeled number in a financial embed.
33. **`system` filter added to all three route commands**: `/best-route`, `/top-routes`,
    and `/mixed-routes` all gained an optional `system` choice (Stanton/Pyro/Nyx - the
    exact three values confirmed live via UEX `/terminals`, so a fixed dropdown rather than
    free text/autocomplete). A system filter requires *both* ends of a route to be confirmed
    in the requested system - a route that crosses systems doesn't satisfy "stay in Pyro"
    just because one end happens to be there (see entry 34: `auto-load-only` was origin-only
    at the time this entry was written, but was changed to the same both-ends requirement
    shortly after).
    New shared helpers in `bot/uex/practical_routes.py`: `terminal_in_system` (one terminal)
    and `route_in_system` (both ends, fails closed on an unknown terminal, always true when
    no system is requested). `/mixed-routes` applies it to the shared row pool *before*
    origins/destinations are split out, so a filtered pairing can never mix an in-system
    origin with an out-of-system destination without a separate post-pairing check the way
    `/best-route`/`/top-routes` need (both branches of `/best-route`, and `/top-routes`'
    shared `_send_ranked_routes` helper, filter the already-paired route list directly).
    `SYSTEM_CHOICES` lives once in `bot/cogs/prices.py` and is imported into `trends.py`,
    which already imported other things from `prices.py` - not duplicated per file.
    194 tests passing (3 new): direct coverage for the two new pure helpers and for
    `build_mixed_routes`' new parameter; `/best-route`'s and `/top-routes`' cog-level
    wiring is verified the same way `auto-load-only` was (real command-tree inspection
    proving Discord actually receives the option and its three choices), not a full
    Discord-render test, since no such harness exists for either command yet.
34. **`auto-load-only` changed to require both ends, not just the origin** - explicit user
    correction of the origin-only design from entry 21 ("loading a purchase onto a stored
    ship is a buy-side concept"). This is NOT an independent re-verification against UEX's
    own docs the way the buy/sell status-code asymmetry or the quality-scale mismatch
    elsewhere in this doc were - `is_auto_load` is a plain terminal-level flag with no
    documented buy/sell scoping either way, so record this as user-directed product
    behavior, not a confirmed fact about what the field "really" means. New
    `route_supports_auto_load(origin, destination)` in `bot/uex/practical_routes.py`
    replaces the origin-only `terminal_supports_auto_load` at all three call sites
    (both `/best-route` branches, `/top-routes`' shared `_send_ranked_routes`, and
    `/mixed-routes`, which now folds it into the shared `eligible_rows` pool alongside
    `system` and `space_only` instead of `origins` alone). The one existing test that
    asserted the old origin-only behavior (`test_auto_load_only_checks_the_origin_not_the_
    destination`) was rewritten, not just supplemented, since its premise was no longer
    true. 195 tests passing.
35. **Added `/multi-stop-route`**: chains 2-3 profitable legs (origin -> stop -> ... ->
    destination) instead of the single-hop model every other route command uses. Two
    design questions were confirmed with the user before building: a one-way chain, not
    a loop back to the start, and real distance shown for context only - ranking stays
    profit-first, same as every other route command.
    While scoping this, found UEX exposes `GET /terminals_distances` (real gigameter
    distance between any two terminals, 12h cache/hourly update) that was completely
    unused anywhere in this bot - `/commodities_routes` carries its own `distance` field
    too, but only for its own precomputed single-commodity routes, which doesn't help an
    arbitrary multi-commodity, multi-leg chain the way a terminal-pair-only endpoint
    does. New `UexClient.get_terminal_distance` wraps it, cached the same way every
    other endpoint is (`_ENDPOINT_CACHE_TTL["terminals_distances"] = 12h`).
    Extracted `allocate_pair_cargo` (the greedy profit/SCU loader) and
    `build_pair_opportunities` (the filtered origin/destination pairing map) out of
    `build_mixed_routes` in `bot/uex/mixed_routes.py` into standalone, exported
    functions - pure reorganization, `build_mixed_routes`'s own behavior and its full
    existing test suite are unchanged, but the new `bot/uex/multi_stop_routes.py` reuses
    both instead of duplicating the allocation math.
    The chain search bounds itself to the ~20 most profitable single legs' endpoint
    terminals as a candidate set (no scan of every terminal in the snapshot, no extra API
    calls for the search itself), then does a bounded DFS over that small graph for
    2-3-leg simple paths (no repeated terminal - a naive search over a profitable-both-
    ways pair would otherwise happily produce A->B->A). Ship capacity resets every leg
    (cargo is fully sold before the next leg's purchase) but budget compounds forward: a
    profitable leg's revenue becomes the next leg's available capital, so a budget that
    only covers leg 1 alone can still fund leg 2 once leg 1 sells. A 1-leg result is
    excluded - that's `/mixed-routes`' job, not this command's.
    Distance is fetched live, per leg, only for the up-to-5 final routes returned (at
    most 15 calls, never for the search itself) - a missing/failed lookup never blocks
    the route from displaying, same "never let a lookup's own failure block the result"
    convention already used for marketplace item-name resolution; the route's total
    distance is marked partial instead of silently understated.
    201 tests passing (6 new, in `tests/test_multi_stop_routes.py`): a 3-terminal chain
    outranking any single hop, budget compounding across legs, no terminal ever revisited
    within one chain, a plain 2-terminal hop never appearing as a result, and a system
    filter excluding a chain whose *middle* terminal (not just an endpoint) fails it.
36. **`/multi-stop-route` silently "stuck thinking" - Discord's combined embed limit, not
    a hang**: bundling up to 5 multi-leg routes into one message (the same pattern
    `/mixed-routes` uses) hit Discord's documented combined 6,000-character limit across
    all embeds in one message, since each multi-stop route's per-leg fields carry 2-3x
    the content of a single hop. Confirmed via the live Pi's journalctl traceback, not
    guessed - `discord.HTTPException: 400 ... Embed size exceeds maximum size of 6000`,
    uncaught, so no followup ever reached Discord and the interaction just looked
    permanently "thinking." Fixed by sending one embed per route as its own followup
    message instead of batching, with a try/except `HTTPException` fallback to a short
    plain-text summary if a single route's embed is still too large on its own. No new
    tests (a Discord-send-path fix with no existing harness for it, same gap noted for
    `/mixed-routes` in entry 31); verified against the real traceback and the full suite
    staying green.
37. **Route-features audit: 4 more confirmed defects fixed, all independently
    re-verified against the code before touching anything** (a pasted external review
    flagged 6 issues total; the embed-size one was entry 36, already fixed by the time
    the review arrived):
    - `/best-route` (both branches) and `/top-routes` filtered `auto-load-only`/`system`
      *after* already ranking-and-truncating to the display size (`MAX_FIELD_ROWS`=5, or
      `TOP_SCORED_ROUTES_KEEP`=10) - a route that would pass the filter but wasn't in
      that top N was silently unreachable. `/top-routes`' version was one layer deeper
      and more serious: the 45-minute `refresh_trending()` background loop itself
      discarded ~67 of ~77 candidates by score alone, for every user, before any filter
      could ever run - no reordering inside the command could recover what the loop had
      already thrown away. Fixed by filtering the FULL candidate list first in both
      `/best-route` branches (widening the fallback branch's ranked pool via a new
      `ROUTE_FILTER_CANDIDATE_POOL`=25 constant before slicing to `MAX_FIELD_ROWS`), and
      by having `refresh_trending()` store every candidate it computes instead of
      pre-truncating, moving `TOP_SCORED_ROUTES_KEEP`/`TOP_IN_STOCK_ROUTES_KEEP` to a
      *display* cap applied in `_send_ranked_routes` after filtering (new
      `display_limit` parameter, threaded from `/top-routes`' `strict` branch).
    - `allocate_pair_cargo` (shared by `/mixed-routes` and `/multi-stop-route`, extracted
      in entry 35) picks by highest profit-*per-unit* first, which under a binding
      budget can pick substantially worse cargo than the same budget could otherwise
      earn - concrete counterexample used to build the regression test: buy 90/sell 140
      vs buy 10/sell 19, budget 100, capacity 10; profit-per-unit-first nets 59, buying
      only the cheaper commodity nets 90 with the same inputs. This is pre-existing
      `/mixed-routes` behavior, not something entry 35 introduced - multi-stop just
      calls the same allocator many more times per query, making the ceiling more
      consequential. Fixed (not a full knapsack solver over commodity subsets - a bigger
      change to a function both commands depend on) by also trying profit-*per-aUEC-
      invested* order and keeping whichever ordering earns more; ties keep the original
      per-unit-first result.
    - `build_multi_stop_routes`'s candidate-terminal ranking pass used the caller's
      *original* budget, so an edge unaffordable at the start but reachable once an
      earlier leg's profit compounds the budget could never enter the candidate graph at
      all, regardless of leg order. Fixed by ranking candidates assuming unlimited
      capital (`budget=math.inf`) - the DFS search itself already threads the real,
      path-dependent `remaining_budget` through every allocation and did not need to
      change; only which terminals are *eligible* to be searched did.
    - `MultiStopRoute.investment`/`.revenue`/`.roi_pct` summed each leg's own
      investment/revenue independently, double-counting money recycled through the
      chain - worked example: start with 1000, leg 1 nets 500 profit (1500 on hand), leg
      2 invests 1400 of that for 700 more profit; shown before: investment 2400, ROI
      50%. Real numbers: 1000 needed up front, ends at 2200, true ROI 120%. Fixed with a
      running-balance simulation over the legs (deepest cash deficit before enough
      revenue has come back to cover it = real starting capital required); `profit` and
      the `roi_pct` formula itself were already correct and needed no change.
    205 tests passing (4 new): a concrete `allocate_pair_cargo` counterexample; a
    multi-stop chain unreachable under the old original-budget-only candidate ranking,
    reachable once ranking assumes unlimited capital; and two new end-to-end cog tests
    (`tests/test_route_filter_ordering.py`, following the existing `httpx.MockTransport`
    + hand-built fake `Interaction` pattern from `tests/test_trades.py`) proving a
    lower-ranked/lower-scored but filter-passing route is still returned for
    `/best-route` and `/top-routes`. The `/top-routes` one injects the full candidate
    pool directly rather than running the real `refresh_trending()` loop (impractical
    here - needs live UEX calls across every tradeable commodity), so it proves
    `_send_ranked_routes` filters before its display cap, not that `refresh_trending`'s
    own one-line storage-limit change behaves correctly - that change is simple enough
    to be verified by reading it. The existing
    `test_builds_a_multi_leg_chain_and_sums_profit_across_legs` test's investment/
    revenue assertions were also corrected (1000/1900, not 1500/2400) since it had been
    asserting the bug.
38. **An edit tool's anchor text matched the wrong command - undetected by the full test
    suite and a live deploy, caught only when the user re-checked the deployed code.**
    Fixing `/multi-stop-route`'s embed-size bug (entry 36) used a Python-level find/
    replace anchored on the tail end of its embed-building loop plus the next
    `@app_commands.command` decorator for uniqueness. `/mixed-routes` (defined
    immediately before `/multi-stop-route` in `bot/cogs/prices.py`) ends with
    structurally identical boilerplate - `route_embed.set_footer(...)` /
    `embeds.append(route_embed)` / `await interaction.followup.send(embeds=embeds)` -
    immediately followed by that same next-command decorator, so the anchor matched
    *there* instead. Net effect: `/mixed-routes` gained dead code in an unreachable
    `except` branch referencing `path_label`/`summary_lines` - variables that only exist
    in `/multi-stop-route`'s scope, so hitting that branch would have raised `NameError`
    instead of being handled - while `/multi-stop-route` kept its original `embeds =
    []` line removed (from a separate, correctly-targeted edit in the same session) but
    never gained a replacement, so `embeds.append(route_embed)` referenced a name that
    no longer existed anywhere in the function: guaranteed `NameError` on every call
    with at least one result. Entry 36's own commit, the entry 37 audit round on top of
    it, the full 205-test suite, and a live Pi deploy all passed clean, because nothing
    exercises either command's actual Discord-send call shape - every test up to this
    point checked route-*building* logic, never what gets passed to
    `interaction.followup.send`. Fixed by moving the try/except block to
    `/multi-stop-route`'s actual ending and restoring `/mixed-routes`' original batched
    send verbatim. Added `tests/test_route_send_shape.py` - two end-to-end cog tests
    (same `httpx.MockTransport` + fake `Interaction` pattern) asserting `/multi-stop-
    route` sends one `embed=` followup per route (never a batched `embeds=` list) and
    `/mixed-routes` still sends exactly one batched `embeds=` followup. Confirmed these
    actually catch the regression, not just coincidentally pass, by temporarily
    reintroducing the broken batched code and watching the multi-stop test fail with the
    exact `NameError` this incident produced, before restoring the fix. 207 tests
    passing (2 new). Lesson: when an Edit tool's match could plausibly land in more than
    one structurally-similar location, verify the actual line numbers/surrounding
    content after the edit lands, every time - don't trust "the tool reported success"
    as proof it edited the intended target.
39. **Two of entry 37's own fixes were themselves incomplete - both confirmed by
    reproducing them directly, not by trusting the report that found them:**
    - `allocate_pair_cargo`'s two-ordering fix picks whichever ordering earns more total
      profit, but never checked how many commodities that ordering actually loads. For
      the exact A/B counterexample from entry 37 (buy 90/sell 140 vs buy 10/sell 19,
      budget 100, capacity 10), the higher-profit efficiency ordering loads only B (1
      item, profit 90) - which beats margin-first's A+B (2 items, profit 59) on raw
      profit, but `build_mixed_routes` requires 2 commodities, so the *whole route*
      silently vanished (`build_mixed_routes(...) == []`) even though a valid 2-commodity
      load genuinely existed. A single commodity that fills the ship is `/best-route`'s
      job, not a mixed load's, so preferring pure profit here was wrong. Fixed by adding
      `min_commodities` (default 1): an ordering that doesn't reach it is never preferred
      over one that does, regardless of profit; `build_mixed_routes` now passes
      `min_commodities=2`, `build_multi_stop_routes`'s per-leg calls keep the default (a
      1-commodity leg is fine there).
    - `/best-route`'s fallback fix (entry 37) widened the pre-filter candidate pool from
      `MAX_FIELD_ROWS` (5) to a fixed `ROUTE_FILTER_CANDIDATE_POOL` (25) - real progress,
      but still a cap, and `best_routes`' own `limit` parameter caps which buy/sell-side
      *terminals* get cross-joined at all, not just how many final routes come back. A
      commodity traded at more than 25 terminals on either side could still have its only
      filter-passing pair excluded before any filter ran - confirmed on the live
      collected database for 21 Stanton, 23 Pyro, and 9 Nyx commodities. Fixed by passing
      `max(len(rows), 1) ** 2` instead of a fixed constant - a genuine upper bound on
      possible profitable pairs (each row can be at most one buy AND one sell candidate),
      so it can never truncate anything, removing the now-unused
      `ROUTE_FILTER_CANDIDATE_POOL` constant entirely.
    Both fixes were reproduced directly before being trusted (a `build_mixed_routes` call
    returning `[]` for data that should produce a route; a 30-decoy-buy-terminal scenario
    where the auto-load pair ranks below any fixed cap), and both new tests were
    confirmed to actually catch their bug - not just pass - by temporarily reverting each
    fix and watching the corresponding test fail before restoring it, same discipline as
    entry 38. 209 tests passing (2 new, in `tests/test_mixed_routes.py` and
    `tests/test_route_filter_ordering.py`).
40. **4 more confirmed defects from continued review of entries 37-39, all reproduced
    directly (not assumed) before touching code, and every new test verified to actually
    catch its bug by reverting the fix first:**
    - `/top-routes`' background loop kept only the single highest-scored route *per
      commodity* (`select_best_available_route`/`select_best_in_stock_route`, both
      `max(candidates, key=score)`) - entry 37's fix (keep every commodity, not just the
      top 10 overall) never addressed this: if a commodity's one kept route failed
      `auto-load-only`/`system`, there was no second-best for that same commodity to
      fall back to, since nothing else was ever kept anywhere. Renamed to
      `select_available_routes`/`select_in_stock_routes`, now returning every qualifying
      route per commodity sorted by score. `_send_ranked_routes` dedupes back to one
      route per commodity *after* filtering (entries are already score-sorted, so
      keeping the first occurrence per commodity keeps the highest-scoring survivor) -
      preserves the "one route per commodity" display property while letting a
      same-commodity alternative surface when the top choice is filtered out.
    - `build_multi_stop_routes`'s candidate-terminal ranking (entry 37: rank assuming
      unlimited capital, so budget-compounded-but-later-affordable edges stay reachable)
      could itself be crowded out: 21 decoy edges needing far more capital than any
      realistic chain would ever compound to (individually unaffordable at the real
      budget) scored enormous at unlimited budget and pushed a genuinely
      immediately-affordable 2-leg chain out of the bounded top-20 candidate window
      entirely - `build_multi_stop_routes(...)` returned `[]` for data that should
      produce a route. Fixed by ranking candidates *twice* - once at unlimited budget,
      once at the real starting budget - and taking the union of both top-20 lists (only
      when a real budget was given; with none, the second ranking would be an identical,
      wasted recomputation).
    - `allocate_pair_cargo`'s two-greedy-orderings approach (entry 37) is meaningfully
      non-optimal, not just in the one counterexample it was built to fix - a random
      search over 20,000 small 2-commodity scenarios (capacity 9, budget 51) found cases
      over 2x off optimal (57 vs 157 profit). Added `_exact_allocate`: for a small enough
      candidate set and capacity (`EXACT_SEARCH_MAX_CANDIDATES`=8,
      `EXACT_SEARCH_MAX_CAPACITY`=25 - measured ~65-85ms at those thresholds, climbing
      sharply past them), brute-force every subset of size min_commodities..max_commodities
      and every quantity split of all-but-one item in the subset (bounded by capacity,
      since a unit of any commodity always costs exactly 1 SCU), choosing the last item's
      quantity greedily from whatever remains - provably optimal for a fixed subset, and
      exhausting every subset finds the true global optimum. Larger cases keep the
      existing two-ordering approximation - `build_multi_stop_routes`' search can call
      this thousands of times per command, so unbounded exactness isn't affordable there;
      this is a deliberate, documented speed/optimality trade-off, not a claim of
      universal optimality. One existing test's premise (`budget=400` finds no route) no
      longer held once the exact solver could find a real, valid, better one the old
      greedy-only code missed entirely - updated to a budget genuinely below the cheapest
      possible 2-item combination instead.
    - `/multi-stop-route`'s embed-too-large fallback (entry 38) carried
      `summary_lines` (investment/revenue/profit/ROI/distance/confidence) into the
      plain-text fallback but silently dropped `warnings` (risk flags, stock/demand
      limits, practical notes, health) - confirmed by reading the fallback's own variable
      references, then reproducing with a stock-limited leg whose warning never made it
      into the fallback text. Fixed by running the fallback's lines through the same
      `_chunk_lines` helper the embed fields already use (capped at 1900, under
      Discord's separate 2,000-character plain-message limit, not the embed limit this
      fallback exists to route around), sending as many messages as it takes rather than
      dropping anything.
    215 tests passing (6 new): the 21-decoy multi-stop crowding case; the exact-solver's
    171-vs-76-profit case plus one confirming graceful fallback past the exact-search
    thresholds; two `/top-routes` cog-level cases (a same-commodity fallback, and a
    dedupe case proving two passing routes for one commodity still collapse to just the
    higher-scored one); and the multi-stop fallback-preserves-warnings case.
41. **2 more confirmed defects, this time reproduced directly against the real
    collected `data/uexbot.sqlite3` snapshot (2593 rows), not just synthetic data -
    entry 40's own fixes had real-world edges that synthetic test cases hadn't
    surfaced:**
    - `build_multi_stop_routes`' shared `MAX_CHAINS_EXPLORED` budget (2000) was consumed
      in whatever order the `opportunities` dict happened to iterate in, not by how
      promising each branch actually was. On the real snapshot, a 24-SCU ship with a
      100,000-aUEC budget found only a 323,124-profit chain while a genuinely valid
      426,056-profit chain existed - increasing only the search allowance (with nothing
      else changed) found it, confirming the cutoff itself, not a missing route, was the
      cause. The real candidate graph for that data (~30 terminals, ~150 edges) needed
      on the order of 20,000 edge-considerations to exhaust itself, with measured search
      time staying *flat* (~0.3s) even at 100x that - the graph's own size bounds the
      real work regardless of the ceiling. Fixed two ways together: raised
      `MAX_CHAINS_EXPLORED` to 50,000 (comfortable headroom above the measured real
      need), and made exploration order itself profit-prioritized - both each node's
      outgoing edges and which terminal to start from are sorted by profit potential
      (from the existing unlimited-budget ranking pass) descending, so a bounded budget
      is spent on the most promising branches first regardless of dict/set iteration
      order, and a truncated search finds a near-best result even in some future case
      this measurement didn't cover.
    - `allocate_pair_cargo`'s exact solve (entry 39) stops above
      `EXACT_SEARCH_MAX_CAPACITY` (25 SCU) and falls straight to the two-ordering
      heuristic - meaning a *bigger* ship could silently score worse than a smaller one
      on identical data, confirmed on the real snapshot: 25 SCU returned 59,404 profit,
      26 SCU returned 49,360, even though the better 25-SCU load still physically fits
      in 26 SCU of capacity. Fixed by always trying a capped exact solve (as if capacity
      were the threshold) as one candidate even above the threshold, comparing it
      against the heuristic's full-capacity result and keeping whichever earns more -
      the capped solution is always a valid allocation for the larger ship too (it just
      doesn't try to use the extra capacity), so this can only help, never hurt. Also
      added `allocation_is_exact()` and a footer disclosure on `/mixed-routes` and
      `/multi-stop-route` ("cargo allocation above 25 SCU is approximate, not
      proven-optimal") for the cases where even the capped-plus-heuristic result still
      isn't a proven optimum - the review's second point, that the approximation went
      undisclosed in the commands' own wording, was also valid on its own.
    Both were reproduced against the real data first (patching a stale local snapshot's
    schema to add a since-added column rather than trusting a synthetic guess), and both
    new tests were confirmed to actually catch their bug - not just pass - by temporarily
    reverting each fix and watching the test fail before restoring it, same discipline as
    entries 38-40. Constructing a *synthetic* regression test for the traversal-order bug
    took real care: Python's set iteration order for small ints turned out not to follow
    insertion order or numeric order in any way that was easy to predict or lean on, so
    the working version instead controls order directly through a dict-insertion-order
    trick (decoy edges inserted before the real one, all from a shared origin) rather
    than relying on set/hash behavior at all. 218 tests passing (3 new).
42. **3 more confirmed defects, again independently reproduced (two directly against real
    numbers/behavior, not just synthetic data):**
    - `_exact_allocate` (entry 39) computed each candidate's `available` (stock/demand
      limit) as `min(scu_buy, scu_sell, capacity)` - folding the solver's own search
      capacity into a value that also became the reported `available_scu`, so a
      "stock/demand limits this load to N SCU" warning showed the solver's cap, not the
      real market figure. Confirmed against the real snapshot: Astatine (1,570 SCU real
      demand) and Quartz (55 SCU real stock) both reported as capped to 25 SCU. Fixed by
      splitting the single value into `market_available` (real stock/demand, used for the
      returned `MixedCargoItem.available_scu` and never touched by capacity) and
      `search_bound = min(market_available, capacity)` (used only to bound the
      combinatorial search's ranges) - the search space explored is identical to before,
      only the reported number changed.
    - Cargo allocation (`allocate_pair_cargo`, in particular entry 40's "always try a
      capped exact solve" fix) runs a real combinatorial search per candidate route and
      can take meaningful wall-clock time on dense-enough data - reproduced at ~15s for a
      fully-connected 8-terminal/8-commodity synthetic snapshot via `/mixed-routes`
      end-to-end (a single `_exact_allocate` call at 8 candidates/capacity 25 alone
      measured ~0.12s, and `build_mixed_routes` calls it once per origin/destination
      pair - 56 pairs for that snapshot). Both `build_mixed_routes` and
      `build_multi_stop_routes` were called directly on the coroutine handling the
      interaction, so that cost ran synchronously on the bot's one asyncio event loop,
      freezing every other interaction and background poller for the whole duration -
      confirmed experimentally with a concurrent heartbeat coroutine that ticked zero
      times during an equivalent direct synchronous call but kept ticking once offloaded.
      Fixed by wrapping both calls in `await asyncio.to_thread(...)` in `bot/cogs/prices.py` -
      moves the CPU-bound work off the event loop without touching the algorithm itself
      or its optimality guarantees. The regression test checks this directly (the actual
      thread `build_mixed_routes`/`build_multi_stop_routes` runs on must not be
      `threading.main_thread()`) rather than via timing, since a timing/heartbeat-based
      version passed even without the fix - ticks accumulated from unrelated awaits
      earlier in the same command (fetching vehicles, market rows) gave a false pass
      before the blocking call was ever reached.
    - The "cargo allocation is approximate" disclosure (entry 41) checked only
      `ship_vehicle["scu"] > EXACT_SEARCH_MAX_CAPACITY`, so a small ship choosing among
      more than `EXACT_SEARCH_MAX_CANDIDATES` (8) commodities at one stop - which also
      forces the pure two-ordering heuristic, no exact solve at all - got no disclosure.
      It also lived only in the embed footer, so a route needing the plain-text fallback
      (embed too large) silently lost it. Fixed by giving `MixedRoute` and
      `MultiStopLeg`/`MultiStopRoute` their own `is_exact` (from the already-existing but
      previously-uncalled `allocation_is_exact(num_pairs, capacity)`, computed once per
      edge/leg at build time; a chain's `is_exact` is `all(leg.is_exact for leg in legs)`
      - only as exact as its least-exact leg) and switching both the cog's footer checks
      and the multi-stop fallback's line-list to `if not route.is_exact`.
    Building the dense-mixed-route test fixture required a real fix mid-session: an
    initial version used two separate rows per (terminal, commodity) - one buy-only, one
    sell-only - which looked fine passed directly to `build_mixed_routes`, but collapsed
    to just the second row once round-tripped through
    `record_terminal_market_snapshot`/`get_mixed_route_market_rows`, since the DB's
    upsert key is `(id_commodity, id_terminal)` and the real schema expects one row per
    pair carrying both buy and sell fields together - caught because the cog-level test
    returned "no routes found" in under a millisecond instead of taking real time.
    All three fixes were confirmed to actually catch their bug - not just pass - by
    temporarily reverting each one and watching the corresponding test fail before
    restoring it, same discipline as entries 38-41. 224 tests passing (6 new).
43. **Entry 42's two `build_mixed_routes`-caller fixes were incomplete - a third caller,
    `/intelligence-brief`, was missed entirely.** `bot/cogs/intelligence_brief.py`'s
    `_routes_embed` calls the same `build_mixed_routes` as `/mixed-routes` and
    `/multi-stop-route` but wasn't touched in entry 42: it still called it directly
    (not offloaded, so a dense snapshot could stall the bot exactly like the fixed
    commands used to), and never checked `route.is_exact` at all, so a recommendation
    could be an unproven approximation - a controlled example matching an earlier
    allocator counterexample (75 vs a feasible 189 profit) - with no warning, unlike
    `/mixed-routes`' footer for the same case. Both fixed the same way as entry 42:
    `await asyncio.to_thread(build_mixed_routes, ...)`, and an
    `if not route.is_exact: notes.append(...)` alongside this function's other
    per-route conditional warnings (risk labels, unknown risk metadata, cross-system).
    New test file `tests/test_intelligence_brief_routes.py` (this cog had no
    request-level test harness before) - a thread-identity check for the offload (same
    approach as entry 42, not timing-based) and a disclosure check reusing the existing
    `/mixed-routes` 30-SCU-ship fixture shape. Both confirmed to actually catch their bug
    by reverting and restoring each fix. The lesson generalizes: when a shared helper
    gets a caller-side fix (offloading, a new disclosure field), grep for *every* caller
    before considering it done, not just the one(s) the original report named. 226 tests
    passing (2 new).
44. **Live incident: the daily digest showed terminal-market data as "overdue" (3h15m
    since the last snapshot) even though `intelligence.py`'s collector runs every 2h.**
    Diagnosed from `journalctl` on the Pi: at 17:54:41, the terminal-market snapshot
    failed with `sqlite3.OperationalError: database is locked` (caught and logged, not
    fatal - see `bot/cogs/intelligence.py`'s existing try/except - but it silently
    dropped that collection cycle). Root cause: `intelligence.py`'s 1h (data-health) and
    2h (terminal-market) loops both start counting from the same bot-startup moment, so
    they coincide every 2 hours - confirmed directly in the logs (both fire within ~1
    second of each other at 01:54, 03:54, 05:54, ... every odd hour). Each opens its own
    `aiosqlite` connection via `Database.connect()`/`init()`, which never set
    `PRAGMA journal_mode` or `PRAGMA busy_timeout` - meaning every connection ran on
    aiosqlite/sqlite3's own implicit default (rollback-journal mode, 5s busy timeout).
    Reproduced standalone: two connections to the same file, one holding a write
    transaction open, the second's write fails at ~5.5s with the exact same error. Most
    2h coincidences (8 of 9 that day) resolved fine within that 5s window; this one
    didn't, most likely because terminal-market's own write (up to 2,593 rows via
    `executemany`) is the single largest write in the whole app and can occasionally run
    long enough to blow past 5s when it overlaps another writer. Fixed in
    `bot/db/database.py` with a new `_configure_connection()` (called from both `init()`
    and `connect()`, since WAL persists in the file itself but `busy_timeout` is a
    per-connection runtime setting that resets on every new connection): enables
    `PRAGMA journal_mode=WAL` (also makes each commit itself faster/cheaper, shrinking
    the window a lock is held at all) and raises `busy_timeout` to 30s. New
    `tests/test_database_concurrency.py`: a fast deterministic PRAGMA-value check, plus
    a real concurrency test holding a write lock for 6s (deliberately past the old 5s
    default, to prove the fix covers contention the old implicit default wouldn't have,
    not just contention either default would tolerate) - both confirmed to fail with the
    exact production error when the fix is temporarily reverted. Side benefit: the full
    test suite got noticeably faster (roughly halved) purely from WAL's cheaper commits.
    228 tests passing (2 new).
45. **A full project audit (`data/full-audit-20260905/AUDIT_REPORT.md`, 15 findings: 4 P1,
    11 P2) landed 2026-09-05 - all 4 P1s fixed and independently verified same day**,
    each confirmed against real code first, then reproduced failing before the fix and
    passing after (fix temporarily reverted, test re-run, restored):
    - **A01 - a rejected listing deletion was reported as successful.** `client.py`'s
      `_request` only ever raised for `_AUTH_ERROR_STATUSES` or a literal `"error"`
      status; any other status (built for GET's soft "nothing matched" cases like
      `no_trades_found`) fell through to "log and return data" - so DELETE
      `/marketplace_listings` rejecting with a real documented status like
      `user_not_verified` (confirmed against `docs/UEX_API_2.0_reference.md`'s own DELETE
      status list) returned normally with `data: null`, and `_cancel_listed_job` reported
      the listing deleted and released the reservation even though nothing was actually
      deleted on UEX. Fixed by making `_request` require an explicit `"ok"` status for
      POST/DELETE specifically (both of this client's only two write endpoints document
      exclusively real rejection reasons alongside `"ok"` - no soft-empty-result case
      exists for either) while leaving GET's existing soft-status handling untouched.
      `tests/test_client_write_status.py` (new) plus
      `tests/test_personal_inventory.py::test_rejected_delete_must_not_release_inventory`.
    - **A02 - a double-click could post the same listing twice.**
      `ConfirmListingView.confirm` (`bot/cogs/marketplace.py`) set `self.resolved = True`
      but never checked it - two already-dispatched callbacks could both get past that
      line before either's `edit_message` round-trip disabled the button on Discord's
      side, so both reached the real POST. Fixed with a check-before-set guard on both
      `confirm` and `cancel`: safe because asyncio is single-threaded and nothing awaits
      between the check and the set, so the second callback to actually run always
      observes the first one's write. `tests/test_marketplace.py::
      test_confirm_listing_view_only_posts_once_on_concurrent_double_click` reproduced
      `await_count == 2` before the fix.
    - **A03 - confirming an uncertain POST's sale could queue a live duplicate.**
      `inventory_confirm_sale` auto-relisted a job's unsold remainder whenever
      `auto_relist` was set, with no check on whether the *original* POST's outcome was
      ever actually confirmed. A job only reaches `needs_confirmation` with `listing_id`
      still NULL via `mark_inventory_post_failed(ambiguous=True)` (a network error or
      missing `id_listing` right after the POST) or `flag_stale_inventory_post_jobs` - in
      both cases UEX's own acceptance of that POST was never verified, so a live,
      untracked listing may already exist. Every *other* `needs_confirmation` path (both
      call sites of `mark_inventory_post_needs_confirmation`) only fires after
      independently observing an empty `GET /marketplace_listings` for that `listing_id` -
      i.e. already confirmed gone - so relisting there was always safe and had to stay
      allowed. Fixed in `confirm_ambiguous_inventory_sale` (`bot/db/database.py`): compute
      `original_listing_unresolved = job["listing_id"] is None` and AND it into the
      returned `auto_relist` flag; the cog surfaces a distinct message telling the user to
      manually check UEX for a stray duplicate before using `/inventory-sell` themselves
      in that case. Releasing the local reservation stays unconditional either way (it
      never touches UEX, so it's always safe) - only the automatic *new POST* is gated.
      `tests/test_personal_inventory.py`:
      `test_uncertain_post_cannot_relist_without_resolving_live_listing`,
      `test_uncertain_post_message_tells_the_user_to_check_uex_manually`, and
      `test_resolved_ambiguous_listing_can_still_auto_relist` (the contrast case, proving
      the fix doesn't over-block the safe path).
    - **A04 - a floor raised during posting could be silently ignored.**
      `set_inventory_minimum_price` deliberately excludes jobs in `'posting'` status from
      its `UPDATE` (a currently-posting job's in-flight write shouldn't be edited out from
      under it) - but `_post_one_job` then used that same frozen `job["minimum_price"]`
      snapshot (read before `claim_inventory_post_job` even ran) for both the custom-price
      floor check and the live-pricing floor, so a floor raised anywhere during the
      pricing fetch's real network round trips (`_fetch_live_price`) was never honored by
      the actual write. Fixed by re-reading the item's live `minimum_price` from
      `get_inventory_item` right before building the POST payload (after pricing
      completes, not before) and re-flooring the recommendation against that live value
      via `dataclasses.replace` (`PriceRecommendation` is frozen) - applies to both the
      custom and computed pricing paths, closing the window regardless of which one hit
      it. `tests/test_personal_inventory.py::test_floor_increase_during_pricing_is_respected`
      and `test_custom_price_still_checked_against_a_floor_raised_during_posting`.

    The remaining 11 P2 findings (recovery fidelity, negotiation-alert scoping, embed
    total-size budgeting, scanner sold-out filtering, data-health staleness, a transient
    DB error killing a collector task, and three deploy/revert-script gaps) are not yet
    fixed - see the audit report for full detail and suggested repair order. 238 tests
    passing (10 new).
46. **All 11 P2 findings from the same audit (entry 45) fixed and independently verified
    the same session** - each confirmed against real code, reproduced failing before its
    fix and passing after (temporarily reverted, test re-run, restored):
    - **A05 - a custom relist price silently became 0.** `confirm_ambiguous_inventory_sale`
      retained `pricing_strategy: custom` into its returned dict but dropped
      `custom_price` entirely; `inventory_confirm_sale`'s replacement-job spec then never
      included it, so `create_inventory_post_jobs` defaulted it to 0 and rejected the
      replacement as below the floor - the user's stack fell out of the automatic system
      with no explanation beyond "could not be rescheduled." Fixed by carrying
      `custom_price` through both the DB method's return value and the cog's spec dict.
      `tests/test_personal_inventory.py::test_custom_price_survives_relist_after_ambiguous_sale_resolution`.
    - **A06 - one user's seen-state suppressed a different user's pending notification.**
      `negotiation_message_seen` was keyed on a bare `message_id PRIMARY KEY` - genuinely
      global, not per-recipient, confirmed straight from the schema. Two different Discord
      users each independently watching the same negotiation (opposite sides, or even both
      watching the same listing) shared one row per message, so whichever user's baseline
      seed or delivered DM reached it first silently marked it seen for the other too.
      Fixed with a `(user_id, message_id)` composite primary key, via the same
      detect-and-rebuild pattern as `_migrate_pricing_strategy_check`
      (`_migrate_negotiation_message_seen_scope`, `bot/db/database.py`) - pre-migration
      rows carried no per-user attribution at all, so each old message_id is copied to
      every user who had alerts enabled *at migration time* (the closest safe
      approximation: it can't under-notify anyone who already saw it via a real poll, and
      a user enabling alerts afterward always re-seeds from scratch regardless of this
      table's contents anyway). `is_negotiation_message_seen`/`mark_negotiation_message_seen`
      now take `user_id`; both call sites in `bot/cogs/negotiation_alerts.py` updated.
      `tests/test_negotiation_alerts.py::test_negotiation_message_seen_is_scoped_per_user`.
    - **A07 - a failed DM was recorded as delivered.** `_notify_user` swallowed
      `discord.HTTPException` (DMs closed, a transient outage) and returned nothing, but
      its caller (`_check_negotiation`) still called `mark_negotiation_message_seen`
      unconditionally right after - a permanently-discarded notification the instant
      `send()` raised, with no path back. Fixed by making `_notify_user` return whether
      delivery actually succeeded, and only marking the message seen when it did; an
      unseen message naturally gets retried on the next 5-minute poll cycle - a real,
      bounded retry, not a special-cased mechanism.
      `tests/test_negotiation_alerts.py::test_failed_dm_delivery_is_retried_not_permanently_discarded`.
    - **A08 - per-field/name limits (1024/256 chars) don't protect Discord's separate
      6000-char TOTAL embed limit.** Confirmed on both cited surfaces: ten valid inventory
      entries reached 7,642 characters, and ten warning-rich routes reached 10,078 -
      neither had any single field anywhere near its own 1024-char cap, so nothing existing
      would have caught it, and Discord rejects the entire send in that case (losing every
      field, not just the overflow). Fixed differently per surface, matching each one's
      existing UX rather than forcing a shared shape: `/inventory` already has real
      page/page_count navigation, so `_paginate_inventory_fields`
      (`bot/cogs/personal_inventory.py`) now splits pages on the total-char budget *in
      addition to* the existing 10-row cap - nothing is dropped, a page that would overflow
      just becomes two pages. `/top-routes` (`_send_ranked_routes`, `bot/cogs/trends.py`)
      has no such paging concept and sends one embed per call, so the shared
      `_add_chunked_fields` helper (`bot/cogs/prices.py`, used by several route commands)
      now returns `False` the moment adding a chunk would push the embed's total past
      budget (measured via discord.py's own `len(embed)`, the same total Discord enforces);
      `_send_ranked_routes` stops adding further routes at that point and appends
      "N more route(s) omitted - message size limit" to the footer, so a truncation is
      visible rather than a silent gap. Routes are already score-sorted, so what's kept is
      always the best-ranked subset. New `tests/test_trends_embed_budget.py` and
      `tests/test_personal_inventory.py::test_inventory_page_fits_the_total_embed_limit_even_with_long_fields`.
    - **A09 - a sold-out listing could be flagged as a live steal.** `find_steals`
      (`bot/uex/scanner.py`) compared price against the fair-price index with no
      availability check at all - a listing marked sold out with zero remaining stock
      still qualified as a 90%-below-average deal, even though nothing is actually
      purchasable at that price. Fixed with an `is_sold_out`/`in_stock<=0` exclusion,
      reusing `_flag` from `bot/uex/inventory.py` rather than duplicating the same
      string/bool/int parsing a second time. An unreported (missing) stock value is *not*
      treated as zero - only a confirmed unavailability excludes a listing.
      `tests/test_scanner.py` gained 4 new cases (sold-out, zero-stock-not-flagged,
      still-flags-in-stock, unreported-stock-not-excluded).
    - **A10 - stored terminal health never aged between collections.**
      `classify_terminal_health` (`bot/uex/data_health.py`) classified purely from UEX's
      own age/TTL fields, captured once at collection time - if the hourly data-health
      collector stops running (a crash, a bug, an outage), the last successfully stored
      row keeps whatever numbers UEX reported back then forever, so a terminal not
      actually re-checked in days could still read "fresh." Fixed by comparing the row's
      own `last_seen` (this bot's local collection timestamp, always present on
      `terminal_data_health_state`) against a new `now` parameter (defaults to real time,
      injectable for tests): past `LOCAL_COLLECTION_STALE_HOURS` (6h - several missed
      cycles' worth of slack above the collector's own 1h interval, not a tight cutoff), a
      "fresh"/"recent" classification downgrades to "unknown" - not "stale", since this is
      doubt about the bot's own data, not a claim that UEX's underlying prices expired; an
      already-stale/limited classification is left alone, since it carries more specific
      information than a generic "unknown" would. `tests/test_intelligence.py` gained 4
      cases (old collection → unknown, recent collection unaffected, missing `last_seen`
      unaffected, staleness never overrides an existing stale/limited status).
    - **A11 - a single recent supply/demand change could disappear entirely.**
      `get_terminal_market_shifts` (`bot/db/database.py`) first restricted to the 24h
      window, then required 2+ observations *inside* it - since this table records changes
      rather than periodic samples, a long-stable market with exactly one recent change
      has only one in-window row and was silently dropped, however large that change was.
      The first fix attempt (compare against the closest baseline strictly *before* the
      window) broke an existing, equally valid case: a commodity/terminal pair only ever
      observed recently, with no earlier baseline at all (nothing predates the window,
      e.g. newly tracked) - `test_terminal_market_shifts_compare_oldest_and_newest_observation`
      caught this immediately (both its observations sit inside a 2-hour span). The actual
      fix ranks candidate baselines in priority order per pair - prefer the most recent
      observation at or before the window start, however old; fall back to the earliest
      observation still inside the window otherwise - via one combined ranked CTE rather
      than two separately-joined ones, so both cases resolve correctly in a single query. A
      pair with no observation earlier than its own latest one (a single-ever data point)
      still has no baseline candidate and is correctly excluded, same as before. Two new
      tests in `tests/test_intelligence.py` (single-change-against-an-old-baseline,
      single-observation-pair-excluded) alongside the pre-existing multi-observation test,
      all three now passing together.
    - **A12 - a transient DB error could permanently stop marketplace collection.**
      `snapshot_item_activity` (`bot/cogs/marketplace.py`) only wrapped its UEX fetch in
      try/except; the DB write (`upsert_marketplace_item_activity`) and two further steps
      (`upsert_marketplace_tier_stats`, the summary-logging queries) sat unguarded -
      `discord.ext.tasks`' own auto-reconnect only covers a specific set of network
      exceptions, not arbitrary ones, so an uncaught `sqlite3.OperationalError` (e.g. a
      lock collision with another collector writing at the same moment) permanently killed
      this hourly loop until the bot was restarted, confirmed by directly starting the real
      decorated task with an injected error and checking `.failed()`. Fixed by wrapping all
      three remaining unguarded steps in their own try/except, matching the pattern
      `update_liquidity_scores` already used - one failed step no longer prevents the
      others from running, and no failure here ever escapes the loop.
      `tests/test_marketplace.py::test_transient_database_error_does_not_kill_marketplace_collector`.
    - **A13 - deployment's missing-pip abort bypassed its own rollback.** bash's `ERR`
      trap does not fire for an explicit `exit N` (only for a command that itself fails
      under `set -e`) - `deploy_and_backup.sh`'s missing-virtualenv branch calls `exit 1`
      directly, which `trap rollback_on_failure ERR` silently let bypass the promised
      rollback entirely, leaving the service stopped with no recovery attempted. Fixed by
      switching to `trap rollback_on_failure EXIT`, which fires on every termination path
      (a failing command, an explicit exit, or normal completion) - `rollback_on_failure`'s
      own `DEPLOY_SUCCEEDED`/`SERVICE_STOPPED` guards are what keep it a no-op on success,
      not the trap type, so this needed no other change. Verified directly: a throwaway
      harness reproducing the same trap/guard structure confirmed rollback now fires on an
      injected `exit 1` where it previously wouldn't have.
    - **A14 - revert still assumed the wrong Pi virtualenv path.** Unlike
      `deploy_and_backup.sh` (fixed in entry from a prior session), `revert_last_deploy.sh`
      still hardcoded `.venv/bin/pip`, so a requirements-changing revert on the Pi (which
      uses `venv`, no dot) would fail after the code/DB were already restored but before
      the service restarted. Fixed with the identical `.venv`-then-`venv` detection.
      Verified directly (three branches - `.venv` present, `venv` present, neither -
      confirmed to resolve correctly via a throwaway harness using `-f` checks, since
      Windows Git-Bash's `/tmp` doesn't honor `chmod +x` for a reliable `-x` test - same
      known limitation as before; the real Linux Pi is unaffected).
    - **A15 - revert wrote a backup its own loader couldn't read.** The pre-revert
      snapshot `revert_last_deploy.sh` writes before undoing a revert (so the revert itself
      is undoable) only recorded `commit`/`db_path` into `meta.txt`, but the loader
      unconditionally expands `timestamp_utc` and `branch` under `set -u` - reverting to
      *that* snapshot later (undoing the undo) crashed with `timestamp_utc: unbound
      variable` before touching anything. Fixed by writing the same complete 4-field
      format `deploy_and_backup.sh` already uses, and defensively defaulting the two
      informational-only fields (`${timestamp_utc:-unknown}`, `${branch:-unknown}`) at
      their one point of use, so a meta.txt from any other source missing just those two
      degrades gracefully instead of crashing - `commit`/`db_path` stay hard-required via
      the pre-existing explicit checks, since the revert logic actually needs them.
      Verified directly: the fixed writer's output loads cleanly, the defensive defaults
      handle the old incomplete format, and removing the defaults reproduces the exact
      original crash against that same old format.

    256 tests passing (18 new since entry 45).
47. **A follow-up review of `b1170e8`/`bd2d50b` (`data/audit-bd2d50b/REVIEW.md`, 2026-09-05)
    found 5 P2 gaps INTRODUCED BY those two fix rounds themselves - not new independent
    defects, but places where a fix was subtly incomplete or its own regression tests
    happened to bypass the exact mechanism that still had a hole.** All 5 fixed and
    verified the same session, each confirmed to fail without its fix and pass with it:
    - **A failed DM still let the negotiation checkpoint advance.** The A07 fix (entry 46)
      made `_check_negotiation` skip marking a failed-delivery message as seen, but the
      function still unconditionally `return`ed `True` - so `poll_negotiation_messages`
      still advanced that negotiation's `date_modified` checkpoint. Since the poller skips
      any negotiation whose `date_modified` hasn't moved past its checkpoint, the
      undelivered message was never retried unless the negotiation got further, unrelated
      activity later. A07's own regression test called `_check_negotiation` directly,
      bypassing the poller's checkpoint gate entirely - missing exactly this interaction.
      Fixed by tracking `all_delivered` across the loop and returning that instead of a
      hardcoded `True`. New test drives two real `poll_negotiation_messages` cycles (not
      `_check_negotiation` directly) with delivery failing then succeeding.
    - **The A11 (entry 46) fallback baseline picked the wrong end of the window.** The
      unified ranked-candidates CTE ordered every tier `... DESC` - correct for the
      pre-window-baseline tier (want the closest one to the window boundary, i.e. most
      recent), but wrong for the in-window fallback tier, which needs the EARLIEST
      in-window observation, not the most recent. A newly tracked pair with 3+ in-window
      observations (100 @ -3h, 600 @ -2h, 200 @ -1h = latest) picked -2h (600) as
      "baseline" instead of -3h (100), reporting -400 instead of the correct +100 - the
      existing 2-observation test couldn't distinguish "most recent other row" from
      "earliest row" since with only 2 rows they're the same row. Fixed by splitting back
      into two separately-ranked CTEs (`pre_window_baseline` DESC, `in_window_earliest`
      ASC) and `COALESCE`-preferring the pre-window one - each tier gets the ordering it
      actually needs. Also made the window boundary consistent with the "at or before"
      policy stated in the fix's own docstring (`windowed` now `>`, `pre_window_baseline`
      now `<=`, complementary rather than both using non-matching comparisons). New test
      reproduces the exact 3-observation case; the 2-observation and single-observation
      tests from entry 46 still pass unchanged.
    - **A confirmed rejection was still classified as ambiguous.** The A01 fix (entry 45)
      made `UexClient._request` raise for any non-"ok" POST/DELETE status, but
      `_post_one_job`'s classifier still matched `str(exc)` against the OLD message
      prefixes (`"uex api error"`, `"uex auth error"`, `"quota reached"`) to decide
      "definitely rejected" vs. "ambiguous" - the new `"UEX rejected POST ..."` message
      never matched any of them, so a definite, documented rejection
      (`user_active_listings_limit_reached`) was treated as ambiguous, reserving inventory
      and telling the user to manually check for a listing UEX never created. Root cause:
      classifying an outcome by parsing exception message text is inherently fragile to
      message-format changes - exactly what A01 did while fixing something else entirely.
      Fixed structurally: new `UexRejectedError(UexApiError)` (`bot/uex/exceptions.py`)
      marks a response UEX explicitly and definitely rejected; `UexAuthError` and
      `UexRateLimitError` now inherit from it (both were already treated as definite by
      the old string classifier); `_request`'s two raise-on-rejection sites now raise
      `UexRejectedError`; `_post_one_job` now checks `isinstance(exc, UexRejectedError)`
      instead of message prefixes. Stable against any future message-text change.
    - **The A08 (entry 46) route-embed budget didn't reserve for the real footer.**
      `_add_chunked_fields`'s 100-char reserve assumed only a short truncation notice would
      follow, but `/top-routes`' real footer (explanation + refresh timestamp + ship note)
      is attached AFTER the field loop and can run well past 100 characters - confirmed to
      let the final assembled embed land at 6,009 characters (254-char footer) with
      longer-than-fixture terminal names. Fixed by computing and attaching the real footer
      BEFORE the field loop in `_send_ranked_routes`, so `_add_chunked_fields`' own
      `len(embed)` check already reflects it; the omission-count suffix is appended
      afterward as a short, bounded addition the existing reserve still covers. Applied the
      same reordering to `/best-route`'s two branches and `/multi-stop-route`'s per-route
      embed in `bot/cogs/prices.py` for consistency, since all three had the identical
      footer-after-loop shape (`/multi-stop-route`'s already has a full plain-text fallback
      on `discord.HTTPException` that independently guarantees no data loss even before
      this fix, so it was lower risk, but still worth correcting to avoid needlessly
      triggering that fallback). New test sweeps terminal-name padding 0-150 checking
      `len(embed) <= 6000` throughout, reproducing the exact 6,009-char/254-char-footer
      case from the review at padding=80.
    - **The A08 budget check could keep a route but silently drop its trailing warning.**
      `_add_chunked_fields` added each chunk of a logical field one at a time, checking
      budget before each - so a route whose first chunk (price/summary) fit but whose
      second chunk (a cargo-risk warning, which lands in a trailing continuation chunk)
      didn't would end up with its first chunk visible and its warning silently missing.
      The caller only tracks whole-route omission, so this route wasn't even counted as
      "omitted" in the footer - it looked like a normal, fully-displayed, warning-free
      (i.e. safe-looking) route. Fixed by making `_add_chunked_fields` preflight the total
      length of ALL chunks before adding any of them - all-or-nothing, never partial. New
      `tests/test_prices_chunked_fields.py` unit-tests the helper directly (reproducing the
      review's exact byte-length fixture); `tests/test_trends_embed_budget.py` adds an
      end-to-end sweep confirming every displayed route field carries "Cargo risk:" text,
      reproducing the review's route-5-visible-without-its-warning case exactly.

    All 6 of the review's own probes (`data/audit-bd2d50b/test_fix_review.py`) pass against
    the fixes. 264 tests passing (8 new).
48. **A second follow-up review of `b95390c` (`data/audit-b95390c/REVIEW.md`, 2026-09-05)
    found 2 more P2 gaps - both regressions from entry 47's own fixes, the same pattern as
    entry 47 itself.** Both fixed and verified the same session:
    - **`/multi-stop-route`'s warnings section could vanish with the route embed still
      "successfully" sent.** Entry 47's atomic `_add_chunked_fields` fix is exactly right
      for a per-route logical field (never show a route with its warning silently
      missing) - but `/multi-stop-route` also uses it for ONE call covering the entire
      accumulated warnings section for a route, and ignored its return value. If the leg
      fields + route summary already consumed most of the 6000-char budget, the warnings
      call could return `False` and add NOTHING - the resulting smaller, warning-free
      embed then sends without raising `discord.HTTPException`, so the existing
      too-large plain-text fallback (which independently rebuilds the full warning list)
      never triggers. Every cargo-risk/cross-system/stale-health warning disappears with
      no visible sign anything was omitted. Fixed by checking the warnings call's return
      value and manually entering the same fallback path used for a real send failure.
      New `tests/test_route_send_shape.py::
      test_multi_stop_route_falls_back_to_plain_text_when_only_the_warnings_section_overflows`
      (a controlled 3-leg/3-commodity-per-leg route, following the review's own approach
      of injecting a fixed route via `monkeypatch` rather than trying to craft real market
      data that happens to land on this exact budget boundary).
    - **The 3-observation ordering fix (entry 47) still mixed two different baseline
      rows' measurements.** The `baseline` selection used `COALESCE(pwb.scu_buy,
      iwe.scu_buy)` and, separately, `COALESCE(pwb.scu_sell, iwe.scu_sell)` - column by
      column, not row by row. Whenever the real pre-window baseline row (`pwb`) exists
      but has just ONE of its two measurements NULL, the OTHER measurement silently came
      from a completely different row (the in-window fallback, `iwe`) - presenting one
      "since baseline" comparison actually built from two different points in time.
      Confirmed: a 48h-old baseline (supply unknown, demand 500) plus in-window rows at
      -3h (600, 400) and -1h/latest (200, 300) reported previous_supply=600 (borrowed
      from -3h) alongside previous_demand=500 (correctly from the 48h baseline) - a
      fabricated -400 supply_change describing no real comparison. Fixed with a
      `baseline` CTE that picks the row ONCE (`pwb` if it exists at all via `CASE WHEN
      pwb.id_commodity IS NOT NULL`, else `iwe`) and takes both measurements from that
      one row - a genuinely NULL measurement on the chosen row stays NULL, and its
      `*_change` becomes NULL too (not a number computed against a substituted value),
      which `intelligence_brief.py`'s `if r["supply_change"]` ranking filter already
      excludes naturally since `None` is falsy - no downstream handling needed. New
      `tests/test_intelligence.py::
      test_terminal_market_shifts_never_mixes_measurements_from_two_baseline_rows`.

    Both of the review's own probes (`data/audit-b95390c/test_followup.py`) pass against
    the fixes. 266 tests passing (2 new).
49. **Self-directed audit of the whole `be40410..bcf9631` fix chain (2026-09-05/06), not
    prompted by an external review this time** - two general-purpose subagents each
    independently audited half the changed files (data/API layer vs. Discord-cogs/scripts),
    plus a `security-review` skill pass over the same range. The security pass found
    nothing (this chain only strengthens existing security properties - see its own report
    for the specific areas checked and cleared). The two correctness subagents together
    found 9 more real gaps, all following the exact same shape this whole chain has shown
    repeatedly: a shared helper's contract improves, and a caller nobody re-checked keeps
    the old, now-unsafe assumption. All 9 fixed and independently verified:
    - **`/best-route`'s two branches never checked `_add_chunked_fields`'s return value**
      (`bot/cogs/prices.py`) - unlike `/top-routes` (trends.py), which already stops and
      discloses. A route past budget just vanished with no "N more omitted" note. Fixed
      identically on both branches (the primary UEX-routes path and the derive-routes-
      ourselves fallback), each independently, since they're materially different code
      paths sharing only the same bug shape.
    - **`/multi-stop-route`'s per-leg loop had the same unchecked-return-value gap** - and
      it's worse than a missing disclosure: the route's title and "Route summary" field
      both unconditionally describe ALL of `route.legs` regardless of which leg fields
      actually got added, so a silently-dropped middle leg left the embed
      self-contradictory (claims N legs, shows fewer, still totals profit for all N).
      Fixed by tracking `all_legs_fit` and folding it into the existing `embed_too_large`
      check already used for the warnings section (entry 48) - a dropped leg now routes
      into the same full-fidelity plain-text fallback as a real send failure. Found and
      fixed a second bug while implementing this: the gate right after
      (`if warnings_fit:`) only checked the warnings flag, not the combined
      `embed_too_large` - so a route with a dropped leg but warnings that DID fit would
      still have sent the incomplete embed anyway. Changed to `if not embed_too_large:`.
    - **`/mixed-routes` had no embed-size budget protection at all**, and batches up to 5
      embeds into ONE message with no try/except. Discord enforces its 6,000-char embed
      limit as a SUM across every embed in one message, not per individual embed -
      confirmed by this same codebase's own comment in `/multi-stop-route` (which
      switched to one-embed-per-message after discovering exactly this in testing).
      Reproduced separately: 5 realistic routes each measured ~2,092 chars individually
      (fine) but ~10,460 combined - an unhandled `discord.HTTPException`, the exact
      "stuck thinking, no followup ever sent" failure this codebase already fixed once for
      `/multi-stop-route` and never checked for `/mixed-routes`. Fixed by switching the
      warnings section to the atomic `_add_chunked_fields` (checked), building a
      plain-text fallback string per route alongside each embed, and wrapping the batched
      send in the same try/except-then-fallback pattern already proven for
      `/multi-stop-route`.
    - **`ConfirmDeleteListingView` had no double-click guard at all**
      (`bot/cogs/marketplace.py`) - unlike its sibling `ConfirmListingView` (fixed under
      A02), which shares the identical real-DELETE-behind-a-button shape. Fixed with the
      same check-then-set `resolved` guard, applied to both `confirm` and `cancel`.
    - **`revert_last_deploy.sh` had no rollback trap at all** - unlike
      `deploy_and_backup.sh` (fixed under A13), any failure between stopping the service
      and restarting it left the bot down with no automatic recovery. Adapted (not
      copied) for this script's different shape: `deploy_and_backup.sh` never overwrites
      the DB so its rollback only restores the commit, but `revert_last_deploy.sh` DOES
      overwrite `db_path` partway through - so its `rollback_on_failure` also restores
      the DB from the just-taken `PRE_REVERT_DIR` snapshot whenever the failure happens
      after that overwrite. `CURRENT_COMMIT`/`CURRENT_BRANCH` moved to the top of the
      script (before anything destructive) so they're always defined by the time the trap
      could ever need them. Verified via a throwaway harness simulating a mid-script
      failure after the DB overwrite: confirmed the DB is restored, the original commit
      checked out, and the service restarted.
    - **`revert_last_deploy.sh`'s closing note still referenced `$branch` unguarded**,
      unlike line 44's `${branch:-unknown}` (A15's own fix, applied to only one of the two
      places `$branch` appears) - a meta.txt missing that field would crash on the very
      last line under `set -u`, AFTER the DB restore/checkout/restart had already fully
      succeeded, reporting a false failure at the least helpful possible moment.
    - **A DELETE retried after a network error, then rejected, was misclassified as a
      definite rejection.** `UexClient._request` never auto-retries POST after a network
      failure (the request may have already reached UEX), but DOES retry DELETE (documented
      as safe since "a repeated DELETE just gets listing_not_found the second time") - true
      before A01, but A01 made ANY non-"ok" DELETE status raise `UexRejectedError`
      ("definitely nothing happened, no reconciliation needed"). A `listing_not_found` on a
      network-retried DELETE is genuinely ambiguous, though: it could mean the listing never
      existed, OR that the FIRST (lost-response) attempt already deleted it. Fixed by
      raising the plain, ambiguous `UexApiError` instead specifically when `method ==
      "DELETE" and last_error is not None` (i.e. this attempt followed an earlier network-
      level failure) - a DELETE rejected on its first attempt is untouched, still a genuine
      `UexRejectedError`. `_cancel_listed_job` (`personal_inventory.py`) doesn't currently
      branch on the exception type for this call, so this fix's practical effect today is
      restoring the accuracy of `UexRejectedError`'s own contract for any future caller that
      reasonably trusts it (exactly why that type was introduced in the first place -
      entry 47's finding 3).
    - **`format_health_note` never learned about A10's new "unknown" cause.**
      `classify_terminal_health` can reach `status == "unknown"` two structurally different
      ways: UEX's own TTL metadata being absent (the original cause), or UEX's metadata
      saying "fresh" while the BOT's own collection has gone stale (A10, entry 46) - but
      `format_health_note` kept hardcoding "TTL metadata missing" for both, which is
      actively wrong for the second cause (the metadata is very much present and says the
      opposite). Confirmed self-contradictory in practice: a locally-stale row showed
      "TTL metadata missing; last update 0d ago" - claiming metadata is missing while
      quoting an age figure that came from that same "missing" metadata. Fixed by adding
      `TerminalDataHealth.locally_stale: bool` (set by `classify_terminal_health` at the
      same point it downgrades to "unknown" for this reason) and branching on it in
      `format_health_note` for a message that actually describes what's wrong.

    9 new regression tests, each confirmed to fail without its fix and pass with it.
    275 tests passing.
50. **A fourth external follow-up review (`data/audit-b21aba0/REVIEW.md`, 2026-09-06),
    against entry 49's own commit, found 3 more gaps - confirming the staging note's own
    prediction that this chain hadn't necessarily terminated.** All 3 fixed:
    - **`revert_last_deploy.sh`'s `DB_OVERWRITTEN` flag was set AFTER the destructive
      copy succeeded, not before.** `cp` can fail partway through (disk exhaustion, an
      I/O error) after already truncating/partially overwriting `db_path` - under
      `set -e` that failure exits immediately, so a flag set only on success left
      `rollback_on_failure` thinking the DB was never touched, skipping restoration of a
      destination that might now hold a partially-written, corrupt file. Verified via a
      throwaway harness reproducing the exact failure both ways: with the flag set
      after the copy, a simulated mid-copy failure left the corrupted content in place;
      with it moved before the copy, the same failure correctly triggered restoration
      from `PRE_REVERT_DIR`. Also brought this script's `PRE_REVERT_DIR` snapshot and its
      rollback restoration up to parity with `deploy_and_backup.sh`'s own backup step,
      which already backs up the `-wal`/`-shm` sidecars alongside the main DB file -
      this script's pre-revert snapshot and its recovery path had never done either.
    - **`/mixed-routes`' plain-text fallback (added earlier this session) dropped the
      footer entirely** - which is where the `route.is_exact` approximation disclosure
      and the budget/space-only/capital-access notes actually live. An approximate
      route's qualification silently vanished the moment the batch send was rejected or
      a route's own warnings didn't fit, unlike `/multi-stop-route`'s fallback, which
      already includes its equivalent disclosure explicitly. Fixed by appending `footer`
      itself to each route's fallback text block.
    - **The 3-observation/NULL-baseline fix (entry 47/48) only made the BASELINE side of
      `get_terminal_market_shifts` NULL-safe - the CURRENT (`latest`) side was still
      wrapped in `COALESCE(latest.scu_buy, 0)`.** A known baseline (500) paired with a
      genuinely unknown current value (UEX simply didn't report `scu_buy` this cycle)
      computed `0 - 500 = -500`, inventing a complete-depletion shift that
      `current_supply` itself correctly reports as unknown, not zero. Fixed by making
      each `*_change` NULL whenever EITHER side is NULL, symmetrically (`CASE WHEN
      baseline.scu_buy IS NULL OR latest.scu_buy IS NULL THEN NULL ELSE latest.scu_buy -
      baseline.scu_buy END`), removing the now-unnecessary `COALESCE`-to-zero entirely
      rather than layering a second guard on top of it.

    3 new regression tests (2 promoted from the review's own probes, 1 shell-only finding
    verified via harness rather than pytest, matching this repo's established practice for
    scripts), each confirmed to fail without its fix and pass with it. 277 tests passing.
51. **A fifth external follow-up review (`data/audit-33d659f/REVIEW.md`, 2026-09-06),
    against entry 50's own commit, found 1 more gap in the same function entry 50 had just
    touched.** `revert_last_deploy.sh`'s `rollback_on_failure` moving `DB_OVERWRITTEN=1`
    before the destructive copy (entry 50) correctly fixed the skipped-restoration path,
    but the *recovery* copy itself (`cp "$PRE_REVERT_DIR/..." "$db_path"`) still only did
    `|| echo "...fix manually." >&2` on failure and fell straight through to sidecar
    handling, `git checkout`, and an unconditional `sudo systemctl start` - so if the
    original copy failed for a persistent reason (disk/I/O problem), the recovery copy
    could fail the exact same way, and the bot would still be started against whatever
    was left in `db_path` (unrestored, possibly still the partially-written file from the
    original failure). The sidecar cleanup/copy loop ran unconditionally afterward too,
    regardless of whether the main file actually came back. Verified via a throwaway
    harness (extracting the real `rollback_on_failure` function verbatim via `sed` and
    driving it with mocked `cp`/`git`/`sudo`, matching entry 50's own verification style):
    confirmed the pre-fix function called `systemctl start` in all three failure
    scenarios (main DB restore cp fails, sidecar restore cp fails, `git checkout` back to
    `CURRENT_COMMIT` fails) - none of the three left the service stopped. Fixed by
    tracking a `restore_ok` flag through every recovery step (main DB cp, each sidecar cp,
    and `git checkout`) and gating the final `sudo systemctl start` on all of them having
    actually succeeded; on any failure the handler now says so explicitly and leaves the
    service stopped rather than starting it against unverified state. `git checkout`
    failure was folded into the same gate even though the review's reproduction only
    named the DB/sidecar copies - it's the identical "continue past a failed recovery
    step toward an unconditional restart" shape, one line further down in the same
    function, and entry 49's own generalized lesson is to check a fix's bug shape against
    every place it appears, not just the specific line a report named. Re-ran the same
    harness against the fix: all three failure scenarios now correctly leave the service
    stopped, and the clean-recovery control case still restarts normally. 277 tests
    passing (bash-only fix, no new pytest cases - matching entry 50's precedent for
    shell-script findings).
52. **Saved Trading Preferences (roadmap item, not an audit fix) - a new
    `bot.cogs.trading_preferences` cog storing per-user route-filter defaults, applied by
    `/best-route`, `/top-routes`, `/mixed-routes`, and `/multi-stop-route` whenever their
    matching option is left unset.** `/set-trading-preferences` (space-only,
    capital-ship-access, auto-load-only, system, risk-tolerance - all optional, only
    passed fields change, matching `negotiation_alert_settings`'s partial-update UPSERT
    idiom via a `bot/uex/trading_preferences.py` `UNSET` sentinel), `/clear-trading-
    preferences`, `/my-trading-preferences`. Two design decisions made with the user
    before implementation, since neither mapped cleanly onto anything that already
    existed:
    - **"Capital-ship access" is a new independent force-on toggle, not just "use my
      capital ship."** Previously, `capital_access_only` in `build_mixed_routes`/
      `build_multi_stop_routes` was ONLY ever derived from `requires_capital_cargo_access
      (ship_vehicle)` - there was no way to preview capital-tier routes without actually
      setting a capital ship as your default. The saved preference now ORs into that same
      variable (`requires_capital_cargo_access(ship_vehicle) or prefs["capital_ship_
      access"]`) rather than replacing it, so a genuine capital ship still always
      triggers the filter regardless of the preference. Per the user's own spec ("extra
      large hangers or external freight elevators, docking is always buggy"),
      `supports_capital_cargo_access` (`bot/uex/mixed_routes.py`) was also fixed to check
      the terminal's own `has_freight_elevator` field - already joined into every market
      row by `get_mixed_route_market_rows` (`t.has_freight_elevator` has been in that
      SELECT since before this feature) but never actually checked by this function,
      which only looked at `has_loading_dock`/`station_has_loading_dock` and
      `station_pad_types`. `has_docking_port` (a real, separate `/terminals` field per
      `docs/UEX_API_2.0_reference.md`) is deliberately never referenced - Star Citizen's
      docking-collar mechanic is exactly the unreliable case the user wanted excluded, and
      UEX's own doc-comment convention (`/commodities_routes`' `has_loading_dock_*`
      fields are documented as `// external freight elevator / autoload area`) confirms
      `has_loading_dock` means freight/autoload infrastructure, not the docking mechanic,
      so including it was already safe.
    - **Risk tolerance is stored and shown, but deliberately NOT enforced yet.** The
      user chose "store now, filter later" over building real filtering immediately.
      `risk_tolerance` accepts low/medium/high via `/set-trading-preferences` and is
      surfaced in `/my-trading-preferences` and route-command footers via
      `describe_active_preferences`, but no route command excludes any commodity based on
      it - every occurrence is labeled "(not yet enforced)" so this isn't mistaken for a
      real guarantee. Building the actual filter (against `is_illegal`/`is_explosive`/
      `is_volatile_qt`/`is_volatile_time`/`is_buggy`) is future work.

    A real scoping gap, surfaced deliberately rather than silently: **space-only and
    capital-ship-access preferences only affect `/mixed-routes` and `/multi-stop-route`**,
    because those are the only two commands whose underlying pure functions
    (`build_pair_opportunities`) already support `space_only`/`capital_access_only` as
    filters at all - `/best-route` and `/top-routes` have no equivalent filter capability
    today (they don't use `get_mixed_route_market_rows`/`build_pair_opportunities` at
    all). Extending those two commands to support space-only/capital-ship filtering would
    mean porting filtering logic across genuinely different code paths - correctly
    bucketed as part of the future Centralized Route Presentation roadmap item, not
    silently done here under a "low complexity" feature. `auto_load_only` and
    `preferred_system`, by contrast, already existed uniformly on all 4 commands, so
    those two preferences apply everywhere. `/my-trading-preferences` and
    `/set-trading-preferences`'s option descriptions both say "mixed-routes/multi-stop-
    route only" next to the two scoped fields so this isn't discoverable only by reading
    code.

    Verified end-to-end, not just via pytest: stopped the live Pi service (same Discord
    bot token as local dev, confirmed with the user first since running both
    simultaneously would fight over one gateway connection), ran the local bot with
    `PYTHONUNBUFFERED=1` and confirmed `Loaded extension bot.cogs.trading_preferences`
    and `Synced 59 commands` (was 56) with no errors, then immediately restarted the Pi
    service (confirmed back to normal, 56 commands - the Pi hasn't been redeployed with
    this feature yet). Actually invoking the new commands in a live Discord server was
    not completed this round (Claude in Chrome wasn't connected, and downtime on the live
    Pi bot was minimized by not troubleshooting that further) - that's the one item on
    `CONTRIBUTING.md`'s pre-flight checklist still open for this feature. 21 new tests in
    `tests/test_trading_preferences.py` (pure formatting, DB partial-update semantics,
    cog command behavior, and route-command wiring) plus 2 new assertions in
    `tests/test_mixed_routes.py` for the `has_freight_elevator`/`has_docking_port` fix.
    298 tests passing.
53. **Default ship folded into Saved Trading Preferences (user follow-up on entry 52) -
    `user_ship_preference` is superseded by a `ship_name` column on
    `user_trading_preferences`, and a real SQLite grammar limitation was found and
    worked around along the way.** The user's reasoning: a ship shapes route
    recommendations the same way space-only/auto-load/system do, so it belongs in the
    same saved-preferences row, not a separate table. `get_default_ship`/
    `set_default_ship`/`clear_default_ship` (`bot/db/database.py`) are now thin wrappers
    over `get_trading_preferences`/`set_trading_preferences` - `/set-default-ship`,
    `/clear-default-ship`, and `/my-ship` (`bot/cogs/ships.py`) needed zero code changes
    beyond a docstring, since they only ever called those three methods by name.
    `/set-trading-preferences` gained a `ship` option with the same `resolve_ship`
    validation and `ship_name_autocomplete` `/set-default-ship` already used. Two
    deliberately different clear semantics: `/clear-default-ship` only clears the ship
    (`clear_default_ship` reads the current value first, so its existing bool-return
    contract - "was anything actually cleared" - still holds); `/clear-trading-
    preferences` now clears the ship too, since it's the same row (`DELETE FROM
    user_trading_preferences`) - both the command description and confirmation message
    say so explicitly, since this is a real behavior change from entry 52's version of
    that command.

    **Migrating real, already-live data**: the Pi already has a real user's saved ship
    in `user_ship_preference` (confirmed against a copy of the local dev DB too - a
    `Polaris`, coincidentally a capital ship). `_migrate_ship_preference_into_
    trading_preferences` (`bot/db/database.py`, called from `init()` after
    `_run_migrations` so the new `ship_name` column exists first) copies it over,
    designed to run on every startup, not once: new rows are inserted where none exist
    yet, and a NULL `ship_name` on an existing row is backfilled - but a `ship_name`
    already set via the new path (`/set-trading-preferences` or `/set-default-ship`,
    both of which write here now) is never overwritten by a stale value from the old
    table on a later restart. `user_ship_preference` itself is left in place, unwritten,
    matching this codebase's additive-only/never-drop convention for tables that have
    held real data.

    **A genuine SQLite grammar limitation, not a typo**: the first version of that
    migration used one statement - `INSERT INTO user_trading_preferences (...) SELECT
    ... FROM user_ship_preference ON CONFLICT(user_id) DO UPDATE SET ... WHERE
    ship_name IS NULL` - and failed with `near "DO": syntax error` on this project's
    actual SQLite build (3.50.4, bundled with Python 3.13). Confirmed via a standalone
    repro this ISN'T a mistake in the query: SQLite's own documented upsert-from-SELECT
    example (the `phonebook2`/`tempPhonebook` example from sqlite.org's UPSERT page)
    fails identically on this build, while the exact same `ON CONFLICT DO UPDATE`
    clause after `INSERT INTO ... VALUES (...)` (used everywhere else in this file)
    works fine - the limitation is specifically pairing `ON CONFLICT` with an INSERT
    whose source is a SELECT, not `ON CONFLICT`/`DO UPDATE` in general. `DO NOTHING`
    and a CTE-wrapped SELECT both failed the same way, ruling out `DO UPDATE`
    specifically or the SELECT's own shape as the cause. Worked around with two plain
    statements (an `INSERT ... SELECT ... WHERE user_id NOT IN (...)` for brand-new
    rows, then an `UPDATE ... SET ship_name = (SELECT ...) WHERE ship_name IS NULL AND
    user_id IN (...)` for backfilling existing rows) instead of hunting for alternate
    upsert syntax - both statement forms are unambiguously supported, and the two-step
    version was verified for correctness and idempotency (new-row, backfill, and
    already-set-don't-clobber cases, run 3x in a row) before being trusted. Worth
    remembering if a future migration reaches for `INSERT ... SELECT ... ON CONFLICT
    DO UPDATE/NOTHING` again on this project: it doesn't work here regardless of how
    correct the SQL looks against SQLite's own docs - test it standalone first.

    Verified against real data, not just synthetic tests: ran the actual migration
    against a byte-for-byte copy of the local dev database (which has one real saved
    ship) and confirmed the ship migrated correctly and stayed stable across 3 repeated
    `init()` calls - the first attempt at this check appeared to fail (`ship_name` came
    back `None`), which turned out to be a test-harness artifact (a git-bash-style
    absolute path like `/c/Users/...` doesn't resolve correctly when handed to the
    native Windows Python 3.13 used for this check, silently opening a fresh empty
    database at a misinterpreted path instead of erroring) - re-run with a plain
    relative path confirmed the migration was correct all along. Also re-ran the same
    stop-Pi/run-local/restart-Pi verification as entry 52 (same live token constraint):
    confirmed `Loaded extension bot.cogs.trading_preferences` and `Synced 59 commands`
    against the real local dev DB (not just a fresh test DB) with no errors, and
    confirmed via direct query afterward that the real `Polaris` row had actually been
    copied into `user_trading_preferences`. 10 new tests (migration correctness/
    idempotency/non-clobbering, ship round-trip through the new storage, clear-ship-only
    vs. clear-everything semantics). 308 tests passing.
54. **Load-Limiting Explanations (roadmap item) - every `/mixed-routes`/`/multi-stop-route`
    cargo item now says which constraint actually capped its quantity: stock, demand,
    cargo space, or budget.** `MixedCargoItem` gained a `limiting_factors: tuple[str,
    ...]` field (`bot/uex/mixed_routes.py`), and a proof (in `allocate_pair_cargo`'s
    docstring) that every item is provably capped by at least one of the four: since
    every included item has strictly positive profit per unit, an item below its own
    stock/demand cap must be capacity- or budget-bound instead, or a strictly more
    profitable allocation (one more unit of that item) would have been found and chosen
    instead. That proof only holds for `_exact_allocate`'s FINAL/aggregate totals,
    though - it explores every valid combination jointly, not sequentially, so nothing
    about "what remained at some intermediate point" matters there. `_greedy_fill` is
    different: it processes commodities in a fixed order and never revisits an earlier
    one once a later one consumes more of the shared capacity/budget pool, so it needs
    each item's own LOCAL remaining capacity/budget at the moment it was actually picked,
    not the final totals after the whole pass. Confirmed this distinction matters, not
    just theoretically: a standalone repro using final-aggregate values for the greedy
    path misattributed an item that was genuinely only budget-bound as also "cargo
    space"-bound, purely because a later item exhausted whatever capacity happened to
    remain - `test_greedy_path_does_not_misattribute_an_earlier_budget_bound_item_as_
    cargo_space` (`tests/test_mixed_routes.py`) reproduces this exact scenario through
    the public `allocate_pair_cargo` API (2 real commodities + 7 low-profit fillers to
    force the greedy path past `EXACT_SEARCH_MAX_CANDIDATES`) and was verified to fail
    against the aggregate-based version before being restored to the correct local-value
    one.

    Display: replaced the old imprecise per-item warning (`"origin stock limits this
    load to N SCU"`, which fired whenever the market's own stock/demand was below the
    ship's FULL capacity - not necessarily related to why THIS item's actual allocated
    quantity ended up what it did) with a warning built from the new precise
    `format_limiting_factors(item.limiting_factors)` in both `/mixed-routes` and
    `/multi-stop-route`. First attempt put this text inline on the cargo line itself
    instead of in the `warnings` list - wrong, because `/multi-stop-route`'s plain-text
    overflow fallback explicitly omits per-leg cargo-line detail ("Full leg-by-leg cargo/
    distance details omitted") and only carries `warnings`, so the new explanation would
    have silently vanished in exactly the large/complex-route scenario where it matters
    most. Moved into `warnings` instead, matching where the old (now-replaced) stock/
    demand warning already lived - `test_multi_stop_route_fallback_preserves_warnings`
    (already existing, updated for the new wording) confirms this survives the fallback
    path. Two existing tests needed their synthetic fixtures updated after the old
    warning's removal shortened the warnings section enough to no longer trigger the
    size-overflow condition they were built to test - not a logic bug, just recalibrating
    manually-constructed `MixedCargoItem`s that never previously specified
    `limiting_factors` at all.

    12 new tests in `tests/test_mixed_routes.py` (stock/demand/cargo-space/budget/tie,
    each via both the exact and greedy solver paths where applicable, plus the
    local-vs-aggregate regression above). 317 tests passing.

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
- **`scripts/deploy_and_backup.sh` had its first real-Pi run on 2026-09-06** (deploying
  `bcf9631` → `3ec9e9c`, see entry 51's staging note) - confirmed passwordless
  `sudo systemctl stop`/`start` works non-interactively, the plain `data/uexbot.sqlite3`
  default path is correct, the explicit-refspec fetch handles the Pi's main-only
  auto-fetch quirk, and the post-deploy service came up clean (18 cogs, 56 commands
  synced, no errors). **`scripts/revert_last_deploy.sh` still hasn't had a real-Pi run**
  (timeline entry 27) - its logic is verified against a fake git repo with stubbed
  `sudo`/`systemctl`/`cp`/`git` (most recently entries 49-51's harness rounds), but never
  against the actual `uex-trade-bot.service` unit or a real `data/uexbot.sqlite3`. Exercise
  it for real the next time a Pi deploy needs undoing, rather than assuming the harness
  coverage transfers completely.
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
- **Current staging state (2026-09-06)**: `TestBranch` is deployed and running live on the
  Pi (`uex-trade-bot.service`, host `arkwatcher`) - it is no longer just a local-validation
  branch. Local (PC) and the Pi's databases have been fully merged at least twice now; the
  established practice is to back up both sides before any such merge and pull the Pi's
  backup down to the PC afterward, so nothing valuable lives only on the Pi's disk. The full
  suite has 317 passing tests (see entries 45-54 - all 15 original audit findings plus 20
  gaps found across five rounds of review/audit of those fixes, four external and one
  self-directed, are now fixed, plus entry 52's new Saved Trading Preferences feature,
  entry 53's default-ship consolidation into it, and entry 54's load-limiting
  explanations). The Pi was brought up to `8bc2e8c` (entry 53's commit) via
  `scripts/deploy_and_backup.sh` on 2026-09-06 - but entry 54's work (not yet committed
  as of this writing) has NOT been deployed, so the Pi is currently one commit behind
  `origin/TestBranch` once that
  work is committed. Re-check git log on the Pi before assuming either point is still
  true, since it will drift the moment another round of fixes or features is committed
  without a matching deploy. This chain has now run FIVE review rounds past the
  original audit, each finding real gaps in the round before it (5, then 2, then 9, then 3,
  then 1) - there is no established pattern of the count trending to zero, so don't assume
  round N+1 won't find anything just because round N's count was small (the 9-then-3 dip
  already looked like convergence before this 1-finding round showed it wasn't a trend, just
  variance). Re-check live service and branch state rather than assuming this point-in-time
  operational note is still current.
- The data collectors in `bot/cogs/intelligence.py` only pay off once they've been running a
  while - most of the `ROADMAP.md` intelligence backlog depends on accumulated history, so
  those features will look broken/empty if built and tested against a fresh database.

