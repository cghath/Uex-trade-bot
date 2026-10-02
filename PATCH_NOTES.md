# Patch Notes

What changed for players, newest first. Each entry bumps the minor version by one (2.0 is
the oldest entry below); the next new entry is 2.45.

---

## 2.44 - 2026-10-01 - Done with a loadout

**Added**
- `/ship-loadout` - A Done button removes the loadout message once you're finished with it, even after its other buttons have greyed out from sitting idle or a bot restart. Anything you added to your shopping list stays there.

---

## 2.43 - 2026-10-01 - Turret guns in ship loadouts

**Fixed**
- `/ship-loadout` - Recommends guns for turrets it used to leave out: manned turrets, and remote turrets whose gimbals the game locks. The Idris-M now lists all 22 of its turret guns, and the Perseus, Constellation Andromeda and Redeemer gain their manned-turret guns.

**Changed**
- `/ship-loadout` - Between two guns within 5% DPS of each other, the one with more alpha damage wins, not only on an exact tie.

---

## 2.42 - 2026-10-01 - Better weapon picks in ship loadouts

**Changed**
- `/ship-loadout` - Never recommends a scattergun, and replaces one a ship comes with. When two guns do the same DPS, the one with more alpha damage wins, and every gun line shows both, like "1,266 DPS / 84.4 alpha".
- `/ship-loadout` - Always keeps a ship's stock point-defense turrets (PDCs): they shoot down incoming missiles and never run out of ammo.

---

## 2.41 - 2026-10-01 - Recommended ship loadouts

**Added**
- `/ship-loadout` - Pick a ship and get a recommended part for every slot: Balanced, Stealth, Tank or Budget, with buttons to switch. Each line shows the part, how it compares with the stock one, its price and shop, or says to keep the stock part when nothing sold beats it. Guns stay in the ship's own gimbals. Stealth picks the lowest EM signature. Shows the total cost and the loadout's total power pips. "Add all to shopping list" adds every purchase to your private ship parts list. Add a location and ties go to the nearest shop.
- `/ship-parts-finder` - A "Recommend a loadout" button opens the same loadout for the ship and location you're browsing.

**Changed**
- `/ship-parts-finder` - Ships the Star Citizen Wiki lists twice under one name, like the Cutlass Black, Carrack and Polaris, now say so instead of reporting no slots. Their parts aren't available yet.

---

## 2.40 - 2026-10-01 - Suspicious prices flagged

**Added**
- `/best-route`, `/mixed-routes`, `/multi-stop-route` and `/intelligence-brief` - A route warns when its buy or sell price is 4x or more off what every other terminal charges for that commodity. It could be a real deal or a UEX data error, so check before you fly. This would have caught UEX once listing Fresh Food at 2,614 instead of about 21,614.

---

## 2.39 - 2026-10-01 - Misspelled ores understood

**Fixed**
- `/where-to-mine` and `/refinery-advisor` - A misspelled ore sent without picking a suggestion, like "Quantanium", finds the right one instead of failing. A typo that could be two different ores still asks you to pick.

---

## 2.38 - 2026-10-01 - Pick listings by name

**Changed**
- `/marketplace-listing` - Pick one of your listings, favorites or deals from a list by name, instead of typing its id. Typing an id still works.
- `/marketplace-delete-listing` - Pick one of your own listings from a list by name.
- `/marketplace-search`, `/my-favorites` and `/my-negotiations` - A "Show details for…" menu under the results shows any listing in full, privately.

---

## 2.37 - 2026-10-01 - Prices for the commodity you named

**Fixed**
- `/price` - Shows only the commodity you named. Gold, Diamond and Carbon showed another commodity's higher price: Gold's top sell was Golden Medmon's 71,000, against Gold's own ~31,000.
- `/best-route` - Tin and Jaclium get their own routes, not Astatine's or Jaclium (Ore)'s.
- `/refinery-advisor` - Gold (Ore)'s sell price is Gold's own, not Golden Medmon's.
- Price and stock alerts - A Gold alert no longer goes off on Golden Medmon's price or stock.
- `/trending`, `/top-routes` and `/commodity-history` - Each commodity uses only its own prices.

**Changed**
- `/price`, `/best-route` and `/commodity-history` - A name that matches several commodities, like "Gol", asks which one you mean.
- `/price` - Says so in a line of text when no terminal buys or sells the commodity, instead of an empty card.

