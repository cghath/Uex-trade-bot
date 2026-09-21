# Patch Notes

What changed for players, newest first. Command names are shown as on the production bot;
the AI bot uses the same commands with an `ai-` prefix (`/price` -> `/ai-price`).

---

## 2026-09-21 - Blueprints

**New**
- `/blueprint-search` - Type a blueprint name (partial names and small typos are fine) and see which contracts award it, who
  gives them, and the drop chance where the Star Citizen Wiki has one. It also shows what it takes to craft the blueprint;
  the `craft_quantity` option (1-10,000) scales the material amounts. Buttons: **Configure crafting** (choose the required
  materials and their quality), **Add to shopping list**, and **Mine <ore>** (opens where-to-mine for that ore). If two
  different blueprints share a name, the bot asks which one you mean and shows each with its ID. Very long results are cut
  with "Showing X of Y contract groups" instead of failing, and blueprint data that doesn't line up (a partial download or a
  mix of game versions) is refused rather than shown. The data comes from the Star Citizen Wiki and is refreshed every 12
  hours.
- `/blueprint-list` - Opens your own private thread with one combined shopping list of the materials for every blueprint you
  have added. It has **Refresh list** and **Clear list** buttons that only you can use. Needs a server where the bot is
  allowed to create private threads.

**Changed**
- `/intro` - The guide has a new Blueprints section listing the two commands above.
