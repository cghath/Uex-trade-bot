# UEX Corp API 2.0 — Complete Endpoint Reference

> Machine-readable reference scraped from https://uexcorp.space/api/documentation/ on 2026-08-22.
> Covers all 89 documented endpoints: 77 GET, 8 POST, 4 DELETE.
> Source of truth is the live docs; regenerate this file if UEX ships a new API version.

## How to use this file (notes for an AI agent)

- Every endpoint below is a `## METHOD /resource` heading. Search by resource name.
- `**Input**` blocks list request parameters; `**Output**` blocks list response fields inside `data`.
- Types are copied verbatim from the docs: `int`, `float`, `string`, `mixed`, `int|null`, `string|null`, `array`, `bool`.
- `[FK -> get_xxx]` means that field is a foreign key you can resolve against the `xxx` endpoint.
- `// comments` are the documentation's own inline notes, preserved verbatim.
- `_none_` means the docs show no content for that section (e.g. an endpoint that takes no parameters).
- Comments beginning `// optional` / `// required` / `// at least one is required` are the docs' own requirement markers. When no marker is present, treat the parameter as optional unless the endpoint's example URL includes it.

## Base URL and request shapes

```
https://api.uexcorp.uk/2.0/{resource}/
https://api.uexcorp.uk/2.0/{resource}/{param1}/{value1}/{param2}/{value2}/...
https://api.uexcorp.uk/2.0/{resource}/?{param1}={value1}&{param2}={value2}...
```

Path-segment style and query-string style are equivalent. Trailing slash is accepted.

## Global definitions

| Item | Value |
| --- | --- |
| API version | 2.0 |
| Daily quota | 172,800 requests (120 requests/minute) |
| Authentication | Bearer Token |
| Content type | `application/json` |
| Methods allowed | GET, POST, DELETE |

## Authentication