---

## 2.36 - 2026-10-01 - Refinery prices for every ore

**Fixed**
- `/refinery-advisor` - Shows a sell price for Taranite, Lindinium, Savrilium, Torite and Aslarite, which had none.
- `/refinery-advisor` - A negative yield bonus reads "-3%", not "+-3%".

---

## 2.35 - 2026-10-01 - Inventory listings tracked again

**Fixed**
- `/inventory-sell` - Listings you've posted are checked, repriced and reported on again. Since September 30 the
  bot stopped at the first listing that had gone live on UEX, so none of it happened.
- `/marketplace-listing`, `/marketplace-delete-listing`, `/inventory-cancel-post`, `/my-favorites`, `/my-negotiations`
  - No longer fail on a listing that's live on UEX.

---

## 2.34 - 2026-09-30 - Risky hedges are labelled

**Fixed**
- `/best-route`, `/top-routes` - A "Hedge:" line now shows the ⚠️ cargo risk of what it suggests, e.g. "⚠️ Cargo
  risk: restricted in some jurisdictions". Since 2.33 a hedge can be any cargo, illegal goods included, and it had no
  label.
- Route tracking threads - The "this could fill it" suggestion after a short purchase, and the other cargo in a backup
  route, carry the same label.

---

## 2.33 - 2026-09-30 - Risk tolerance removed

**Removed**
- `/set-trading-preferences` `risk-tolerance` - Gone. Almost nobody used it, and Low and Medium left out the same
  goods, the illegal ones. Route suggestions no longer leave out any cargo for being risky.
  - Routes still label risky cargo with ⚠️, e.g. "⚠️ Cargo risk: restricted in some jurisdictions", so you can see
    what you'd be hauling before you pick a route.
  - A risk tolerance you saved before is simply no longer used. Your other saved preferences are unchanged.

---

## 2.32 - 2026-09-30 - Fix a trade, price every stack

**Added**
- `/trade-log` - A menu under your list removes a wrong entry. To correct one, remove it and log it again.
- `/inventory-sell` - "Enter a custom price..." works for any number of stacks, with a price for each (five per form).
  A button lets you set the next five or change a price without starting over.

**Changed**
- `/trade-log-add` - The commodity and terminal options autocomplete.

---

## 2.31 - 2026-09-30 - One place for your ship

**Removed**
- `/set-default-ship`, `/clear-default-ship` - Your default ship is now set with `/set-trading-preferences ship:`,
  alongside your other saved defaults. Choose "No default ship" in its list to clear it. Your saved ship is unchanged.

**Changed**
- Every "set a default ship" hint now points to `/set-trading-preferences`.

---

## 2.30 - 2026-09-30 - Clearer wording

**Changed**
- `/price` - The footer only explains the markers actually shown on that reply, and says prices update every 30 min.
- `/liquidity-rank`, `/liquidity-trends`, the daily digest - Call the score the Sellability Rating, as everywhere else.
- `/set-digest-channel`, `/digest-disable` - Hidden from members without the Manage Server permission, who couldn't
  use them anyway.

**Fixed**
- `/best-route` - When UEX has no route data for a commodity, the profit percentage is now labelled ROI (profit per
  aUEC spent), which is what it always was.
- Route lists - Distances are written "Gm" everywhere.
- Daily digest - No more "just now ago".
- `/ship-parts-finder` - A failed ship lookup explains what happened instead of showing an error code.

---

## 2.29 - 2026-09-30 - Fewer route commands

**Changed**
- `/top-routes` - Now has `origin` and `destination` options. Set `origin` for routes starting where you are (what
  `/routes-from` did), both for routes between two terminals (what `/route-on-the-way` did), or just `destination`
  for routes ending somewhere.
- `/multi-stop-route` - Now has an `origin` option to start the chain where you are (what `/route-from-multi` did).

**Removed**
- `/routes-from`, `/route-on-the-way` and `/route-from-multi` - Folded into the two commands above, with the same
  results.

---

## 2.28 - 2026-09-30 - Nearby means nearby

**Changed**
- Route tracking threads - After a sell-side shortfall, the suggested buyer is now really nearby: in the same star
  system and within 25 Gm, and the thread says how far it is. If nothing that close buys it, the thread says so.
