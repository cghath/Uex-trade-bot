# UEX Trading Bot

A Discord bot for Star Citizen trading, built on the [UEX Corp API 2.0](https://uexcorp.space/api/documentation/).

Current features:

- **Commodity trading** — `/price`, `/best-route`, and `/top-routes` find live terminal prices,
  profitable runs, and ranked routes with an optional strict live-availability filter.
- **Commodity research** — `/trending`, `/movers`, and `/commodity-history` cover player trade
  volume, price movement, and price charts.
- **UEX Marketplace** — search listings, review current and historical Marketplace prices, manage
  listings/favorites/negotiations, and receive matching-listing alerts.
- **Sellability ratings** — `/liquidity-rank` and `/liquidity-trends` provide the bot's own
  all-item sellability score and history (distinct from the raw UEX activity numbers behind
  `/marketplace-trending`/`/marketplace-movers` above). `/scan-now` and its optional channel
  alerts run the **Raw Materials Deal Scanner**: it compares only Commodities and Harvestables
  with a reported quality against the matching 30-day quality tier, currency, and unit. Crafted
  gear is deliberately excluded because UEX does not expose its modifiers as structured pricing
  data.
- **Personal inventory and guarded relisting** — manually record catalogued, game-earned item
  stacks; review the same Sellability Rating beside direct UEX item links; and explicitly
  authorize guarded UEC sell listings. An unsold listing relists 5% lower every 48 hours (pausing
  instead whenever a negotiation is open) down to a hard manual floor, then asks what to do next.
  Ambiguous UEX results stop for confirmation instead of risking duplicates.
- **Alerts and digest** — commodity-price alerts, terminal-restock alerts, Marketplace listing
  alerts, and a configurable daily digest.
- **Personal tools** — a local trade ledger, server leaderboard, saved cargo ship, and private UEX
  account linking for personal trade, listing, favorite, and negotiation data.
- **Ship shopping** — `/where-to-buy-ship` lists every in-game terminal that sells or rents a ship,
  with aUEC prices cheapest first and rentals grouped by star system (1-day rate).
  `/ship-parts-finder` (ready for testing) browses a ship's component slots, lists only parts that
  fit and are sold, ranked by each slot's key stat, and keeps a private shopping list of locked-in parts.
  `/ship-loadout` recommends one part per slot (Balanced, Stealth, Tank or Budget), says where the
  stock part is already the best pick, and adds every purchase to that list in one click.

Run `/intro` in Discord for the complete categorized command guide.

### Multi-user support

This bot is designed to be added to one server and used by everyone in it, each with their own
UEX account. `/link-uex-account` opens a private Discord form (a modal) where you paste your UEX
secret key — modals aren't posted in the channel and aren't visible to other members, unlike
regular slash command options. Once linked, your key is encrypted at rest (see "Security notes"
below) and used only when *you* run a command like `/uex-trades`. Everything else — `/price`,
`/best-route`, alerts, the local trade ledger — already worked per-user or used public data, so
no changes were needed there.

### Security notes

- Per-user UEX secret keys are encrypted at rest with a key file (`data/credentials.key`,
  auto-generated on first run) using [Fernet](https://cryptography.io/en/latest/fernet/) symmetric
  encryption. Anyone with both that key file *and* the SQLite database could decrypt stored keys,
  so treat the whole `data/` folder as sensitive — it's already excluded via `.gitignore`.
- When you move the bot to the Pi (see below), copy the entire `data/` folder along with it. If
  you regenerate `credentials.key` without the matching database (or vice versa), previously
  linked accounts will silently show as unlinked and members will need to `/link-uex-account` again.
- The bot owner's own `UEX_SECRET_KEY` in `.env` is optional and only used as a fallback if a user
  hasn't linked an account — normal usage doesn't rely on it at all.

### How trending/movers/history work

UEX doesn't expose a simple "trade volume" number, so these commands are built from a few
different real fields rather than one obvious endpoint:

- **`/trending`** sums `scu_buy_users_rows` + `scu_sell_users_rows` per commodity — UEX's own
  count of real player-submitted trade trips in the last 15 days — across all terminals selling
  it. That field is only returned when you query one commodity at a time, so a background task
  loops every tradeable commodity (~1-2 minutes total, paced well under the rate limit) every 45
  minutes and caches the ranked result; the slash command just reads that cache, so it always
  answers instantly. Right after startup the first refresh hasn't run yet, so `/trending` may say
  "still gathering data" for a few minutes.
- **`/movers`** uses `/commodities_prices_all`, a single bulk call covering every commodity at
  every terminal, and compares each commodity's current sell price to its own `price_sell_avg`
  to find the biggest swings. This is a fast, on-demand command — no background task needed.
- **`/commodity-history`** pulls `/commodities_prices_history` (up to 500 recent snapshots for one
  commodity at one terminal) and renders a chart with matplotlib. If UEX's own precomputed
  `/commodities_routes` doesn't have distance/ROI data for a particular commodity yet, `/best-route`
  quietly falls back to computing routes from raw price rows instead.

### A note on inventory management

UEX does not expose a live in-game cargo hold, so the bot cannot discover newly looted or crafted
items automatically. `/inventory-add` is the source of truth for personal Marketplace stock;
quality and location create separate stacks. `/inventory` shows quantity, reservations, manual
price floor, Sellability Rating, and a clickable UEX item page. `/inventory-sell` opens a paged
checklist and an explicit authorization preview, where a custom price can be set per stack; posting happens within minutes, though UEX
staff approval before a listing actually goes live is outside the bot's control. Scheduled posts
are catalogued UEC sell listings only. An unsold listing with no open negotiation relists 5% lower
every 48 hours down to its hard floor, then DMs to ask what to do next; an open negotiation pauses
relisting instead so it isn't disrupted.

When UEX explicitly reports a lower `in_stock` value or sold-out state, local quantity is updated.
If a listing disappears without a final stock value, `/inventory-confirm-sale` asks for the actual
quantity rather than guessing. Sold-out asking prices can inform a recommendation, but they are not
misrepresented as verified final transaction prices. Private inventory notes are never posted.
Every inventory command, checklist, preview, error, and confirmation is ephemeral in Discord;
background posting/sale updates are delivered only by private DM.

## 1. Get your UEX credentials

1. Log into [uexcorp.space](https://uexcorp.space) with your account.
2. Go to your account's **My Apps** page and create an app. This gives you an **app token**
   (Bearer token) — this is `UEX_APP_TOKEN`. It authenticates the bot itself for market/reference data.
3. Public data (terminals, commodities, prices, items) needs no further auth.
4. For `/uex-trades` (reading *your* trade history), you also need your personal **secret key**
   from your UEX account page — that's `UEX_SECRET_KEY`. Skip this if you only care about market
   prices, not your own trade history.

## 2. Get a Discord bot token

1. Go to the [Discord Developer Portal](https://discord.com/developers/applications) → New Application.
2. Bot tab → Reset Token → copy it. This is `DISCORD_BOT_TOKEN`.
3. Under **Bot**, leave all three privileged intents (Presence, Server Members, Message Content)
   off. The bot only uses slash commands, buttons and forms, so it never needs to read messages.
4. Under **OAuth2 → URL Generator**, check the `bot` and `applications.commands` scopes, then these
   permissions:
   - `View Channels`, `Send Messages`, `Embed Links`, `Read Message History`;
   - `Attach Files`, for charts and long lists;
   - `Create Private Threads`, `Send Messages in Threads` and `Manage Threads`, for the private
     threads that route tracking, the ship-parts list and the blueprint list use.

   Use the generated URL to invite the bot to your server.
5. (Optional, recommended while developing) Enable Developer Mode in Discord, right-click your
   test server → Copy Server ID → put it in `DISCORD_DEV_GUILD_ID` in `.env`. This makes slash
   commands sync instantly to that one server instead of up to an hour globally.

## 3. Run it locally (Windows/Mac/Linux dev machine)

```bash
git clone <this repo>   # or just copy the folder
cd uex-trading-bot
python -m venv .venv
source .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install -r requirements.txt

cp .env.example .env
# edit .env: DISCORD_BOT_TOKEN, UEX_APP_TOKEN, (optional) UEX_SECRET_KEY, DISCORD_DEV_GUILD_ID

python -m bot.main
```

The SQLite database file is created automatically at `data/uexbot.sqlite3` (configurable via
`DATABASE_PATH` in `.env`), and a `data/credentials.key` file is generated alongside it the first
time anyone links a UEX account (see "Security notes" above).

> Setting up an **additional** Windows dev machine, including SSH access back to your
> deployment host? [`docs/WINDOWS_DEV_SETUP.md`](docs/WINDOWS_DEV_SETUP.md) scripts most
> of it.

## 4. Deploying to a Raspberry Pi 5 for permanent hosting

The Pi 5 is ARM64, and everything this project uses (`discord.py`, `httpx`, `aiosqlite`,
`python-dotenv`, `cryptography`, `matplotlib`) ships prebuilt ARM64 wheels on PyPI for current
Python versions, so there's nothing to compile from source.

```bash
# On the Pi, with Raspberry Pi OS (64-bit) and Python 3.11+:
sudo apt update && sudo apt install -y python3-venv python3-pip git

git clone <this repo> ~/uex-trading-bot   # or scp the folder over
cd ~/uex-trading-bot
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

cp .env.example .env
nano .env   # fill in the same values you used locally
```

### Run it as a systemd service (survives reboots/crashes)

Create `/etc/systemd/system/uex-trade-bot.service`:

```ini
[Unit]
Description=UEX Trading Discord Bot
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=pi
WorkingDirectory=/home/pi/uex-trading-bot
ExecStart=/home/pi/uex-trading-bot/.venv/bin/python -m bot.main
Restart=on-failure
RestartSec=10
Environment=PYTHONUNBUFFERED=1

[Install]
WantedBy=multi-user.target
```

Then:

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now uex-trade-bot
sudo systemctl status uex-trade-bot     # check it's running
journalctl -u uex-trade-bot -f          # live logs
```

To deploy updates later: `scripts/deploy_and_backup.sh [branch]` (defaults to `TestBranch`) -
stops the service, snapshots the current DB + git commit into `backups/pi/`, fast-forwards,
reinstalls dependencies if `requirements*.txt` changed, then restarts. If the new version has
a problem, `scripts/revert_last_deploy.sh` restores that snapshot and checks out the recorded
commit (itself snapshotting the state it's discarding first). Avoids the manual git
pull/restart dance and the need to merge PC/Pi databases after a bad deploy - just revert.

The same three manual steps still work if you'd rather do it by hand: `git pull`,
`pip install -r requirements.txt` if dependencies changed, `sudo systemctl restart uex-trade-bot`.

**When migrating from your dev machine to the Pi, copy the whole `data/` folder** (not just the
code) so everyone's already-linked UEX accounts keep working — see "Security notes" above.

## Project layout

```
bot/
  main.py            entrypoint, bot setup, cog loading (INITIAL_COGS), slash command sync
  config.py          .env loading and validation
  discord_ui.py      BotView/BotModal base classes: failed clicks answer, expired buttons grey out
  route_pages.py     route results as one message, a route per page
  delivery.py        alert/notification delivery (channel with DM fallback) and message fitting
  autocomplete.py    time-limited autocomplete fetches
  wiki_api.py        Star Citizen Wiki API client (ship loadouts, part stats, blueprints)
  uex/               pure, unit-tested helpers plus the UEX client:
    client.py        async UEX API 2.0 client (auth, caching, rate-limit handling)
    trading.py, trends.py, mixed_routes.py, multi_stop_routes.py, backup_routes.py,
    route_confidence.py, route_presentation.py, supply_demand.py, commodity_risk.py,
    data_health.py, practical_routes.py, trading_preferences.py,
    marketplace.py, scanner.py, inventory.py, charts.py, ships.py, ship_shops.py,
    ship_parts.py, ship_part_display.py, ship_loadout.py, item_finder.py, refinery.py,
    mining_locations.py, stock_alerts.py, leaderboard.py, status.py, exceptions.py, ...
  db/
    database.py      SQLite schema + queries (aiosqlite)
    crypto.py        Fernet key management for encrypting per-user secret keys
  cogs/              one per feature area; "loop" = a background poller
    account.py            /link-uex-account, /unlink-uex-account, /uex-account-status
    prices.py             /price, /terminal-history, /best-route, /mixed-routes, /multi-stop-route,
                          /diminishing-returns
    trends.py             /trending, /top-routes, /movers, /commodity-history + loop
    route_progression.py  route tracking threads (the Track this route button) + loops
    trading_preferences.py /set-trading-preferences, /clear-trading-preferences, /my-trading-preferences
    ships.py              the shared ship-name autocomplete (no commands)
    alerts.py             /alert-add, /alert-list, /alert-remove (list/remove cover all 3 alert types) + loop
    stock_alerts.py       /stock-alert-add + loop
    marketplace_alerts.py /marketplace-alert-add + loop
    negotiation_alerts.py /negotiation-alerts + loop
    trades.py             /trade-log-add, /trade-log, /uex-trades, /leaderboard
    marketplace.py        /marketplace-search, /marketplace-trending, /marketplace-movers,
                          /marketplace-average, /marketplace-history, /marketplace-index-status,
                          /my-favorites, /my-negotiations, /marketplace-post, /marketplace-listing,
                          /marketplace-delete-listing + loop
    personal_inventory.py /inventory-add, /inventory, /inventory-set-minimum, /inventory-remove,
                          /inventory-sell, /inventory-post-now, /inventory-confirm-sale,
                          /inventory-cancel-post, /inventory-resolve-floor + loop
    liquidity.py          /liquidity-rank, /liquidity-trends
    scanner.py            /set-scanner-channel, /scanner-status, /scan-now + loop
    digest.py             /set-digest-channel, /digest-disable, /digest-now + loop
    intelligence.py       background market/data-health/fuel/reference collectors, no commands
    intelligence_brief.py /intelligence-brief
    refinery.py           /refinery-advisor
    mining_locations.py   /where-to-mine
    blueprints.py         /blueprint-search, /blueprint-list + loop (blueprint_planner.py: its shopping list)
    item_finder.py        /ingame-item-finder
    ship_shops.py         /where-to-buy-ship
    ship_parts_finder.py  /ship-parts-finder, /ship-loadout + loop
    diagnostics.py        /test-dm, /command-usage
    help.py               /intro (the categorized command guide)
scripts/
  deploy_and_backup.sh, revert_last_deploy.sh   Pi deploys and rollback (see above)
  sync_pi_backups.sh      archive the Pi's deploy backups to this PC, keep the Pi lean
  dump_status_codes.py    one-off diagnostic: dump UEX /commodities_status code definitions
tests/                    pytest suite
```

## Rate limits & caching

UEX allows 120 requests/min and 172,800/day per app token. The client caches responses
in memory using the TTLs UEX itself documents per endpoint (e.g. 30 min for prices, 12h for
terminal/commodity reference data), so repeated `/price` lookups for the same commodity within
that window don't re-hit the API.

## Ideas for what else the UEX API enables (not yet built)

- `/fuel_prices`: cheapest refuel stops. The bot already collects fuel prices in the background,
  but no command shows them yet.
- `/companies`, `/factions`: reputation/contact info lookups
- `/data_submit`: the bot could let users submit price observations back to UEX
