# Patch Notes

What changed for players, newest first. Command names are shown as on the production bot;
the AI bot uses the same commands with an `ai-` prefix (`/price` -> `/ai-price`).

---

## 2026-09-21 - Backup route button

**New**
- **Backup route** button on `/best-route`, `/top-routes`, `/routes-from` and `/route-on-the-way` - On a route that would use
  **all** the stock or demand on record, press it for a private plan B that **keeps the commodity you may already have
  bought**. It can fill your spare hold with something else at the same terminals, offer a different destination when that
  is clearly better (at least 10% more profit, since travel time isn't counted), and - if you haven't bought yet - show the
  best load from that terminal without it. Often the honest answer is that nothing beats your plan; it says so and tells you
  to continue as planned. Only the player who ran the command can press it, and like the Track button it stops working after
  about 15 minutes or a bot restart.

**Changed**
- The stock warning on those routes now points at the button. A `/best-route` result built from its price-row fallback has no
  per-route buttons, so it still points at `/mixed-routes`.

---

## 2026-09-21 - Stock warnings and hedges on route lists

**Changed**
- `/top-routes`, `/routes-from`, `/route-on-the-way` - A route that would use **all** the stock or demand currently on
  record now shows the same warning `/best-route` does, and, if your ship has room left over (and your budget covers it),
  a `Hedge:` line suggesting another commodity for the same trip. A hedge only appears when a second commodity actually
  trades between those same two terminals, which is uncommon, so expect the warning far more often than the hedge line.
  Routes limited by your ship or budget are unchanged.

---

## 2026-09-21 - Pin mixed loads to a terminal, and 4-hop chains

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

## 2026-09-21 - Hedge reports in route tracking

**Changed**
- Route tracking threads - After you report a buy-side shortfall, the thread suggests one hedge commodity for the same trip.
  Press **Report** and enter how much you actually bought (and, optionally, the price). At the destination it asks whether
  you sold it. Your reports feed the bot's market data the same way tracked legs do.

_Ref: 2386e9a_

---

## 2026-09-21 - Stock warnings and hedges on /best-route

**Changed**
- `/best-route` - When a route would use **all** the stock or demand currently on record, it now warns you (if the real
  amount is lower when you arrive, your hold is left half empty) and, if your ship has room left over, adds a `Hedge:` line
  suggesting another commodity for the same trip. Nothing changes when your ship or budget is what limits the haul.

_Ref: 72941af_

---

## 2026-09-21 - Short route lists explain themselves

**Changed**
- `/top-routes`, `/routes-from`, `/route-on-the-way` - When fewer routes qualify than the list normally shows (for example
  with **auto-load-only** or a star-system filter on), the footer now says how many qualify, such as "only 3 routes currently
  qualify (this list shows up to 10)", instead of quietly showing a short list.

_Ref: 5f5b3ea_

---

## 2026-09-21 - Routes ranked by what you can earn

**Changed**
- `/top-routes`, `/routes-from`, `/route-on-the-way` - Routes are now ranked by the profit **you can actually earn** with your
  saved ship and/or budget, not by UEX's headline figure (which assumes unlimited cargo and cash). For example, a route
  listed at 54.5M profit needed 58M to run and would net only about 314k for a 1,440 SCU ship with a 2M aUEC budget, while
  a Corundum route listed at only 1.4M would net about 1.3M. Nothing changes until you have saved a ship or a budget.

_Ref: cb98740_

---

## 2026-09-21 - Refinery advisor

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

## 2026-09-21 - Blueprints

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
