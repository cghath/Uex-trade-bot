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
  route rankings, ship cargo math, and price/trade-volume trends.

## Backlog & Ideas
- [ ] **Volatility Alerts**: Notify users of sudden price swings in specific commodities.
- [ ] **Quality Premium Analysis**: Data visualization of how much extra UEC is paid for higher quality tiers.
- [ ] **User Dashboard**: A summary of the user's current "Portfolio" (linked accounts, current holdings, and active listings).
