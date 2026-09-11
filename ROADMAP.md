# Uex-trade-bot Roadmap

## Project Vision
A comprehensive tool for navigating the UEX economy, providing actionable insights into marketplace liquidity and commodity arbitrage.

## Completed

- [x] **Marketplace Sellability**: `/liquidity-rank` identifies attractive items to list
  using a 0-100 rating based on completed deals, open negotiations, competing sell listings,
  and active buy postings. `/liquidity-trends` tracks rating history and movers.
- [x] **Raw Materials Deal Scanner**: Scans Commodities and Harvestables with reported quality for
  sell listings below their matching 30-day fair price, accounting for quality tier, currency,
  unit, and a minimum sample size before alerting. Crafted gear is intentionally excluded.
- [x] **Commodity arbitrage tools**: Current prices, best routes, route scoring, stock-aware
  route rankings, ship cargo math, price/trade-volume trends, mixed-commodity loads, and
  multi-stop (2-3 leg) trade chains with live inter-terminal distance and real starting-
  capital/ROI accounting.
- [x] **UEX Data Collection Foundation**: The always-on Pi collector records change-only
  terminal market states, data freshness, fuel prices, refinery yields, reference metadata,
  and Marketplace tier changes so future intelligence features have real history to use.
- [x] **Personal Inventory and Guarded Relisting**: Discord-managed catalog item stacks,
  Sellability Ratings, direct UEX links, balanced price recommendations, hard manual floors,
  automatic no-interest relisting (5% lower every 48h, pausing on open negotiations) down to
  the floor, and explicit handling for ambiguous sale quantities.

## Backlog & Ideas

### UEX Data Intelligence

- [x] **Route Confidence Rating**: Score trade routes by price freshness, player-report depth,
  stock, demand, and price volatility so recommendations clearly communicate their reliability.
- [x] **Terminal Data Health**: Surface how recently each terminal was reported and warn when
  a price or route relies on stale UEX data.
- [x] **Supply & Demand History**: Save periodic terminal stock and demand snapshots to reveal
  regular sell-outs, dependable buyers, and possible restock patterns.
- [x] **Practical Route Checks**: Account for freight elevators, loading docks, container-size
  limits, cargo centers, refuel/repair availability, and player-owned or monitored locations.
- [x] **Commodity Risk Labels**: Mark illegal, volatile, explosive, known-buggy, and other
  operationally relevant commodity traits in route recommendations.
- [ ] **Refinery Advisor**: Compare refinery yield bonuses, processing choices, and current
  refined-material sale value for mining runs.
- [ ] **Fuel-Aware Profit**: Estimate fuel costs and show route profit after fuel for the
  user's selected ship.
- [ ] **Marketplace Depth Analytics**: Extend sellability with buy-to-sell ratios, listing-price
  distribution, listing turnover, and availability/source signals where UEX provides them.

- [ ] **Volatility Alerts**: Notify users of sudden price swings in specific commodities.
- [ ] **Quality Premium Analysis**: Data visualization of how much extra UEC is paid for higher quality tiers.

### Personalization & Workflow

- [x] **Saved Trading Preferences**: Shipped 2026-09-06. `/set-trading-preferences`,
  `/clear-trading-preferences`, `/my-trading-preferences` store per-user defaults for
  space-only terminals, capital-ship access, auto-loading, preferred system, and risk
  tolerance (risk tolerance is stored/shown, not yet enforced - a deliberate scoping
  choice). Applied automatically by `/best-route`, `/top-routes`, `/mixed-routes`, and
  `/multi-stop-route` whenever their matching option is left unset; space-only/
  capital-ship-access only affect the latter two today. Default ship (`/set-default-ship`)
  was folded into the same `user_trading_preferences` row rather than kept in its own
  table, per user direction - see `PROJECT_CONTEXT.md` entries 52-53 for the full design
  history, the real SQLite migration bug found and fixed along the way, and the known
  gap (space-only/capital-access filtering doesn't exist yet for `/best-route`/
  `/top-routes` - bundled into Centralized Route Presentation below instead).
- [ ] **Personalized `/intelligence-brief` Entry Point** *(complexity: Medium - dependency
  now satisfied, Saved Trading Preferences shipped above)*: Answer "what should I do right
  now?" using the user's
  saved ship, available budget, preferred systems, and safety settings to surface a
  handful of good options with buttons/links into the relevant commands, instead of
  requiring the user to already know which command to run.