- **Public GET endpoints** need no credentials. Most reference/price data is public.
- **App-scoped endpoints** use an application API token created on the *My Apps* page
  (https://uexcorp.space/api/apps), sent as `Authorization: Bearer <api_token>`.
- **User-scoped endpoints** (anything touching a specific user's wallet, trades, fleet,
  refinery jobs, marketplace negotiations, or Datacenter submissions) additionally require the
  user's 40-character secret key from their UEX profile, sent as the `secret-key` header.
- **All POST and DELETE endpoints require the `secret-key` header.** Missing or wrong values
  return `missing_secret_key` / `invalid_secret_key`.
- Each endpoint's `**Auth:**` line states what that specific endpoint needs; `none` means public.

Typical authenticated request:

```
GET https://api.uexcorp.uk/2.0/user_trades
Authorization: Bearer <api_token>
secret-key: <40-char user secret key>
```

### Client version lock

API keys can be locked to a client version. When locked, requests must send a matching
`X-Client-Version` header or they are rejected. This lets an app owner cut off outdated clients.

## Response envelope

Every response is JSON with a `status` field.

| Case | Shape |
| --- | --- |
| Success | `{ "status": "ok", "data": ... }` |
| Internal error | `{ "status": "error", "http_code": 500, "message": ... }` |
| Invalid request | `{ "status": "<error_code>", "data": ... }` |
| Rate limited | `{ "status": "requests_limit_reached", "data": ... }` |

Do not branch on HTTP status alone — always read `status`. Each endpoint below lists the
`status` values it can return under `**Response status:**`.

## Pricing conventions

Default averaging window is **15 days**.

Variation tolerance (+/-) applied per data type:

| Commodities | Items | Ore sales | Vehicle sales | Vehicle rentals | Vehicle pledges | Fuel |
| --- | --- | --- | --- | --- | --- | --- |
| 25% | 100% | 60% | 10% | 60% | 10% | 20% |

Pricing unit per data type:

| Commodities | Items | Ore sales | Vehicle sales | Vehicle rentals | Vehicle pledges | Fuel |
| --- | --- | --- | --- | --- | --- | --- |
| per SCU | per unit | per SCU | per unit | per unit | per unit | per SCU |

## Data caveats

UEX data is crowdsourced from community reports, so errors occur and are corrected as found.
Data structures are kept stable within a major version; breaking changes land in a new API version.
Respect each endpoint's `Cache TTL` — caching client-side avoids burning the 120 req/min quota.

## Conventions worth knowing

- `date_added` / `date_modified` are UNIX timestamps (integers, seconds).
- Location hierarchy: `star_system > planet > orbit > moon > city / space_station / outpost / poi`,
  with `terminal` as the leaf where trading happens. Price rows carry the whole id chain, so you can
  filter at any level without extra lookups.
- `*_all` variants return the full unfiltered dataset (large payloads, longer cache TTLs); the
  non-`_all` variant expects filters.
- `id_*` fields resolve against the endpoint named in the `[FK -> ...]` marker.
- Many list endpoints accept up to 10 comma-separated ids where the type is `mixed`.

---

# Endpoint index

- GET /categories
- GET /categories_attributes
- GET /cities
- GET /commodities
- GET /commodities_alerts
- GET /commodities_averages
- GET /commodities_prices
- GET /commodities_prices_all
- GET /commodities_prices_history
- GET /commodities_ranking
- GET /commodities_raw_averages
- GET /commodities_raw_prices
- GET /commodities_raw_prices_all
- GET /commodities_routes
- GET /commodities_status
- GET /companies
- GET /contacts
- GET /contracts
- GET /crew
- GET /currencies_index
- GET /currencies_index_history
- GET /data_extract
- GET /data_info
- GET /data_monitor
- GET /data_parameters
- GET /factions
- GET /fleet
- GET /fuel_prices
- GET /fuel_prices_all
- GET /game_versions
- GET /game_versions_all
- GET /items
- GET /items_attributes
- GET /items_prices
- GET /items_prices_all
- GET /jump_points
- GET /jurisdictions
- GET /marketplace_averages
- GET /marketplace_averages_all
- GET /marketplace_favorites
- GET /marketplace_listings
- GET /marketplace_negotiations
- GET /marketplace_negotiations_messages
- GET /marketplace_prices_averages
- GET /marketplace_prices_averages_all
- GET /marketplace_prices_history
- GET /marketplace_trends
- GET /moons
- GET /orbits
- GET /orbits_distances
- GET /organizations
- GET /outposts
- GET /planets
- GET /poi
- GET /polls
- GET /polls_audit
- GET /refineries_audits
- GET /refineries_capacities
- GET /refineries_methods
- GET /refineries_yields
- GET /release_notes
- GET /space_stations
- GET /star_systems
- GET /terminals
- GET /terminals_distances
- GET /user
- GET /user_notifications
- GET /user_refineries_jobs
- GET /user_trades
- GET /vehicles
- GET /vehicles_loaners
- GET /vehicles_prices
- GET /vehicles_purchases_prices
- GET /vehicles_purchases_prices_all
- GET /vehicles_rentals_prices
- GET /vehicles_rentals_prices_all
- GET /wallet_balance
- POST /data_edit
- POST /data_submit
- POST /marketplace_advertise
- POST /marketplace_negotiations_messages
- POST /user_refineries_jobs_add
- POST /user_trades_add
- POST /user_trades_edit
- POST /wallet_add
- DELETE /data_remove
- DELETE /marketplace_listings
- DELETE /user_refineries_jobs_remove
- DELETE /user_trades_remove

---

# Endpoints

## GET /categories

Get a list of item and service categories.

- **Auth:** none
- **Cache TTL:** +1 day
- **Update frequency:** Patch Cycle
- **Response status:** ok

**Example URLs**
```
https://api.uexcorp.uk/2.0/categories?type={string}&section={string}
```

**Input**
```
type       string    // item, service, contract | optional
section    string    // optional
```

**Output**
```
id                 int
type               string    // item, service, contract
section            string
name               string
is_game_related    int       // if exists in-game
is_mining          int       // mining related
date_added         int       // timestamp
date_modified      int       // timestamp
```

## GET /categories_attributes

Get attributes from categories (type 'item' only)

- **Auth:** none
- **Cache TTL:** +1 day
- **Update frequency:** Patch Cycle
- **Response status:** ok

**Example URLs**
```
https://api.uexcorp.uk/2.0/categories_attributes?id_category={int}
```

**Input**
```
id_category    int    // optional
```

**Output**
```
id                  int
id_category         int       // [FK -> get_categories]
name                string
category_name       int       // parent   [FK -> get_categories]
description         string
is_lower_better     int       // does not apply to all attributes
date_added          int       // timestamp
date_modified       int       // timestamp
```

## GET /cities

Retrieve the list of cities within a star system.

- **Auth:** none
- **Cache TTL:** +1 day
- **Update frequency:** Patch Cycle
- **Response status:** ok

**Example URLs**
```
// example 1
https://api.uexcorp.uk/2.0/cities?id_star_system={int}

// example 2
https://api.uexcorp.uk/2.0/cities?id_planet={int}

// example 3
https://api.uexcorp.uk/2.0/cities?id_orbit={int}

// example 4
https://api.uexcorp.uk/2.0/cities?id_moon={int}
```

**Input**
```
id_star_system      int    // optional
id_faction          int    // optional
id_jurisdiction     int    // optional
id_planet           int    // optional
id_orbit            int    // optional
id_moon             int    // optional
```

**Output**
```
id                       int
id_star_system           int          // [FK -> get_star_systems]
id_planet                int          // [FK -> get_planets]
id_orbit                 int          // [FK -> get_orbits]
id_moon                  int          // [FK -> get_moons]
id_faction               int          // [FK -> get_factions]
id_jurisdiction          int          // [FK -> get_jurisdictions]
name                     string
code                     string
is_available             int          // UEX
is_available_live        int          // Star Citizen
is_visible               int          // UEX (public)
is_default               int
is_monitored             int
is_armistice             int
is_landable              int
is_decommissioned        int
has_quantum_marker       int
has_trade_terminal       int
has_habitation           int
has_refinery             int
has_cargo_center         int
has_clinic               int
has_food                 int
has_shops                int
has_refuel               int
has_repair               int
has_gravity              int
has_loading_dock         int
has_docking_port         int
has_freight_elevator     int
pad_types                string|null  // XS|S|M|L|XL
wiki                     string|null
date_added               int          // timestamp
date_modified            int          // timestamp
star_system_name         string|null
planet_name              string|null
orbit_name               string|null
moon_name                string|null
faction_name             string|null
jurisdiction_name        string|null
```

## GET /commodities

Get a list of all commodities covered by UEX.

- **Auth:** none
- **Cache TTL:** +1 hour
- **Update frequency:** Patch Cycle
- **Response status:** ok

**Example URLs**
```
https://api.uexcorp.uk/2.0/commodities
```

**Input**
_none_

**Output**
```
id                     int
id_parent              int|null
id_item                int|null
ids_star_systems       string|null   // comma-separated star system IDs
ids_planets            string|null   // comma-separated planet IDs
ids_moons              string|null   // comma-separated moon IDs
ids_poi                string|null   // comma-separated POI IDs (mining-related)
ids_orbits             string|null   // comma-separated orbit IDs (Lagrange points)
uuid                   string|null   // game UUID
name                   string
code                   string        // UEX code
slug                   string        // UEX slug
kind                   string|null
weight_scu             int|null      // tons
price_buy              float         // average / SCU
price_sell             float         // average / SCU
is_available           int           // UEX
is_available_live      int           // Star Citizen
is_visible             int           // UEX (public)
is_extractable         int           // mining only
is_mineral             int
is_raw                 int
is_pure                int
is_refined             int           // refined form
is_refinable           int           // can be refined
is_harvestable         int
is_buyable             int
is_sellable            int
is_temporary           int
is_illegal             int           // if restricted in certain jurisdictions
is_volatile_qt         int           // if volatile in quantum travel
is_volatile_time       int           // if it becomes unstable over time
is_inert               int           // inert gas
is_explosive           int           // risk of explosion
is_buggy               int           // has known bugs reported recently
is_fuel                int
wiki                   string|null
date_added             int           // timestamp
date_modified          int           // timestamp
```

## GET /commodities_alerts

Obtain a list of the latest commodities alerts

- **Auth:** none
- **Cache TTL:** +30 minutes
- **Update frequency:** Hourly
- **Response status:** ok

**Example URLs**
```
https://api.uexcorp.uk/2.0/commodities_alerts?id_commodity={int}
```

**Input**
```
id_commodity    int    // optional
```

**Output**
```
id_commodity        int       // [FK -> get_commodities]
id_terminal          int       // [FK -> get_terminals]

// prices
price_buy            float
price_sell           float

// scu
scu_buy              float
scu_sell             float

// inventory
status_buy           int       // [FK -> get_commodities_status]
status_sell          int       // [FK -> get_commodities_status]

// etc
date_added           int       // timestamp
game_version         string
commodity_name       string
commodity_code       string
commodity_slug       string
star_system_name     string|null
planet_name          string|null
orbit_name           string|null
moon_name            string|null
space_station_name   string|null
outpost_name         string|null
city_name            string|null
faction_name         string|null
terminal_name        string
terminal_code        string
terminal_slug        string
```

## GET /commodities_averages

Retrieve a list of average prices and stock data of a specific commodity in the last 15 days. (CAX Index)

- **Auth:** Bearer Token
- **Cache TTL:** +30 minutes
- **Update frequency:** Hourly
- **Response status:** requires_id_commodity_or_id_terminal, ok

**Example URLs**
```
https://api.uexcorp.uk/2.0/commodities_averages?id_commodity={int}
```

**Input**
```
id_commodity    int    // required
```

**Output**
```
id                              int
id_commodity                    int          // [FK -> get_commodities]

// buy
price_buy                       float        // last
price_buy_min                   float
price_buy_min_week              float
price_buy_min_month             float
price_buy_max                   float
price_buy_max_week              float
price_buy_max_month             float
price_buy_avg                   float
price_buy_avg_week              float
price_buy_avg_month             float
price_buy_users                 float        // last 15d, average from user trades
price_buy_users_rows            int|null     // trips in the last 15d, coming from user trades

// sell
price_sell                      float        // last
price_sell_min                  float
price_sell_min_week             float
price_sell_min_month            float
price_sell_max                  float
price_sell_max_week             float
price_sell_max_month            float
price_sell_avg                  float
price_sell_avg_week             float
price_sell_users                float        // last 15d, average from user trades
price_sell_users_rows           int|null     // trips in the last 15d, coming from user trades

// scu buy
scu_buy                         float        // last
scu_buy_min                     float
scu_buy_min_week                float
scu_buy_min_month               float
scu_buy_max                     float
scu_buy_max_week                float
scu_buy_max_month               float
scu_buy_avg                     float
scu_buy_avg_week                float
scu_buy_avg_month               float
scu_buy_total                   float        // cumulative stock available
scu_buy_total_week              float
scu_buy_total_month             float
scu_buy_users                   float        // last 15d, average from user trades
scu_buy_users_rows              int|null     // trips in the last 15d, coming from user trades

// reported stockfloat (sell)
scu_sell_stock                  float        // last
scu_sell_stock_week             float
scu_sell_stock_month            float

// scu sell
scu_sell                        float        // last calculated, based on reported level (scu_sell_stock)
scu_sell_min                    float
scu_sell_min_week               float
scu_sell_min_month              float
scu_sell_max                    float
scu_sell_max_week               float
scu_sell_max_month              float
scu_sell_avg                    float
scu_sell_avg_week               float
scu_sell_avg_month              float
scu_sell_total                  float        // cumulative amount demanded
scu_sell_total_week             float
scu_sell_total_month            float
scu_sell_users                  float        // last 15d, average from user trades
scu_sell_users_rows             int|null     // trips in the last 15d, coming from user trades

// inventory buy
status_buy                      int|null
status_buy_min                  int|null
status_buy_min_week             int|null
status_buy_min_month            int|null
status_buy_max                  int|null
status_buy_max_week             int|null
status_buy_max_month            int|null
status_buy_avg                  int|null
status_buy_avg_week             int|null
status_buy_avg_month            int|null

// inventory sell
status_sell                     int|null
status_sell_min                 int|null
status_sell_min_week            int|null
status_sell_min_month           int|null
status_sell_max                 int|null
status_sell_max_week            int|null
status_sell_max_month           int|null
status_sell_avg                 int|null
status_sell_avg_week            int|null
status_sell_avg_month           int|null

// variation coefficient, lower is better
volatility_price_buy            float
volatility_price_sell           float
volatility_scu_buy              float
volatility_scu_sell             float
volatility_buy                  float        // deprecated
volatility_sell                 float        // deprecated

// etc
cax_score                       int          // uex score, higher is better
game_version                    string
date_added                      int          // timestamp
date_modified                   int          // timestamp
commodity_name                  string
commodity_code                  string
commodity_slug                  string
```

## GET /commodities_prices

Retrieve a list of prices for all commodities.

- **Auth:** none
- **Cache TTL:** +30 minutes
- **Update frequency:** Hourly
- **Response status:** missing_required_input, ok

**Example URLs**
```
https://api.uexcorp.uk/2.0/commodities_prices?id_terminal={int}
https://api.uexcorp.uk/2.0/commodities_prices?id_commodity={int}
https://api.uexcorp.uk/2.0/commodities_prices?terminal_name={string}
https://api.uexcorp.uk/2.0/commodities_prices?commodity_name={string}
```

**Input**
```
id_terminal       mixed     // up to 10 ids separated by comma
id_commodity      int
terminal_name     string
terminal_code     string
terminal_slug     string
commodity_name    string
commodity_code    string
commodity_slug    string
```

**Output**
```
id                              int
id_commodity                    int
id_star_system                  int
id_planet                       int
id_orbit                        int
id_moon                         int
id_city                         int
id_outpost                      int
id_poi                          int
id_faction                      int
id_terminal                     int
price_buy                       float        // last
price_buy_min                   float
price_buy_min_week              float
price_buy_min_month             float
price_buy_max                   float
price_buy_max_week              float
price_buy_max_month             float
price_buy_avg                   float
price_buy_avg_week              float
price_buy_avg_month             float
price_buy_users                 float        // last 15d, average from user trades
price_buy_users_rows            int|null     // trips in the last 15d, coming from user trades
price_sell                      float        // last
price_sell_min                  float
price_sell_min_week             float
price_sell_min_month            float
price_sell_max                  float
price_sell_max_week             float
price_sell_max_month            float
price_sell_avg                  float
price_sell_avg_week             float
price_sell_avg_month            float
price_sell_users                float        // last 15d, average from user trades
price_sell_users_rows           int|null     // trips in the last 15d, coming from user trades
scu_buy                         float        // last
scu_buy_min                     float
scu_buy_min_week                float
scu_buy_min_month               float
scu_buy_max                     float
scu_buy_max_week                float
scu_buy_max_month               float
scu_buy_avg                     float
scu_buy_avg_week                float
scu_buy_avg_month               float
scu_buy_users                   float        // last 15d, average from user trades
scu_buy_users_rows              int|null     // trips in the last 15d, coming from user trades
scu_sell_stock                  float        // last amount of SCU reported at location
scu_sell_stock_avg              float
scu_sell_stock_avg_week         float
scu_sell_stock_avg_month        float
scu_sell                        float        // last
scu_sell_min                    float
scu_sell_min_week               float
scu_sell_min_month              float
scu_sell_max                    float
scu_sell_max_week               float
scu_sell_max_month              float
scu_sell_avg                    float
scu_sell_avg_week               float
scu_sell_avg_month              float
scu_sell_users                  float        // last 15d, average from user trades
scu_sell_users_rows             int|null     // trips in the last 15d, coming from user trades
status_buy                      int|null
status_buy_min                  int|null
status_buy_min_week             int|null
status_buy_min_month            int|null
status_buy_max                  int|null
status_buy_max_week             int|null
status_buy_max_month            int|null
status_buy_avg                  int|null
status_buy_avg_week             int|null
status_buy_avg_month            int|null
status_sell                     int|null
status_sell_min                 int|null
status_sell_min_week            int|null
status_sell_min_month           int|null
status_sell_max                 int|null
status_sell_max_week            int|null
status_sell_max_month           int|null
status_sell_avg                 int|null
status_sell_avg_week            int|null
status_sell_avg_month           int|null
volatility_price_buy            float
volatility_price_sell           float
volatility_scu_buy              float
volatility_scu_sell             float
volatility_buy                  float        // deprecated
volatility_sell                 float        // deprecated
faction_affinity                int|null     // datarunner's affinity average at a location (between -100 and 100)
container_sizes                 string|null  // in scu, csv values, 1|2|4|8|16|24|32
quality                         int|null     // last
quality_min                     int|null
quality_min_week                int|null
quality_min_month               int|null
quality_max                     int|null
quality_max_week                int|null
quality_max_month               int|null
quality_avg                     int|null
quality_avg_week                int|null
quality_avg_month               int|null
game_version                    string
date_added                      int          // timestamp
date_modified                   int          // timestamp
commodity_name                  string
commodity_code                  string
commodity_slug                  string
star_system_name                string|null
planet_name                     string|null
orbit_name                      string|null
moon_name                       string|null
space_station_name              string|null
outpost_name                    string|null
city_name                       string|null
terminal_name                   string
terminal_code                   string
terminal_slug                   string
terminal_mcs                    int|null     // deprecated
terminal_is_player_owned         int
```

## GET /commodities_prices_all

Retrieve a list of prices for all commodities in all terminals, all at once

- **Auth:** none
- **Cache TTL:** +30 minutes
- **Update frequency:** Hourly
- **Response status:** ok

**Example URLs**
```
https://api.uexcorp.uk/2.0/commodities_prices_all
```

**Input**
_none_

**Output**
```
id                     int
id_commodity           int          // [FK -> get_commodities]
id_terminal            int          // [FK -> get_terminals]
price_buy              float        // last
price_buy_avg          float
price_sell             float        // last
price_sell_avg         float
scu_buy                float        // last
scu_buy_avg            float
scu_sell_stock         float        // last
scu_sell_stock_avg     float        // average reported
scu_sell               float
scu_sell_avg           float
status_buy             int|null     // [FK -> get_commodities_status]
status_sell            int|null     // [FK -> get_commodities_status]
container_sizes        string|null  // in scu, csv values, 1|2|4|8|16|24|32
quality                int|null     // 0-1000
date_added             int          // timestamp
date_modified          int          // timestamp
commodity_name         string
commodity_code         string
commodity_slug         string
terminal_name          string
terminal_code          string
terminal_slug          string
```

## GET /commodities_prices_history

Obtain a price history of a commodity at a specific location

- **Auth:** none
- **Cache TTL:** +12 hours
- **Update frequency:** Hourly
- **Response status:** missing_id_terminal, missing_id_commodity, ok

**Example URLs**
```
https://api.uexcorp.uk/2.0/commodities_prices_history?id_terminal={int}&id_commodity={int}
```

**Input**
```
id_terminal      int
id_commodity     int
game_version     string    // e.g. 4.9
```

**Output**
```
id                    int
id_commodity          int          // [FK -> get_commodities]
id_star_system        int          // [FK -> get_star_systems]
id_planet             int          // [FK -> get_planets]
id_orbit              int          // [FK -> get_orbits]
id_moon               int          // [FK -> get_moons]
id_city               int          // [FK -> get_cities]
id_outpost            int          // [FK -> get_outposts]
id_poi                int          // [FK -> get_poi]
id_terminal           int          // [FK -> get_terminals]
id_faction            int          // [FK -> get_factions]

// prices
price_buy             float
price_sell            float

// scu
scu_buy               float
scu_sell_stock        float
scu_sell              float

// inventory
status_buy            int|null     // [FK -> get_commodities_status]
status_sell           int|null     // [FK -> get_commodities_status]
quality               int|null     // 0-1000

// etc
game_version          string
date_added            int
commodity_name        string
commodity_code        string
commodity_slug        string
star_system_name      string|null
planet_name           string|null
orbit_name            string|null
moon_name             string|null
space_station_name    string|null
outpost_name          string|null
city_name             string|null
faction_name          string|null
terminal_name         string
terminal_code         string
terminal_slug         string
```

## GET /commodities_ranking

**DEPRECATED.** Retrieves the UEX Commodities Average Index™ Ranking. Consider using the `cax_score` column in the [commodities_averages](https://uexcorp.space/api/documentation/id/get_commodities_averages) table as a replacement.

- **Auth:** none
- **Cache TTL:** +30 minutes
- **Update frequency:** Daily
- **Response status:** ok

**Example URLs**
```
https://api.uexcorp.uk/2.0/commodities_ranking
```

**Input**
_none_

**Output**
```
id                                       int       // [FK -> get_commodities]
code                                     string
slug                                     string
name                                     string
is_temporary                             int

// prices
price_buy_avg_month                      float
price_sell_avg_month                     float

// scu
scu_buy_avg_month                        float
scu_sell_avg_month                       float

// inventory
status_buy_avg_month                     int|null  // [FK -> get_commodities_status]
status_sell_avg_month                    int|null  // [FK -> get_commodities_status]

// price coefficient of variation. higher is worse
volatility_price_buy                     float
volatility_price_sell                    float
volatility_scu_buy                       float
volatility_scu_sell                      float
volatility_buy                           float     // deprecated
volatility_sell                          float     // deprecated

// etc
cax_score                                int       // commodity score, higher is better

// investment
investment                               float
investment_per_scu                       float

// profit
profitability                            float
profitability_relative_percentage        float
profitability_per_scu                    float

// availability
availability_buy                         int|null  // number of locations buying
availability_sell                        int|null  // number of locations selling

// best buy price reference
price_buy_minimum                        float
terminal_id_price_buy_minimum            int
terminal_slug_price_buy_minimum          int

// best sell price reference
price_sell_maximum                       float
terminal_id_price_sell_maximum           int
terminal_slug_price_sell_maximum         int
```

## GET /commodities_raw_averages

Retrieve a list of average prices of a specific commodity (raw) in the last 15 days. (CAX Index)

- **Auth:** none
- **Cache TTL:** +30 minutes
- **Update frequency:** Hourly
- **Response status:** requires_id_commodity_or_id_terminal, ok

**Example URLs**
```
https://api.uexcorp.uk/2.0/commodities_raw_averages?id_commodity={int}
```

**Input**
```
id_commodity    int    // required
```

**Output**
```
id                          int
id_commodity                int    [FK -> get_commodities]

// buy
price_buy                   float    // last
price_buy_min               float
price_buy_min_week          float
price_buy_min_month         float
price_buy_max               float
price_buy_max_week          float
price_buy_max_month         float
price_buy_avg               float
price_buy_avg_week          float
price_buy_avg_month         float
price_buy_users             float    // last 15d, average from user trades
price_buy_users_rows        int|null // trips in the last 15d, coming from user trades

// sell
price_sell                  float    // last
price_sell_min               float
price_sell_min_week          float
price_sell_min_month         float
price_sell_max               float
price_sell_max_week          float
price_sell_max_month         float
price_sell_avg               float
price_sell_avg_week          float
price_sell_users             float    // last 15d, average from user trades
price_sell_users_rows        int|null // trips in the last 15d, coming from user trades

// etc
game_version                string
date_added                  int    // timestamp
date_modified                int    // timestamp
commodity_name               string
commodity_code                string
commodity_slug                 string
```

---

## GET /commodities_raw_prices

Retrieve a list of prices for all unrefined (raw/ore) commodities.

- **Auth:** none
- **Cache TTL:** +30 minutes
- **Update frequency:** Hourly
- **Response status:** requires_id_commodity_or_id_terminal, ok

**Example URLs**
```
// example 1
https://api.uexcorp.uk/2.0/commodities_raw_prices?id_terminal={int}
// example 2
https://api.uexcorp.uk/2.0/commodities_raw_prices?id_commodity={int}
```

**Input**
```
// at least one is required
id_terminal    mixed    // up to 10 ids separated by comma
id_commodity   int
```

**Output**
```
id                    int
id_commodity          int    [FK -> get_commodities]
id_star_system        int    [FK -> get_star_systems]
id_planet             int    [FK -> get_planets]
id_orbit              int    [FK -> get_orbits]
id_moon               int    [FK -> get_moons]
id_city               int    [FK -> get_cities]
id_outpost            int    [FK -> get_outposts]
id_poi                int    [FK -> get_poi]
id_terminal           int    [FK -> get_terminals]
id_faction            int    [FK -> get_factions]

// buy
price_buy             float    // last
price_buy_min         float
price_buy_min_week    float
price_buy_min_month   float
price_buy_max         float
price_buy_max_week    float
price_buy_max_month   float
price_buy_avg         float
price_buy_avg_week    float
price_buy_avg_month   float

// sell
price_sell            float    // last
price_sell_min        float
price_sell_min_week   float
price_sell_min_month  float
price_sell_max        float
price_sell_max_week   float
price_sell_max_month  float
price_sell_avg        float
price_sell_avg_week   float
price_sell_avg_month  float

// factions
faction_affinity      int    // datarunner's affinity average at a location (between -100 and 100)

// etc
game_version              string
date_added                int    // timestamp
date_modified             int    // timestamp
commodity_name            string
commodity_code            string
commodity_slug            string
star_system_name          string|null
planet_name               string|null
orbit_name                string|null
moon_name                 string|null
space_station_name        string|null
outpost_name              string|null
city_name                 string|null
faction_name              string|null
terminal_name             string
terminal_code             string
terminal_slug             string
terminal_is_player_owned  int
```

---

## GET /commodities_raw_prices_all

Retrieve a list of prices for all raw commodities in all terminals, all at once

- **Auth:** none
- **Cache TTL:** +30 minutes
- **Update frequency:** Hourly
- **Response status:** ok

**Example URLs**
```
https://api.uexcorp.uk/2.0/commodities_raw_prices_all
```

**Input**
_none_

**Output**
```
id                int
id_commodity      int    [FK -> get_commodities]
id_terminal       int    [FK -> get_terminals]
price_buy         float
price_buy_avg     float
price_sell        float
price_sell_avg    float
date_added        int    // timestamp
date_modified     int    // timestamp
commodity_name    string
commodity_code    string
commodity_slug    string
terminal_name     string
terminal_code     string
terminal_slug     string
```

---

## GET /commodities_routes

Retrieve a list of common routes calculated based on data reports

- **Auth:** none
- **Cache TTL:** +30 minutes
- **Update frequency:** Hourly
- **Response status:** missing_one_required_inputs, ok

**Example URLs**
```
// Example 1
https://api.uexcorp.uk/2.0/commodities_routes?id_terminal_origin={int}
// Example 2
https://api.uexcorp.uk/2.0/commodities_routes?id_terminal_origin={int}&id_terminal_destination={int}
// Example 3
https://api.uexcorp.uk/2.0/commodities_routes?id_commodity={int}
```

**Input**
```
// at least one is required
id_terminal_origin    int
id_planet_origin      int
id_orbit_origin       int
id_commodity          int

// optional inputs
id_terminal_destination    int
id_planet_destination      int
id_orbit_destination       int
id_faction_origin          int
id_faction_destination     int
investment                 int
```

**Output**
```
id                              int
id_commodity                    int
id_star_system_origin           int
id_star_system_destination      int
id_planet_origin                int
id_planet_destination           int
id_orbit_origin                 int
id_orbit_destination            int
id_terminal_origin              int
id_terminal_destination         int
id_faction_origin               int
id_faction_destination          int
code                            string    // unique route hash, e.g. https://uexcorp.space/trade/route?code={code}

// prices
price_origin                    float    // scu
price_origin_users              float    // scu, coming from users trades
price_origin_users_rows         float    // user trips
price_destination                float    // scu
price_destination_users          float    // scu, coming from users trades
price_destination_users_rows     float    // user trips
price_margin                     float
price_roi                        float

// scu
scu_origin                       float
scu_origin_users                 float    // average from users trades
scu_origin_users_rows            float    // number of user trades used to calculate 'scu_origin_users'
scu_destination                  float
scu_destination_users            float    // average from users trades
scu_destination_users_rows       float    // number of user trades used to calculate 'scu_destination_users'
scu_margin                       float    // percentage

// volatility
volatility_origin                float    // price coefficient of variation. higher is worse
volatility_destination           float    // price coefficient of variation. higher is worse

// inventory
status_origin                    int    // stock level at origin, higher is better
status_destination                int    // stock level at destination, lower is better

// etc
investment                       float    // maximum investment expected (scu_buy_avg * price_buy_avg)
profit                           float    // maximum profit expected
distance                         float    // Distance in Giga Meters (GM)
score                            int    // UEX score level, higher is better
container_sizes_origin           string|null    // csv
container_sizes_destination      string|null    // csv
game_version_origin              string|null    // e.g. 3.24
game_version_destination         string|null    // e.g. 4.0

// location attributes
has_docking_port_origin          int
has_docking_port_destination     int
has_freight_elevator_origin      int
has_freight_elevator_destination int
has_loading_dock_origin          int    // external freight elevator / autoload area
has_loading_dock_destination     int    // external freight elevator / autoload area
has_refuel_origin                int
has_refuel_destination           int
has_cargo_center_origin          int
has_cargo_center_destination     int
has_quantum_marker_origin        int
has_quantum_marker_destination   int
is_monitored_origin              int    // uee commlink
is_monitored_destination         int    // uee commlink
is_space_station_origin          int
is_space_station_destination     int
is_on_ground_origin               int
is_on_ground_destination          int

// commodity
commodity_name                    string
commodity_code                    string
commodity_slug                    string

// location details
origin_star_system_name              string|null
origin_planet_name                   string|null
origin_orbit_name                    string|null
origin_terminal_name                 string|null
origin_terminal_code                 string|null
origin_terminal_slug                 string|null
origin_terminal_is_player_owned      int
origin_faction_name                  string|null
destination_star_system_name         string|null
destination_planet_name              string|null
destination_orbit_name               string|null
destination_terminal_name            string|null
destination_terminal_code            string|null
destination_terminal_slug            string|null
destination_terminal_is_player_owned int
destination_faction_name             string|null

// dates
date_added                           int    // unix timestamp
```

---

## GET /commodities_status

Obtain a list of inventory states that are displayed at every trading terminal.

- **Auth:** none
- **Cache TTL:** +1 day
- **Update frequency:** Daily
- **Response status:** ok

**Example URLs**
```
https://api.uexcorp.uk/2.0/commodities_status
```

**Input**
_none_

**Output**
```
code                  string    // status code
name                  string
name_short            string
name_abbr             string
percentage            string    // range label
percentage_start      float
percentage_end        float
colors                string
```

---

## GET /companies

Retrieve a list of all companies in the Star Citizen universe.

- **Auth:** none
- **Cache TTL:** +1 day
- **Update frequency:** Patch Cycle
- **Response status:** ok

**Example URLs**
```
https://api.uexcorp.uk/2.0/companies?is_vehicle_manufacturer={int}
```

**Input**
```
is_item_manufacturer      int    // show only item manufacturers, such as Apocalypse Arms, Clark...
is_vehicle_manufacturer   int    // show only vehicle manufacturers, such as Anvil, Aegis...
```

**Output**
```
id                          int
id_faction                  int    [FK -> get_factions]
name                        string
nickname                    string
wiki                        string|null
industry                    string|null    // main activity
is_item_manufacturer        int
is_vehicle_manufacturer     int
date_added                  int    // timestamp
date_modified                int    // timestamp
```

---

## GET /contacts

Obtain a list of all known Star Citizen contacts (mission givers)

- **Auth:** none
- **Cache TTL:** +1 day
- **Update frequency:** Rarely
- **Response status:** ok

**Example URLs**
```
https://api.uexcorp.uk/2.0/contacts/
```

**Input**
_none_

**Output**
```
id                      int
id_star_system          int    [FK -> get_star_systems]
id_planet               int    [FK -> get_planets]
id_orbit                int    [FK -> get_orbits]
id_moon                 int    [FK -> get_moons]
id_space_station        int    [FK -> get_space_stations]
id_city                 int    [FK -> get_cities]
id_outpost              int    [FK -> get_outposts]
id_poi                  int    [FK -> get_poi]
id_faction              int    [FK -> get_factions]
id_company              int    [FK -> get_companies]
id_jurisdiction         int    [FK -> get_jurisdictions]
name                    string
description             string
is_available            int    // UEX
is_available_live       int    // Star Citizen
is_visible              int    // UEX (public)
game_version            string    // introduction
date_added              int    // timestamp
date_modified           int    // timestamp
star_system_name        string|null
planet_name             string|null
orbit_name              string|null
moon_name               string|null
space_station_name      string|null
city_name               string|null
outpost_name            string|null
poi_name                string|null
faction_name            string|null
company_name            string|null
jurisdiction_name       string|null
```

---

## GET /contracts

Obtain a list of all known Star Citizen contacts (mission givers)

- **Auth:** none
- **Cache TTL:** +1 day
- **Update frequency:** Rarely
- **Response status:** ok

**Example URLs**
```
https://api.uexcorp.uk/2.0/contracts/
```

**Input**
_none_

**Output**
```
id                       int
id_parent                int
id_star_system           int    [FK -> get_star_systems]
id_planet                int    [FK -> get_planets]
id_orbit                 int    [FK -> get_orbits]
id_moon                  int    [FK -> get_moons]
id_space_station         int    [FK -> get_space_stations]
id_city                  int    [FK -> get_cities]
id_outpost               int    [FK -> get_outposts]
id_poi                   int    [FK -> get_poi]
id_faction               int    [FK -> get_factions]
id_company               int    [FK -> get_companies]
id_jurisdiction          int    [FK -> get_jurisdictions]
id_contact               int    [FK -> get_contacts]
name                     string
description              string|null
payout                   float|null
is_available             int    // UEX
is_available_live        int    // Star Citizen
is_visible               int    // UEX (public)
game_version             string    // introduction
date_added               int    // timestamp
date_modified            int    // timestamp
star_system_name         string|null
planet_name              string|null
orbit_name               string|null
moon_name                string|null
space_station_name       string|null
city_name                string|null
outpost_name             string|null
poi_name                 string|null
faction_name             string|null
company_name             string|null
jurisdiction_name        string|null
contact_name             string|null
contact_description      string|null
```

---

## GET /crew

Search for users listed in the Crew Directory

- **Auth:** none
- **Cache TTL:** —
- **Update frequency:** Patch Cycle
- **Response status:** ok

**Example URLs**
```
https://api.uexcorp.uk/2.0/crew?specialization={string},{string}...
https://api.uexcorp.uk/2.0/crew?specialization={string}&day_availability={string}
https://api.uexcorp.uk/2.0/crew?specialization={string}&timezone={string}
```

**Input**
```
specialization       string    // required, e.g. trade
day_availability      string    // e.g. weekend
time_availability      string    // e.g. evening
languages               string    // e.g. en,pt
archetypes              string    // e.g. explorer
timezone                string    // ISO format, e.g. America/Sao_Paulo
username                 string
```

**Output**
```
name                  string    // full name
username              string    // in-game nick
twitch_username       string|null    // twitch username, if account is connected to twitch
day_availability      string|null    // reference below
time_availability     string|null    // reference below
specializations       string|null    // reference below
languages              string|null    // reference below
archetypes              string|null    // reference below
timezone                string|null    // ISO format, e.g. America/Sao_Paulo
avatar                  string|null    // UEX CDN
bio                      string|null    // user profile bio
date_added                int    // UEX sign up date
```

**Notes**
```
References (values returned by the "reference below" output fields):

specializations: datarunner, escort, exploration, engineer, gunner, hauling, medical,
                  mercenary, mining, other, pilot, piracy, racer, refining, refueling,
                  repairing, roleplay, salvaging, scanning, scientist, towing, trading, transit

day_availability: weekdays, weekends

time_availability: morning, afternoon, evening

languages: ar, ca, zh, nl, en, fr, de, it, jp, pt, ru, es, xx

archetypes: artist, engineer, explorer, lover, novice, outlaw, player_one, protector,
            strategist, support, trickster, warlord
```

---

## GET /currencies_index

Retrieve the current UEX Currency Index with full basket composition

- **Auth:** none
- **Cache TTL:** +1 day
- **Update frequency:** Daily (00:05 UTC)
- **Response status:** ok

**Example URLs**
```
https://api.uexcorp.uk/2.0/currencies_index
```

**Input**
```
currency    string (optional)    // e.g. UEC — filter to a single currency
```

**Output**
```
id                    int
currency              string    // UEC, WIF, ...
index_value           float    // Laspeyres index — 100 = base period (Dec 2023)
basket_value          float    // Σ(weight × avg_price) — weighted avg of current prices
methodology           string    // e.g. laspeyres_dynamic_v1
data_window_days      int    // rolling avg window used for price calculations
date_modified         int    // Unix timestamp of last update
components            array    // basket commodities for the latest snapshot
    id_commodity          int
    commodity_name        string
    commodity_code        string
    commodity_slug        string
    weight                float    // 0.0–1.0, proportional to all-time submission volume
    avg_price             float    // rolling avg sell price over data_window_days
    base_price            float    // avg sell price at base period (Dec 2023) — Pi,0
    data_points           int    // price observations backing this avg
    ids_sources           string    // comma-separated IDs of source price records — see commodities_prices_history
    contribution          float    // weight × (avg_price ÷ base_price) × 100 — index points contributed
    date_added            int    // Unix timestamp of this component snapshot
```

**Notes**
```
Index formula (Laspeyres): index = Σ( weight_i × (avg_price_i ÷ base_price_i) ) × 100

Each commodity's contribution is its share of the total index value. The sum of all
contributions equals index_value.

Weights are derived from all-time submission volume and renormalized each snapshot to
sum to 1.0, excluding commodities with no data in the current window.

A value above 100 means the basket costs more UEC than at the base period (purchasing
power has decreased).
```

## GET /currencies_index_history

Retrieve historical UEX Currency Index snapshots with per-commodity component detail

- **Auth:** none
- **Cache TTL:** +1 day
- **Update frequency:** Daily (00:05 UTC)
- **Response status:** ok
- **Limits:** Maximum of 2000 rows per request

**Example URLs**
```
https://api.uexcorp.uk/2.0/currencies_index_history
```

**Input**
```
currency        string       // optional, e.g. UEC — filter to a single currency
date_from       int          // optional, Unix timestamp, default: 90 days ago
date_to         int          // optional, Unix timestamp, default: now, max range 2 years
```

**Output**
```
id                  int
currency            string      // UEC, WIF, ...
index_value         float       // Laspeyres index — 100 = base period (Dec 2023)
basket_value        float       // Σ(weight × avg_price) — weighted avg of current prices
methodology         string      // e.g. laspeyres_dynamic_v1
data_window_days    int         // rolling avg window used for price calculations
date_added          int         // Unix timestamp of this snapshot
components          array       // basket commodities for this snapshot
  id_commodity        int
  commodity_name      string
  commodity_code      string
  commodity_slug      string
  weight              float     // 0.0–1.0, proportional to all-time submission volume
  avg_price           float     // rolling avg sell price over data_window_days
  base_price          float     // avg sell price at base period (Dec 2023) — Pi,0
  data_points         int       // price observations backing this avg
  ids_sources         string    // comma-separated IDs of source price records   [FK -> get_commodities_prices_history]
  contribution        float     // weight × (avg_price ÷ base_price) × 100 — index points contributed
```

---

## GET /data_extract

Extract updated plain text data from UEX

- **Auth:** none
- **Cache TTL:** +1 hour
- **Update frequency:** Daily basis
- **Response status:** _none_

**Example URLs**
```
https://api.uexcorp.uk/2.0/data_extract?data={string}
```

**Input**
```
data    string    // required. One of:
                   //   commodities_routes — obtain the top 30 commodities routes according to UEX
                   //   commodities_prices — obtain the last commodities average prices
                   //   last_commodity_data_reports — obtain the last 30 commodities reports sent by Datarunners
```

**Output**
```
_Plain text response (no structured JSON fields documented on the page)._
```

---

## GET /data_info

Read the reports you submitted to the UEX Datacenter, and what happened to them

- **Auth:** Bearer Token
- **Cache TTL:** —
- **Update frequency:** —
- **Response status:** service_unavailable, access_denied, missing_secret_key, invalid_secret_key, user_not_found, user_disabled, user_not_allowed, invalid_type, invalid_status, invalid_limit, ok
- **Maximum rows allowed:** 100 rows

**Example URLs**
```
https://api.uexcorp.uk/2.0/data_info/id/1234567/
https://api.uexcorp.uk/2.0/data_info/type/commodity/status/under_review/limit/25/
```

**Input**
```
// header
secret-key       string        // required user secret key, should be passed via header, obtained in user profile

// parameters
id               int|null      // a single report, by ID
type             string|null   // commodity, item, vehicle_buy, vehicle_rent
id_terminal      int|null      // only reports submitted at this terminal
status           string|null   // lifecycle state — see status reference below
username         string|null   // only reports from this datarunner
limit            int|null      // 1 to 100, defaults to 50

// status reference (allowed values for the `status` field)
//   pending        - waiting for the approval bot
//   under_review   - the bot could not decide, a moderator will
//   queued         - PTU report parked until that version goes live
//   approved       - cleared, waiting for the next consolidation
//   consolidated   - folded into the live prices
//   declined       - rejected as inconsistent or invalid
//   expired        - sat unapproved for too long
```

**Output**
```
id                      int
type                    string      // see the status reference above
status                  string
id_user                 int         // the datarunner who reported it
username                string|null
id_terminal             int
id_commodity            int
id_item                 int
id_category             int
id_vehicle              int
name                    string|null // item name, when reported by name
price_buy               float|null
price_sell              float|null
price_rent              float|null
scu_buy                 int
scu_sell                int         // as reported, not the projected demand
status_buy              int         // 1 - out of stock, 7 - maximum
status_sell             int         // 1 - out of stock, 7 - maximum
quality                 int|null
container_sizes         string|null // csv
faction_affinity        int
details                 string|null
game_version            string
is_missing              int         // reported as no longer sold at the terminal
is_new_item             int
is_new_at_location      int
is_ptu_report           int
is_contested            int         // flagged or disputed by the community
is_owner                int         // 1 when the report is yours
is_editable             int         // 1 when you may edit or remove it
has_attachments         int
has_comments            int
date_added              int         // timestamp
date_modified           int         // timestamp
date_checked            int         // timestamp
date_approved           int         // timestamp
date_declined           int         // timestamp
date_consolidated       int         // timestamp
date_expired            int         // timestamp
date_queued             int         // timestamp
```

---

## GET /data_monitor

Retrieve terminal price update status for all terminals monitored by UEX Datarunners

- **Auth:** Bearer Token
- **Cache TTL:** +1 hour
- **Update frequency:** Hourly
- **Response status:** ok, invalid_type

**Example URLs**
```
https://api.uexcorp.uk/2.0/data_monitor/                          // all terminals, all types
https://api.uexcorp.uk/2.0/data_monitor?type={string}              // filter by data type
https://api.uexcorp.uk/2.0/data_monitor?id_star_system={int}       // filter by star system
https://api.uexcorp.uk/2.0/data_monitor?id_faction={int}           // filter by faction
```

**Input**
```
// all parameters are optional
type              string    // commodity, item, commodity_raw, vehicle_buy, vehicle_rent, fuel
id_star_system    int       // [FK -> get_star_systems]
id_faction        int       // [FK -> get_factions]
```

**Output**
```
id_terminal                       int          // [FK -> get_terminals]
ids_reports                       int[]|null   // ids of pending non-consolidated, non-expired reports (live only, excludes PTU; includes declined), null if none
type                               string       // commodity, item, commodity_raw, vehicle_buy, vehicle_rent, fuel
terminal_name                      string
terminal_nickname                  string
terminal_code                      string
terminal_slug                      string
id_star_system                     int          // [FK -> get_star_systems]
star_system_name                   string
orbit_name                         string
orbit_nickname                     string
orbit_code                         string
game_version                       string       // game version of the most recent price entry
prices_total                       int          // total number of price entries tracked for this terminal
prices_updated                     int          // number of entries updated within the TTL window
prices_updated_percentage          int          // prices_updated / prices_total * 100
last_update_days_limit             int          // TTL in days for this data type
last_update_days                   int          // days elapsed since the most recent price update
last_update_days_percentage        int          // percentage of TTL remaining (0 = expired)
last_update                        int          // unix timestamp of the most recent price update
id_report                          int|null     // id of the last non-declined, non-removed report for this terminal
has_recent_reports                 bool         // true if ids_reports is not null
has_ptu_reports                    bool         // true if there are unconsolidated PTU reports for the current PTU game version
```

---

## GET /data_parameters

Retrieve a list of specific parameters that UEX uses for managing prices and updates.

- **Auth:** none
- **Cache TTL:** +1 day
- **Update frequency:** Rarely / Patch basis (CIG)
- **Response status:** _none_

**Example URLs**
```
https://api.uexcorp.uk/2.0/data_parameters
```

**Input**
```
_none_
```

**Output**
```
// global settings
is_accepting_reports          int             // indicates if the system accepts community reports
is_accepting_ptu_reports      int             // indicates if the system accepts PTU reports
is_datacenter_enabled         int             // Check UEX Data module operational status
game_version                  string          // current LIVE version operated by UEX
game_version_ptu              string          // current PTU version operated by UEX (if 'is_accepting_ptu_reports' is active)

// 'commodity' type reports
is_accepted                   int             // indicates acceptance of this report type by the system
is_temporary_enabled          int             // displays temporary commodities on the website
price_variation                int             // UEX accepted price variation limits (up/down) for buying and selling
scu_variation                  int             // UEX accepted SCU variation limit (up/down)
ttl                            int             // days until a price is considered outdated
notification                   null|string     // staff alerts

// 'item' type reports
is_accepted                   int             // indicates acceptance of this report type by the system
price_variation                int             // UEX accepted price variation limits (up/down) for buying and selling
ttl                            int             // days until a price is considered outdated
notification                   null|string     // staff alerts

// 'vehicle_rent' type reports
is_accepted                   int             // indicates acceptance of this report type by the system
price_variation                int             // UEX accepted price variation limits (up/down) for buying and selling
ttl                            int             // days until a price is considered outdated
notification                   null|string     // staff alerts

// 'vehicle_buy' type reports
is_accepted                   int             // indicates acceptance of this report type by the system
price_variation                int             // UEX accepted price variation limits (up/down) for buying and selling
ttl                            int             // days until a price is considered outdated
notification                   null|string     // staff alerts
```

---

## GET /factions

Retrieve a list of all known Star Citizen factions

- **Auth:** none
- **Cache TTL:** +1 day
- **Update frequency:** Rarely
- **Response status:** ok

**Example URLs**
```
https://api.uexcorp.uk/2.0/factions/
```

**Input**
```
_none_
```

**Output**
```
id                          int
ids_star_systems           int          // csv
ids_factions_friendly       string|null  // csv
ids_factions_hostile        string|null  // csv
name                        string
wiki                        string|null  // wiki page
is_piracy                   int
is_bounty_hunting           int
date_added                  int          // timestamp
date_modified                int          // timestamp
```

---

## GET /fleet

Obtain user fleet vehicles

- **Auth:** Bearer Token
- **Cache TTL:** —
- **Update frequency:** Realtime
- **Response status:** missing_secret_key, invalid_secret_key, user_not_found, user_not_allowed, ok

**Example URLs**
```
https://api.uexcorp.uk/2.0/fleet
```

**Input**
```
// header
secret-key    string    // required user secret key, should be passed via header, obtained in user profile
```

**Output**
```
id                    int
id_organization       int          // [FK -> get_organizations]
id_vehicle            int          // [FK -> get_vehicles]
name                  string       // user vehicle name
serial                string|null  // vehicle identification
description           string|null  // vehicle lore or description
date_added            int          // timestamp
organization_name     string|null  // if linked to an org
model_name            string       // vehicle name
is_hidden             int          // public hidden
is_pledged            int          // das geld!
```

**Response status detail**
```
missing_secret_key
invalid_secret_key
user_not_found
user_not_allowed      // user banned or disabled by UEX
ok
```

---

## GET /fuel_prices

Retrieve a list of all fuel prices.

- **Auth:** none
- **Cache TTL:** +30 minutes
- **Update frequency:** Hourly
- **Response status:** missing_required_input, ok

**Example URLs**
```
// example 1
https://api.uexcorp.uk/2.0/fuel_prices?id_terminal={int}
// example 2
https://api.uexcorp.uk/2.0/fuel_prices?id_commodity={int}
// example 3
https://api.uexcorp.uk/2.0/fuel_prices?terminal_name={string}
// example 4
https://api.uexcorp.uk/2.0/fuel_prices?commodity_name={string}
```

**Input**
```
// at least one is required
id_terminal          mixed     // up to 10 ids separated by comma
id_commodity         int
terminal_name        string
terminal_code        string
terminal_slug        string
commodity_name       string
commodity_code       string
commodity_slug       string
```

**Output**
```
id                            int
id_commodity                  int          // [FK -> get_commodities]
id_star_system                int          // [FK -> get_star_systems]
id_planet                     int          // [FK -> get_planets]
id_orbit                      int          // [FK -> get_orbits]
id_moon                       int          // [FK -> get_moons]
id_city                       int          // [FK -> get_cities]
id_outpost                    int          // [FK -> get_outposts]
id_poi                        int          // [FK -> get_poi]
id_faction                    int          // [FK -> get_factions]
id_terminal                   int          // [FK -> get_terminals]
id_faction                    int          // [FK -> get_factions]

// buy
price_buy                     float        // last
price_buy_min                 float
price_buy_min_week            float
price_buy_min_month           float
price_buy_max                 float
price_buy_max_week            float
price_buy_max_month           float
price_buy_avg                 float
price_buy_avg_week            float
price_buy_avg_month           float

// factions
faction_affinity              int          // datarunner's affinity average at a location (between -100 and 100)

// etc
game_version                  string
date_added                    int          // timestamp
date_modified                  int          // timestamp
commodity_name                string
commodity_code                string
commodity_slug                string
star_system_name              string|null
planet_name                   string|null
orbit_name                    string|null
moon_name                     string|null
space_station_name            string|null
outpost_name                  string|null
city_name                     string|null
terminal_name                 string
terminal_code                 string
terminal_slug                 string
terminal_mcs                  int          // maximum container size operated by freight elevator (in SCU)
```

---

## GET /fuel_prices_all

Retrieve a list of all fuel prices in all terminals, all at once

- **Auth:** none
- **Cache TTL:** +30 minutes
- **Update frequency:** Hourly
- **Response status:** ok

**Example URLs**
```
https://api.uexcorp.uk/2.0/fuel_prices_all
```

**Input**
```
_none_
```

**Output**
```
id                    int
id_commodity          int          // [FK -> get_commodities]
id_terminal           int          // [FK -> get_terminals]
price_buy             float
price_buy_avg         float
date_added            int          // timestamp
date_modified          int          // timestamp
commodity_name        string
commodity_code        string
commodity_slug        string
terminal_name         string
terminal_code         string
terminal_slug         string
```

---

## GET /game_versions

Obtain the Star Citizen versions currently operated by UEX. It may be out of sync with Star Citizen releases sometimes.

- **Auth:** none
- **Cache TTL:** +1 day
- **Update frequency:** Patch basis (CIG)
- **Response status:** _none_

**Example URLs**
```
https://api.uexcorp.uk/2.0/game_versions
```

**Input**
```
_none_
```

**Output**
```
live    string|null    // current live version, e.g. '4.9'
ptu     string|null    // current ptu versions, e.g. '4.10.0' or empty if there is no PTU set
```

## GET /game_versions_all

Retrieve the full list of historical Star Citizen game versions known to UEX, ordered chronologically (oldest first). Versions without a known release date are not included; pre-numeric early patches are listed by their wiki name (e.g. 'Hangar Module', 'Patch 1' through 'Patch 11.2').

- **Auth:** none
- **Cache TTL:** +1 day
- **Update frequency:** Patch basis (CIG)
- **Response status:** —

**Example URLs**
```
https://api.uexcorp.uk/2.0/game_versions_all
```

**Input**
```
_none_
```

**Output**
```
id                int
game_version      string    // e.g. '4.0.0', '3.24.2a', 'Hangar Module', 'Patch 1'
date_added        int       // release date as Unix timestamp
```

---

## GET /items

Retrieve a comprehensive list of Star Citizen items, including ship components, weapons, and more.

- **Auth:** none
- **Cache TTL:** +1 day
- **Update frequency:** Patch Cycle
- **Response status:** requires_id_category, ok

**Example URLs**
```
https://api.uexcorp.uk/2.0/items?id_category={int}
https://api.uexcorp.uk/2.0/items?id_company={int}
https://api.uexcorp.uk/2.0/items?uuid={string}
https://api.uexcorp.uk/2.0/items?size={string}
```

**Input**
```
// one of these inputs are required
id_category    int       // required   [FK -> get_categories]
id_company     int       [FK -> get_companies]
uuid           string    // star citizen uuid
size           string
```

**Output**
```
id                          int       // route ID, may change during website updates
id_parent                   int
id_category                 int       [FK -> get_categories]
id_company                  int       [FK -> get_companies]
id_vehicle                  int       // if linked to a vehicle   [FK -> get_vehicles]
name                        string
section                     string|null    // coming from categories
category                    string|null    // coming from categories
company_name                string|null    // coming from companies
vehicle_name                string|null    // coming from vehicles
slug                        string    // UEX URLs
size                        string|null
uuid                        string|null    // star citizen uuid
color                       string|null    // red | blue | green | yellow | orange | purple | pink | brown | black | white | gray
color2                      string|null    // red | blue | green | yellow | orange | purple | pink | brown | black | white | gray
url_store                   string|null    // pledge store URL
wiki                        string|null    // wiki URL
quality                     int|null       // quality level
is_exclusive_pledge         int
is_exclusive_subscriber     int
is_exclusive_concierge      int
is_commodity                int
is_harvestable               int
screenshot                  string    // item image URL (suspended due to server costs)
attributes                  json      // deprecated
notification                json      // heads up about an item, such as known bugs, etc.
game_version                string
date_added                  int       // timestamp
date_modified                int      // timestamp
```

---

## GET /items_attributes

Obtain a list of attributes of a specific item. Item attributes are primarily sourced from in-game terminals; in some cases exceptions may be made to include data obtained through datamining. Some attributes might not precisely reflect the actual in-game values, potentially due to outdated information on in-game terminals not yet updated by the CIG team.

- **Auth:** none
- **Cache TTL:** +1 day
- **Update frequency:** Daily
- **Response status:** requires_id_item_or_id_category_or_uuid (deprecated), requires_id_category_or_id_company_or_uuid, ok

**Example URLs**
```
https://api.uexcorp.uk/2.0/items_attributes?id_item={int}
```

**Input**
```
// at least one is required
id_item        int
id_category    int
uuid           string|null    // star citizen uuid
```

**Output**
```
id                        int
id_item                   int            [FK -> get_items]
id_category               int            [FK -> get_categories]
id_category_attribute     int            [FK -> get_categories_attributes]
category_name             string|null
item_name                 string
item_uuid                 string|null
item_wiki                 string|null
attribute_name            string
value                     string|null
unit                      string|null
date_added                int            // timestamp
date_modified             int            // timestamp
```

---

## GET /items_prices

Retrieve a comprehensive list of prices for all items, including armor, ship components, weapons, and more.

- **Auth:** none
- **Cache TTL:** +1 day
- **Update frequency:** Daily
- **Response status:** requires_id_item_or_id_terminal, ok

**Example URLs**
```
// example 1
https://api.uexcorp.uk/2.0/items_prices?id_terminal={int}
// example 2
https://api.uexcorp.uk/2.0/items_prices?id_item={int}
```

**Input**
```
// at least one of these parameters are required
id_terminal     mixed    // up to 10 ids separated by comma
id_item         int
id_category     int      [FK -> get_categories]
uuid            string|null    // star citizen uuid
```

**Output**
```
id                          int
id_item                     int      [FK -> get_items]
id_parent                   int      [FK -> get_items]
id_category                 int      [FK -> get_categories]
id_vehicle                  int      [FK -> get_vehicles]
id_star_system               int     [FK -> get_star_systems]
id_planet                   int      [FK -> get_planets]
id_orbit                    int      [FK -> get_orbits]
id_moon                     int      [FK -> get_moons]
id_city                     int      [FK -> get_cities]
id_outpost                  int      [FK -> get_outposts]
id_poi                      int      [FK -> get_poi]
id_faction                  int      [FK -> get_factions]
id_terminal                 int      [FK -> get_terminals]
price_buy                   float    // last, per unit
price_buy_min                float
price_buy_min_week           float
price_buy_min_month          float
price_buy_max                float
price_buy_max_week           float
price_buy_max_month          float
price_buy_avg                float
price_buy_avg_week           float
price_buy_avg_month          float
price_sell                   float   // last, per unit
price_sell_min                float
price_sell_min_week           float
price_sell_min_month          float
price_sell_max                float
price_sell_max_week           float
price_sell_max_month          float
price_sell_avg                float
price_sell_avg_week           float
price_sell_avg_month          float
durability                   float   // last (%)
durability_min                float
durability_min_week           float
durability_min_month          float
durability_max                float
durability_max_week           float
durability_max_month          float
durability_avg                float
durability_avg_week           float
durability_avg_month          float
faction_affinity             int     // datarunner's affinity average at a location (between -100 and 100)
game_version                 string
date_added                   int     // timestamp
date_modified                 int    // timestamp
item_name                    string
item_wiki                    string|null    // wiki URL
star_system_name             string|null
planet_name                  string|null
orbit_name                   string|null
moon_name                    string|null
space_station_name           string|null
outpost_name                 string|null
city_name                    string|null
terminal_name                string
terminal_code                string
terminal_is_player_owned      int
```

---

## GET /items_prices_all

Retrieve a list of prices for all items in all terminals, all at once.

- **Auth:** none
- **Cache TTL:** +12 hours
- **Update frequency:** Hourly
- **Response status:** ok

**Example URLs**
```
https://api.uexcorp.uk/2.0/items_prices_all
```

**Input**
```
_none_
```

**Output**
```
id                int
id_item           int            [FK -> get_items]
id_terminal       int            [FK -> get_terminals]
id_category       int            [FK -> get_categories]
price_buy         float          // last, per unit
price_sell        float          // last, per unit
date_added        int            // timestamp
date_modified     int            // timestamp
item_name         string
item_uuid         string|null    // star citizen uuid
terminal_name     string
```

---

## GET /jump_points

Retrieve a list of all jump points in the game.

- **Auth:** none
- **Cache TTL:** +1 day
- **Update frequency:** Patch Cycle
- **Response status:** ok

**Example URLs**
```
https://api.uexcorp.uk/2.0/jump_points?id_star_system={int}
```

**Input**
```
id_star_system_origin         int    // optional
id_star_system_destination    int    // optional
id_orbit_origin                int   // optional
id_orbit_destination           int   // optional
```

**Output**
```
id                              int
id_star_system_origin           int         [FK -> get_star_systems]
id_star_system_destination      int         [FK -> get_star_systems]
id_orbit_origin                 int         [FK -> get_orbits]
id_orbit_destination            int         [FK -> get_orbits]
star_system_name_origin         string
star_system_name_destination    string
orbit_name_origin               string|null
orbit_name_destination          string|null
date_added                      int         // timestamp
date_modified                   int         // timestamp
```

---

## GET /jurisdictions

Retrieve a list of all known Star Citizen jurisdictions.

- **Auth:** none
- **Cache TTL:** +1 day
- **Update frequency:** Rarely
- **Response status:** ok

**Example URLs**
```
https://api.uexcorp.uk/2.0/jurisdictions/
```

**Input**
```
_none_
```

**Output**
```
id                      int
id_faction              int            // csv   [FK -> get_factions]
name                    string
nickname                string
is_available            int            // UEX
is_available_live       int            // Star Citizen
is_visible              int            // UEX (public)
is_default              int
wiki                    string|null    // wiki page
date_added              int            // timestamp
date_modified           int            // timestamp
faction_name            string|null
```

---

## GET /marketplace_averages

**DEPRECATED** (Apr 12, 2026 — "This endpoint has been deprecated as part of our move toward a more transparent and accurate model. Its use is strongly discouraged." Migrate to marketplace_prices_averages (filtered) or marketplace_prices_averages_all (full dump).) Retrieve a list of average prices in the UEX Marketplace.

- **Auth:** none
- **Cache TTL:** +1 day
- **Update frequency:** Daily
- **Response status:** requires_id_item_or_id_terminal, ok

**Example URLs**
```
https://api.uexcorp.uk/2.0/marketplace_averages?id_item={int}
```

**Input**
```
// at least one of these parameters are required
id_item         int
id_category     int         [FK -> get_categories]
uuid            string|null    // star citizen uuid
```

**Output**
```
id                int
id_item           int       [FK -> get_items]
id_category       int       [FK -> get_categories]
quality_tier      int       // 0 = Q0, 1 = Q1-499, 2 = Q500-599, 3 = Q600-699, 4 = Q700-799, 5 = Q800-899, 6 = Q900-949, 7 = Q950-1000
quality_count     int       // number of listings contributing to this average
currency          string
price_buy         float     // average in the last 30 days, per unit
price_buy_week    float
price_buy_month   float
price_sell        float     // average in the last 30 days, per unit
price_sell_week   float
price_sell_month  float
game_version      string
date_added        int       // timestamp
date_modified     int       // timestamp
item_name         string
```

Notes: records limited to the last 30 days (based on date_modified).

---

## GET /marketplace_averages_all

**DEPRECATED** (Apr 12, 2026 — "This endpoint has been deprecated as part of our move toward a more transparent and accurate model." Migrate to marketplace_prices_averages_all (full dump) or marketplace_prices_averages (filtered).) Retrieve a list of average prices for all items in the UEX Marketplace, all at once.

- **Auth:** none
- **Cache TTL:** +12 hours
- **Update frequency:** Hourly
- **Response status:** ok

**Example URLs**
```
https://api.uexcorp.uk/2.0/marketplace_averages_all
```

**Input**
```
_none_
```

**Output**
```
id                int
id_item           int            [FK -> get_items]
id_category       int            [FK -> get_categories]
quality_tier      int            // 0 = Q0, 1 = Q1-499, 2 = Q500-599, 3 = Q600-699, 4 = Q700-799, 5 = Q800-899, 6 = Q900-949, 7 = Q950-1000
quality_count     int            // number of listings contributing to this average
currency          string
price_buy         float          // average in the last 30 days, per unit
price_sell        float          // average in the last 30 days, per unit
date_added        int            // timestamp
date_modified     int            // timestamp
item_name         string
item_uuid         string|null    // Star Citizen UUID
```

Notes: records limited to the last 30 days (based on date_modified).

---

## GET /marketplace_favorites

List all advertisements favorited by an user.

- **Auth:** Bearer Token
- **Cache TTL:** —
- **Update frequency:** Realtime
- **Response status:** missing_secret_key, invalid_secret_key, user_not_found, user_not_allowed, ok

**Example URLs**
```
https://api.uexcorp.uk/2.0/marketplace_favorites
```

**Input**
```
secret_key    string    // required, should be passed as header
```

**Output**
```
id                       int
id_listing               int
date_added               int            // timestamp
operation                string         // buy | sell
type                     string         // item | service | contract
slug                     string
category                 string
title                    string
description              string
unit                     string|null
price                    float
in_stock                 int
advertiser_name          string
advertiser_username      string
advertiser_avatar        string|null
```

## GET /marketplace_listings

List all active marketplace advertisements, limited by 100

- **Auth:** none
- **Cache TTL:** —
- **Update frequency:** Realtime
- **Response status:** ok

**Example URLs**
```
https://api.uexcorp.uk/2.0/marketplace_listings/
https://api.uexcorp.uk/2.0/marketplace_listings?id={int}
https://api.uexcorp.uk/2.0/marketplace_listings?slug={string}
https://api.uexcorp.uk/2.0/marketplace_listings?username={string}
https://api.uexcorp.uk/2.0/marketplace_listings?id_item={int}
https://api.uexcorp.uk/2.0/marketplace_listings?id_item={int}&operation={buy|sell}   // 1,000 row limit unlocked
```

**Input**
```
id          int      // optional
slug        string   // optional
username    string   // advertiser ign, optional
id_item     int      // optional, filter by item
operation   string   // optional, buy|sell — unlocks 1,000 row limit when combined with id_item
```

**Output**
```
id                    int
id_category           int          // [FK -> get_categories]
id_item               int          // [FK -> get_items]
id_star_system        int          // [FK -> get_star_systems]
id_terminal           int          // [FK -> get_terminals]
id_organization       int          // [FK -> get_organizations]
operation             string       // transaction type
type                  string       // ad type
slug                  string
title                 string
description           string
unit                  string|null
price                 float
price_old             float
currency              string       // e.g. UEC
language              string       // locale identifier, e.g. en_US
location              string|null  // item or service location, e.g. Port Tressler
source                string|null  // looted|pledged|purchased_in_game|pirated|gifted|crafted
availability          string|null  // immediate|ready_pickup|on_demand|pre_order|work_order|reserve_only|scheduled|in_progress|negotiable
durability            string|null  // 0-100
quality               string|null  // 0-100
in_stock              int
is_sold_out           int
user_name             string       // advertiser full name
user_username         string       // advertiser ign
user_avatar           string|null  // advertiser avatar (if any)
total_views           int          // deprecated
total_negotiations    int          // deprecated
votes                 int          // votes received
photos                string(65535) // urls in array
video_url             string|null  // youtube video URL
hours_expiration      int          // e.g. 1|2|3|5|12|24|48|72|120|168|336|720
date_added            int          // timestamp
date_approved         int          // date when listing was approved by UEX staff
date_expiration       int          // timestamp of when advertisement will stop showing

// Reference: operation
buy
sell

// Reference: type
item
service
contract
```

---

## GET /marketplace_negotiations

List all advertisement deals associated with a user.

- **Auth:** Bearer Token
- **Cache TTL:** —
- **Update frequency:** Realtime
- **Response status:** missing_secret_key, invalid_secret_key, user_not_found, user_not_allowed, ok

**Example URLs**
```
https://api.uexcorp.uk/2.0/marketplace_negotiations
https://api.uexcorp.uk/2.0/marketplace_negotiations?id_listing={int}
```

**Input**
```
secret_key    string   // required, should be passed as header
id            int
id_listing    int
hash          string
```

**Output**
```
id                     int
id_listing             int         // [FK -> get_marketplace_listings]
hash                   string      // negotiation identifier
price                  float       // price (when negotiation started)
unit                   string      // unit (when negotiation started)
currency               string      // currency (when negotiation started)
deal_value             float|null  // final agreed deal value (set by the paying side on successful close)
deal_value_currency    string|null // currency of deal_value
listing_title          string
listing_slug           string
advertiser_name        string
advertiser_username    string
advertiser_avatar      string|null
client_name            string
client_username        string
client_avatar          string|null
is_listing_advertiser  int
date_added             int         // negotiation initiated (timestamp)
date_modified          int         // last interaction date (timestamp)
date_closed            int         // deal end date (timestamp)
date_closed_client     int         // deal end date (timestamp)
```

---

## GET /marketplace_negotiations_messages

Obtain messages from a negotiation

- **Auth:** Bearer Token
- **Cache TTL:** —
- **Update frequency:** Realtime
- **Response status:** missing_secret_key, invalid_secret_key, user_not_found, user_not_allowed, missing_id_negotiation_or_hash, negotiation_not_found, no_messages_found, ok

**Example URLs**
```
https://api.uexcorp.uk/2.0/marketplace_negotiations_messages?hash={string}
```

**Input**
```
secret_key       string   // required, should be passed as header
id_negotiation   int
hash             string
```

**Output**
```
id                  int
id_listing          int          // [FK -> get_marketplace_listings]
id_negotiation      int          // [FK -> get_marketplace_negotiations]
event               string|null  // internal events, default is null
message             string|null
listing_title       string
listing_slug        string
negotiation_hash    string
user_name           string
user_username       string
user_avatar         string
api_name            string       // message source
date_added          int          // timestamp
date_read           int          // timestamp
```

---

## GET /marketplace_prices_averages

Retrieve average prices from the UEX Marketplace, per item, quality tier, operation and currency

- **Auth:** none
- **Cache TTL:** +1 hour
- **Update frequency:** Hourly
- **Response status:** requires_id_item_or_id_category_or_item_uuid_or_item_name, exceeded_id_item_query_limit, ok

**Example URLs**
```
// example 1
https://api.uexcorp.uk/2.0/marketplace_prices_averages?id_item={int}

// example 2 — multiple items (up to 10)
https://api.uexcorp.uk/2.0/marketplace_prices_averages?id_item={int},{int}

// example 3
https://api.uexcorp.uk/2.0/marketplace_prices_averages?item_name={string}
```

**Input**
```
// at least one of these parameters is required
id_item        mixed   // up to 10 ids separated by comma
id_category    int     // [FK -> get_categories]
item_uuid      string  // star citizen item uuid
item_name      string

// optional filters
operation      string|null  // buy | sell
quality_tier   int|null     // 0 = Q0, 1 = Q1–499, 2 = Q500–599, 3 = Q600–699, 4 = Q700–799, 5 = Q800–899, 6 = Q900–949, 7 = Q950–1000
currency       string|null  // e.g. UEC, AUEC
game_version   string|null
```

**Output**
```
id                  int
id_item             int          // [FK -> get_items]
id_category         int          // [FK -> get_categories]
item_uuid           string|null  // star citizen uuid
item_slug           string
item_name           string
quality_tier        int          // 0 = Q0, 1 = Q1–499, 2 = Q500–599, 3 = Q600–699, 4 = Q700–799, 5 = Q800–899, 6 = Q900–949, 7 = Q950–1000
operation           string       // buy | sell
currency            string
unit                string
listings_count      int          // number of active listings contributing to this average
price_avg           float        // current average from active listings, per unit
price_avg_week      float        // 7-day rolling average, per unit
price_avg_month     float        // 30-day rolling average, per unit
game_version        string
date_added          int          // timestamp
date_modified       int          // timestamp
```

---

## GET /marketplace_prices_averages_all

Retrieve average prices for all items in the UEX Marketplace, all at once

- **Auth:** none
- **Cache TTL:** +1 hour
- **Update frequency:** Hourly
- **Response status:** ok

**Example URLs**
```
https://api.uexcorp.uk/2.0/marketplace_prices_averages_all
```

**Input**
_none_

**Output**
```
id                  int
id_item             int          // [FK -> get_items]
quality_tier        int          // 0 = Q0, 1 = Q1–499, 2 = Q500–599, 3 = Q600–699, 4 = Q700–799, 5 = Q800–899, 6 = Q900–949, 7 = Q950–1000
operation           string       // buy | sell
currency            string
unit                string
listings_count      int          // number of active listings contributing to this average
price_avg           float        // current average from active listings, per unit
price_avg_week      float        // 7-day rolling average, per unit
price_avg_month     float        // 30-day rolling average, per unit
game_version        string
date_added          int          // timestamp
date_modified       int          // timestamp
item_name           string
item_uuid           string|null  // star citizen uuid
item_slug           string

// Notes
// one record per unique combination of id_item + quality_tier + operation + currency + unit
// for filtered queries use marketplace_prices_averages [FK -> get_marketplace_prices_averages]
```

---

## GET /marketplace_prices_history

Retrieve historical price snapshots from the UEX Marketplace, one record per listing per price change

- **Auth:** none
- **Cache TTL:** +1 hour
- **Update frequency:** Hourly
- **Response status:** requires_at_least_one_filter, exceeded_id_item_query_limit, ok

**Example URLs**
```
// example 1
https://api.uexcorp.uk/2.0/marketplace_prices_history?id_item={int}

// example 2 — multiple items (up to 10)
https://api.uexcorp.uk/2.0/marketplace_prices_history?id_item={int},{int}

// example 3
https://api.uexcorp.uk/2.0/marketplace_prices_history?id_terminal={int}
```

**Input**
```
// at least one of these parameters is required
id_item          mixed   // up to 10 ids separated by comma
id_listing       int
id_terminal      int
id_star_system   int
id_category      int
item_uuid        string  // star citizen item uuid
item_name        string

// optional filters
operation        string|null  // buy | sell
quality_tier     int|null     // 0 = Q0, 1 = Q1–499, 2 = Q500–599, 3 = Q600–699, 4 = Q700–799, 5 = Q800–899, 6 = Q900–949, 7 = Q950–1000
currency         string|null  // e.g. UEC, AUEC
game_version     string|null
date_start       string|null  // YYYY-MM-DD, default: last 30 days
date_end         string|null  // YYYY-MM-DD, default: today
```

**Output**
```
id                  int
id_item             int
id_listing          int
id_terminal         int
id_star_system      int
id_category         int          // from snapshot
item_id_category    int          // from items table
item_uuid           string|null  // star citizen uuid
item_slug           string
item_name           string
operation           string       // buy | sell
price               float        // per unit
unit                string
currency            string
quality             int          // 0–1000
quality_tier        int          // 0 = Q0, 1 = Q1–499, 2 = Q500–599, 3 = Q600–699, 4 = Q700–799, 5 = Q800–899, 6 = Q900–949, 7 = Q950–1000
game_version        string
date_added          int          // timestamp — when this snapshot was recorded
date_removed        int          // timestamp — 0 if still active
terminal_name       string|null
terminal_nickname   string|null
terminal_slug       string|null
star_system_name    string|null
star_system_code    string|null
```

---

## GET /marketplace_trends

Retrieve the most traded items in the UEX Marketplace, ordered by negotiation activity and total active listings

- **Auth:** none
- **Cache TTL:** +1 hour
- **Update frequency:** Hourly
- **Response status:** ok

**Example URLs**
```
// example 1 — all trending items
https://api.uexcorp.uk/2.0/marketplace_trends/

// example 2 — filter by item name
https://api.uexcorp.uk/2.0/marketplace_trends?item_name={string}

// example 3 — filter by category
https://api.uexcorp.uk/2.0/marketplace_trends?id_category={int}

// example 4 — filter by currency
https://api.uexcorp.uk/2.0/marketplace_trends?currency={string}
```

**Input**
```
// all parameters are optional
id_item        int     // [FK -> get_items]
item_name      string
id_category    int     // [FK -> get_categories]
currency       string  // UEC, WIF, MGS
quality_tier   int     // 0 = Q0, 1 = Q1–499, 2 = Q500–599, 3 = Q600–699, 4 = Q700–799, 5 = Q800–899, 6 = Q900–949, 7 = Q950–1000 — default: 0
```

**Output**
```
id_item                    int          // [FK -> get_items]
item_name                  string
item_slug                  string
currency                   string
price_avg_sell             float|null   // current avg from active sell listings
price_avg_month_sell       float|null   // 30-day rolling average, sell
price_min_sell             float|null
price_max_sell             float|null
listings_count_sell        int|null
price_avg_buy              float|null   // current avg from active buy listings
price_avg_month_buy        float|null   // 30-day rolling average, buy
price_min_buy              float|null
price_max_buy              float|null
listings_count_buy         int|null
total_listings_count       int          // sell + buy active listings
negotiations_count         int
negotiations_open          int
negotiations_success       int
link_prices                string       // UEX marketplace listings page (current active prices)
link_prices_history        string       // UEX marketplace averages page (30-day price history, quality_tier=q0)

// Notes
// results are ordered by negotiations_count DESC, total_listings_count DESC
// up to 500 items returned
// only items with at least one active listing are included
```

---

## GET /moons

Retrieve a list of all moons within a star system.

- **Auth:** none
- **Cache TTL:** +1 day
- **Update frequency:** Patch Cycle
- **Response status:** ok

**Example URLs**
```
// example 1
https://api.uexcorp.uk/2.0/moons?id_star_system={int}

// example 2
https://api.uexcorp.uk/2.0/moons?id_planet={int}
```

**Input**
```
id_star_system    int   // optional
id_faction        int   // optional
id_jurisdiction   int   // optional
id_planet         int   // optional
```

**Output**
```
id                    int
id_star_system        int          // [FK -> get_star_systems]
id_planet             int          // [FK -> get_planets]
id_orbit              int          // [FK -> get_orbits]
id_faction            int          // [FK -> get_factions]
id_jurisdiction       int          // [FK -> get_jurisdictions]
name                  string
name_origin           string       // first moon names
code                  string       // our code
is_available          int          // UEX
is_available_live     int          // Star Citizen
is_visible            int          // UEX (public)
is_default            int
date_added            int          // timestamp
date_modified         int          // timestamp
star_system_name      string|null
planet_name           string|null
orbit_name            string|null
faction_name          string|null
jurisdiction_name     string|null
```

---

## GET /orbits

Retrieve a list of all planets, planetoids and lagrange points orbiting a star.

- **Auth:** none
- **Cache TTL:** +1 day
- **Update frequency:** Patch Cycle
- **Response status:** ok

**Example URLs**
```
https://api.uexcorp.uk/2.0/orbits?id_star_system={int}
```

**Input**
```
id_star_system    int   // optional
id_faction        int   // optional
id_jurisdiction   int   // optional
is_lagrange       int   // optional
```

**Output**
```
id                    int
id_star_system        int          // [FK -> get_star_systems]
id_faction            int          // [FK -> get_factions]
id_jurisdiction       int          // [FK -> get_jurisdictions]
name                  string
name_origin           string       // discovery name
code                  string(10)   // our code
is_available          int          // UEX
is_available_live     int          // Star Citizen
is_visible            int          // UEX (public)
is_default            int
is_lagrange           int
is_man_made           int
is_asteroid           int
is_planet             int
is_star               int
is_jump_point         int
date_added            int          // timestamp
date_modified         int          // timestamp
star_system_name      string|null
faction_name          string|null
jurisdiction_name     string|null
```

---

## GET /orbits_distances

Obtain the last orbital distances reported by Datarunners

- **Auth:** none
- **Cache TTL:** +1 day
- **Update frequency:** Patch Cycle
- **Response status:** missing_id_star_system, ok

**Example URLs**
```
https://api.uexcorp.uk/2.0/orbits_distances?id_star_system={int}
```

**Input**
```
id_star_system                int   // deprecated
id_star_system_origin         int
id_star_system_destination    int
id_orbit_origin                int   // optional
id_orbit_destination           int   // optional
```

**Output**
```
id                          int
id_star_system              int      // [FK -> get_star_systems]  // deprecated
id_star_system_origin       int      // [FK -> get_star_systems]
id_star_system_destination  int      // [FK -> get_star_systems]
id_orbit_origin              int     // [FK -> get_orbits]
id_orbit_destination         int     // [FK -> get_orbits]
distance                    float    // value in Gigameters (Gm)
game_version                string
date_added                  int      // timestamp
date_modified                int     // timestamp
star_system_name            string
orbit_origin_name           string|null
orbit_destination_name      string|null
```

## GET /organizations

Retrieve a list of all organizations added to the UEX website

- **Auth:** Bearer Token
- **Cache TTL:** —
- **Update frequency:** Patch Cycle
- **Response status:** missing_id_organization_or_slug, ok

**Example URLs**
```
// example 1
https://api.uexcorp.uk/2.0/organizations?id_organization={int}
// example 2
https://api.uexcorp.uk/2.0/organizations?slug={string}
```

**Input**
```
id_organization    int    // required
slug               string // required if id_organization is empty
```

**Output**
```
id               int
slug             string      // same from RSI website
name             string
description      string|null
logo             string|null
date_added       int         // timestamp
date_modified    int         // timestamp
```

## GET /outposts

Retrieve a list of all outposts within a star system.

- **Auth:** none
- **Cache TTL:** +1 day
- **Update frequency:** Patch Cycle
- **Response status:** ok

**Example URLs**
```
https://api.uexcorp.uk/2.0/outposts?id_star_system={int}
```

**Input**
```
id_star_system     int    // optional
id_faction         int    // optional
id_jurisdiction    int    // optional
id_planet          int    // optional
id_orbit           int    // optional
id_moon            int    // optional
```

**Output**
```
id                        int
id_star_system            int             [FK -> get_star_systems]
id_planet                 int             [FK -> get_planets]
id_orbit                  int             [FK -> get_orbits]
id_moon                   int             [FK -> get_moons]
id_faction                int             [FK -> get_factions]
id_jurisdiction           int             [FK -> get_jurisdictions]
name                      string
nickname                  string
is_available              int             // UEX
is_available_live         int             // Star Citizen
is_visible                int             // UEX (public)
is_default                int
is_monitored              int
is_armistice              int
is_landable               int
is_decommissioned         int
has_quantum_marker        int
has_trade_terminal        int
has_habitation            int
has_refinery              int
has_cargo_center          int
has_clinic                int
has_food                  int
has_shops                 int
has_refuel                int
has_repair                int
has_gravity               int
has_loading_dock          int
has_docking_port          int
has_freight_elevator      int
pad_types                 string|null     // XS|S|M|L|XL
date_added                int             // timestamp
date_modified             int             // timestamp
star_system_name          string|null
planet_name               string|null
orbit_name                string|null
moon_name                 string|null
faction_name              string|null
jurisdiction_name         string|null
```

## GET /planets

Retrieve a list of all planets within a star system.

- **Auth:** none
- **Cache TTL:** +1 day
- **Update frequency:** Patch Cycle
- **Response status:** ok

**Example URLs**
```
https://api.uexcorp.uk/2.0/planets?id_star_system={int}
```

**Input**
```
id_star_system     int    // optional
id_faction         int    // optional
id_jurisdiction    int    // optional
is_lagrange        int    // optional
```

**Output**
```
id                    int
id_star_system        int            [FK -> get_star_systems]
id_faction            int            [FK -> get_factions]
id_jurisdiction       int            [FK -> get_jurisdictions]
name                  string
name_origin           string         // discovery name
code                  string         // our code
is_available          int            // UEX
is_available_live     int            // Star Citizen
is_visible            int            // UEX (public)
is_default            int
is_lagrange           int
date_added            int            // timestamp
date_modified         int            // timestamp
star_system_name      string|null
faction_name          string|null
jurisdiction_name     string|null
```

## GET /poi

Retrieve a list of points of interest

- **Auth:** none
- **Cache TTL:** +1 day
- **Update frequency:** Patch Cycle
- **Response status:** ok

**Example URLs**
```
https://api.uexcorp.uk/2.0/poi?id_star_system={int}
```

**Input**
```
id_star_system      int    // optional
id_faction          int    // optional
id_jurisdiction     int    // optional
id_planet           int    // optional
id_orbit            int    // optional
id_moon             int    // optional
id_space_station    int    // optional
id_city             int    // optional
id_outpost          int    // optional
```

**Output**
```
id                        int
id_star_system            int             [FK -> get_star_systems]
id_planet                 int             [FK -> get_planets]
id_orbit                  int             [FK -> get_orbits]
id_moon                   int             [FK -> get_moons]
id_space_station          int             [FK -> get_space_stations]
id_city                   int             [FK -> get_cities]
id_outpost                int             [FK -> get_outposts]
id_faction                int             [FK -> get_factions]
id_jurisdiction           int             [FK -> get_jurisdictions]
name                      string
nickname                  string
is_available              int             // UEX
is_available_live         int             // Star Citizen
is_visible                int             // UEX (public)
is_default                int
is_monitored              int
is_armistice              int
is_landable               int
is_decommissioned         int
is_mining_related         int
has_quantum_marker        int
has_trade_terminal        int
has_habitation            int
has_refinery              int
has_cargo_center          int
has_clinic                int
has_food                  int
has_shops                 int
has_refuel                int
has_repair                int
has_gravity               int
has_loading_dock          int
has_docking_port          int
has_freight_elevator      int
pad_types                 string|null     // XS|S|M|L|XL
date_added                int             // timestamp
date_modified             int             // timestamp
star_system_name          string|null
planet_name               string|null
orbit_name                string|null
moon_name                 string|null
space_station_name        string|null
outpost_name              string|null
city_name                 string|null
faction_name              string|null
jurisdiction_name         string|null
```

## GET /polls

Get a list of community polls, or a specific poll with full results.

- **Auth:** none
- **Cache TTL:** —
- **Update frequency:** —
- **Response status:** ok, error

**Example URLs**
```
https://api.uexcorp.uk/2.0/polls
```

**Input**
```
id             string|int    // poll UUID or numeric ID (returns single poll with options)
id_item        int           // filter by linked item          [FK -> get_items]
id_vehicle     int           // filter by linked vehicle       [FK -> get_vehicles]
id_commodity   int           // filter by linked commodity     [FK -> get_commodities]
status         string        // active (default), closed, all
category       string        // economy, trade_marketplace, mining, items_components, ships_vehicles, locations, development, community, other
sort           string        // newest (default), most_voted, ending_soon
```

**Output**
```
id                       string       // UUID
id_item                  int          [FK -> get_items]
id_vehicle               int          [FK -> get_vehicles]
id_commodity             int          [FK -> get_commodities]
question                 string
type                     string       // single, multiple
category                 string
slug                     string
game_version             string
user_username            string
total_votes              int
total_voters             int
is_featured              int
is_official              int
is_recurring             int          // 1 when the poll resets each game patch (tracking poll)
is_active                int
options                  array        // only when requesting single poll by id
options[].id             int
options[].label          string
options[].total_votes    int
options[].percentage     float
date_opened              int          // timestamp
date_closed              int          // timestamp
```

Notes (verbatim from page): All polls require RSI-verified voters, ensuring one real player = one vote. Polls linked to game entities can be filtered by id_item, id_vehicle, or id_commodity. The game_version field indicates which Star Citizen version was current when the poll was created.

## GET /polls_audit

Get the public audit trail for a specific poll, showing all votes.

- **Auth:** none
- **Cache TTL:** —
- **Update frequency:** —
- **Response status:** ok, error

**Example URLs**
```
https://api.uexcorp.uk/2.0/polls_audit
```

**Input**
```
id    string|int    // poll UUID or numeric ID (required)
```

**Output**
```
voter         string    // username or "Anonymous"
option        string    // option label selected
date_added    int       // timestamp
```

Notes (verbatim from page): "Anonymous voters are displayed as 'Anonymous' in the public audit trail. Results are ordered by date descending (most recent first), limited to 500 records."

## GET /refineries_audits

Retrieve a list of all refinery audits submitted by Data Runners.

- **Auth:** none
- **Cache TTL:** +1 day
- **Update frequency:** Monthly (at least)
- **Response status:** ok

**Example URLs**
```
https://api.uexcorp.uk/2.0/refineries_audits
```

**Input**
_none_

**Output**
```
id                     int
id_commodity           int            [FK -> get_commodities]
id_star_system         int            [FK -> get_star_systems]
id_planet              int            [FK -> get_planets]
id_orbit               int            [FK -> get_orbits]
id_moon                int            [FK -> get_moons]
id_space_station       int            [FK -> get_space_stations]
id_city                int            [FK -> get_cities]
id_outpost             int            [FK -> get_outposts]
id_poi                 int            [FK -> get_poi]
id_faction             int            [FK -> get_factions]
id_terminal            int            [FK -> get_terminals]
yield                  int            // yield bonus percentage
capacity               int            // refinery capacity percentage
method                 int            // refining method
quantity               int            // units
quantity_yield         int            // units
quantity_inert         int            // units
total_cost             int            // cost in UEC
total_time             int            // time in minutes
date_added             int            // timestamp
date_reported          int            // timestamp
game_version           string
datarunner             string|null    // datarunner ign
commodity_name         string
star_system_name       string|null
planet_name            string|null
orbit_name             string|null
moon_name              string|null
space_station_name     string|null
city_name              string|null
outpost_name           string|null
terminal_name          string
```

Note (verbatim from page): Limits — Maximum of 500 rows.

## GET /refineries_capacities

Retrieve a list of the estimated capacity percentages for all refineries.

- **Auth:** none
- **Cache TTL:** +1 day
- **Update frequency:** Monthly (at least)
- **Response status:** ok

**Example URLs**
```
https://api.uexcorp.uk/2.0/refineries_capacities
```

**Input**
_none_

**Output**
```
id                     int
id_commodity           int            [FK -> get_commodities]
id_star_system         int            [FK -> get_star_systems]
id_planet              int            [FK -> get_planets]
id_orbit               int            [FK -> get_orbits]
id_moon                int            [FK -> get_moons]
id_space_station       int            [FK -> get_space_stations]
id_city                int            [FK -> get_cities]
id_outpost             int            [FK -> get_outposts]
id_poi                 int            [FK -> get_poi]
id_faction             int            [FK -> get_factions]
id_terminal            int            [FK -> get_terminals]
id_report              int            // last report
value                  int            // yield bonus percentage
value_week             int            // yield bonus percentage, last 7 days
value_month            int            // yield bonus percentage, last 30 days
date_added             int            // timestamp
date_modified          int            // timestamp
star_system_name       string|null
planet_name            string|null
orbit_name             string|null
moon_name              string|null
space_station_name     string|null
city_name              string|null
outpost_name           string|null
terminal_name          string
```

Note (verbatim from page): Limits — Maximum of 500 rows.

## GET /refineries_methods

Retrieve a list of the refining methods used by all in-game refineries

- **Auth:** none
- **Cache TTL:** +1 day
- **Update frequency:** Patch cycle
- **Response status:** ok

**Example URLs**
```
https://api.uexcorp.uk/2.0/refineries_methods
```

**Input**
_none_

**Output**
```
id               int
name             var(255)
code             var(255)
rating_yield     int    // 1 low | 2 medium | 3 high
rating_cost      int    // 1 low | 2 medium | 3 high
rating_speed     int    // 1 slow | 2 medium | 3 fast
date_added       int    // timestamp
date_modified    int    // timestamp
```

## GET /refineries_yields

Retrieve a list of all refineries yields bonuses per commodity

- **Auth:** none
- **Cache TTL:** +1 day
- **Update frequency:** Monthly (at least)
- **Response status:** ok

**Example URLs**
```
https://api.uexcorp.uk/2.0/refineries_yields
```

**Input**
_none_

**Output**
```
id                     int
id_commodity           int            [FK -> get_commodities]
id_star_system         int            [FK -> get_star_systems]
id_planet              int            [FK -> get_planets]
id_orbit               int            [FK -> get_orbits]
id_moon                int            [FK -> get_moons]
id_space_station       int            [FK -> get_space_stations]
id_city                int            [FK -> get_cities]
id_outpost             int            [FK -> get_outposts]
id_poi                 int            [FK -> get_poi]
id_faction             int            [FK -> get_factions]
id_terminal            int            [FK -> get_terminals]
id_report              int            // last report
value                  int            // percentage of yield bonus at refinery
value_week             int            // percentage of yield bonus at refinery (last 7 days)
value_month            int            // percentage of yield bonus at refinery (last 30 days)
date_added             int            // timestamp
date_modified          int            // timestamp
commodity_name         string
star_system_name       string|null
planet_name            string|null
orbit_name             string|null
moon_name              string|null
space_station_name     string|null
city_name              string|null
terminal_name          string
```

Note (verbatim from page): Limits — Maximum of 500 rows.

## GET /release_notes

Output UEX dev notes.

- **Auth:** none
- **Cache TTL:** +1 day
- **Update frequency:** Monthly
- **Response status:** _none_ (no Response Status row present on the page)

**Example URLs**
```
https://api.uexcorp.uk/2.0/release_notes
```

**Input**
```
_none_
```

**Output**
```
id              int
date_updated    int
content         text(65535)
```

---

## GET /space_stations

Retrieve a list of all space stations within a star system.

- **Auth:** none
- **Cache TTL:** +1 day
- **Update frequency:** Patch Cycle
- **Response status:** ok

**Example URLs**
```
https://api.uexcorp.uk/2.0/space_stations?id_star_system={int}
```

**Input**
```
id_star_system     int    // optional
id_faction         int    // optional
id_jurisdiction    int    // optional
id_planet          int    // optional
id_orbit           int    // optional
id_moon            int    // optional
id_city            int    // optional
```

**Output**
```
id                        int
id_star_system            int    [FK -> get_star_systems]
id_planet                 int    [FK -> get_planets]
id_orbit                  int    [FK -> get_orbits]
id_moon                   int    [FK -> get_moons]
id_city                   int    // city next to space station   [FK -> get_cities]
id_faction                int    [FK -> get_factions]
id_jurisdiction           int    [FK -> get_jurisdictions]
name                      string
nickname                  string    // our nickname
is_available              int    // UEX
is_available_live         int    // Star Citizen
is_visible                int    // UEX (public)
is_default                int
is_monitored              int
is_armistice              int
is_landable               int
is_decommissioned         int
is_lagrange               int
is_jump_point             int
has_quantum_marker        int
has_trade_terminal        int
has_habitation            int
has_refinery              int
has_cargo_center          int
has_clinic                int
has_food                  int
has_shops                 int
has_refuel                int
has_repair                int
has_gravity               int
has_loading_dock          int
has_docking_port          int
has_freight_elevator      int
pad_types                 string|null    // XS|S|M|L|XL
date_added                int    // timestamp
date_modified             int    // timestamp
star_system_name          string|null
planet_name               string|null
orbit_name                string|null
city_name                 string|null
faction_name              string|null
jurisdiction_name         string|null
```

---

## GET /star_systems

Retrieve a list of all star systems in the Star Citizen universe.

- **Auth:** none
- **Cache TTL:** +1 day
- **Update frequency:** Patch Cycle
- **Response status:** ok

**Example URLs**
```
https://api.uexcorp.uk/2.0/star_systems
```

**Input**
```
_none_
```

**Output**
```
id                    int
id_faction            int       [FK -> get_factions]
id_jurisdiction       int       [FK -> get_jurisdictions]
name                  string
code                  string    // our code
is_available          int       // UEX
is_available_live     int       // Star Citizen
is_visible            int       // UEX (public)
is_default            int
wiki                  string|null    // Wiki URL
date_added            int       // timestamp
date_modified         int       // timestamp
faction_name          string|null
jurisdiction_name     string|null
```

---

## GET /terminals

Retrieve a comprehensive list of all terminals in the game, including trade terminals, item terminals, vehicle rentals, and more.

- **Auth:** none
- **Cache TTL:** +12 hours
- **Update frequency:** Patch Cycle
- **Response status:** invalid_type, ok

**Example URLs**
```
https://api.uexcorp.uk/2.0/terminals?id_star_system={int}
https://api.uexcorp.uk/2.0/terminals?id_planet={int}
https://api.uexcorp.uk/2.0/terminals?name={string}
https://api.uexcorp.uk/2.0/terminals?code={string}
```

**Input**
```
id_star_system      int
id_planet           int
id_orbit            int
id_moon             int
id_space_station    int
id_city             int
id_outpost          int
id_poi               int
id_faction           int
id_company           int
type                  string    // one of: commodity | item | commodity_raw | vehicle_buy | vehicle_rent | fuel | refinery_audit
name                  string
fullname              string
displayname           string
code                  string
```

**Output**
```
id                             int
id_star_system                int    [FK -> get_star_systems]
id_planet                     int    [FK -> get_planets]
id_orbit                       int    [FK -> get_orbits]
id_moon                        int    [FK -> get_moons]
id_space_station               int    [FK -> get_space_stations]
id_outpost                     int    [FK -> get_outposts]
id_poi                          int    [FK -> get_poi]
id_city                         int    [FK -> get_cities]
id_faction                      int    [FK -> get_factions]
id_company                      int    [FK -> get_companies]
name                            string
fullname                        string    // rule based, in-game aligned canonical name
nickname                        string    // short name
displayname                     string    // storage name | name displayed on the terminal screen
code                            string    // our code
type                            string    // our type
contact_url                     string|null    // contact page URL (player terminals only)
screenshot                      string
screenshot_full                 string
screenshot_author               string
mcs                             int    // deprecated, replaced by max_container_size
is_available                    int    // UEX
is_available_live               int    // Star Citizen
is_visible                      int    // UEX (public)
is_default_system               int    // uex default
is_affinity_influenceable       int    // reputation affects prices
is_habitation                   int
is_refinery                     int
is_cargo_center                 int
is_medical                      int
is_food                         int
is_shop_fps                     int    // trading fps items
is_shop_vehicle                 int    // trading vehicle stuff
is_refuel                       int
is_repair                       int
is_nqa                          int    // no questions asked terminal
is_jump_point                   int    // located at jump point
is_player_owned                 int
is_auto_load                    int
has_loading_dock                int
has_docking_port                int
has_freight_elevator            int
game_version                    string|null    // last updated
date_added                      int    // timestamp
date_modified                   int    // timestamp
star_system_name                string|null
planet_name                     string|null
orbit_name                      string|null
moon_name                       string|null
space_station_name              string|null
outpost_name                    string|null
city_name                       string|null
faction_name                    string|null
company_name                    string|null
max_container_size              int    // in scu, csv values, 1|2|4|8|16|24|32
```

---

## GET /terminals_distances

Estimate the distance (in gigameters) between two terminals within the Star Citizen universe.

- **Auth:** none
- **Cache TTL:** +12 hours
- **Update frequency:** Hourly
- **Response status:** missing_id_terminal_origin, missing_id_terminal_destination, ok

**Example URLs**
```
https://api.uexcorp.uk/2.0/terminals_distances?id_terminal_origin={int}&id_terminal_destination={int}
```

**Input**
```
id_terminal_origin         int    [FK -> get_terminals]
id_terminal_destination    int    [FK -> get_terminals]
```

**Output**
```
orbit_name_origin              string|null
terminal_name_origin           string
terminal_nickname_origin       string
terminal_code_origin           string
orbit_name_destination         string|null
terminal_name_destination      string
terminal_nickname_destination  string
terminal_code_destination      string
distance                       float    // gigameters
```

---

## GET /user

Obtain details from a specific user such as name, avatar, etc.

- **Auth:** none
- **Cache TTL:** none
- **Update frequency:** Realtime
- **Response status:** missing_secret_key_or_username, invalid_secret_key, user_not_found, user_not_allowed (user banned or disabled by UEX), ok

**Example URLs**
```
https://api.uexcorp.uk/2.0/user/?username=[string]
```

**Input**
```
secret-key    string    // header, one of these inputs are required
username      string    // required if no secret key is provided
```

**Output**
```
id                        int
ids_factions              string|null    // separated by comma   [FK -> get_factions]
ids_star_systems          string|null    // separated by comma   [FK -> get_star_systems]
name                      string
username                  string
email                     string|null    // only available through secret key
avatar                    string|null
bio                       string|null
website_url               string|null
timezone                  string|null
language                  string|null
discord_username          string|null    // only available through secret key
twitch_username           string|null
day_availability          string|null    // csv; values: weekdays, weekends
time_availability         string|null    // csv; values: morning, afternoon, evening
specializations           string|null    // csv; values: datarunner, escort, exploration, engineer, gunner, hauling, medical, mercenary, mining, other, pilot, piracy, racer, refining, refueling, repairing, roleplay, salvaging, scanning, scientist, towing, trading, transit
languages                 string|null    // csv; values: ar, ca, zh, nl, en, fr, de, it, jp, pt, ru, es, xx
archetypes                string|null    // csv; values: artist, engineer, explorer, lover, novice, outlaw, player_one, protector, strategist, support, trickster, warlord
is_datarunner             int
is_datarunner_banned      int
is_staff                  int
is_away_game              int|null
date_added                int
date_modified             int
date_disabled             int|null
date_rsi_verified         int|null
date_twitch_verified      int|null
```

---

## GET /user_notifications

Retrieve the latest notifications for a user account.

- **Auth:** Bearer Token
- **Cache TTL:** none
- **Update frequency:** Realtime
- **Response status:** missing_secret_key, invalid_secret_key, user_not_found, user_not_allowed (user banned or disabled by UEX), ok

**Example URLs**
```
https://api.uexcorp.uk/2.0/user_notifications/
```

**Input**
```
secret-key    // header
```

**Output**
```
id             int
message        string
redir          string        // URL path
code           string|null   // optional, notification identifier
date_added     int
date_read      int
```

---

## GET /user_refineries_jobs

Obtain a list of refinery jobs made by an user.

- **Auth:** Bearer Token
- **Cache TTL:** none
- **Update frequency:** Realtime
- **Response status:** missing_secret_key, invalid_secret_key, user_not_found, user_not_allowed (user banned or disabled by UEX), no_refinery_jobs_found, ok

**Example URLs**
```
https://api.uexcorp.uk/2.0/user_refineries_jobs/
```

**Input**
```
secret-key    // header
```

**Output**
```
id                       int
id_terminal              int       [FK -> get_terminals]
id_refinery_method       int       [FK -> get_refineries_methods]
cost                     float
time_minutes             int
date_added               int
date_modified            int
date_expiration          int
terminal_name            string
items                    array
items[id]                int
items[id_commodity]      int       [FK -> get_commodities]
items[quantity]          int       // units
items[yield]             int       // units
items[yield_bonus]       int       // percent
items[commodity_name]    string
items[commodity_code]    string
items[commodity_slug]    string
```

---

## GET /user_trades

Obtain a list of trade transactions made by an user.

- **Auth:** Bearer Token
- **Cache TTL:** none
- **Update frequency:** Realtime
- **Response status:** missing_secret_key, invalid_secret_key, user_not_found, user_not_allowed (user banned or disabled by UEX), no_trades_found, ok

**Example URLs**
```
https://api.uexcorp.uk/2.0/user_trades/
```

**Input**
```
secret-key    // header
```

**Output**
```
id                        int
id_terminal               int            [FK -> get_terminals]
id_commodity              int            [FK -> get_commodities]
id_user_fleet             int            [FK -> get_fleet]
id_vehicle                int            [FK -> get_vehicles]
id_organization           int            [FK -> get_organizations]
operation                 string
scu                       int
price                     float          // scu
date_added                int
date_modified             int
user_name                 string
user_username             string
commodity_name            string
terminal_name             string
user_fleet_name           string|null
user_fleet_serial         string|null
user_fleet_screenshot     string|null
vehicle_name              string|null
organization_name         string|null
```

---

## GET /vehicles

Retrieve a list of Star Citizen vehicles, including spaceships and ground vehicles.

- **Auth:** none
- **Cache TTL:** +12 hours
- **Update frequency:** Patch Cycle
- **Response status:** ok

**Example URLs**
```
https://api.uexcorp.uk/2.0/vehicles?id_company={int}
```

**Input**
```
id_company    int    // vehicle manufacturer, optional
```

**Output**
```
id                       int
id_company               int       // manufacturer   [FK -> get_companies]
id_parent                int       // parent ship series
ids_vehicles_loaners     string    // csv
name                     string
name_full                string
slug                     string
uuid                     string|null    // star citizen uuid
scu                      float
crew                     string    // csv
mass                     float
width                    float
height                   float
length                   float
fuel_quantum             float     // scu
fuel_hydrogen            float     // scu
container_sizes          string    // scu, csv
is_addon                 int       // e.g. galaxy refinery module
is_boarding              int
is_bomber                int
is_cargo                 int
is_carrier               int
is_civilian              int
is_concept                int
is_construction           int
is_datarunner             int
is_docking                int    // docking port
is_emp                    int
is_exploration             int
is_ground_vehicle          int
is_hangar                  int    // contains hangar, e.g. polaris
is_industrial               int
is_interdiction             int
is_loading_dock             int    // operated in loading docks, e.g. hull-c
is_medical                  int
is_military                 int
is_mining                   int
is_passenger                int
is_qed                      int
is_racing                   int
is_refinery                 int
is_refuel                   int
is_repair                   int
is_research                 int
is_salvage                  int
is_scanning                 int
is_science                  int
is_showdown_winner          int
is_spaceship                int
is_starter                  int
is_stealth                  int
is_tractor_beam             int
is_quantum_capable          int
url_photo                   string|null
url_store                   string|null
url_brochure                string|null
url_hotsite                 string|null
url_video                   string|null
url_photos                  array(65535)    // sourced from RSI website, not currently updated; deprecated
pad_type                    string|null     // XS|S|M|L|XL
game_version                string          // version it was announced or updated
date_added                  int             // timestamp
date_modified                int            // timestamp
company_name                 string|null    // manufacturer
```

## GET /vehicles_loaners

Retrieve a list of Star Citizen vehicles loaners for a specific vehicle ID

- **Auth:** none
- **Cache TTL:** +12 hours
- **Update frequency:** Weekly
- **Response status:** ok

**Example URLs**
```
https://api.uexcorp.uk/2.0/vehicles_loaners?id_vehicle={int}
https://api.uexcorp.uk/2.0/vehicles_loaners?name={string}
```

**Input**
```
id_vehicle    int              // optional
uuid          string|null      // star citizen uuid
name          string           // optional
```

**Output**
```
id                     int             [FK -> get_vehicles]
id_company             int             // manufacturer   [FK -> get_companies]
id_parent              int             // parent ship series
ids_vehicles_loaners   string          // csv
name                   string
name_full              string
uuid                   string|null     // star citizen uuid
scu                    int
crew                   string
is_addon               int
is_concept             int
is_civilian            int
is_military            int
is_exploration         int
is_passenger           int
is_industrial          int
is_mining              int
is_salvage             int
is_refinery            int
is_cargo               int
is_medical             int
is_racing              int
is_repair              int
is_refuel              int
is_interdiction        int
is_tractor_beam        int
is_qed                 int
is_emp                 int
is_construction        int
is_datarunner          int
is_science             int
is_boarding            int
is_stealth             int
is_research            int
is_carrier             int
is_ground_vehicle      int
is_spaceship           int
is_showdown_winner     int
url_store              string|null
url_brochure           string|null
url_hotsite            string|null
url_video              string|null
url_photos             array(65535)
game_version           string          // version it was announced or updated
date_added             int             // timestamp
date_modified          int             // timestamp
company_name           string|null     // manufacturer name

// loaners
loaners[][id]                    int             [FK -> get_vehicles]
loaners[][id_company]            int             [FK -> get_companies]
loaners[][id_parent]             int
loaners[][ids_vehicles_loaners]  string
loaners[][name]                  string
loaners[][name_full]             string
loaners[][scu]                   int
loaners[][crew]                  string
loaners[][is_addon]              int
loaners[][is_concept]            int
loaners[][is_civilian]           int
loaners[][is_military]           int
loaners[][is_exploration]        int
loaners[][is_passenger]          int
loaners[][is_industrial]         int
loaners[][is_mining]             int
loaners[][is_salvage]            int
loaners[][is_refinery]           int
loaners[][is_cargo]              int
loaners[][is_medical]            int
loaners[][is_racing]             int
loaners[][is_repair]             int
loaners[][is_refuel]             int
loaners[][is_interdiction]       int
loaners[][is_tractor_beam]       int
loaners[][is_qed]                int
loaners[][is_emp]                int
loaners[][is_construction]       int
loaners[][is_datarunner]         int
loaners[][is_science]            int
loaners[][is_boarding]           int
loaners[][is_stealth]            int
loaners[][is_research]           int
loaners[][is_carrier]            int
loaners[][is_ground_vehicle]     int
loaners[][is_spaceship]          int
loaners[][is_showdown_winner]    int
loaners[][url_store]             string|null
loaners[][url_brochure]          string|null
loaners[][url_hotsite]           string|null
loaners[][url_video]             string|null
loaners[][url_photos]            array(65535)
loaners[][game_version]          string|null     // version it was announced or updated
loaners[][date_added]            int
loaners[][date_modified]         int
loaners[][company_name]          string|null
```

---

## GET /vehicles_prices

Obtain a daily updated list of vehicle prices in CIG's pledge store, managed either automatically by our bot or manually by the staff.

- **Auth:** none
- **Cache TTL:** +12 hours
- **Update frequency:** Daily
- **Response status:** ok

**Example URLs**
```
https://api.uexcorp.uk/2.0/vehicles_prices?id_vehicle={int}
```

**Input**
```
id_vehicle    int              // optional
uuid          string|null      // star citizen uuid
```

**Output**
```
id                    int
id_vehicle            int
price                 float
price_warbond         float
price_package         float
price_concierge       float
on_sale               int
on_sale_warbond       int
on_sale_package       int
on_sale_concierge     int
currency              var(255)   // e.g. USD
game_version          string
date_added            int        // timestamp
date_modified         int        // timestamp
vehicle_name          string
```

---

## GET /vehicles_purchases_prices

Retrieve a list of all in-game vehicle purchase prices.

- **Auth:** none
- **Cache TTL:** +12 hours
- **Update frequency:** Hourly
- **Response status:** requires_id_vehicle_or_id_terminal, ok

**Example URLs**
```
// example 1
https://api.uexcorp.uk/2.0/vehicles_purchases_prices?id_terminal={int}

// example 2
https://api.uexcorp.uk/2.0/vehicles_purchases_prices?id_vehicle={int}
```

**Input**
```
id_terminal    mixed          // up to 10 ids separated by comma
uuid           string|null    // star citizen uuid
id_vehicle     int
```

**Output**
```
id                            int
id_vehicle                    int          [FK -> get_vehicles]
id_star_system                int          [FK -> get_star_systems]
id_planet                     int          [FK -> get_planets]
id_orbit                      int          [FK -> get_orbits]
id_moon                       int          [FK -> get_moons]
id_city                       int          [FK -> get_cities]
id_outpost                    int          [FK -> get_outposts]
id_poi                        int          [FK -> get_poi]
id_faction                    int          [FK -> get_factions]
id_terminal                   int          [FK -> get_terminals]
price_buy                     float        // last
price_buy_min                 float
price_buy_min_week            float
price_buy_min_month           float
price_buy_max                 float
price_buy_max_week            float
price_buy_max_month           float
price_buy_avg                 float
price_buy_avg_week            float
price_buy_avg_month           float
faction_affinity              int          // datarunner's affinity average at a location (between -100 and 100)
game_version                  string
date_added                    int          // timestamp
date_modified                 int          // timestamp
datarunner                    string|null  // updated by
star_system_name              string|null
planet_name                   string|null
orbit_name                    string|null
moon_name                     string|null
space_station_name            string|null
outpost_name                  string|null
city_name                     string|null
terminal_name                 string
terminal_code                 string
terminal_is_player_owned      int
```

---

## GET /vehicles_purchases_prices_all

Retrieve a list of prices for all vehicles purchases in all terminals, all at once

- **Auth:** none
- **Cache TTL:** +12 hours
- **Update frequency:** Hourly
- **Response status:** ok

**Example URLs**
```
https://api.uexcorp.uk/2.0/vehicles_purchases_prices_all
```

**Input**
```
_none_
```

**Output**
```
id                int
id_vehicle        int      [FK -> get_vehicles]
id_terminal       int      [FK -> get_terminals]
price_buy         float    // last
date_added        int      // timestamp
date_modified     int      // timestamp
vehicle_name      string
terminal_name     string
```

---

## GET /vehicles_rentals_prices

Retrieve a list of all in-game vehicle rental prices.

- **Auth:** none
- **Cache TTL:** +12 hours
- **Update frequency:** Hourly
- **Response status:** requires_id_vehicle_or_id_terminal, ok

**Example URLs**
```
// example 1
https://api.uexcorp.uk/2.0/vehicles_rentals_prices?id_terminal={number}

// example 2
https://api.uexcorp.uk/2.0/vehicles_rentals_prices?id_vehicle={number}
```

**Input**
```
id_terminal    mixed          // up to 10 ids separated by comma
uuid           string|null    // star citizen uuid
id_vehicle     int
```

**Output**
```
id                            int
id_vehicle                    int          [FK -> get_vehicles]
id_star_system                int          [FK -> get_star_systems]
id_planet                     int          [FK -> get_planets]
id_orbit                      int          [FK -> get_orbits]
id_moon                       int          [FK -> get_moons]
id_city                       int          [FK -> get_cities]
id_outpost                    int          [FK -> get_outposts]
id_poi                        int          [FK -> get_poi]
id_faction                    int          [FK -> get_factions]
id_terminal                   int          [FK -> get_terminals]
price_rent                    float        // last
price_rent_min                float
price_rent_min_week           float
price_rent_min_month          float
price_rent_max                float
price_rent_max_week           float
price_rent_max_month          float
price_rent_avg                float
price_rent_avg_week           float
price_rent_avg_month          float
faction_affinity              int          // datarunner's affinity average at a location (between -100 and 100)
game_version                  string
date_added                    int          // timestamp
date_modified                 int          // timestamp
datarunner                    string|null  // updated by
star_system_name              string|null
planet_name                   string|null
orbit_name                    string|null
moon_name                     string|null
space_station_name            string|null
outpost_name                  string|null
city_name                     string|null
terminal_name                 string
terminal_code                 string
terminal_is_player_owned      int
```

---

## GET /vehicles_rentals_prices_all

Retrieve a list of prices for all vehicles rentals in all terminals, all at once

- **Auth:** none
- **Cache TTL:** +12 hours
- **Update frequency:** Hourly
- **Response status:** ok

**Example URLs**
```
https://api.uexcorp.uk/2.0/vehicles_rentals_prices_all
```

**Input**
```
_none_
```

**Output**
```
id                int
id_vehicle        int      [FK -> get_vehicles]
id_terminal       int      [FK -> get_terminals]
price_rent        float    // last
date_added        int      // timestamp
date_modified     int      // timestamp
vehicle_name      string
terminal_name     string
```

---

## GET /wallet_balance

Retrieve user wallet balance

- **Auth:** Bearer Token
- **Cache TTL:** —
- **Update frequency:** Realtime
- **Response status:** missing_secret_key, invalid_secret_key, user_not_found, user_not_allowed (user banned or disabled by UEX), ok

**Example URLs**
```
https://api.uexcorp.uk/2.0/wallet_balance/
```

**Input (POST)**
```
secret-key    // header
```

**Output**
```
balance    float
```

## POST /data_edit

Correct a report you submitted to the UEX Datacenter.

The values you send replace the report's values entirely. A field you omit is cleared, not kept — send the corrected report in full, as you would with data_submit. The terminal, report type, commodity/item/vehicle, and game version cannot be changed. Editing returns the report to the pipeline start; earlier approvals or declines clear. Datarunners edit only their own reports until consolidation. Moderators, administrators, and developers can edit any report, including consolidated ones.

- **Auth:** `secret-key` header required (listed under Input). Note: the docs' own Auth badge is blank ("—") on several write endpoints, but the header is still mandatory — omitting it returns `missing_secret_key`.
- **Cache TTL:** —
- **Update frequency:** —
- **Response status:** service_unavailable, invalid_input, access_denied, missing_secret_key, invalid_secret_key, missing_id, user_not_found, user_disabled, user_not_allowed, report_not_found, invalid_type, type_not_available, report_consolidated, faction_affinity_under_minimum_range, faction_affinity_under_maximum_range, has_no_prices_and_no_is_missing_set, has_both_price_buy_and_price_sell, has_prices_and_is_missing_set, has_both_scu_buy_and_scu_sell, cannot_have_both_status_buy_and_status_sell, cannot_have_both_price_buy_and_status_sell, cannot_have_both_price_buy_and_scu_sell, cannot_have_both_price_sell_and_status_buy, cannot_have_both_price_sell_and_scu_buy, invalid_status_buy, invalid_status_sell, invalid_container_size, invalid_quality, ok

**Example URLs**
```
https://api.uexcorp.uk/2.0/data_edit
```

**Input**
```
// Header
secret-key           string       // required user secret key, obtained in user profile

// POST
id                   int          // required; the report to correct
is_production        int          // 1 for production, 0 for testing
price_buy            float        // price for types 'commodity', 'item', 'vehicle_buy'; commodity prices are per SCU; only one price input allowed; leave empty if 'is_missing'
price_sell           float        // price for types 'commodity', 'item'
price_rent           float        // price for type 'vehicle_rent'
is_missing           int          // if item is missing at terminal, send 1, else 0
scu_buy              int          // inventory amount displayed at terminal (only for type 'commodity')
scu_sell             int          // inventory amount displayed at terminal (only for type 'commodity')
status_buy           int          // inventory status (only for type 'commodity'); see get_commodities_status [FK -> get_commodities_status]; 1 = out of stock, 7 = maximum
status_sell          int          // inventory status (only for type 'commodity'); see get_commodities_status [FK -> get_commodities_status]; 1 = out of stock, 7 = maximum
quality              int|null     // quality of commodity at terminal (only for type 'commodity', 0-1000)
container_sizes      string|null  // e.g. "1,2,4,8,16,24,32"; csv; only for type 'commodity'
faction_affinity     int          // affinity level between -100 and 100 (only for types 'commodity' and 'item')
details              string|null  // e.g. "trade terminal is not working"
```

**Input Example**
```json
{
  "id": 1234567,
  "is_production": 0,
  "price_sell": 120,
  "scu_sell": 593,
  "status_sell": 2,
  "details": "corrected a typo in the price"
}
```

**Output**
```
id                    int
type                  string
is_new_at_location    int      // recomputed from the corrected values
date_modified         int      // timestamp, 0 when is_production is 0
```

---

## POST /data_submit

Submit reports to the UEX Datacenter.

- **Auth:** `secret-key` header required (listed under Input). Note: the docs' own Auth badge is blank ("—") on several write endpoints, but the header is still mandatory — omitting it returns `missing_secret_key`.
- **Cache TTL:** —
- **Update frequency:** —
- **Response status:** ok, service_unavailable, invalid_input, no_api_found, missing_secret_key, invalid_secret_key, ptu_reports_not_allowed, invalid_date, invalid_game_version, max_rows_exceeded, faction_affinity_under_minimum_range, faction_affinity_under_maximum_range, faction_affinity_not_allowed_for_current_type, user_not_found, user_not_allowed, user_disabled, missing_type, invalid_type, type_not_available, missing_id_terminal, terminal_not_found, not_allowed_player_terminal, missing_prices_array, invalid_prices_array, reference_key_not_supplied, too_many_reports, duplicated_report, no_commodities_found, no_items_found, no_categories_found, no_vehicles_found, invalid_prices_array_format, screenshot_length_exceeds_limit, screenshot_required, image_upload_error, image_storage_error, database_error, missing_id_commodity, invalid_id_commodity, has_no_prices_and_no_is_missing_set, has_both_price_buy_and_price_sell, has_both_scu_buy_and_scu_sell, cannot_have_both_status_buy_and_status_sell, cannot_have_both_price_buy_and_status_sell, cannot_have_both_price_sell_and_status_buy, cannot_have_both_price_buy_and_scu_sell, cannot_have_both_price_sell_and_scu_buy, invalid_status_buy, invalid_status_sell, invalid_quality, invalid_id_item, missing_id_category, invalid_id_category, id_item_or_name_not_provided, has_both_price_buy_and_price_sell, missing_category_or_subcategory, invalid_category, invalid_subcategory, missing_id_vehicle, invalid_id_vehicle, has_prices_and_is_missing_set

**Example URLs**
```
https://api.uexcorp.uk/2.0/data_submit
```

**Input**
```
// Header
secret-key                     string     // required; user secret key obtained in user profile; should be passed via header

// POST Body
id_terminal                    int        // required [FK -> get_terminals]
type                           string     // required; one of: commodity, item, vehicle_buy, vehicle_rent
is_production                  int        // required; 1 for production, 0 for testing
prices[0]                      array      // required; array of price objects (max 500 rows per submission)
prices[0][id_commodity]        int        // required for type 'commodity' [FK -> get_commodities]
prices[0][id_item]             int        // only for type 'item' [FK -> get_items]
prices[0][id_category]         int        // required if item `name` is provided [FK -> get_categories]
prices[0][name]                string|null // item name; required only if `id_item` is missing
prices[0][id_vehicle]          int        // required for type 'vehicle_rent' or 'vehicle_buy' [FK -> get_vehicles]
prices[0][price_buy]           float      // buy price; only one price field allowed; leave empty if 'is_missing'
prices[0][price_sell]          float      // sell price; only one price field allowed; leave empty if 'is_missing'
prices[0][price_rent]          float      // rent price; exclusive for type 'vehicle_rent'
prices[0][is_missing]          int        // 1 if item is missing at terminal, else 0
prices[0][scu_buy]             int        // inventory amount displayed at terminal (only for type 'commodity')
prices[0][scu_sell]            int        // inventory amount displayed at terminal (only for type 'commodity')
prices[0][status_buy]          int        // [FK -> get_commodities_status]; values 1-7 (out of stock to maximum)
prices[0][status_sell]         int        // [FK -> get_commodities_status]; values 1-7 (out of stock to maximum)
prices[0][quality]             int|null   // quality of commodity at terminal; range 0-1000 (only for type 'commodity')
faction_affinity               int        // faction affinity level between -100 and 100 (only for types 'commodity' and 'item')
container_sizes                string|null // csv of container sizes in SCU (allowed values: 1, 2, 4, 8, 16, 24, 32)
details                        string|null // additional report details (e.g., "trade terminal is not working")
game_version                   string|null // Star Citizen version; default is LIVE version (4.9)
screenshot                     string     // PNG/JPG image in base64 format, up to 10.00 MB; required for new datarunners (90-day evaluation period)
date_added                     int|null   // report date (optional; must be a past date within the last 30 days)
```

**Status Reference: status_buy|status_sell**
```
1  Out of Stock (Empty)
2  Very Low Inventory
3  Low Inventory
4  Medium Inventory
5  High Inventory
6  Very High Inventory
7  Maximum Inventory (Full)
```

**Input Example: commodity**
```json
{
  "id_terminal": 89,
  "type": "commodity",
  "is_production": 0,
  "prices": [
    { "id_commodity": 1, "price_sell": 120, "scu_sell": 593, "status_sell": 2 },
    { "id_commodity": 4, "price_sell": 900, "scu_sell": 652, "status_sell": 5 },
    { "id_commodity": 24, "price_buy": 136, "scu_buy": 529, "status_buy": 1 }
  ],
  "faction_affinity": 15,
  "details": "The Commons have become a junkyard!",
  "game_version": "4.9",
  "screenshot": "SSBrbmV3IHlvdSB3b3VsZCBkbyB0aGlzIDotKSBDaGVlcnMh"
}
```

**Input Example: item**
```json
{
  "id_terminal": 169,
  "type": "item",
  "is_production": 0,
  "prices": [
    { "id_item": 2641, "price_buy": 303 },
    { "id_item": 1406, "price_buy": 1000 },
    { "id_item": 618, "price_sell": 5000 }
  ],
  "faction_affinity": 10,
  "details": "No burritos found",
  "game_version": "4.9"
}
```

**Input Example: item (new items only)**
```json
{
  "id_terminal": 169,
  "type": "item",
  "is_production": 0,
  "prices": [
    { "name": "Picoball", "id_category": 37, "price_sell": 1000000 }
  ],
  "details": "No burritos found",
  "game_version": "4.9"
}
```

**Input Example: vehicle_buy**
```json
{
  "id_terminal": 112,
  "type": "vehicle_buy",
  "is_production": 0,
  "prices": [
    { "id_vehicle": 113, "price_buy": 4912500 },
    { "id_vehicle": 34, "price_buy": 4925500 }
  ],
  "details": "Orison is beautiful!",
  "game_version": "4.9"
}
```

**Input Example: vehicle_rent**
```json
{
  "id_terminal": 147,
  "type": "vehicle_rent",
  "is_production": 0,
  "prices": [
    { "id_vehicle": 10, "price_rent": 245344 },
    { "id_vehicle": 4, "price_rent": 28769 }
  ],
  "details": "NBIS voice announcer get on my nerves!!11!",
  "game_version": "4.9"
}
```

**Output**
```
ids_reports    string|null   // report identifiers
date_added     int           // timestamp of report submission
username       string|null   // datarunner IGN (in-game name)
```

**Maximum rows allowed:** 500 rows per submission

---

## POST /marketplace_advertise

Create a new listing in the UEX Marketplace.

- **Auth:** `secret-key` header required (listed under Input). Note: the docs' own Auth badge is blank ("—") on several write endpoints, but the header is still mandatory — omitting it returns `missing_secret_key`.
- **Cache TTL:** —
- **Update frequency:** —
- **Response status:** service_unavailable, missing_secret_key, invalid_secret_key, missing_operation, invalid_operation (sell, buy, rent, trade), missing_type, invalid_type (item, service, contract), missing_id_category, missing_unit, invalid_unit (reference below), missing_title, missing_description, invalid_image_data, image_data_exceeds_limit, missing_currency (UEC, WIF, MGS), invalid_currency, missing_language, invalid_language (en_US, de_DE, es_ES, fr_FR, it_IT, pt_BR, ru_RU, zh_CN), category_not_found, item_not_found, user_not_found (type 'item' only), user_not_allowed, user_not_verified, user_active_listings_limit_reached, image_upload_error, image_storage_error, ok

**Example URLs**
```
https://api.uexcorp.uk/2.0/marketplace_advertise/
```

**Input**
```
// Header
secret-key          string          // required user secret key, obtained in user profile

// POST
id_category         int             // [FK -> get_categories]
id_item              int|null       // [FK -> get_items]
id_star_system       int|null       // [FK -> get_star_systems]
id_terminal          int|null       // [FK -> get_terminals]
id_organization       int|null      // only for admin and trader roles [FK -> get_organizations]
operation             string        // sell|buy|rent|trade
type                  string        // item|service|contract
language              string        // en_US|de_DE|es_ES|fr_FR|it_IT|pt_BR|ru_RU|zh_CN
unit                  string        // reference below
price                 int
currency              string        // UEC|WIF|MGS
location              string        // trade location, e.g. Port Tressler
title                 string(140)   // alphanumeric and dashes only
description           string(65535)
source                string        // looted|pledged|purchased_in_game|pirated|gifted|crafted
in_stock              int
availability          string        // immediate|ready_pickup|on_demand|pre_order|work_order|reserve_only|scheduled|in_progress|negotiable
durability            string        // 0-100
hours_expiration      int           // offer expires in 1|2|3|5|12|24|48|72|120|168|336|720 hours
video_url             string
image_data            string(10485760)  // jpg or png in base64, with or without data URI prefix, up to 10.00 MB
is_hidden             int           // 1 to hide; 0 or null to publish after approval
is_tv_allowed         int           // 1 to allow UEX TV display if featured, 0 to prevent
is_production         int           // 1 for production, 0 for sandbox
```

**Unit Reference**
```
Type: "item"
box, crate, cscu, dozen, hundred, pack, pair, scu, set, stack, thousand, unit

Type: "service"
contract, cycle, day, event, expedition, gm, hour, minute, mission, month, operation, route, run, service, session, shift, trip, week

Type: "contract"
contract, mission
```

**Output**
```
id_listing        int
url               string|null
url_image         string|null
url_thumbnail     string|null
date_expiration   int
```

---

## POST /marketplace_negotiations_messages

Post messages in a negotiation.

- **Auth:** `secret-key` header required (listed under Input). Note: the docs' own Auth badge is blank ("—") on several write endpoints, but the header is still mandatory — omitting it returns `missing_secret_key`.
- **Cache TTL:** —
- **Update frequency:** —
- **Response status:** missing_secret_key, invalid_secret_key, user_not_found, user_not_allowed, missing_id_negotiation_or_hash, missing_message, negotiation_not_found, negotiation_closed, ok

**Example URLs**
```
https://api.uexcorp.uk/2.0/marketplace_negotiations_messages/
```

**Input**
```
// Header
secret-key         string          // required user secret key obtained in user profile, passed via header

// POST
id_negotiation     int             // required as one of pair (id_negotiation or hash) [FK -> get_marketplace_negotiations]
hash               string          // required as one of pair (id_negotiation or hash) [FK -> get_marketplace_negotiations]
message            string(65535)   // required
is_production      int             // optional; 1 for production, 0 for testing
```

**Input Examples**
```json
{
  "is_production": 0,
  "hash": "943a702d06f34599aee1f8da8ef9f7296031d699",
  "message": "Hello, world! :blush:"
}
```
```json
{
  "is_production": 0,
  "id_negotiation": 123,
  "message": "Hello, world! :blush:"
}
```

**Output**
```
id_message    int
```

---

## POST /user_refineries_jobs_add

Add a new refinery job to the user account.

- **Auth:** `secret-key` header required (listed under Input). Note: the docs' own Auth badge is blank ("—") on several write endpoints, but the header is still mandatory — omitting it returns `missing_secret_key`.
- **Cache TTL:** —
- **Update frequency:** —
- **Response status:** missing_secret_key, user_not_found, user_not_allowed, user_not_verified, invalid_secret_key, missing_id_terminal, missing_id_refinery_method, terminal_not_found, missing_cost, missing_time_minutes, missing_items, no_items_found, missing_item_id_commodity, commodity_not_found, missing_item_quantity, missing_item_yield, refinery_method_not_found, ok

**Example URLs**
```
https://api.uexcorp.uk/2.0/user_refineries_jobs_add/
```

**Input**
```
// Header
secret-key                string   // required user secret key, should be passed via header, obtained in user profile

// POST
id_terminal                int     // required [FK -> get_terminals]
id_refinery_method         int     // required [FK -> get_refineries_methods]
cost                       float   // refining cost in UEC
time_minutes               int     // e.g. 135 minutes for 2h 15m
refinery_capacity          int     // optional, current capacity at terminal
items                      array
items[0][id_commodity]     int     // required, only raw commodities [FK -> get_commodities]
items[0][quantity]         int     // required, quantity refined in units
items[0][yield]            int     // required, quantity yield in units
items[0][yield_bonus]      int     // optional, refinery yield bonus
is_production              int     // 1 for production, 0 for testing
```

**Output**
```
id_user_refinery_job    int    // user refinery job unique ID
```

---

## POST /user_trades_add

Add a new trade run to the user account.

- **Auth:** `secret-key` header required (listed under Input). Note: the docs' own Auth badge is blank ("—") on several write endpoints, but the header is still mandatory — omitting it returns `missing_secret_key`.
- **Cache TTL:** —
- **Update frequency:** —
- **Response status:** missing_secret_key (user secret key not provided), user_not_found (user not found with provided secret key), user_not_allowed (user banned or disabled by administrator), user_not_verified (user account not verified on RSI website), invalid_secret_key (user secret key length should be exactly 40 characters), missing_operation (transaction type not provided), invalid_operation (invalid transaction type—should be 'buy' or 'sell'), missing_id_terminal (terminal ID not provided), terminal_not_found (terminal ID not found), missing_id_commodity (commodity ID not provided), commodity_not_found (commodity ID not found), missing_scu (SCU not provided), missing_price (commodity price per SCU not provided), vehicle_not_found (vehicle ID not found), ok (all good!)

**Example URLs**
```
https://api.uexcorp.uk/2.0/user_trades_add/
```

**Input**
```
// Header
secret-key       string   // required user secret key, obtained in user profile

// POST Body
id_terminal      int      // required [FK -> get_terminals]
id_commodity     int      // required [FK -> get_commodities]
id_user_fleet    int      // optional, user fleet vehicle ID [FK -> get_fleet]
operation        string   // required, transaction type, should be 'buy' or 'sell'
scu              int      // required, amount purchased/sold in SCU
price            float    // required, values in UEC per SCU
is_production    int      // 1 for production, 0 for testing
```

**Input Example**
```json
{
  "is_production": 0,
  "id_terminal": 29,
  "id_commodity": 18,
  "operation": "buy",
  "scu": 110,
  "price": 2441
}
```

**Output**
```
id_user_trade    int    // user trade unique ID
```

---

## POST /user_trades_edit

Edit an existing user trade run.

- **Auth:** `secret-key` header required (listed under Input). Note: the docs' own Auth badge is blank ("—") on several write endpoints, but the header is still mandatory — omitting it returns `missing_secret_key`.
- **Cache TTL:** —
- **Update frequency:** —
- **Response status:** missing_secret_key (user secret key not provided), user_not_found (user not found with provided secret key), user_not_allowed (user banned or disabled by administrator), user_not_verified (user account not verified on RSI website), invalid_secret_key (secret key length should be exactly 40 characters), missing_id (user trade ID not provided), trade_not_found (user trade ID not found), missing_operation (transaction type not provided), invalid_operation (invalid transaction type, should be 'buy' or 'sell'), missing_id_terminal (terminal ID not provided), terminal_not_found (terminal ID not found), missing_id_commodity (commodity ID not provided), commodity_not_found (commodity ID not found), missing_scu (SCU not provided), missing_price (commodity price per SCU not provided), vehicle_not_found (vehicle ID not found), ok (all good!)

**Example URLs**
```
https://api.uexcorp.uk/2.0/user_trades_edit/
```

**Input**
```
// Header
secret-key       string   // required user secret key, obtained in user profile

// POST
id               int      // required, user trade ID [FK -> get_user_trades]
id_terminal      int      // required [FK -> get_terminals]
id_commodity     int      // required [FK -> get_commodities]
id_user_fleet    int      // optional, user fleet vehicle ID [FK -> get_fleet]
operation        string   // required, transaction type: 'buy' or 'sell'
scu              int      // required, amount purchased/sold in SCU
price            float    // required, values in UEC per SCU
is_production    int      // 1 for production, 0 for testing
```

**Input Example**
```json
{
  "is_production": 0,
  "id": 561,
  "id_terminal": 29,
  "id_commodity": 18,
  "operation": "buy",
  "scu": 240,
  "price": 2441
}
```

**Output**
_none_

---

## POST /wallet_add

Add a new wallet registry.

- **Auth:** `secret-key` header required (listed under Input). Note: the docs' own Auth badge is blank ("—") on several write endpoints, but the header is still mandatory — omitting it returns `missing_secret_key`.
- **Cache TTL:** —
- **Update frequency:** —
- **Response status:** missing_secret_key (user secret key not provided), user_not_found (user not found with provided secret key), user_not_allowed (user banned or disabled by administrator), user_not_verified (user account not verified on RSI website), invalid_secret_key (user secret key length should be exactly 40 characters), missing_description (transaction memo not supplied), missing_operation (transaction type not provided), invalid_operation (invalid transaction type, should be 'debit' or 'credit'), missing_value (transaction value not informed), ok (all good!)

**Example URLs**
```
https://api.uexcorp.uk/2.0/wallet_add/
```

**Input**
```
// Header
secret-key       string   // required user secret key, should be passed via header, obtained in user profile

// POST
description      string   // required, transaction memo
operation        string   // required, transaction type, should be 'debit' or 'credit'
value            float    // required, values in UEC
is_production    int      // 1 for production, 0 for testing
```

**Input Example**
```json
{
    "is_production": 0,
    "description": "Income: Astatine Sale in Nyx",
    "operation": "credit",
    "value": 1450180
}
```

**Output**
```
transaction_hash    string    // unique transaction hash
```

---

## DELETE /data_remove

Retract a report you submitted to the UEX Datacenter.

Datarunners may only remove their own reports until consolidation. Once values become part of published averages and price history, removal requires moderator/administrator/developer privileges. Moderators and administrators can remove any report, including consolidated ones. When removing a consolidated report, its published price and matching history entry are also removed.

Removal constitutes a soft delete — the report stops counting, leaves the approval queue, and disappears from data_info, but the record and event trail remain. To correct rather than drop a value, use data_edit. A removed report no longer blocks new submissions for the same terminal and reference.

- **Auth:** `secret-key` header required (listed under Input). Note: the docs' own Auth badge is blank ("—") on several write endpoints, but the header is still mandatory — omitting it returns `missing_secret_key`.
- **Cache TTL:** —
- **Update frequency:** —
- **Response status:** service_unavailable (service temporarily unavailable), access_denied (authentication error), missing_secret_key (datarunner secret), invalid_secret_key (datarunner secret), missing_id (report ID not provided), user_not_found (datarunner not found), user_disabled (datarunner banned or blocked), user_not_allowed (user is not a datarunner, or blocked by administrator), report_not_found (report does not exist, was already removed, or belongs to another datarunner), type_not_available (report type cannot be managed through the API), report_consolidated (report already folded into the live prices), ok (all good)

**Example URLs**
```
https://api.uexcorp.uk/2.0/data_remove
https://api.uexcorp.uk/2.0/data_remove/id/1234567/
```

**Input**
```
// Header
secret-key    string    // required user secret key, obtained in user profile

// Input Parameters
id            int       // required, one report per call
```

**Output**
```
id              int
type            string
date_removed    int    // timestamp
```

---

## DELETE /marketplace_listings

Remove an existing listing.

- **Auth:** `secret-key` header required (listed under Input). Note: the docs' own Auth badge is blank ("—") on several write endpoints, but the header is still mandatory — omitting it returns `missing_secret_key`.
- **Cache TTL:** —
- **Update frequency:** —
- **Response status:** missing_secret_key (user secret key not provided), user_not_found (user not found with provided secret key), user_not_allowed (user banned or disabled by administrator), user_not_verified (user account not verified on RSI website), invalid_secret_key (user secret key length should be exactly 40 characters), missing_id (listing ID not provided), listing_not_found (listing ID not found), ok (all good!)

**Example URLs**
```
https://api.uexcorp.uk/2.0/marketplace_listings?id={int}&is_production=0
```

**Input**
```
// Header
secret-key       string    // required user secret key, should be passed via header, obtained in user profile

// Query string (from URL example)
id               int       // listing ID
is_production    int       // as shown in URL example (0 in the sample URL)
```

**Output**
_none_

---

## DELETE /user_refineries_jobs_remove

Remove an existing refinery job from an user account.

- **Auth:** `secret-key` header required (listed under Input). Note: the docs' own Auth badge is blank ("—") on several write endpoints, but the header is still mandatory — omitting it returns `missing_secret_key`.
- **Cache TTL:** —
- **Update frequency:** —
- **Response status:** missing_secret_key (user secret key not provided), user_not_found (user not found with provided secret key), user_not_allowed (user banned or disabled by administrator), user_not_verified (user account not verified on RSI website), invalid_secret_key (user secret key length should be exactly 40 characters), missing_id (user refinery job ID not provided), refinery_job_not_found (user refinery job ID not found), ok (all good!)

**Example URLs**
```
https://api.uexcorp.uk/2.0/user_refineries_jobs_remove?id={int}&is_production=0
```

**Input**
```
// Header
secret-key       string    // required user secret key, obtained in user profile

// Query string (from URL example)
id               int       // user refinery job ID
is_production    int       // as shown in URL example (0 in the sample URL)
```

**Output**
_none_

---

## DELETE /user_trades_remove

Remove an existing user trade run.

- **Auth:** `secret-key` header required (listed under Input). Note: the docs' own Auth badge is blank ("—") on several write endpoints, but the header is still mandatory — omitting it returns `missing_secret_key`.
- **Cache TTL:** —
- **Update frequency:** —
- **Response status:** missing_secret_key (user secret key not provided), user_not_found (user not found with provided secret key), user_not_allowed (user banned or disabled by administrator), user_not_verified (user account not verified on RSI website), invalid_secret_key (user secret key length should be exactly 40 characters), missing_id (user trade ID not provided), trade_not_found (user trade ID not found), ok (all good!)

**Example URLs**
```
https://api.uexcorp.uk/2.0/user_trades_remove?id={int}&is_production=0
```

**Input**
```
// Header
secret-key       string    // required user secret key, should be passed via header, obtained in user profile

// Query string (from URL example)
id               int       // user trade ID
is_production    int       // as shown in URL example (0 in the sample URL)
```

**Output**
_none_

