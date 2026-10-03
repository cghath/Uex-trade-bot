# Bot Handoff

A running, bidirectional queue of changes made in one bot's repo that still need porting to
the other: production `uex-trading-bot-v2` (deployed on the Pi as `uex-trade-bot.service`)
and its AI-chat fork `aiv2` (started as a carbon copy of this one, so most changes in either
are relevant to both - command names differ only by aiv2's `ai-` prefix, e.g. `/price` ->
`/ai-price`). Previously scoped production -> aiv2 only, with reverse-direction changes
deliberately left untracked; widened to cover both directions once work started happening
directly in aiv2 rather than always starting in production first.

This is a live checklist, not a point-in-time log - unlike `PROJECT_CONTEXT.md`/
`CONTRIBUTING.md` (see their own "no new standalone log files" convention under
`PROJECT_CONTEXT.md`'s "Where to look for what"), entries here get checked off as they're
actually ported, not kept as permanent history. Once every entry in a section above a point
is checked, feel free to delete the checked block rather than let it grow forever - `git log`
on this file preserves the record if it's ever needed again.

**How to use this:** after landing a change in EITHER repo that's relevant to the other, add
an entry under that direction's section below before moving on. When you port a change,
check its box. If you skip an entry because it doesn't apply on the other side (e.g. it's
Discord-bot-only glue, or the other bot never had the feature being touched), check it anyway
and say why in a parenthetical - a skipped-and-explained box is worth more than an unchecked
one you can no longer remember the reason for.

**Format:** `- [ ] <repo> <PR #N or commit hash> - one-line summary (key files touched)`

---

## To port: production -> aiv2

The "Backfilled from 2026-09-22" block (PRs #27-33, #35, #36) was fully ported to aiv2
(`master` @ `28e5df3`) the same day and cleared per this file's own convention above - see
`git log -p -- AI_BOT_HANDOFF.md` for the checked-off detail if it's ever needed again,
including which items were genuine no-ops (#27, since aiv2 never had the Backup route
button) versus deliberately not mirrored (#30's patch-note version numbers, since aiv2's own
notes format already carries a `_Ref:` commit hash per entry). PR #39 (porting 7 aiv2-side
audit fixes back to production) was the reverse direction and was already done as of that
date - no open item, nothing to record here.