- `/ship-parts-finder` - Your shopping list now shows each part's cheapest shop and price right now, every time it's
  drawn, plus what it cost when you locked it in if that changed. "Refresh list" updates the prices.
- `/refinery-advisor` - Says how often its data updates (yield bonuses every 24h, sell prices every 30 min) instead of
  calling it live, and says when UEX didn't answer instead of showing no data.

---

## 2.27 - 2026-09-30 - Saved preferences, said plainly

**Changed**
- `/intelligence-brief` - Its route suggestions now use your saved `/set-trading-preferences` defaults (budget,
  space-only, capital-ship access, auto-load-only, system and risk tolerance), like every other route command. Its
  own budget and space-only options still win when you set them.
- Every route command - The footer lists the filters shaping the results, and marks the ones that came from your
  saved preferences "(saved)". `/mixed-routes`, `/multi-stop-route` and `/route-from-multi` used to leave out a
  saved auto-load, system or risk filter.
- `/set-trading-preferences`, `/my-trading-preferences` - Now say which commands each default really applies to.
  Several named the wrong ones.

**Fixed**
- `/my-trading-preferences` - No longer says your default ship may have been renamed when UEX's ship list just
  didn't load.

---

## 2.26 - 2026-09-30 - Route results in one message

**Changed**
- `/top-routes`, `/routes-from`, `/route-on-the-way`, `/best-route`, `/mixed-routes`, `/multi-stop-route`,
  `/route-from-multi` - Results now arrive as one message showing a route at a time, instead of a message per
  route. Use ◀ ▶ to page through them (only whoever ran the command can), and **Track this route** to track the
  one showing (anyone can). Every route can now be tracked, not just the first five.

---

## 2.25 - 2026-09-30 - Pick from a list; linking checks your key

**Changed**
- `/inventory-set-minimum`, `/inventory-remove`, `/inventory-post-now` - Pick the stack from a list of your own
  stacks instead of looking up its number in `/inventory` first.
- `/inventory-confirm-sale`, `/inventory-cancel-post`, `/inventory-resolve-floor` - Pick the listing from a list,
  showing only the ones that command can act on.
- `/link-uex-account`, `/uex-account-status` - Now say that a linked account appears on the server's `/leaderboard`,
  and how to come off it.

**Fixed**
- `/link-uex-account` - Checks your key with UEX before saving it. A wrong key is no longer "linked" only to fail
  later, and the confirmation names the UEX account it belongs to.

---

## 2.24 - 2026-09-30 - Choose where alerts arrive

**Changed**
- `/alert-add`, `/stock-alert-add`, `/marketplace-alert-add` - All three now have the same `delivery` option: **DM me**
  (the default) or **Post in this channel and ping me**. Price alerts used to only post in the channel, and
  Marketplace alerts only DM. Alerts you already have keep arriving where they did.
- `/stock-alert-add` - The `scope` option is now `delivery`, and new restock alerts are DMs unless you pick the channel.
- `/alert-list`, and each alert's confirmation - Now say how often each type fires (a price alert once, restock and
  Marketplace alerts every time) and where each of your alerts arrives.

---

## 2.23 - 2026-09-30 - Risk tolerance now works

**Fixed**
- `/set-trading-preferences` `risk-tolerance` - It now does what it says. It used to be saved but ignored.
  - **Low** leaves illegal, explosive, volatile and buggy goods out of route suggestions.
  - **Medium** leaves out illegal and buggy goods.
  - **High** (the default) leaves out nothing.

  It applies to `/top-routes`, `/routes-from`, `/route-on-the-way`, `/mixed-routes`, `/multi-stop-route`,
  `/route-from-multi`, `/diminishing-returns`, and every "Hedge:" suggestion. If it leaves nothing, the reply says so.
- `/best-route` - Still shows the commodity you ask for, but says when it's outside your risk tolerance.

---

## 2.22 - 2026-09-29 - Expired buttons look expired

**Changed**
- Buttons and menus - Once they stop working, they now grey out instead of looking usable. Before, clicking one
  just showed "This interaction failed". Run the command again for fresh ones.
- `/blueprint-search` - The Configure crafting menu now closes after 10 idle minutes instead of 15.

---

## 2.21 - 2026-09-29 - No more endless "thinking…"

