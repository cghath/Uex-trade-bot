# UEX Trading Bot

A Discord bot for Star Citizen trading, built on the [UEX Corp API 2.0](https://uexcorp.space/api/documentation/).

Current features:

- **Commodity trading** — `/price`, `/best-route`, and `/top-routes` find live terminal prices,
  profitable runs, and ranked routes with an optional strict live-availability filter.
- **Commodity research** — `/trending`, `/movers`, and `/commodity-history` cover player trade
  volume, price movement, and price charts.
- **UEX Marketplace** — search listings, review current and historical Marketplace prices, manage
  listings/favorites/negotiations, and receive matching-listing alerts.
- **Marketplace Intelligence** — `/liquidity-rank` and `/liquidity-trends` provide all-item
  sellability ratings and history. `/scan-now` and its optional channel alerts run the **Raw
  Materials Deal Scanner**: it compares only Commodities and Harvestables with a reported quality
  against the matching 30-day quality tier, currency, and unit. Crafted gear is deliberately
  excluded because UEX does not expose its modifiers as structured pricing data.
- **Alerts and digest** — commodity-price alerts, terminal-restock alerts, Marketplace listing
  alerts, and a configurable daily digest.
- **Personal tools** — a local trade ledger, server leaderboard, saved cargo ship, and private UEX
  account linking for personal trade, listing, favorite, and negotiation data.

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

### A note on "inventory management"

UEX's API does not expose a live in-game cargo hold (there's no endpoint that says "you're
currently carrying 40 SCU of Laranite in your Cutlass"). What it has instead is `/user_trades`,
a history of trades logged through UEX's own tools. So this bot approximates inventory tracking
two ways: a local trade ledger you fill in via Discord commands, and a pull of your real UEX trade
history if you use UEX's own logging tools too. If UEX adds a live fleet/cargo endpoint later,
this is the natural place to wire it in (`bot/uex/client.py` + a new cog).

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
3. Under **Bot**, no privileged intents are required for this bot (it only uses slash commands).
4. Under **OAuth2 → URL Generator**, check `bot` and `applications.commands` scopes, then
   `Send Messages`, `Embed Links`, `Read Message History` permissions. Use the generated URL to
   invite the bot to your server.
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

Create `/etc/systemd/system/uexbot.service`:

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
sudo systemctl enable --now uexbot
sudo systemctl status uexbot     # check it's running
journalctl -u uexbot -f          # live logs
```

To deploy updates later: `git pull` (or re-copy files), `pip install -r requirements.txt` if
dependencies changed, then `sudo systemctl restart uexbot`.

**When migrating from your dev machine to the Pi, copy the whole `data/` folder** (not just the
code) so everyone's already-linked UEX accounts keep working — see "Security notes" above.

## Project layout

```
bot/
  main.py            entrypoint, bot setup, cog loading, slash command sync
  config.py          .env loading and validation
  discord_ui.py      shared Discord UI helpers (embeds, pagination)
  uex/
    client.py        async UEX API 2.0 client (auth, caching, rate-limit handling)
    trading.py       buy/sell/route ranking helpers (fallback when /commodities_routes lacks data)
    trends.py        trade-volume + price-mover aggregation (pure functions, unit tested)
    charts.py        matplotlib price-history chart rendering
    marketplace.py   Marketplace listings + 30-day rolling price averages
    scanner.py       Raw Materials Deal Scanner matching logic (pure functions, unit tested)
    ships.py         ship data lookups
    stock_alerts.py  terminal stock-level change detection
    leaderboard.py   UEX leaderboard fetching
    status.py        /commodities_status code definitions
    exceptions.py
  db/
    database.py      SQLite schema + queries (aiosqlite)
    crypto.py        Fernet key management for encrypting per-user secret keys
  cogs/
    account.py            /link-uex-account (modal), /unlink-uex-account, /uex-account-status
    prices.py             /price, /best-route
    alerts.py             /alert-add, /alert-list, /alert-remove + background poller
    trades.py             /trade-log-add, /trade-log, /uex-trades
    trends.py             /trending, /movers, /commodity-history + background trending refresh
    marketplace.py        /marketplace-average and Marketplace lookups
    marketplace_alerts.py Marketplace listing alerts + background poller
    scanner.py             /set-scanner-channel, /scanner-status, /scan-now + background poller
    stock_alerts.py       terminal stock alerts + background poller
    ships.py              ship info commands
    digest.py             scheduled guild digest posts
    diagnostics.py        bot health/diagnostic commands
    help.py               /help
scripts/
  dump_status_codes.py    one-off diagnostic: dump UEX /commodities_status code definitions
tests/                    pytest suite (pure-logic helpers)
```

## Rate limits & caching

UEX allows 120 requests/min and 172,800/day per app token. The client caches responses
in memory using the TTLs UEX itself documents per endpoint (e.g. 30 min for prices, 12h for
terminal/commodity reference data), so repeated `/price` lookups for the same commodity within
that window don't re-hit the API.

## Ideas for what else the UEX API enables (not yet built)

- `/vehicles`, `/vehicles_prices` — ship purchase/rental price comparisons across terminals
- `/fuel_prices` — cheapest refuel stops
- `/marketplace_listings` — player-to-player marketplace search
- `/companies`, `/factions` — reputation/contact info lookups
- `/refineries_yields`, `/refineries_methods` — mining refinery yield calculators
- `/data_submit` — the bot could let users submit price observations back to UEX