- [ ] **Unified Inventory & Selling Workflow** *(complexity: High)*: One private view
  covering what you own, what's listed, open negotiations, completed sales, and items
  needing attention, with suggested prices and Sellability Ratings explained inline and
  clear controls for minimum prices, relisting, and pausing automation. Supersedes the
  earlier "User Dashboard" idea - negotiations aren't currently linked to
  `personal_inventory`/`marketplace_post_jobs` by anything but `id_listing`, so this
  needs real design work, not just a bigger embed.

### Recommendation Trust & Transparency

- [x] **Load-Limiting Explanations**: Shipped 2026-09-06. Every `/mixed-routes`/
  `/multi-stop-route` cargo item now states which constraint capped its quantity - stock,
  demand, cargo space, or budget (approximate-allocation disclosure already existed
  separately via `route.is_exact`). Turned out to be less "just surface what's already
  computed" than expected: `_exact_allocate`'s aggregate totals give an exact answer, but
  `_greedy_fill`'s sequential, never-revisited processing needed each item's own local
  remaining capacity/budget at pick time, not the final totals - see
  `PROJECT_CONTEXT.md` entry 54 for the real misattribution bug this distinction caught
  before it shipped.
- [x] **`/diminishing-returns` chart**: Shipped 2026-09-06, user-initiated (not originally
  on this list). Sweeps a ship's starting budget geometrically against `/multi-stop-route`
  and charts ROI vs. budget, marking where more capital stops changing the recommendation
  at all - real stock/demand/cargo capacity, not a code limit. Building it surfaced one
  more real gap in the candidate-selection fix below (`PROJECT_CONTEXT.md` entries 56-57).
- [x] **Evidence-Level Labels**: Shipped 2026-09-07. `/best-route` and `/top-routes` now
  show an explicit Stock/Demand evidence line for every route instead of silently
  omitting a missing figure - four tiers (`bot/uex/supply_demand.py`'s
  `classify_supply_evidence`/`EvidenceLevel`): "current" (live, fresh-reported),
  "aging" (live, but the terminal's data health is degraded), "inferred" (no live figure,
  but ≥24h of collected observation history to estimate historical availability from -
  a new integration of `/terminal-history`'s existing time-weighted analysis into every
  route recommendation, not just its own standalone command), and "unknown" (genuinely no
  information - never rendered as if it meant a confirmed zero). User picked the fullest
  of three offered scopes, including the inferred-trend fallback specifically. See
  `PROJECT_CONTEXT.md` entry 59 for the full design, the real bug the smoke-test caught
  (a naive/aware datetime mismatch), and verification detail. `/mixed-routes`,
  `/multi-stop-route`, and `/intelligence-brief` weren't extended with the same tiering -
  their cargo items always carry a live stock/demand figure by construction (the
  allocator requires one to build a route at all), so "inferred"/"unknown" don't apply
  there; their existing health-warning/limiting-factor display already covers what those
  commands need.
