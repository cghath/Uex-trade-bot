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

- [ ] **Saved Trading Preferences** *(complexity: Low)*: Store per-user defaults for
  space-only terminals, capital-ship access, auto-loading, cross-system travel, and risk
  tolerance. Route commands read these as defaults instead of requiring the options every
  time, and show which settings are currently active so a user can't accidentally get a
  route that ignores them. Same shape as the existing `user_ship_preference` table -
  gates the personalized brief item below.
- [ ] **Personalized `/intelligence-brief` Entry Point** *(complexity: Medium, depends on
  Saved Trading Preferences)*: Answer "what should I do right now?" using the user's
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

- [ ] **Load-Limiting Explanations** *(complexity: Low)*: State plainly whether cargo
  space, budget, stock, demand, or approximate allocation prevented a better load, so a
  user knows whether a bigger ship or more capital would actually help.
  `_exact_allocate` already tracks `market_available` vs. `search_bound` separately -
  mostly a matter of surfacing what's already computed.
- [ ] **Evidence-Level Labels** *(complexity: Low)*: Distinguish current reported stock,
  older observations, inferred trends, and approximate calculations on every
  recommendation, and make "no information" look different from "no demand." Builds on
  data already tracked by Terminal Data Health and Route Confidence Rating rather than
  new computation - mostly a consistency pass across display surfaces.
- [ ] **Recommendation Outcome Tracking** *(complexity: High)*: Let a user select a
  route, then report what they actually bought/sold or where stock/access didn't match
  the recommendation. Comparing predicted vs. actual profit surfaces which
  recommendations are dependable. A genuinely new subsystem (a "planned trade" state
  machine + schema + analytics) - nothing existing to build this on top of.

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
- [ ] **Centralized Route Presentation** *(complexity: Medium)*: Share the code that
  assembles warnings, confidence, approximation notices, and Discord-size-safe messages
  across `/best-route`, `/top-routes`, `/mixed-routes`, `/multi-stop-route`, and
  `/intelligence-brief` instead of each maintaining its own copy. This is *why* repeated
  audits kept finding a fix applied to one command and not another - do this before
  Load-Limiting Explanations and Evidence-Level Labels so those land once, not four times.
- [ ] **Codebase Consolidation** *(complexity: High, ongoing)*: Beyond route rendering,
  organize `bot/db/database.py`'s ~30 tables by feature and keep one authoritative
  description of current behavior. Broader than a single ticket - Centralized Route
  Presentation above is its first concrete slice; the rest is an ongoing practice rather
  than a one-time PR.