**Fixed**
- Every command, button and form - If something goes wrong on the bot's side, you now get a message saying so,
  instead of the bot "thinking…" forever or a button that seems to do nothing.
- `/ship-parts-finder` - If a category's parts fail to load, the browser stays up and asks you to pick it again,
  instead of showing the previous parts or replacing the browser with an error.
- `/ship-parts-finder`, `/blueprint-list` - Using them twice in quick succession no longer creates two private
  threads.

---

## 2.20 - 2026-09-29 - Listing ids and tidier lists

**Changed**
- `/marketplace-search`, `/my-favorites`, `/my-negotiations` - Each listing now shows its listing id, the number
  `/marketplace-listing` and `/marketplace-delete-listing` ask for. (`/my-favorites` used to show a different number
  that no command accepted.)
- `/uex-trades` - Trade dates now show as real dates in your own time zone, not raw numbers.
- `/trade-log` - `limit` now goes from 1 to 50.

**Fixed**
- `/my-favorites`, `/my-negotiations`, `/trade-log`, `/uex-trades` - A long list no longer makes the command fail;
  it shows as many entries as fit and says how many more there are.
- `/uex-trades` - No longer tells you your linked key may be invalid when UEX is just having a temporary problem.

---

## 2.19 - 2026-09-29 - Alerts that can actually fire

**Changed**
- `/alert-add`, `/stock-alert-add` - The commodity has to be a real tradeable one now. A typo used to create an alert
  that could never go off; now it's refused with suggestions ("Did you mean **Laranite**?").
- `/stock-alert-add` - When several terminals restock at once, including when a new alert finds stock straight away,
  you get one message listing them cheapest first, instead of a separate ping for every terminal.

---

## 2.18 - 2026-09-29 - Ship Parts Finder doesn't hang on a slow wiki

**Fixed**
- `/ship-parts-finder` - When the Star Citizen Wiki is slow or down, picking a category could leave the browser
  loading forever. The list now appears within about 45 seconds either way; any parts the wiki didn't answer for are
  listed without their stats, with a note saying so, and fill in if you try again a few minutes later.

---

## 2.17 - 2026-09-29 - Route lists survive a UEX hiccup

**Fixed**
- `/trending`, `/top-routes`, `/routes-from`, `/route-on-the-way` - These are refreshed every 45 minutes. If UEX
  failed partway through a refresh, the lists used to be quietly replaced with an incomplete version. Now a refresh
  that misses more than a tenth of commodities keeps the previous, complete lists instead. When a partial list is
  shown, the footer says so ("partial refresh: 3 of 159 commodities couldn't be fetched"), and a "nothing found"
  reply says some routes may be missing.

---

## 2.16 - 2026-09-29 - Ship Parts Finder: pick up where you left off

**New**
- `/ship-parts-finder` - The browser now has a **↻ Refresh** button. If you step away and the dropdowns stop
  responding, tap it to bring the browser back on the same ship and category, without running the command again.
  After 30 minutes idle the browser now greys out and says so, instead of looking usable. Your locked-in parts were
  never affected.

---

## 2.15 - 2026-09-29 - Charts and suggestions stay quick

**Fixed**
- `/liquidity-trends`, `/marketplace-history`, `/commodity-history`, `/diminishing-returns` - Drawing a chart
  no longer pauses every other command while it renders.
- Ship, commodity, ore and Marketplace category suggestions (`/set-default-ship`, `/price`, `/where-to-mine`,
  `/refinery-advisor`, `/marketplace-post` and others) no longer go blank for a while when UEX is slow.
  If UEX doesn't answer in time you get no suggestions right away, and they're ready a moment later.

---

## 2.14 - 2026-09-29 - Route commands say why nothing came back

**Changed**
- `/best-route`, `/top-routes`, `/routes-from`, `/route-on-the-way`, `/mixed-routes`, `/multi-stop-route`,
  `/route-from-multi`, `/diminishing-returns` - When nothing is found because of a filter from your saved
  `/set-trading-preferences` (auto-load-only, a star system, space-only or capital-ship access), the message now
  names that setting and how to override it, instead of just "nothing found right now".
- The same route commands no longer tell you to "set a default ship" when you already have one. They now say
  when your saved ship no longer matches one of UEX's ships, or when UEX's ship list didn't load.