- [x] **Recommendation Outcome Tracking, Phase 1 (local-only)**: Shipped 2026-09-08. A
  "Track this route" button on each route recommendation opens a private Discord
  **thread** (not a channel - guilds cap out at 500 channels total, threads have no such
  limit) scoped to that user, reposts the exact route breakdown the button was attached
  to, then walks each leg one at a time. Every route now sends as its own message with
  its own button directly beneath it, not bundled into one shared embed with every
  button at the end - the first version of `/best-route` (and, once tracking was added,
  `/top-routes` and `/mixed-routes`) had exactly that bundled-buttons shape and needed
  the same per-route-message restructuring live testing caught immediately. Each leg
  reports one of three outcomes: matched the quote (one tap), less than quoted (actual
  SCU, or `is_missing` derived from a report of exactly 0), or more than quoted (actual
  SCU, plus "drained it" - a confident exact write - vs. "my hold/their demand capped
  me, more was there" - a floor-only correction, never written back as if it were the
  true exact figure). A 4th "Abandon route" button (confirm/cancel gate, same pattern as
  `ConfirmDeleteListingView`) lets a user stop tracking early; the thread also
  auto-archives after 48h of inactivity either way.

  Confirmed reports write into the *same* `terminal_market_state`/
  `terminal_market_observations` tables the intelligence collector already writes
  (`record_terminal_market_snapshot`'s new `source` column tags a player report so it's
  never silently blended with UEX's own vetted figures) - understood as a temporary
  correction, not a permanent fix, since there's no fixed/knowable in-game restock rate
  to reason about instead (checked: no CIG-documented restock mechanic more recent than a
  2013 design doc marked "subject to change"; UEX's API exposes no restock-rate field).

  The loop closes: `bot/uex/route_confidence.py`'s `compute_route_confidence` now takes a
  `track_record_modifier` (a bounded +/-10, from real matched-vs-total leg outcomes per
  `(id_commodity, id_terminal, side)`, neutral below a 3-report minimum) - wired into
  `/best-route` and `/top-routes`, which call it directly. `/mixed-routes` and
  `/multi-stop-route` go through the shared `cargo_confidences` helper instead (a larger
  interface change - see the backlog item below) and don't have the modifier yet.

  Live-tested and fixed along the way: clicking "Less"/"More" used to claim the leg
  (locking every button) the instant the button was clicked, before its modal was ever
  submitted - cancelling out of that modal left the leg permanently stuck showing
  "already reported" with no outcome ever recorded, since nothing had actually
  committed. Fixed by moving the claim to the real commit point (the modal's `on_submit`,
  or the drained/capacity-limited follow-up buttons) - `less()`/`more()` now only check
  whether the leg is resolved, never set it.

  Tracking is wired into `/best-route`, `/top-routes`, `/mixed-routes`, and
  `/multi-stop-route` (the latter two flatten a multi-commodity leg into one buy + one
  sell progression-leg per commodity, all buys before all sells - matching how a player
  actually executes it). `/intelligence-brief`'s mixed-route recommendations aren't
  wired in.

- [x] **Show the reported outcome inline on the leg's own message**: Shipped 2026-09-10,
  from a real user screenshot: a reported leg's message went straight to disabled
  buttons with the original "Quoted: ..." text still showing and no visible sign of what
  was actually reported. `describe_leg_outcome` (`bot/uex/route_progression.py`, pure
  text formatting) now renders a short "**Reported:** ..." line per outcome (matched /
  less / missing / more-exact / more-floor / abandoned), and every commit point
  (`LegOutcomeView.matched`, the "less" modal, `MoreOutcomeFollowupView`'s drained/
  capacity-limited buttons, `AbandonConfirmView.confirm`) appends it under the existing
  "Quoted: ..." line via a new `_embed_with_outcome` helper, rather than replacing it -
  so the same message keeps showing both what was quoted and what happened. Sets up the
  Phase 2 UEX-submission button below to have a natural home (next to this line) once
  it's built.
  - Extend `track_record_modifier` to `/mixed-routes`, `/multi-stop-route`, and
    `/intelligence-brief` - these all go through `cargo_confidences`
    (`bot/uex/route_presentation.py`), which would need a per-item track-record lookup
    threaded through, not just one scalar the way `compute_route_confidence`'s direct
    callers take it.
  - Batch a multi-commodity leg's reporting into one modal ("report all 3 commodities for
    this stop") instead of one commodity/side at a time - a 3-hop, 3-commodity
    `/multi-stop-route` chain currently means up to 18 individual leg-report steps.
  - A "Leg 3 of 8"-style progress indicator on longer chains.
  - Persistent views (`custom_id`-based, reconstructed from `route_snapshot` on startup)
    so a bot restart mid-flow doesn't break in-flight leg-outcome buttons until the 48h
    poller sweeps the thread - a real, disclosed gap today, not a silent one.
  - **Phase 2, deferred**: optionally submit a confirmed report to UEX's own
    `POST /data_submit` (real endpoint, confirmed in `docs/UEX_API_2.0_reference.md`), via
    a button next to the "Reported: ..." line the leg's message now shows (see above) -
    user-requested placement, not yet built. Must be explicit per-report opt-in, never
    automatic - authenticated as the individual player (their linked secret key, same
    plumbing as `account.py`). Checked-research
    findings: UEX shows no visible reputation score/tier (only a raw-volume "most active"
    leaderboard), but their Terms of Use warn that repeatedly submitting improper reports
    risks a temporary account lock, with no accuracy threshold disclosed. Report
    validation is mostly automated (an approval bot approves/declines, escalating only
    ambiguous cases to human moderators). Build and test entirely against
    `is_production=0` (a real UEX sandbox flag) before ever sending `is_production=1`.
    Also respect submission-specific limits beyond the general 120 req/min cap: 500
    rows/call max, 1000 reports/30 min, and a 5-minute block on resubmitting the same
    item+location.

- [x] **Show Investment consistently across all route commands**: Shipped 2026-09-08.
  `/mixed-routes` and `/multi-stop-route` already showed `Investment: **X** · Revenue:
  **Y aUEC**` per route/leg; `/best-route` and `/top-routes` only ever showed total `Run
  profit`, never the aUEC actually needed to buy the cargo for that profit - a real gap
  for anyone weighing whether they can afford a haul at all, not just how profitable it
  is once they can. `CargoEstimate` (`bot/uex/ships.py`) now carries an `investment`
  field (`price_origin * max_scu`, via a new optional `price_origin` parameter on
  `estimate_route_cargo` - `None` when omitted, so any caller that predates this change
  keeps working unchanged) surfaced in both commands' cargo line, matching the other
  two's existing wording. Noticed while designing `/routes-from` below.
- [x] **`/routes-from`**: Shipped 2026-09-08. Best trade routes starting from wherever
  the player currently is - a `location` option (terminal name, with autocomplete off
  the local `terminal_reference` cache, no live UEX call) rather than `/best-route`'s
  commodity anchor or `/top-routes`' unanchored global ranking. Deliberately not its own
  ranking engine: filters the SAME background-refreshed candidate pool `/top-routes`
  already maintains (comprehensive across every commodity UEX has route data for, not
  truncated) down to routes whose origin matches the resolved terminal, then hands that
  filtered list to the exact same shared `_send_ranked_routes` `/top-routes` uses - so it
  gets evidence-level labels, health warnings, `track_record_modifier` confidence
  calibration, and tracking buttons for free, no new presentation logic. Location
  resolution (`Database.resolve_terminal_id_by_name`) uses the same tiered exact-then-
  unique-substring match as `find_item_id_by_name` - never guesses between two candidate
  terminals.
- [x] **Rank `/top-routes`/`/routes-from` by profit, not UEX's own score**: Shipped
  2026-09-08. Checked UEX's own API docs while reviewing `/routes-from`'s output:
  `score: int // UEX score level, higher is better` is the ENTIRE published definition -
  no formula, no breakdown of what it weighs, nothing else anywhere in the reference. A
  fully opaque black box with no way to explain to a player why one route outranked
  another. `select_available_routes`/`select_in_stock_routes`/`rank_top_scored_routes`
  (`bot/uex/trends.py`) now sort by UEX's own `profit` figure (already trusted directly
  by `/best-route`'s own primary-branch ranking) with `price_roi` as a tie-breaker,
  instead of `score` - transparent, already-displayed figures a player can verify
  themselves. `ScoredRouteEntry.score` is now optional and no longer required for a route
  to qualify (previously a route missing only a UEX score was silently excluded
  entirely). The "UEX score" display line was initially replaced with a raw
  `Profit: **X aUEC**` line - see the very next entry for why that line was removed
  again almost immediately.
- [x] **Remove the confusing duplicate "Profit" line from `/top-routes`/`/routes-from`**:
  Shipped 2026-09-08, same day as the ranking change above. A real user screenshot caught
  it: the `Profit: **X aUEC**` line added to disclose the new ranking basis (previous
  entry) is UEX's own route-level `profit` figure - computed off the FULL stock/demand
  volume, not scaled to any ship - shown right next to the already-existing, correctly
  ship-scaled `Run profit: **Y aUEC** for this haul` line. On a real route this was a
  ~17x gap (5,136,000 vs. 308,160) with nothing telling the two numbers apart, easy to
  misread as "my real profit is 5.1M." `/best-route` never had this problem - it only
  ever shows PER-UNIT profit up top, never a second lump-sum figure. Fixed by dropping
  the raw `r.profit` line entirely (`bot/cogs/trends.py:_build_route_field`) - the
  ranking basis is already disclosed in the command's footer text
  ("Ranked by profit (ROI% as a tie-breaker)"), so the per-route body doesn't also need
  to show the literal value. Per-unit margin and the ship-scaled run profit are
  unaffected.
- [x] **Clarify `/multi-stop-route`/`/mixed-routes` descriptions mention the ROI
  tie-breaker**: Shipped 2026-09-08. Both commands were already ranking by
  `(profit, roi_pct)` from their very first commits (`bot/uex/multi_stop_routes.py`/
  `bot/uex/mixed_routes.py`'s own `routes.sort(...)` calls) - unrelated to the
  `/top-routes` UEX-score fix above, which never applied to either of them. But their
  embed descriptions only said "ranked by total profit"/"ranked by estimated haul
  profit", which reads as ROI playing no part - a user seeing a real route's footer say
  only "ranked by total profit" reasonably asked whether ROI was actually used. Reworded
  to match `/top-routes`' existing wording exactly: "ranked by profit (ROI% as a
  tie-breaker)". Disclosure only, no ranking behavior changed.
- [x] **`/route-from-multi`**: Shipped 2026-09-08. `/route-from-multi`, multi-stop
  routing's counterpart to `/routes-from` - chains of 2-3 profitable hops anchored to a
  `location` option (same terminal-name autocomplete/resolution as `/routes-from`)
  instead of `/multi-stop-route`'s unconstrained "search from anywhere" candidate
  selection. Unlike `/routes-from` (which filters an already-computed background pool),
  multi-stop chains have no such cache - this runs a live, thread-offloaded
  `build_multi_stop_routes` call per request, same as `/multi-stop-route` itself, now
  with a new `start_terminal_id` parameter. Restricting the search to one origin needed
  more than just overriding which terminal the DFS starts from: the candidate-terminal
  window that bounds the whole search is built from globally profit-ranked edges, so a
  single-hop-only version of "force the anchor's own opportunities into that window"
  still let an unrelated, more-profitable cluster of edges elsewhere in the market
  crowd out the anchor's genuine 2nd/3rd-leg terminals, silently truncating an anchored
  search's real reach and sometimes returning nothing at all. Fixed with a bounded BFS
  (real `opportunities` edges only, capped at `MAX_LEGS` hops) from the anchor,
  force-adding every terminal actually reachable from it regardless of global ranking -
  caught by a test built specifically to crowd out a valid 2-leg anchor chain with 25
  higher-profit decoy edges elsewhere. `terminal_name_autocomplete` moved from
  `trends.py` to `prices.py` (trends.py already imports several things FROM prices.py,
  so the reverse direction would have been a circular import) - `/routes-from`'s own
  autocomplete wiring updated to import it from its new home, no behavior change.
  `/multi-stop-route`'s ~200-line per-route embed/warnings/tracking-view/fallback
  sending logic extracted into a shared `_send_multi_stop_routes` helper both commands
  call, rather than duplicating it a third time (the ship/prefs/capital-access setup
  block above IS still duplicated across `/mixed-routes`, `/multi-stop-route`,
  `/diminishing-returns`, and now this command - matching that pre-existing, not-yet-
  centralized convention rather than doing a larger unrelated refactor).

### Route Economics Depth

- [ ] **Fuel-Aware Profit**: Estimate fuel costs and show route profit after fuel for the
  user's selected ship.
- [ ] **Travel-Aware Ranking** *(complexity: High, needs scoping)*: Offer estimated
  profit-per-minute alongside total profit, with transparent travel/loading assumptions,
  visible uncertainty, and total-profit ranking always still available. The real
  complexity is the travel-time model itself - Star Citizen quantum travel isn't
  `distance / speed` (spool-up, interdiction, calibration) - so this needs a deliberate
  decision on how seriously to model it before it gets an estimate.

### Platform & Reliability

- [ ] **Collector Health Dashboard** *(complexity: Medium)*: Show each background
  collector's last successful run, consecutive failure count, and next attempt; retry
  transient database failures with a bounded delay; notify when failures persist.
  Mechanical instrumentation across the existing `tasks.loop` collectors rather than new
  design.
- [x] **Centralized Route Presentation**: Shipped 2026-09-07. New `bot/uex/
  route_presentation.py` is now the single home for the warning/confidence/chunking
  logic `/best-route`, `/top-routes`, `/mixed-routes`, `/multi-stop-route`, and
  `/intelligence-brief` each used to maintain their own copy of. Closed real gaps found
  by auditing all five side by side: `/top-routes` and `/best-route`'s primary branch had
  no cross-system warning at all; `/intelligence-brief` had no terminal-health warnings,
  no limiting-factor explanation, no confidence rating, and zero Discord embed-size
  protection. See `PROJECT_CONTEXT.md` entry 58 for the full design (in particular how
  `travel_warning`'s `has_real_distance` parameter unifies three previously-divergent
  cross-system wordings) and verification detail. Landed just ahead of Evidence-Level
  Labels below, specifically so that lands once through this shared module instead of
  four times.
- [ ] **Codebase Consolidation** *(complexity: High, ongoing)*: Beyond route rendering,
  organize `bot/db/database.py`'s ~30 tables by feature and keep one authoritative
  description of current behavior. Broader than a single ticket - Centralized Route
  Presentation above is its first concrete slice; the rest is an ongoing practice rather
  than a one-time PR.
