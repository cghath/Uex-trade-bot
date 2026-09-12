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
- [x] **Refinery Advisor**: Shipped 2026-09-12. `/refinery-advisor ore-1 [ore-2] [ore-3]` -
  up to three raw/refinable commodities at once, since a mined rock/asteroid usually yields
  more than one, autocompleted from live `/commodities` filtered to `is_raw` and
  `is_refinable`. For each requested ore: ranks refinery terminals by yield bonus (reading
  the already-collected `refinery_yield_observations` - see UEX Data Collection Foundation
  above - via a new `get_latest_refinery_yields_for_commodity`, which selects only the most
  recent `recorded_day`'s rows per commodity, not just the highest yield_bonus across all
  history); lists only the high-yield refining methods (`rating_yield == 3` of UEX's 9
  `/refineries_methods`, cheapest then fastest); and shows the refined commodity's current
  best sell price/terminal (live `/commodities_prices`, resolved via `id_parent` - the
  field UEX uses to link a raw commodity to its refined counterpart). For 2-3 ores, ranks
  terminals by the SUM of whatever of the requested ores each has yield data for (a
  terminal missing data for one ore is still ranked on its smaller sum, not excluded
  outright, so a genuinely best single stop for a partial match still surfaces) - a
  deliberate additive approximation, not weighted by how much of each ore was actually
  mined, since this command doesn't ask for quantities. Deliberately excludes UEX's
  `/refineries_capacities` endpoint: despite its docs claiming "yield bonus percentage,"
  real values observed live (397, 4845, 181924, ...) are clearly not percentages, so its
  actual meaning is unverified and it isn't used rather than guessed at (matches this
  project's "verify empirically, don't infer from names" convention - see
  `commodities_status`/`marketplace_listings.quality` in `CONTRIBUTING.md`). No new DB
  table or background collector: refining methods are fetched live per call (client-side
  cached 24h, matching UEX's own patch-cycle cadence) rather than persisted, since the
  list is small (9 rows) and stable. 17 new tests (pure resolve/rank/filter logic, a
  latest-recorded-day DB round trip seeded with a stale higher-yield row to prove the
  query actually filters by date rather than sorting all history, and five end-to-end
  command tests including the multi-ore combined-ranking and duplicate-ore dedup cases).
  Verified locally: `Loaded extension bot.cogs.refinery` and `Synced 64 commands` (63 to
  64, exactly the one new command) with no `CommandSyncFailure`.
- [x] **Where to Mine**: Shipped 2026-09-12. `/where-to-mine ore` - one raw/mineable
  commodity, autocompleted from live `/commodities` filtered to `is_raw` only (NOT
  `is_refinable`, unlike `/refinery-advisor` - a hand-mined material with no refinery
  pathway, e.g. Jaclium, is just as relevant to "where do I find this" as anything that
  gets refined). Shows the star system(s), planet(s), moon(s), and named mining-related
  POIs (asteroid belts/rings, via UEX's `/poi` and its `is_mining_related` flag) a
  commodity's own `ids_star_systems`/`ids_planets`/`ids_moons`/`ids_poi` fields resolve
  to, using three new reference-list client methods (`get_star_systems`/`get_planets`/
  `get_moons`, cached 24h like `get_poi`). Originally scoped as part of a broader "mining-
  route planner" idea; deliberately narrowed to a single-location lookup instead, per
  direct user correction - a real mining run sticks to one site, not a multi-stop route
  the way trading does. Deliberately does not rank or recommend one location over
  another when a commodity has several: UEX has no per-site richness/abundance data at
  all, and live-checked, the real mining-related POI set is only 7 rows total with none
  decommissioned, so even the one real differentiating signal available
  (`is_landable`/`has_quantum_marker`/`is_decommissioned`) doesn't actually distinguish
  anything in practice - a plain list is the honest answer, not a fabricated "best spot."
  Confirmed live that Jaclium and Diamond (Raw) both have every location field empty in
  UEX's own data (not a bug here) - per the user, Jaclium's real source is a distinct
  gameplay loop ("Hathor"), not a minable deposit at all, which UEX has no way to flag;
  the command's generic "no location data available" message is left as-is rather than
  hardcoding a game fact the bot can't verify against any UEX field. 13 new tests (pure
  resolve/describe logic, including a stale/unknown reference id being silently dropped
  rather than shown as a fabricated name, and end-to-end command tests). Verified
  locally: `Loaded extension bot.cogs.mining_locations` and `Synced 65 commands` (64 to
  65) with no `CommandSyncFailure`.

  **Follow-up same day**: added a per-ore mining difficulty rating to the same command's
  embed (`bot/uex/mining_difficulty.py`) - the one place in this entire bot that uses
  static, externally-sourced game constants instead of live UEX data or a collected
  observation, and disclosed as such in both the module's own docstring and the command's
  footer whenever it's shown. Sourced from SC DataHub's data-mined per-ore stats (resistance,
  instability, optimal charge window, explosion multiplier - https://sc-datahub.com/tools/
  mining/ores), which UEX has no equivalent of anywhere in its API (confirmed by grepping
  the full endpoint reference for mass/resistance/instability/charge before starting this -
  zero matches). Went through three narrower framings first, each confirmed unbuildable
  before landing here: a per-mineral crew-size recommendation (crew size is a ship
  attribute, not a mineral one - the same MOLE needs the same crew regardless of what it's
  mining), a specific-rock laser-count recommendation (a rock's real mass/resistance/
  instability is randomized per-instance in live gameplay, not knowable in advance by any
  external database), before arriving at a general per-mineral difficulty baseline, which
  the game genuinely does have as a fixed design constant. The rating combines only two of
  the source's four stats - instability and resistance - taking whichever rates worse
  (not an average, since either dimension alone can make a rock genuinely hard); optimal
  charge window is shown as a side note only, un-ranked, alongside explosion multiplier
  being left out of the feature entirely, since both carry negative values in the source
  with no documented sign convention the way resistance's does ("high resistance requiring
  high-power lasers," directly supporting a simple higher-is-harder reading) - folding them
  into a score would be guessing, not simplifying. Tier cutoffs (instability: 0-100 low,
  200-400 medium, 550+ high; resistance: <=0.30 low, 0.50-0.65 medium, 0.95 high) land on
  the real gaps in the actual per-ore values, not an arbitrary even split. Difficulty and
  location data are deliberately independent lookups - confirmed via a dedicated test that
  Diamond (no location data in UEX at all) still shows its difficulty rating, and Jaclium
  (whose real source, per the user, is a distinct gameplay loop rather than a minable
  deposit) shows a difficulty rating with no location fields, rather than one gap
  suppressing the other. 16 new tests (the difficulty table's own resolve/tier logic
  including the take-the-worse-not-the-average case, `describe_mining_locations` wiring,
  and end-to-end command field-format checks). Simulated against real UEX data before
  shipping: Quantainium (Raw) - High (resistance 0.95, instability 1000); Iron (Ore) - Low
  (resistance -0.4, instability 50), with a full real location list including Aaron Halo;
  Jaclium (Ore) - Medium difficulty shown alongside "no location data available" (superseded
  by the follow-up immediately below, which gives Jaclium a real location after all).

  **Second follow-up, same day**: added a "richest known concentration" ranking
  (`bot/uex/mining_hotspots.py`) - a THIRD static, externally-sourced reference table in this
  bot, after refining methods' rating labels and the difficulty table above, this time from a
  different community site (SCMINER, https://scminer.rocks/data/ore-by-location as of
  2026-09) that publishes a real per-location concentration percentage for every ore, not
  just presence/absence the way UEX's own `ids_poi`/`ids_moons` linkage does. Kept as its own
  module (not folded into the difficulty table) since it's sourced from a different site with
  its own independent staleness risk. Surfaces every location tied at an ore's own real
  maximum concentration, whatever that count happens to be. Two ores (Savrilium, Torite) have
  one standout 100% location (Breaker Stations' Large Geode) against 2-29% everywhere else for
  either ore. Directly fills the gap the location-lookup shipped with earlier the same day:
  Jaclium's only listed spot is Hathor Caves (19%, FPS hand-mining) - independently
  confirming, from a completely different source than the user's own correction, that
  Jaclium's real acquisition path is the Hathor gameplay loop rather than a standard rock/
  asteroid deposit. Diamond and Cobalt have no entry, matching UEX's own total lack of
  location data for both - two independent sources agreeing there's a real gap, not a
  lookup bug on either side. 6 new tests (the hotspot table's own lookup logic including
  the Jaclium/Hathor Caves case, and `describe_mining_locations`/command wiring). Simulated
  against real UEX data before shipping: Savrilium (Ore)'s hotspot (Breaker Stations, 100%)
  cross-checks cleanly against UEX's own POI list for the same commodity, which separately
  and independently lists "OV Breaker Stations (Nyx)" as a real site - two unrelated sources
  agreeing this location is real, without either one telling the other about it.

  **Same-day correction**: the first shipped version of this table capped every ore at 3
  example locations, preferring named POIs over bare moon names when several tied - caught
  the same day when the user tested `/where-to-mine Quantainium (Raw)` live in Discord and
  noticed real locations they knew about (Cellin, Wala) were missing. Quantainium's real
  source data ties ALL 14 known locations at 2% - the cap was silently showing 1-2 of them.
  Fixing Quantainium alone wasn't enough; the user asked whether the same problem existed for
  other ores, which prompted a full re-audit of all 26 entries against the original source
  data. Ten more ores turned out to be under-capturing their own real tied-max group by
  varying amounts (Agricium, Aslarite, Titanium: 3 shown of 7 real; Bexalite, Borase, Gold: 2
  of 9; Ouratite: 3 of 4; Riccite: 2 of 5; Taranite: 3 of 4; Stileron: 1 of a complete 6-way
  tie). A twelfth inconsistency was found in the same pass, the opposite direction: Copper
  listed Clio/Euterpe (40%) alongside Hurston's real max (44%) even though 40 isn't tied with
  44 at all - fixed by removing the non-tied entries rather than adding more. The fixed cap
  looked tidy but was hiding or padding real, verifiable per-ore data; the table now shows
  exactly what the source shows for every ore, no fixed-size cap in either direction. 4 new
  tests added covering a large tied group (Bexalite, 9-way), a complete tie (Stileron, 6-way),
  and the corrected non-tie exclusion (Copper). Full suite (608 tests) and a local bot start
  reverified clean after the fix.
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
- [x] **`/route-on-the-way`**: Shipped 2026-09-10. User-requested: "find a route from
  where you are to where you are going" - if a profitable haul happens to line up with a
  trip you're already making, this surfaces it. Named to avoid confusion with the rest of
  the `route(s)-from*` family sitting right next to it in `/intro` - `/routes-from` fixes
  only the origin (destination stays open), `/route-from-multi` fixes only the origin
  across a multi-leg chain; this is the one that fixes BOTH ends of a single leg to
  terminals the player names. Considered `/route-from-to` (more literal, less distinctive
  next to the other two) before settling on the chosen name. Single-leg only, not
  multi-stop, by deliberate scope decision - a fixed-destination multi-stop search would
  need `build_multi_stop_routes`' DFS to require the chain's LAST leg land at a specific
  terminal, which it isn't built to do today (only a fixed start); left as a possible
  follow-up rather than folded into this command's first version. Reuses the exact same
  reuse pattern as `/routes-from`: filters the SAME background-refreshed candidate pool
  `/top-routes` maintains, now requiring both `origin_terminal_id` AND
  `destination_terminal_id` to match the two resolved terminals - no new ranking logic, no
  extra UEX calls, gets evidence-level labels/health warnings/confidence/tracking buttons
  for free via the shared `_send_ranked_routes`. Direction-specific (checks
  origin -> destination only, matching how the player actually phrased the question) -
  swap the two options to check the reverse leg. No `system` option (both endpoints are
  already fixed to specific terminals, so a star-system filter would be redundant) -
  the only option dropped relative to `/routes-from`'s set. A `budget` option was added
  the same day, user-requested - see the very next entry.
- [x] **`/route-on-the-way` budget option**: Shipped 2026-09-10. `estimate_route_cargo`
  (`bot/uex/ships.py`), the shared cargo-math helper every single-commodity route command
  (`/best-route`, `/top-routes`, `/routes-from`, `/route-on-the-way`) already calls, never
  had a budget concept at all - only ship-cargo-capacity and real stock/demand ever capped
  the estimate, unlike `/mixed-routes`/`/multi-stop-route`'s separate allocator. Added an
  optional `budget` parameter: when given (and a known `price_origin` to divide it by), it
  becomes a third candidate cap alongside ship capacity and stock - `limited_by` can now
  also read `"budget"`. Only `/route-on-the-way` passes a real value; the other three
  callers are unaffected (`budget=None` by default, identical behavior to before). Tie-
  break priority generalized from the existing ship-vs-stock rule (ship wins ties, more
  actionable than real-world stock) to three-way: `ship` > `budget` > `stock` - a player
  can bring a bigger ship or more capital, but can't make more stock exist. The budget
  itself is disclosed in the command's footer (`· budget 1,000 aUEC`, matching
  `/mixed-routes`' existing footer wording exactly) and, when it's the binding constraint,
  in the route's own Cargo line ("limited by your budget, not cargo space") - showing the
  cap without also disclosing what it evaluated to would leave the player unable to tell
  whether it actually did anything.
- [x] **Saved default budget**: Shipped 2026-09-10, user-requested as a follow-up polish
  to the entry above - budget was the one route-filter field `/mixed-routes`,
  `/multi-stop-route`, `/route-from-multi`, and `/route-on-the-way` all take but
  `/set-trading-preferences` had no way to save a default for, unlike ship/space-only/
  auto-load-only/system/risk-tolerance. `user_trading_preferences` gained a `budget REAL`
  column (additive `ALTER TABLE`, matching this table's existing no-migration-framework
  convention), `DEFAULT_TRADING_PREFERENCES`/`get_trading_preferences`/
  `set_trading_preferences` all extended the same way `ship_name` was - the existing
  atomic `INSERT ... ON CONFLICT DO UPDATE` in `set_trading_preferences` needed no
  structural change at all, since its `DO UPDATE SET` clause and column list are already
  built from `DEFAULT_TRADING_PREFERENCES`'s own keys generically. All four budget-taking
  commands now do `if budget is None: budget = prefs["budget"]`, the identical fallback
  shape already used for `space_only`/`auto_load_only`/`system` in each of them - so a
  saved budget applies automatically and an explicit `budget:` option on any one call
  still overrides it, matching how every other saved preference already behaves.
  `format_trading_preferences` shows the saved default (or "None set"); no dedicated
  clear-budget command was added (`/clear-trading-preferences` already resets the whole
  row, matching how every field except ship, which predates this table, already works).
- [x] **Three defects from a post-implementation audit of `f664a53`**: Shipped
  2026-09-11.
  - **Mixed/multi-stop player reports used the wrong side's market quantity**:
    `MixedCargoItem.available_scu` (`bot/uex/mixed_routes.py`) is
    `min(source.scu_buy, destination.scu_sell)` - the pair-wide minimum, needed for
    the allocator's own quantity math. `bot/cogs/prices.py`'s `/mixed-routes` and
    `/multi-stop-route` flattening both used that SAME pair-minimum as `market_scu`
    for BOTH the buy leg and the sell leg - correct only when the two sides happen
    to match, and silently wrong whenever real stock and demand differ (confirmed:
    origin `scu_buy=95`, destination `scu_sell=80` - a "matched" BUY report wrote
    `scu_buy=80`, understating the origin's real stock by 15 SCU; the reverse
    asymmetry corrupts the sell side identically). This was a regression in the
    PREVIOUS session's own fix for "allocation became terminal stock" (`quantity_scu`
    vs. `market_scu`) - splitting those two apart was correct, but `market_scu` was
    then given the round-trip cap instead of each side's own real figure. Fixed by
    reading `item.source["scu_buy"]`/`item.destination["scu_sell"]` directly (both
    guaranteed real floats for any item that survived `allocate_pair_cargo`'s greedy
    loop, which already required `float()`-ing them to compute its own stock/demand
    caps) instead of `item.available_scu`, at all 4 call sites (buy/sell ×
    `/mixed-routes`/`/multi-stop-route`).
  - **A post-acknowledgement persistence failure could strand a route leg forever**:
    every leg-outcome commit point (`LegOutcomeView.matched`, the "less" modal,
    `MoreOutcomeFollowupView`'s drained/capacity-limited buttons,
    `AbandonConfirmView.confirm`) claims the leg and acknowledges the Discord
    interaction FIRST, then calls `handle_leg_outcome`/`abandon_thread` - by design,
    since the pre-ack failure path already releases the claim on an ack failure (see
    `release_claim`'s own docstring). But nothing covered a failure AFTER the ack: a
    transient DB lock, or the next-leg prompt's `thread.send` hitting a Discord
    hiccup, left the leg's buttons already disabled with nothing durable behind it -
    no error surfaced (this bot has no global app-command error handler), and the
    thread was stuck until the 48h abandonment poller. Fixed with
    `RouteProgression._record_leg_outcome_durably`/`_abandon_thread_durably` -
    thin retry wrappers (3 attempts, 2s apart) around `handle_leg_outcome`/
    `abandon_thread`, which every commit point now calls instead of the raw method.
    Safe to retry the whole call because every step inside it is idempotent (the
    outcome/market-state writes are upserts, the thread-status update is a plain
    UPDATE) - not a durable-queue redesign (this phase's views still aren't
    persistent across a bot restart; see the module's own docstring), just enough
    that a single transient blip no longer stalls a thread outright. One accepted
    edge case: if a retry's own leg-prompt send actually reached Discord but the
    success response was lost, the next leg's prompt can post twice (a harmless
    duplicate, working button pair) - preferred over not retrying at all. After all
    attempts are exhausted, the failure is logged and the thread gets a plain-text
    notice rather than being left silently stuck with no signal at all.
  - **The budget sweep could stop before a higher-budget route became worthwhile**:
    `sweep_budget_curve`'s early-stop heuristic (`bot/uex/multi_stop_routes.py`)
    treated a repeated best-chain signature as proof of real saturation once the
    swept budget could afford ONE unit of the priciest known buy opportunity - but a
    pricier chain can keep improving for several more geometric steps once it
    affords MULTIPLE units of it, still well within real stock/demand and cargo
    capacity (confirmed: a synthetic pricier chain ties a cheap chain's profit the
    instant its own one-unit price is affordable, satisfying the old floor, then
    goes on to beat it substantially at the very next geometric budget step).
    `/diminishing-returns` could tell a player more capital wouldn't help when it
    genuinely would have. Fixed by requiring the swept budget to afford filling the
    ship's ENTIRE cargo hold with the priciest known opportunity before trusting a
    repeated signature (`affordability_floor = max(known_buy_prices) *
    ship_capacity_scu`) - no larger budget could ever need more than a full hold of
    any single opportunity, so past that point real stock/demand/capacity is what's
    actually binding, not budget. A market with an expensive enough opportunity can
    still exhaust every sweep point without ever reaching this floor - the curve
    just keeps climbing instead of falsely declaring a plateau, matching this
    function's existing "deliberately capped at max_points, not run unbounded"
    design.

  One more risk the same audit flagged explicitly as UNCONFIRMED (lower-confidence,
  not counted as a defect) is deliberately left open rather than guessed at:
  `/route-on-the-way` resolves both terminal names and reads trading preferences
  before its first `defer()`/response (same pre-existing shape as `/routes-from`,
  unlike `/route-from-multi`, which already defers first) - a real timing gap under a
  slow/locked local DB, but not measured, and fixing it means threading an
  "already deferred" flag through the shared `_send_ranked_routes` helper all three
  commands share.

  The audit's OTHER flagged risk - whether a player-confirmed report should be
  allowed to extend `terminal_market_state.last_seen`/history "freshness" the same
  way a real UEX poll does - got talked through and decided: **keep current
  behavior.** UEX's own commodity data is itself aggregated from individual players
  submitting reports through UEX's platform (this codebase already tracks how many -
  `buy_report_count`/`sell_report_count`, from UEX's `price_buy_users_rows`/
  `scu_buy_users_rows` fields) - so "UEX-sourced" isn't some independently-verified
  ground truth next to "player-sourced," it's the same kind of evidence funneled
  through a different pipe. A bot-tracked report is a real, first-hand, structured
  observation and is fine to let advance freshness the same way. The thing that
  actually needs "don't over-trust one report" protection - displayed route
  confidence - already has it: `track_record_modifier` stays at 0 until 3+ reports
  accumulate for that `(commodity, terminal, side)`.

- [ ] **Discuss: `buy_report_count`/`sell_report_count` go stale after a player
  correction** *(needs a decision before building)*. Working through the freshness
  question above surfaced a real, adjacent gap that's NOT yet decided or fixed.
  **The mechanism:** `record_player_report_market_update` can only ever touch
  `price_buy`/`price_sell`/`scu_buy`/`scu_sell`/`status_buy`/`status_sell`
  (`Database._PLAYER_REPORT_OPTIONAL_COLUMNS`) - it deliberately never writes
  `buy_report_count`/`sell_report_count`, since a bot report has no UEX-style count
  of its own to give them. So after a correction, the stored count still reflects
  whatever UEX last reported (say, 8 community submissions), even though the price/
  stock figure it now sits next to came from exactly ONE bot-tracked observation.
  **Where it bites:** `compute_route_confidence` (`bot/uex/route_confidence.py`)
  reads that count directly - `report_depth = 25 * min(reports / 10, 1.0)`, up to a
  quarter of the whole confidence score. Traced every consumer: `/top-routes`,
  `/routes-from`, `/route-on-the-way` (via `_send_ranked_routes`'s `market_signals`),
  and `/mixed-routes`, `/multi-stop-route`, `/route-from-multi`, `/intelligence-brief`
  (via `cargo_confidences`) all read the stored, player-report-mutable column - 7
  commands, genuinely affected. `/best-route` is NOT affected - both its branches
  read report counts straight from a fresh UEX API response for that call
  (`live_signals`), never the local table. Net effect: after a correction, those 7
  commands can show inflated confidence - crediting "8 corroborating reports" for a
  figure that's really backed by one.
  - **Option A - leave it as-is.** Simplest, zero risk of a new distortion. Con: a
    corrected pair keeps looking more corroborated than it is, for however long
    until the next real UEX poll happens to overwrite that pair again (unbounded -
    could be minutes, could be the rest of the day).
  - **Option B - reset the touched side's count to 1 whenever a player report
    changes it.** Honest about how thin the evidence actually is right after a
    correction. Con: could swing a route's confidence down sharply the instant
    someone does the RIGHT thing and corrects a stale figure - punishing the
    correction, not rewarding it - and the drop is itself temporary (reverts on the
    next UEX poll), so it may just be trading one kind of temporary distortion for
    another.
  - **Option C - track UEX-sourced and player-sourced report counts as two separate
    numbers**, and have `compute_route_confidence` weigh them explicitly instead of
    conflating into one column. Most honest long-term, and matches how `source`
    already distinguishes the two everywhere else in this table. Con: real schema/
    formula surface area - a new column, a new scoring term, more to test - not a
    small follow-up.
  Needs a decision on which of these (or something else) before any code changes -
  logged here so the reasoning survives to the next discussion instead of getting
  re-derived from scratch.
- [x] **Suppression window for confirmed-empty pairs**: Shipped 2026-09-11,
  user-requested - route recommendations were suggesting the exact same terminal a
  player had just reported empty, with no cooldown before UEX's own next poll
  happened to overwrite the correction (unbounded - could be minutes or most of a
  day). Before picking a duration, mined this bot's own collected
  `terminal_market_observations` history for real empty-to-restocked transition gaps
  rather than guessing: local dev DB (13 buy-side transitions) gave a ~28h median,
  the Pi's full production history (29 transitions) gave ~18h - both solidly in
  "many hours," ruling out anything in the minutes-to-2h range. A separately
  pasted community "tick rate" document claimed 15min-3h full-refill times by
  commodity tier, but didn't hold up when checked - its own cited source (NOVA
  Intergalactic's real wiki page) contains no such numbers, and UEX's own API
  reference has zero restock-rate fields, matching what an earlier session already
  established. Landed on **3 hours** as a deliberate middle point between the
  empirical measurement and the unverifiable-but-not-nothing community claim, and
  **hard-exclude** (a suppressed pair simply doesn't appear, matching how
  auto-load-only/system filters already behave) over showing it with a warning.
  `terminal_market_state` gained `buy_suppressed_until`/`sell_suppressed_until`
  (additive `ALTER TABLE`, one column per side since a pair's two sides deplete and
  recover independently) - set by `Database.suppress_terminal_market_side` whenever
  `bot/uex/route_progression.py`'s new `update_confirms_depletion` says a leg
  outcome means the side is CONFIRMED empty (a `missing` report, or a `more`
  outcome drained to nothing with `precision='exact'` - NOT a positive `less`
  partial, and NOT a `more`+`floor` report, which means there was MORE there, the
  opposite signal). Consumed two different ways depending on how each route family
  already reads market data: `/mixed-routes`/`/multi-stop-route`/`/route-from-multi`
  (and `/intelligence-brief`, which shares the same market-row source) get it for
  free - `get_mixed_route_market_rows()` now masks a suppressed side's `scu_buy`/
  `scu_sell` to 0 in the SQL itself, and the allocator (`allocate_pair_cargo`)
  already treats zero stock/demand as "skip this side" exactly like a genuinely
  empty terminal, so no new filtering code was needed in `bot/uex/mixed_routes.py`
  or `multi_stop_routes.py` at all. `/top-routes`, `/routes-from`, and
  `/route-on-the-way` needed an explicit filter instead (`_send_ranked_routes`
  reads live UEX-scored candidates, not this table, for their price/stock display) -
  added as a new bulk lookup (`get_suppressed_sides_by_ids`) applied on the FULL
  candidate pool, BEFORE `display_limit` truncation - not after, matching this
  codebase's own hard-learned "filter before truncating" rule (a regression test
  specifically proves this: 3 candidates, `display_limit=2`, the highest-ranked one
  suppressed - a truncate-then-filter bug would show only 1 route instead of the 2
  that should still qualify). `/best-route` was deliberately left out of this pass -
  its live-UEX-data shape doesn't share a natural cross-reference point with the
  other six commands' either, and would need its own separate design; logged as a
  known gap rather than silently left inconsistent.
- [x] **Durable recovery + conflicting-report guard for the post-ack retry wrapper**:
  Shipped 2026-09-11, from a follow-up audit of the suppression-window commit above.
  Two confirmed findings against `_record_leg_outcome_durably`/
  `_abandon_thread_durably` (`bot/cogs/route_progression.py`), both in the same
  post-ack retry mechanism that commit introduced:
  1. Exhausting all `POST_ACK_RETRY_ATTEMPTS` left no durable recovery state - the
     leg stayed claimed (buttons disabled, nothing recorded or recorded-but-stuck)
     until the 48h abandonment poller eventually swept the whole thread. Fixed with
     a new `route_progression_pending_actions` table (additive) queued on
     exhaustion via `queue_route_progression_leg_recovery`/
     `queue_route_progression_abandon_recovery`, and a new
     `retry_pending_route_progression_actions` poller (15 min, matching this
     codebase's other short-interval background loops) that finishes the action
     later. Each queued row is fully self-contained (every field
     `handle_leg_outcome`/`abandon_thread` need, including `market_scu`, which
     previously lived nowhere outside `RouteLegInput`) - deliberately never
     dependent on `RouteProgression._active_legs`, so recovery still completes
     even across a bot restart that wiped that in-memory cache. A stale queued
     action for a thread whose status is no longer `in_progress` (resolved some
     other way in the meantime) is discarded rather than replayed.
  2. `record_route_progression_leg_outcome`'s `UPDATE` had no guard against a
     leg that was already reported - two reports for the same leg (a genuinely
     duplicate live prompt from a retried `handle_leg_outcome`, or the retry
     itself) could silently overwrite each other, and a retry could re-post the
     next leg's prompt a second time. Fixed two ways: the `UPDATE` now requires
     `outcome IS NULL`, so only the first report for a leg ever commits (returns
     whether it actually wrote); and a new `advanced_to_index` column on
     `route_progression_threads` plus `claim_route_progression_advance` (an
     atomic conditional `UPDATE`, same check-then-set shape as this codebase's
     other claim guards) makes posting a leg's prompt - or sending the
     completion message - a true one-time action regardless of how many times
     `handle_leg_outcome` runs for it. `handle_leg_outcome` distinguishes a
     losing duplicate (different outcome/values than what's stored - rejected,
     with a channel notice, no market-state write) from a legitimate retry of
     the exact report that already won (same values - proceeds to finish
     whatever step didn't complete last time, since the market-state re-merge
     and the advance-claim are both safe to repeat).
  Eight pre-existing `test_handle_leg_outcome_*` tests had to start seeding a
  real thread/leg row first (`_create_thread_for_leg`), since they'd previously
  called `handle_leg_outcome` against a `thread_id`/`leg_index` with no
  corresponding DB row at all - harmless before this fix (nothing checked for a
  row to exist), but the new `outcome IS NULL` guard needs a real row to match
  against. All 9 new regression tests were verified to fail against the
  pre-fix source via `git stash`.
- [x] **Four failure-path defects in the durable recovery mechanism above**: Shipped
  2026-09-11, from a full coordinated audit of the commit directly above (two
  independent auditor agents plus the coordinator, cross-confirmed). The
  recovery design was correct for the happy path but not yet durable end-to-end
  - all four findings are in what happens when the operation a claim guards, or
  recovery itself, fails partway:
  1. `claim_route_progression_advance`/`advanced_to_index` was committed BEFORE
     the Discord send (`_post_leg_prompt`) or the completion status write
     (`handle_leg_outcome`'s final-leg branch) it was meant to guard - a
     definite failure there left the claim durably marking an action as done
     that never happened, so a retry saw the index as already claimed and
     silently returned without redoing the send/write. Could strand a newly
     tracked route with no first prompt, an acknowledged leg with no next
     prompt, or a fully reported route stuck `in_progress` forever. Fixed with
     a new `release_route_progression_advance_claim` (an atomic conditional
     `UPDATE` reverting the index back down, only if nothing has since moved
     it further) called on definite failure at both sites, so a retry's own
     claim attempt can win again instead of finding a false no-op.
     `start_tracking`'s own first-prompt send is now wrapped the same way and
     tells the user via followup if it fails, instead of letting the exception
     escape the button callback silently.
  2. The recovery poller converted an unresolvable Discord channel to `None`
     and still called `handle_leg_outcome` for it - the outcome got recorded,
     but the next-leg prompt was silently skipped (the existing
     `isinstance(channel, discord.Thread)` gate), and the poller then deleted
     the only durable row telling a future tick to deliver that prompt,
     permanently stranding the route. Fixed by requiring a real, reachable
     `discord.Thread` before running a *non-final* leg's recovery at all -
     leaving the row queued (and its attempt counted) for the next tick
     instead. Completion doesn't need this same guard: the route's own
     final-leg branch already tolerates a missing channel (only the optional
     closing message/archive is skipped), so that path is unchanged.
  3. A transient `aiosqlite.OperationalError` (e.g. a lock beyond the busy
     timeout) escaping the recovery loop's body - from loading pending
     actions, a per-row thread lookup, stale-row deletion, or even the
     failure-accounting call itself - permanently killed the
     `retry_pending_route_progression_actions` `tasks.loop`, since SQLite
     errors aren't in `discord.ext.tasks.Loop`'s own network/timeout reconnect
     set; every future queued recovery would then sit untouched until the
     bot/cog restarted. Fixed with cycle-level containment around the initial
     fetch (log and return, letting the next scheduled tick run) plus per-row
     containment around everything else, including the failure-accounting
     write itself, so one bad row can't take down the rest of the cycle either.
  4. A failed recovery-queue insert (`queue_route_progression_leg_recovery`/
     `queue_route_progression_abandon_recovery`) was logged and swallowed, but
     the user was still unconditionally told the action "has been queued...
     no further action is needed" - exactly during the kind of database outage
     that makes the queue necessary in the first place, silently losing the
     report/abandon with no trace in either `route_progression_legs` or the
     pending-actions table. Fixed by tracking whether the insert actually
     succeeded and branching the channel notice on it - a failed queue write
     now tells the user to report again or contact an admin, never claims
     automatic recovery is in progress.
  Added 9 new regression tests (a DB round-trip for the new release method,
  one per claim-release site, the channel-unavailable recovery gap plus a
  non-regression guard proving a final leg still completes without a
  reachable channel exactly as before, the two recovery-loop containment
  cases, and one false-notice case each for the leg-outcome and abandon
  queues) - 8 of 9 verified to fail against the pre-fix source via `git
  stash` (the final-leg guard test passes on both sides by design, since it
  pins existing behavior the fix must not break).

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
