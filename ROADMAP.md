# Uex-trade-bot Roadmap

## Project Vision
A comprehensive tool for navigating the UEX economy, providing actionable insights into marketplace liquidity and commodity arbitrage.

## Core Features (Planned)

### 1. Liquidity Score (Marketplace)
*   **Goal:** Rank items by how fast they sell based on `negotiations_count` from trends data.
*   **Use Case:** Identify "fast cash" items (high negotiation activity) vs. "safe" long-term holds (low activity, stable price).
*   **Technical Notes:** Requires processing `marketplace_trends` and maintaining an accumulating index of traded items.

### 2. Arbitrage Engine (Commodities)
*   **Goal:** Constantly scan all terminals to find the highest ROI routes.
*   **Use Case:** Filter out "saturated" routes using UEX quality scores to find untapped profit opportunities.
*   **Technical Notes:** Requires processing `commodities_prices_all` and `commodities_routes`.

### 3. Undervalued Scanner (Marketplace)
*   **Goal:** Proactively scan for marketplace listings that are significantly below the "fair" market average.
*   **Use Case:** Automatically notify users of "steals" (e.g., >20% below average) as they are posted.
*   **Technical Notes:** Requires caching `marketplace_prices_averages_all` and tracking `seen_listings` in the database to avoid duplicate alerts.

## Backlog & Ideas
- [ ] **Volatility Alerts**: Notify users of sudden price swings in specific commodities.
- [ ] **Quality Premium Analysis**: Data visualization of how much extra UEC is paid for higher quality tiers.
- [ ] **User Dashboard**: A summary of the user's current "Portfolio" (linked accounts, current holdings, and active listings).