- [ ] PR #40 - Add `/ingame-item-finder`: which shops sell a weapon/armor/ammo/other item,
      closest to a given location first (`bot/cogs/item_finder.py`, `bot/uex/item_finder.py`,
      `bot/uex/client.py`'s new `get_items_prices`, `bot/cogs/help.py`, `bot/main.py`)
- [ ] PR #42 - `/ingame-item-finder`: same-system fallback sort tier for when
      `/terminals_distances` can't price a same-system pair, results grouped into one embed
      field per star system (`bot/uex/item_finder.py`, `bot/cogs/item_finder.py`,
      `bot/db/database.py`'s new `get_terminal_star_system`)
- [ ] PR #43/#45/#46 - `/ingame-item-finder`: shows place split from the terminal name's
      "Vendor - Place" convention (e.g. "GrimHEX" instead of the formal station name) plus
      vendor, one plain-text line per shop - `"**Place** (Vendor) — Price aUEC · Distance"`.
      Port the NET result of these three, not #43 alone: #43 shipped a fixed-width monospace
      table first, #45 widened its columns to fix a real truncation-collision bug, #46 then
      replaced the table entirely with plain text after the wider columns turned out to make
      Discord wrap the rows and break alignment anyway (see PROJECT_CONTEXT.md entry 79 for
      the full story). `bot/uex/item_finder.py`'s `format_item_listing_line` is the only
      formatter that matters now - `build_item_listing_table`/`format_item_listing_header`/
      `format_item_listing_row` and the PR #45 column-width constants were deleted, don't
      port those (`bot/uex/item_finder.py`, `bot/cogs/item_finder.py`)
- [ ] PR #50 - `/ingame-item-finder`'s `item` autocomplete now scopes to items UEX reports at
      least one real shop price for (`/items_prices_all`), not the full catalog - confirmed
      live that ~5,000 of 7,769 catalogued items (cosmetics, ship paint, and similar) have no
      shop listing at all and were suggesting dead ends ("No shop currently lists X for
      sale") every time. New `UexClient.get_items_prices_all()` +
      `sold_item_name_autocomplete` (`bot/uex/client.py`, `bot/cogs/item_finder.py`) -
      deliberately does NOT fall back to the full catalog the way Marketplace's
      `traded_item_autocomplete` does, since an item missing here means it's genuinely not
      sold anywhere, not just a gap in the bot's own tracking
- [x] PR #58 - Add
      `/where-to-buy-ship`: every in-game terminal that sells or rents one ship, aUEC prices
      cheapest first, rentals grouped per star system and labelled as the 1-day rate. No
      location option or distance sort, deliberately (only 7 terminals sell ships). A buy or
      rent row UEX sends with no star system takes its terminal's system from
      `terminal_reference` via the existing `Database.get_terminal_star_system`. Autocomplete
      only offers ships with at least one buy or rent row (same lesson as PR #50). See
      PROJECT_CONTEXT.md entry 81. Four new `UexClient` methods, `get_vehicle_purchase_prices`/
      `get_vehicle_rental_prices`/`get_vehicle_purchase_prices_all`/
      `get_vehicle_rental_prices_all`, each with a 12h `_ENDPOINT_CACHE_TTL` entry
      (`bot/uex/client.py`, new `bot/uex/ship_shops.py`, new `bot/cogs/ship_shops.py`,
      `bot/main.py`'s `INITIAL_COGS`, `bot/cogs/help.py`'s `CATEGORIES`, new
      `tests/test_ship_shops.py`) (ported: aiv2 34b7003, as `/ai-where-to-buy-ship` with #92's
      shop names, plus a chat tool built on the same reply)
- [x] PR #59 - Time-limit `/ingame-item-finder`'s (and `/where-to-buy-ship`'s) autocomplete
      so a slow or cold UEX response returns no suggestions within Discord's ~3s deadline,
      instead of silently timing out: new `bot/autocomplete.py` `gather_within()` (stops
      waiting at 2.5s without cancelling, so the fetch still fills the cache), plus a
      `cog_load` cache pre-load in both cogs. Port the item-finder half if aiv2 has
      `/ingame-item-finder`. The same PR's `ship_parts_shopping_entries` migration only
      matters if aiv2 ever ported `/ship-parts-finder`. See PROJECT_CONTEXT.md entry 82.
      (ported: the `/where-to-buy-ship` half, `bot/autocomplete.py` and its `cog_load`
      pre-load, in aiv2 34b7003. aiv2 has no `/ingame-item-finder` or `/ship-parts-finder`
      yet; porting those from current production brings the rest of this PR with them)
- [ ] PR #60 - `/ship-parts-finder`'s
      comparison text is rebuilt (new `bot/uex/ship_part_display.py`), with a Weapons
      category. Fit is now decided by the wiki's size and tags instead of UEX's catalog size
      (wrong for 18/19 missile racks, 7/86 guns, 6/41 shields). Relevant to aiv2 only if it
      ever ports `/ship-parts-finder`. The reusable parts are new `WikiApiClient` methods
      `get_vehicle_loadout`/`find_item_detail_by_name`/`find_item_variants_by_name`
      (`bot/wiki_api.py`) and two new `ship_parts_reference` columns (`bot/db/database.py`).
      See PROJECT_CONTEXT.md entry 83
- [ ] PR #61 - `/ship-parts-finder` per-category extra stats (weapon per shot/rpm/
      projectile speed; signature and component HP for radar, power plants, coolers,
      shields and quantum drives), all in `bot/uex/ship_part_display.py`. Only relevant
      if aiv2 ever ports `/ship-parts-finder`. See PROJECT_CONTEXT.md entry 84
- [ ] PR #62 - `/ship-parts-finder` pages its list (Previous/Next, dropdown per page,
      15-part cap removed) and ranks each category by its key stat, highest first
      (`bot/uex/ship_part_display.py`, `bot/cogs/ship_parts_finder.py`). Only relevant if
      aiv2 ever ports `/ship-parts-finder`. See PROJECT_CONTEXT.md entry 85
- [ ] PR #63 - `/ship-parts-finder` shops the guns inside turrets (`child_gun_ports`),
      skips weapon ports the game locks, and checks a port's own required_tags against the
      part's tags (`bot/uex/ship_parts.py`, `bot/cogs/ship_parts_finder.py`, three new
      `ship_parts_reference` columns). Only relevant if aiv2 ever ports `/ship-parts-finder`.
      See PROJECT_CONTEXT.md entry 86
- [x] Background loops guarded against any single failure: an outer try/except per loop
      body plus a per-item guard, in `alerts.py`, `stock_alerts.py`, `digest.py`,
      `intelligence.py` (`snapshot_fuel_prices`), `marketplace_alerts.py`,
      `negotiation_alerts.py`, `scanner.py`, `route_progression.py`
      (`poll_abandoned_threads`) and `trends.py` (`refresh_trending`). aiv2 was cloned from
      here, so it very likely has the same unguarded loops. Grep its own `@tasks.loop`s
      too, including any AI-only ones. Test: `tests/test_background_loop_guards.py`.
      See PROJECT_CONTEXT.md entry 87
      (ported to aiv2 in commit `dacd22b`; aiv2's other loops, including `blueprints.py`, were already guarded)
- [ ] `/ship-parts-finder` reliability: a wiki outage is no longer cached as "no detail"
      for 24h, and a slow category load can't overwrite a newer pick or lock a part under
      the wrong slot (`bot/cogs/ship_parts_finder.py`). Also a new `WikiUnavailableError`
      subclass in `bot/wiki_api.py`, raised only when retries run out. It's a subclass, so
      every existing `except WikiApiError` still catches it. Only the finder part matters,
      and only if aiv2 ever ports `/ship-parts-finder`. See PROJECT_CONTEXT.md entry 88
- [x] Notifications marked done only once settled: new `bot/delivery.py` (DELIVERED /
      RETRY / UNDELIVERABLE, `send_dm`, `send_to_channel_or_dm`, `fit_message`), used by
      `alerts.py`, `stock_alerts.py`, `marketplace_alerts.py`, `scanner.py` (which also
      gains a DM fallback) and `negotiation_alerts.py` (long messages trimmed to fit, and
      closed DMs no longer retried every 5 minutes). aiv2 was cloned from here, so it
      almost certainly has the same five delivery paths. Test: `tests/test_alert_delivery.py`.
      See PROJECT_CONTEXT.md entry 89
      (ported to aiv2 in commit `dacd22b`)
- [x] Marketplace quality on the real 0-1000 scale (`format_quality_range`, `QUALITY_MAX`,
      `Range[float, 0, 1000]` on the four quality options), `/marketplace-movers` showing
      each row's own currency (`MarketplaceMoverEntry.currency`), and a Turn off button on
      `/scanner-status` (`ScannerOffView`, `Database.clear_scanner_channel`). Check any aiv2
      AI tools that describe or filter listing quality, too. Test:
      `tests/test_marketplace_labels_and_scanner_off.py`. See PROJECT_CONTEXT.md entry 90
      (ported to aiv2 in commit `dacd22b`; aiv2's chat tools never describe listing quality, so nothing more was needed there)
- [x] Route commands name the real cause of an empty result or missing cargo math:
      `saved_filters_hint`/`saved_filter_labels` (`bot/uex/trading_preferences.py`) on every
      route command's "nothing found" message, and `missing_ship_note`/
      `missing_ship_cargo_line` (`bot/uex/route_presentation.py`) plus a `ship_lookup_failed`
      flag in `prices.py`, `trends.py` and `intelligence_brief.py`. aiv2's `/ai-*` route
      commands were cloned from these, so they likely say "set a default ship" the same way.
      Test: `tests/test_route_messages.py`. See PROJECT_CONTEXT.md entry 91
      (ported to aiv2 in commit `dacd22b`, rewritten for aiv2's restructured route code; the same hints also reach its chat route tools)
- [ ] Pi growth bounded: `UexClient._store_cached` (expired-entry sweep every 200 writes,
      5,000-entry cap), and `liquidity_score_snapshots` kept 14 days via batched
      `Database.prune_liquidity_snapshots`, with the duplicate index dropped and two query
      indexes added in `SCHEMA`. aiv2 shares the client and schema, so it has the same
      growth; rehearse the first prune against a copy of its DB, as here. Test:
      `tests/test_pi_growth.py`. See PROJECT_CONTEXT.md entry 92
- [ ] Charts drawn off the event loop (`asyncio.to_thread` around every `render_*` call,
      `bot/uex/charts.py` on `matplotlib.figure.Figure` instead of pyplot), and the five
      remaining UEX-backed autocompletes time-limited with the new
      `bot/autocomplete.py: fetch_within`. aiv2's AI tools that draw charts, or any aiv2
      autocomplete still doing `try: await uex... except UexApiError`, need the same.
      Test: `tests/test_responsiveness.py`. See PROJECT_CONTEXT.md entry 93
- [ ] `/ship-parts-finder`'s restart-proof ↻ Refresh button (`RefreshBrowserButton`,
      `_RefreshStub`, `refresh_browser`, `PartsBrowserView.on_timeout`) - only matters if
      aiv2 ever ports `/ship-parts-finder`. The general lesson applies to any aiv2 view:
      never put a `DynamicItem` inside a view that can time out, since closing it
      unregisters the pattern bot-wide. Test: `tests/test_ship_parts_refresh.py`.
      See PROJECT_CONTEXT.md entry 94
- [ ] Ship-slot reference refresh asks the wiki only about ships not refreshed in 24h
      (new `ship_parts_reference_status` table, hourly check, 1s between ships, stops
      after 5 wiki outages in a row), and keeps saved slots when the wiki answers empty.
      Only matters if aiv2 ever ports `/ship-parts-finder`. The general lesson for any
      aiv2 `tasks.loop`: a long interval still runs on every start, so a heavy crawl
      needs its own last-run record. Test: `tests/test_ship_parts_reference_refresh.py`.
      See PROJECT_CONTEXT.md entry 95
- [ ] Ship Parts Finder's fitting-variant lookups batched by `DETAIL_BATCH_SIZE`, once per
      name, and `_variants_cached` treating a wiki outage as an outage (flagged in the
      note, skipped for 5 minutes) instead of "no variants". Only matters if aiv2 ever
      ports `/ship-parts-finder`. Test: `tests/test_ship_parts_variant_lookups.py`.
      See PROJECT_CONTEXT.md entry 96
- [ ] Trending/top-routes refresh keeps the previous snapshot when too much of a refresh
      failed (`RefreshGap`, `should_replace_snapshot`, `partial_refresh_note`/`_hint` in
      `bot/uex/trends.py`; `REFRESH_MAX_FAILED_SHARE`, `REFRESH_KEEP_PREVIOUS_MAX_AGE` in
      `bot/cogs/trends.py`), and footers/empty results say when a snapshot is partial.
      aiv2's `/ai-*` route tools read the same caches, so their answers should mention a
      partial snapshot too. Test: `tests/test_partial_refresh.py`. See PROJECT_CONTEXT.md
      entry 97
- [ ] `/ship-parts-finder` loads capped at `LOAD_TIME_BUDGET_SECONDS` (45) via
      `_gather_until` + `gather_within`, so a hanging wiki can't outlast the 15-minute
      interaction window; cut-off lookups finish in the background. Only matters if aiv2
      ever ports `/ship-parts-finder`, but any aiv2 AI tool that waits on the wiki has the
      same ~96s-per-request worst case. Test: `tests/test_ship_parts_load_deadline.py`.
      See PROJECT_CONTEXT.md entry 98
- [ ] Price/stock alert commodity names resolved against UEX before saving
      (`resolve_tradeable_commodity`, `suggest_commodity_names`, `unknown_commodity_message`
      in `bot/uex/trading.py`), both add commands deferring first, and one combined restock
      message per alert per check (`format_restock_message` in `bot/uex/stock_alerts.py`).
      Any aiv2 AI tool that creates alerts should validate the commodity the same way.
      Test: `tests/test_alert_add_and_restock.py`. See PROJECT_CONTEXT.md entry 99
- [ ] Defer before any DB write in 13 handlers (`/trade-log-add`, `/marketplace-alert-add`,
      the inventory commands, the link-account modal, scanner/digest settings, unlink,
      clear-default-ship, clear-trading-preferences, `/negotiation-alerts` off). aiv2 shares
      these cogs; re-run the same first-await sweep there (the entry describes it).
      Test: `tests/test_defer_before_db_writes.py`. See PROJECT_CONTEXT.md entry 100
- [ ] Listing ids in `/marketplace-search`, `/my-favorites` and `/my-negotiations`; the new
      `bot/delivery.py: fit_lines` for plain-text lists (favorites, negotiations, trade log,
      UEX trades); `/trade-log` limit 1-50; `/uex-trades` dates as Discord timestamps and
      its error via `describe_uex_api_error`. aiv2's AI tools that list listings should show
      the listing id too. Tests: `tests/test_listing_ids.py`, `tests/test_long_lists.py`.
      See PROJECT_CONTEXT.md entry 101
- [ ] Failed interactions always answer: `bot/discord_ui.py`'s `on_app_command_error`
      (registered on the tree in `bot/main.py`) and `BotView`/`BotModal` as the base of every
      view and modal; digest's `cog_app_command_error` removed. Also Ship Parts Finder's
      load and thread-post failures, per-player thread locks in the ship parts and blueprint
      services, and `retry_after_seconds` in the UEX client. aiv2's own views and modals
      (AI chat) need the base class too; the subclass walk in
      `tests/test_failed_interactions.py` lists any it misses. See PROJECT_CONTEXT.md entry 102
- [ ] Expired buttons and menus grey out: `BotView.on_timeout`/`grey_out` in
      `bot/discord_ui.py`, every timed view's send site setting `view.origin` or
      `view.message` (`followup.send(..., wait=True)`), `super()` calls in overridden
      `interaction_check`/`on_timeout`, `SetMinimumPricesView.stop()` before handing its
      message on, and the Configure crafting menu's 10-minute timeout. aiv2's own views
      need the same wiring. Tests: `tests/test_expired_views.py`, `tests/bot_views.py`.
      See PROJECT_CONTEXT.md entry 103
- [x] The saved risk tolerance filters routes: `bot/uex/commodity_risk.py`'s
      `outside_risk_tolerance`/`within_risk_tolerance`, applied in `_send_ranked_routes`, the
      four mixed-cargo commands' market pools, every hedge suggestion (including the
      tracking thread's), and a `/best-route` note; `risk_tolerance_hint` for empty results.
      If aiv2's AI tools suggest routes, they should honour it too.
      Tests: `tests/test_risk_tolerance.py`. See PROJECT_CONTEXT.md entry 104 (skipped:
      superseded by PR #96, which removed the saved risk tolerance from production
      altogether - see its entry below, and PROJECT_CONTEXT.md entry 114)
- [ ] One `delivery` option on all three alert commands (`bot/delivery.py`:
      `DELIVERY_CHOICES`, `delivery_scope`, `send_alert`), DM by default; `scope` columns on
      `price_alerts`/`marketplace_alerts` with migrations that keep old alerts' delivery;
      `/alert-list` and the add confirmations saying how often each type fires. If aiv2's
      AI tools create alerts, they need the new `scope` argument.
      Tests: `tests/test_alert_delivery_choice.py`. See PROJECT_CONTEXT.md entry 105
- [ ] Inventory stack/job autocompletes on the six id-taking inventory commands, and the
      UEX key check on /link-uex-account (`UexClient.get_user_profile`) with the
      leaderboard disclosure (`LEADERBOARD_NOTE`). If aiv2 links keys any other way, check
      them the same way.
      Tests: `tests/test_inventory_pickers_and_key_check.py`. See PROJECT_CONTEXT.md entry 106
- [ ] Route results as one paged message: `bot/route_pages.py` (`send_route_pages`,
      `RoutePagesView`, `text_pages`), used by `_send_ranked_routes`, `/best-route`,
      `/mixed-routes` and `_send_multi_stop_routes`; `RouteTrackingView` removed;
      `BotView.grey_out(**edit_kwargs)`. If aiv2's AI chat posts route results, it can send
      them the same way. Tests: `tests/test_route_pages.py`, `tests/route_results.py`.
      See PROJECT_CONTEXT.md entry 107
- [ ] Saved preferences named and disclosed: `PREFERENCE_READERS`/`preference_scope` and
      `describe_active_preferences(..., saved=)` ("Filters: ... (saved)") in
      `bot/uex/trading_preferences.py`; `_filters_note` in the mixed-cargo commands;
      `/intelligence-brief` applying saved preferences; `/my-trading-preferences` naming a
      UEX outage. If aiv2 adds or changes a route command, update `PREFERENCE_READERS`:
      `tests/test_preference_scope.py` checks it against the code. See PROJECT_CONTEXT.md
      entry 108
- [ ] Honest labels: the sell-shortfall reroute's same-system/25 Gm limit
      (`reroute_buyer_ids`, `find_backup_routes(destination_ids=)`, `fetch_terminal_distances`,
      `MAX_REROUTE_DISTANCE_GM`); `/refinery-advisor`'s update intervals
      (`cache_interval_text`) and failure notes; the ship-parts list re-priced on every
      redraw (`list_price_text`); no Message Content intent in `bot/main.py` - check aiv2's
      AI chat doesn't read message text before porting that one. See PROJECT_CONTEXT.md
      entry 109
- [ ] `/routes-from`, `/route-on-the-way` and `/route-from-multi` folded into `/top-routes
      origin:/destination:` and `/multi-stop-route origin:`; `help.py` CATEGORIES and
      `ROUTE_COMMANDS`/`PREFERENCE_READERS` updated. If aiv2's AI tools or prompts name the
      three old commands, point them at the new options. See PROJECT_CONTEXT.md entry 110
- [ ] P3 wording: "Gm" everywhere, `TradeRoute.roi_pct` (was `margin_pct`), digest "just now",
      Sellability Rating wording in `bot/cogs/liquidity.py`, `place_and_vendor_text` for
      shop names, Ship Parts Finder's wiki-failure messages, `/price`'s shown-only footer,
      and `default_permissions(manage_guild=True)` on the digest setup commands and
      `/command-usage`. See PROJECT_CONTEXT.md entry 111
- [ ] `/set-default-ship` and `/clear-default-ship` removed: `bot/cogs/ships.py` keeps only
      `ship_name_autocomplete`, and `/set-trading-preferences`' `ship` option gains a "No
      default ship (clear it)" choice (value `none`). Every hint naming `/set-default-ship`
      points to `/set-trading-preferences`. `tests/test_ship_setting_fold.py` fails on any
      bot string naming a command that doesn't exist; aiv2's `ai-` commands will need its
      command set built the same way. See PROJECT_CONTEXT.md entry 112
- [ ] `/trade-log` remove menu (`AlertRemovePickerView`, `Database.delete_trade_log_entry`)
      and `/trade-log-add` commodity/terminal autocomplete; `AlertRemovePickerView._on_select`
      now defers before its DB write and edits via `edit_original_response`; per-stack
      custom prices on `/inventory-sell` batches (`StackPricesModal`,
      `AuthorizeScheduleView.custom_prices`, `item_name` in the authorize specs). See
      PROJECT_CONTEXT.md entry 113
- [ ] PR #96 - The saved risk tolerance is removed; routes keep their "⚠️ Cargo risk: ..."
      labels. `/set-trading-preferences` loses its `risk-tolerance` option (and
      `RISK_TOLERANCE_CHOICES`); `format_trading_preferences` and the "Filters: ..." footer
      (`describe_active_preferences`, which no longer takes it) stop showing it;
      `Database.set_trading_preferences` no longer takes it and `get_trading_preferences`
      no longer returns it (the `risk_tolerance` column stays, unread, per the additive-only
      schema); the entry-104 filtering helpers are deleted. aiv2 never got that filtering
      (skipped above), so what's left there is the option itself: `/ai-set-trading-preferences`
      still has it and should lose it, along with anywhere it's shown. aiv2's chat
      `trading_preferences` tool already leaves risk tolerance out. Tests:
      `tests/test_risk_tolerance.py` (rewritten), `tests/test_trading_preferences.py`,
      `tests/test_preference_scope.py`. See PROJECT_CONTEXT.md entry 114
- [ ] PR #97 - Hedge lines and the other cargo in a backup load show their "⚠️ Cargo risk: ..."
      label: `cargo_item_line(item, risk=True)` (`bot/uex/route_presentation.py`) at
      `/best-route`'s two "Hedge:" lines, `/top-routes`' ranked list, a tracking thread's
      shortfall hedge, and `_load_lines` in `bot/uex/backup_routes.py` for cargo that isn't
      the player's own. aiv2 shows the same lines unlabelled. Tests:
      `tests/test_risk_tolerance.py`, `tests/test_backup_routes.py`. See PROJECT_CONTEXT.md
      entry 115
- [ ] PR #99 - `UexClient.get_marketplace_listings` wraps UEX's bare-object answer to an
      `id=` lookup in a list (`bot/uex/client.py`). UEX never sends a one-row list there, so every
      `rows[0]` caller raised `KeyError: 0` once a tracked listing was live, which stalled
      inventory reconciliation. aiv2 has the same client code. Tests:
      `tests/test_listing_by_id.py`, and the reconcile harness in `tests/test_inventory.py`
      now mocks the real shape. See PROJECT_CONTEXT.md entry 116
- [ ] PR #102 - `ambiguous_commodity_text` (`bot/cogs/prices.py`) ends a list of more than five
      names with "and N more". aiv2's version (from its own `c4f1aa6`) appends " ..." and then
      the sentence's period, so the question reads "Name 4 .... Which one do you mean?". Test:
      `test_ambiguous_commodity_text_lists_at_most_five_names` in `tests/test_price_command.py`.
      See PROJECT_CONTEXT.md entry 118
- [ ] PR #103 - Marketplace listings picked by name, not typed by id. `/marketplace-listing`
      and `/marketplace-delete-listing` take a `listing` autocomplete (your own listings via
      the linked key's UEX username, plus favorites and open deals for the first), and
      `/marketplace-search`, `/my-favorites` and `/my-negotiations` get a "Show details for..."
      menu (`bot/cogs/marketplace.py`, `bot/uex/marketplace.py`'s listing-choice helpers,
      `UexClient.get_user_username`). Tests: `tests/test_listing_pickers.py`. See
      PROJECT_CONTEXT.md entry 119
- [ ] PR #106 - `/ship-loadout`, and a "Recommend a loadout" button in `/ship-parts-finder`'s
      browser: one recommended part per slot for a Balanced/Stealth/Tank/Budget profile, with
      keep-stock lines, total cost, the total power pips and "Add all to shopping list".
      Pure logic in the new `bot/uex/ship_loadout.py`; `LoadoutView`, `_loadout_slots` and
      `ShipPartsShoppingService.lock_in_many` in `bot/cogs/ship_parts_finder.py`; the new
      `WikiApiClient.get_vehicle_stock_ports` (`bot/wiki_api.py`) for the stock gun inside a
      gimbal, and `WikiDuplicateNameError` for a ship name the wiki uses twice (the Cutlass
      Black), said as a known issue in both commands instead of "no slots"; "ship-loadout" in
      `bot/cogs/help.py`'s Ships category. aiv2 needs the finder's
      earlier entries above first (PRs #60-#63 and the reliability items). Tests:
      `tests/test_ship_loadout.py`, `tests/test_ship_loadout_command.py`,
      `tests/test_wiki_api.py`, `tests/test_ship_parts_finder.py`. See PROJECT_CONTEXT.md
      entry 122
- [ ] PR #107 - `/ship-loadout` guns: never a scattergun (`is_scattergun`, and a stock one is
      always replaced), alpha damage breaking DPS ties (widened to a 5% DPS band by PR #108
      below), gun lines showing "DPS / alpha", and
      PDC slots always keeping their stock turret (`is_point_defense`, `bot/uex/ship_loadout.py`);
      "PDC" in port labels (`format_port_label`, `bot/uex/ship_part_display.py`). Needs PR #106
      above first. Tests: `tests/test_ship_loadout.py`, `tests/test_ship_loadout_command.py`,
      `tests/test_ship_part_display.py`. See PROJECT_CONTEXT.md entry 123
- [ ] PR #108 - `/ship-loadout` turret guns and the alpha band: guns inside locked and
      manned turrets from the single-vehicle tree (`locked_turret_gun_ports`, the cog's
      `_vehicle_stock_tree`), guns within 5% DPS ranked by alpha (`DPS_BAND`,
      `gun_at_least_as_good`, `_rank_guns`), and label fixes in `format_port_label` and
      `SlotGroup.label`. Needs PRs #106 and #107 above first. See PROJECT_CONTEXT.md entry 124
- [ ] PR #109 - `/ship-loadout` Done button (`LoadoutDoneButton`, a DynamicItem registered at
      `cog_load`, with a `_LoadoutDoneStub` in `LoadoutView`, `bot/cogs/ship_parts_finder.py`;
      `BotView.grey_out(keep=)` in `bot/discord_ui.py`): removes the loadout message, and keeps
      working after the view goes idle or the bot restarts. Needs the loadout PRs above first.
      See PROJECT_CONTEXT.md entry 125
- [ ] PR #111 - Ships the wiki lists twice resolve to the plain ship (`base_vehicle_row` in
      `bot/wiki_api.py`, used by `get_vehicle_loadout` and `get_vehicle_stock_ports`): the
      Cutlass Black, Carrack, Polaris and nine more get their slots. Tests:
      `tests/test_wiki_api.py`. See PROJECT_CONTEXT.md entry 126
- [ ] PR #112 - `/ship-parts-finder`'s `location` is optional (`bot/cogs/ship_parts_finder.py`:
      `PartsBrowserView.origin_terminal`/`origin_id` may be None, and ↻ Refresh's custom_id
      carries an empty terminal). Without it no distances show. See PROJECT_CONTEXT.md entry 127
- [ ] PR #114 - `/ship-loadout` keeps every ship's stock missile racks (`STOCK_RACKS` in
      `pick_for_slot`, `bot/uex/ship_loadout.py`; the kept line shows what the rack holds in
      every profile). An empty rack slot still gets a pick. See PROJECT_CONTEXT.md entry 129