- `/mixed-routes` - Its "nothing fits" message now says auto-load is checked at both ends of the route, not
  just the origin, matching what it actually checks.

---

## 2.13 - 2026-09-28 - Scanner off switch, quality and currency fixes

**New**
- `/scanner-status` - Now has a **Turn off** button, the first way to stop Raw Materials Deal Scanner alerts.
  `/set-scanner-channel` turns it back on.

**Fixed**
- `/marketplace-search`, `/marketplace-alert-add` - Quality filters now use the same 0-1000 scale as in-game
  quality, and reject numbers outside it. They used to say 0-100, which made a filter like "at least 80" match
  almost everything.
- `/alert-list` - A Marketplace alert with no maximum quality now shows "0-1000", not "0-100".
- `/marketplace-movers` - Prices show each item's own currency (UEC, WIF or MGS) instead of always "UEC".
- Price, restock and Marketplace alerts, deal scanner posts and negotiation DMs are no longer lost when Discord
  has a brief hiccup. They're retried on the next check. A negotiation message too long for a Discord DM now
  arrives trimmed, with a pointer to read the rest on UEX.

---

## 2.12 - 2026-09-25 - Ship Parts Finder (ready for testing)

**New**
- `/ship-parts-finder` - Pick a ship and your location to browse every component slot on it: weapons, gun mounts,
  missile racks, power plants, coolers, shield generators, quantum drives and radar. Guns inside turrets get their
  own slots. Each slot lists only parts that really fit it and that a shop currently sells, ranked by the stat that
  matters most (quantum speed, power generation, DPS and so on), with the cheapest shop selling it, how far that
  shop is from you, and the part's key stats. Long lists page with Previous/Next. Lock parts into a private shopping list in your own thread, and remove
  them one at a time when you've bought them. Still being tested, so report anything that looks wrong.

---

## 2.11 - 2026-09-25 - Where to Buy a Ship

**New**
- `/where-to-buy-ship` - Find every in-game shop that sells a ship for aUEC, and every terminal that rents it,
  cheapest first. Rentals are grouped by star system and show the 1-day rate. Each line also says how long ago
  its price was last reported. The ship list only suggests ships you can actually buy or rent in-game. Listed
  under Ship & Cargo in `/intro`.

---

## 2.10 - 2026-09-23 - In-game Item Finder

**New**
- `/ingame-item-finder` - Find every shop currently selling a weapon, armor, ammo, or other in-game item,
  closest to a location you pick first. Each result shows where it is and its price. Shows up to 15 shops;
  the footer says how many more exist if there are extras.

---

## 2.9 - 2026-09-22 - /my-ship folded into /my-trading-preferences

**Changed**
- `/my-trading-preferences` - Now shows your saved ship's live cargo capacity (SCU), or a note if it no longer
  matches UEX's current ship list (renamed or removed), the same detail `/my-ship` used to show.

**Removed**
- `/my-ship` - Use `/my-trading-preferences` instead, which now shows the same ship detail alongside your other
  saved defaults.

---

## 2.8 - 2026-09-22 - Reroute suggestions when a sale falls short

**Changed**
- Route tracking threads - After you report a sell-side shortfall (the destination bought less than quoted), the
  thread now suggests a different terminal that still buys the unsold remainder, if one exists nearby. If none does,
  it says so instead of staying quiet. The existing buy-side hedge (suggesting a commodity to fill leftover cargo
  space) now also says so explicitly when nothing trades between your two terminals, instead of silently doing
  nothing.

---

## 2.7 - 2026-09-21 - Stock warnings and hedges on route lists

**Changed**
- `/top-routes`, `/routes-from`, `/route-on-the-way` - A route that would use **all** the stock or demand currently on
  record now shows the same warning `/best-route` does, and, if your ship has room left over (and your budget covers it),
  a `Hedge:` line suggesting another commodity for the same trip. A hedge only appears when a second commodity actually
  trades between those same two terminals, which is uncommon, so expect the warning far more often than the hedge line.
  Routes limited by your ship or budget are unchanged.

_Ref: b5ec596_

---

## 2.6 - 2026-09-21 - Pin mixed loads to a terminal, and 4-hop chains

**New**
- `/mixed-routes` `origin` and `destination` options - Show only the best mixed loads that start at a terminal you choose,
  end at one, or both, for example "the best load from where I'm standing." Terminal names autocomplete the same way
  `/route-from-multi`'s location does.
