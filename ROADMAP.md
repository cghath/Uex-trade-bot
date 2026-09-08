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
- [ ] **Recommendation Outcome Tracking** *(complexity: High)*: Let a user select one of
  the 5 suggested routes via a button, which opens a private Discord **thread** (not a
  channel - guilds cap out at 500 channels total, threads have no such limit, and a
  thread still supports one-user-plus-bot membership) scoped to that user for progression
  tracking; the thread closes/archives once the route completes or is abandoned. Each leg
  reports one of three outcomes: matched the quote (default, one tap), less than quoted
  (actual SCU, or `is_missing` if the commodity wasn't there at all), or more than quoted
  (actual SCU bought/sold, plus a follow-up: "drained it" - a confident exact write - vs.
  "my hold/their demand capped me, more was there" - a floor-only correction, "at least N
  confirmed," never written back as if it were the true exact figure). A genuinely new
  subsystem (schema + a thread-lifecycle cog) - nothing existing to build this on top of.
  Design settled across a design-discussion session (not yet implemented):
  - **Phase 1, local-only**: confirmed reports write into the *same* `terminal_market_state`/
    `terminal_market_observations` tables the intelligence collector already writes (reuse
    `record_terminal_market_snapshot`, tag rows with a new `source` column so a player
    report is never silently blended with UEX's own vetted figures in evidence
    classification). Understood as a temporary correction, not a permanent fix - the next
    scheduled UEX poll can overwrite it, and there's no fixed/knowable in-game restock rate
    to reason about instead (checked: no CIG-documented restock mechanic more recent than a
    2013 design doc explicitly marked "subject to change"; UEX's own API exposes no restock-
    rate field). Highest-value initial consumer: feed confirmed outcomes into
    `route_confidence.py`/the Evidence-Level tiers for calibration, since that improves
    every route command simultaneously with no external dependency - ranked well above
    trade-history/leaderboard display, which is engagement value but doesn't make any
    future recommendation better.
  - **Phase 2, deferred**: optionally submit the same confirmed report to UEX's own
    `POST /data_submit` (real endpoint, confirmed in `docs/UEX_API_2.0_reference.md`).
    Must be explicit per-report opt-in, never automatic - it's authenticated as the
    individual player (their linked secret key, same plumbing as `account.py`), and
    checked-research findings apply: UEX shows no visible reputation score/tier (only a
    raw-volume "most active" leaderboard), but their Terms of Use do warn that repeatedly
    submitting improper reports risks a temporary account lock, with no accuracy threshold
    disclosed. Report validation is mostly automated (an approval bot approves/declines,
    escalating only ambiguous cases to human moderators). Build and test entirely against
    `is_production=0` (a real UEX sandbox flag - exercises full validation without ever
    entering their live pipeline) before ever sending `is_production=1`. Also respect
    submission-specific limits beyond the general 120 req/min cap: 500 rows/call max,
    1000 reports/30 min, and a 5-minute block on resubmitting the same item+location.

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