- [ ] PR #115 - `delete_marketplace_listing` sends `is_production=1` (`bot/uex/client.py`):
      without it UEX answered deletes "ok" and the listings stayed live. Check aiv2's copy of
      the client. See PROJECT_CONTEXT.md entry 130
- [ ] PR #117 - Alerts only ping their owner (`owner_only_mentions` in `bot/delivery.py`'s
      `send_dm`/`send_to_channel_or_dm`) and the bot never pings @everyone or roles
      (`allowed_mentions` default in `bot/main.py`). Check aiv2's AI replies too: they echo
      player text. See PROJECT_CONTEXT.md entry 131
- [ ] PR #118 - `delete_marketplace_listing` reads the listing back and returns whether it's gone
      (`bot/uex/client.py`); the 48h relist, `/inventory-cancel-post` and the marketplace delete
      button act only on a confirmed delete (`bot/cogs/personal_inventory.py`,
      `bot/cogs/marketplace.py`). Needs PR #115 first. See PROJECT_CONTEXT.md entry 132

## To port: aiv2 -> production

- [x] aiv2's ship-loadout port (uncommitted there on 2026-10-02) - `LoadoutView` keeps the
      location as `origin_terminal`, not `origin`, which is `BotView`'s interaction: a loadout
      with a location never greyed out when idle (`bot/cogs/ship_parts_finder.py`).
      (ported to production in PR #113, PROJECT_CONTEXT.md entry 128)
- [x] aiv2 commit `c3c14ec` (fix #1 only) - typo-tolerant ore names in `/where-to-mine` and
      `/refinery-advisor` (`resolve_raw_material_name` in `bot/uex/trading.py`). Not logged
      here when aiv2 made it; found in a 2026-10-01 review of aiv2's log. Its other fixes are
      AI-chat or eval work, and fix #2 (city TDD terminals) needs a free-text terminal resolver
      production doesn't have.
      (ported to production in PR #104, PROJECT_CONTEXT.md entry 120)
- [x] aiv2 commit `a6bd024` - Cross-terminal price-outlier warnings: a commodity's buy/sell
      price checked against every other terminal trading it in the same snapshot, flagged
      when 4x-or-more off the median (`bot/uex/price_outliers.py`,
      `bot/uex/route_presentation.py`, `bot/cogs/prices.py`, `bot/cogs/intelligence_brief.py`,
      `bot/cogs/ai_chat.py`). Wired into `/ai-mixed-routes`, `/ai-multi-stop-route`,
      `/ai-route-from-multi`, `/ai-best-route` (both branches), `/ai-intelligence-brief` -
      `/ai-top-routes` and siblings intentionally left uncovered (see the commit message for
      why). Production already has its own copy of this same feature, built there first, on
      the unmerged `feature/price-outlier-detection` branch - porting this entry likely
      means merging that branch rather than re-implementing from scratch, but check it's
      still current (and still unmerged) before assuming a fresh port is needed.
      (ported to production in PR #105, PROJECT_CONTEXT.md entry 121: rebuilt on today's
      code from aiv2's version, since that branch was 79 commits behind)
- [x] aiv2 commit `f2785ae` - Refinery Advisor finds sell prices for ores UEX
      links only from the refined side, and signs negative yield bonuses ("-3%", not "+-3%"):
      new `refined_form`/`format_yield_bonus` (`bot/uex/refinery.py`), used in
      `bot/cogs/refinery.py`; 5 new tests in `tests/test_refinery.py`. Checked live: 5 of 32
      refinable ores (Taranite, Lindinium, Savrilium, Torite, Aslarite) have `id_parent` 0 on
      the raw row while the refined row still links back, so production's `/refinery-advisor`
      shows no sell price for them today. Found through aiv2 chat testing. Production's
      `bot/uex/refinery.py` and `tests/test_refinery.py` matched aiv2's before this change, so
      it ports cleanly.
      (ported to production in PR #101, PROJECT_CONTEXT.md entry 117)
- [x] aiv2 commit `c4f1aa6` - Price only the commodity asked for. UEX's
      `/commodities_prices?commodity_name=` matches by SUBSTRING, and every lookup by name used
      the mixed rows as-is. Checked live 2026-09-29: "Gold" returns Gold + Golden Medmon, so
      `/price Gold` shows Golden Medmon's 71,000 (real Gold sells for about 31,000) and
      `/refinery-advisor` quotes it as Gold's sell price; "Tin" returns Astatine first, so
      `/best-route Tin` builds an Astatine route. About 8 commodities affected (Gold, Tin,
      Iron, Diamond, Carbon, Borase, Hydrogen, ship ammunition). Same seven lookups in
      production (`TestBranch` @ `040323d`): `bot/cogs/prices.py` (`/price` ~219 and best-route
      ~433), `refinery.py` ~213, the `alerts.py` ~199 and `stock_alerts.py` ~114 pollers (a Gold
      alert fires on Golden Medmon's price), and `trends.py` ~268 (trending/top-routes refresh)
      and ~1060 (`/commodity-history`). Port only the core fix - new `rows_for_commodity`/
      `rows_for_known_commodity` in `bot/uex/trading.py`, `/price` asking "which one?" when a
      typed name matches several and replying in text when nothing trades; tests in
      `tests/test_exact_commodity_lookups.py` (a fake UEX that matches by substring like the real
      one), `test_trading.py`, `test_price_command.py`, `test_refinery.py`. Skip aiv2's chat
      price tool, its `price_summary.py` facts and the evals - production has no AI. Port the
      refinery entry above (`f2785ae`) first: both touch the refinery sell-price lookup.
      (ported to production in PR #102, PROJECT_CONTEXT.md entry 118)