- `/multi-stop-route` and `/route-from-multi` `max-legs` option - Choose 4 hops instead of the default 3. It can find more
  profit when your budget allows, but takes about twice as long. Leaving it unset changes nothing.

**Changed**
- `/mixed-routes` - Its description now explains that it hedges against one item's stock or demand running short.

_Ref: d036daf_

---

## 2.5 - 2026-09-21 - Hedge reports in route tracking

**Changed**
- Route tracking threads - After you report a buy-side shortfall, the thread suggests one hedge commodity for the same trip.
  Press **Report** and enter how much you actually bought (and, optionally, the price). At the destination it asks whether
  you sold it. Your reports feed the bot's market data the same way tracked legs do.

_Ref: 2386e9a_

---

## 2.4 - 2026-09-21 - Stock warnings and hedges on /best-route

**Changed**
- `/best-route` - When a route would use **all** the stock or demand currently on record, it now warns you (if the real
  amount is lower when you arrive, your hold is left half empty) and, if your ship has room left over, adds a `Hedge:` line
  suggesting another commodity for the same trip. Nothing changes when your ship or budget is what limits the haul.

_Ref: 72941af_

---

## 2.3 - 2026-09-21 - Short route lists explain themselves

**Changed**
- `/top-routes`, `/routes-from`, `/route-on-the-way` - When fewer routes qualify than the list normally shows (for example
  with **auto-load-only** or a star-system filter on), the footer now says how many qualify, such as "only 3 routes currently
  qualify (this list shows up to 10)", instead of quietly showing a short list.

_Ref: 5f5b3ea_

---

## 2.2 - 2026-09-21 - Routes ranked by what you can earn

**Changed**
- `/top-routes`, `/routes-from`, `/route-on-the-way` - Routes are now ranked by the profit **you can actually earn** with your
  saved ship and/or budget, not by UEX's headline figure (which assumes unlimited cargo and cash). For example, a route
  listed at 54.5M profit needed 58M to run and would net only about 314k for a 1,440 SCU ship with a 2M aUEC budget, while
  a Corundum route listed at only 1.4M would net about 1.3M. Nothing changes until you have saved a ship or a budget.

_Ref: cb98740_

---

## 2.1 - 2026-09-21 - Refinery advisor

**Changed**
- `/refinery-advisor` - Refineries in the ore's own mining system now come first, and **every** refinery in that system is
  shown (up to 12) instead of a flat top 5. Before, the best yield could be in a system where you can't mine the ore at all
  (Quantainium's top yield is a Nyx refinery, but it is only mined in Stanton). Refineries outside the mining system are
  still listed, marked with a warning sign, and the footer explains why. When you enter several ores, refineries are judged
  against the star systems where **every** ore is mined, not where any one of them is. If no system mines all of them, it
  falls back to any of them and says so. An ore whose mining systems aren't known is left out of that judgement and named
  in the note.

_Ref: c34f469_

---

## 2.0 - 2026-09-21 - Blueprints

**New**
- `/blueprint-search` - Type a blueprint name (partial names and small typos are fine) and see which contracts award it, who
  gives them, and the drop chance where the Star Citizen Wiki has one. It also shows what it takes to craft the blueprint;
  the `craft_quantity` option (1-10,000) scales the material amounts. Buttons: **Configure crafting** (choose the required
  materials and their quality, with Previous/Next buttons if a recipe has more options than fit on one screen), **Add to
  shopping list**, and **Mine <ore>** (opens where-to-mine for that ore). If two different blueprints share a name, the bot asks which one you mean and shows each with its ID. Very long results are cut
  with "Showing X of Y contract groups" instead of failing, and blueprint data that doesn't line up (a partial download or a
  mix of game versions) is refused rather than shown. The data comes from the Star Citizen Wiki and is refreshed every 12
  hours.
- `/blueprint-list` - Opens your own private thread with one combined shopping list of the materials for every blueprint you
  have added. It has **Refresh list** and **Clear list** buttons that only you can use. Needs a server where the bot is
  allowed to create private threads. If the bot can't update your thread, it tells you instead of going quiet.

**Changed**
- `/intro` - The guide has a new Blueprints section listing the two commands above.

_Ref: 3c392a0_
