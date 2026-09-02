"""One-off diagnostic: dumps UEX's real /commodities_status code definitions (buy side and
sell side) so we can see exactly what "Out of Stock" / "Maximum" / etc. mean, instead of
inferring it from public docs alone.

Run this from the repo root with the venv active, same as the bot itself (the -m form
keeps the repo root on the import path so `bot.*` imports resolve):

    python -m scripts.dump_status_codes

It uses your existing .env (same UEX_APP_TOKEN the bot already runs with) - read-only, no
writes, doesn't touch Discord at all. Safe to run any time, including while the bot is running.
"""
from __future__ import annotations

import asyncio

from bot.config import Config
from bot.uex.client import UexClient


def _fmt_pct(row: dict) -> str:
    start = row.get("percentage_start")
    end = row.get("percentage_end")
    if start is None and end is None:
        return "n/a"
    return f"{start}-{end}%"


async def main() -> None:
    config = Config.from_env()
    client = UexClient(app_token=config.uex_app_token)

    print("Fetching /commodities_status ...\n")
    status_data = await client.get_commodities_status()

    for side in ("buy", "sell"):
        rows = status_data.get(side) or []
        print(f"=== {side.upper()} SIDE ({len(rows)} codes) ===")
        for row in sorted(rows, key=lambda r: r.get("code", 0)):
            code = row.get("code")
            name = row.get("name")
            name_short = row.get("name_short")
            pct = _fmt_pct(row)
            print(f"  code={code!r:>4}  name={name!r:<20} name_short={name_short!r:<15} band={pct}")
        print()

    # Cross-reference: pull the real live row for the "Waste -> Everus Harbor" route from the
    # /top-routes strict:True output, so we can see the raw numbers behind that specific
    # "sell side: Out Stock" label next to its actual sell price.
    print("Cross-checking a real commodity (Waste) across terminals for context:\n")
    try:
        rows = await client.get_commodities_prices(commodity_name="Waste")
    except Exception as exc:  # noqa: BLE001 - diagnostic script, just report and move on
        print(f"  (couldn't fetch: {exc})")
        rows = []

    for row in rows:
        terminal = row.get("terminal_name", "?")
        status_sell = row.get("status_sell")
        price_sell = row.get("price_sell")
        scu_sell = row.get("scu_sell")
        print(
            f"  {terminal:<45} status_sell={status_sell!r:>4}  price_sell={price_sell!r:>10}  "
            f"scu_sell={scu_sell!r}"
        )

    await client.aclose()


asyncio.run(main())
