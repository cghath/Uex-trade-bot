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
- [ ] **User Dashboard**: A summary of the user's current "Portfolio" (linked accounts, current holdings, and active listings).
