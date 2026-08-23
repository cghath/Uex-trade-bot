"""Background-only UEX data collection for future intelligence features.

Nothing in this cog creates a Discord command. Its job is to turn the Pi's uptime into
useful history while storing only changed states, not an expensive full duplicate snapshot
on every poll.
"""
from __future__ import annotations

import asyncio
import logging
from collections.abc import Iterable

from discord.ext import commands, tasks

from bot.uex.exceptions import UexApiError

logger = logging.getLogger("uexbot.intelligence")

TERMINAL_MARKET_SNAPSHOT_HOURS = 2
DATA_HEALTH_SNAPSHOT_HOURS = 1
FUEL_SNAPSHOT_HOURS = 6
REFERENCE_SNAPSHOT_HOURS = 24
FUEL_BATCH_SIZE = 10  # UEX documents id_terminal batches of up to ten.
FUEL_REQUEST_DELAY_SECONDS = 0.6  # Keeps this background work well below 120 req/min.


def _chunks(values: list[int], size: int) -> Iterable[list[int]]:
    for start in range(0, len(values), size):
        yield values[start : start + size]


def _is_true(value: object) -> bool:
    return value is True or str(value).strip() == "1"


class Intelligence(commands.Cog):
    """Collect durable UEX history without adding user-facing feature surface yet."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.snapshot_terminal_market.start()
        self.snapshot_data_health.start()
        self.snapshot_fuel_prices.start()
        self.refresh_reference_data.start()

    def cog_unload(self) -> None:
        self.snapshot_terminal_market.cancel()
        self.snapshot_data_health.cancel()
        self.snapshot_fuel_prices.cancel()
        self.refresh_reference_data.cancel()

    @tasks.loop(hours=TERMINAL_MARKET_SNAPSHOT_HOURS)
    async def snapshot_terminal_market(self) -> None:
        try:
            rows = await self.bot.uex.get_commodities_prices_all()
            changed, total = await self.bot.db.record_terminal_market_snapshot(rows)
            logger.info("Terminal market snapshot: %d changed states across %d rows", changed, total)
        except UexApiError as exc:
            logger.warning("Terminal market snapshot failed: %s", exc)
        except Exception:
            logger.exception("Terminal market snapshot failed unexpectedly")

    @snapshot_terminal_market.before_loop
    async def before_terminal_market_snapshot(self) -> None:
        await self.bot.wait_until_ready()

    @tasks.loop(hours=DATA_HEALTH_SNAPSHOT_HOURS)
    async def snapshot_data_health(self) -> None:
        try:
            rows = await self.bot.uex.get_data_monitor()
            changed, total = await self.bot.db.record_terminal_data_health_snapshot(rows)
            logger.info("Terminal data-health snapshot: %d changed states across %d rows", changed, total)
        except UexApiError as exc:
            logger.warning("Terminal data-health snapshot failed: %s", exc)
        except Exception:
            logger.exception("Terminal data-health snapshot failed unexpectedly")

    @snapshot_data_health.before_loop
    async def before_data_health_snapshot(self) -> None:
        await self.bot.wait_until_ready()

    @tasks.loop(hours=FUEL_SNAPSHOT_HOURS)
    async def snapshot_fuel_prices(self) -> None:
        try:
            terminals = await self.bot.uex.get_terminals()
            terminal_ids = [
                int(row["id"])
                for row in terminals
                if row.get("id") is not None and _is_true(row.get("is_refuel"))
            ]
        except (UexApiError, TypeError, ValueError) as exc:
            logger.warning("Fuel snapshot could not determine refuel terminals: %s", exc)
            return

        fuel_rows: list[dict] = []
        for ids in _chunks(terminal_ids, FUEL_BATCH_SIZE):
            try:
                fuel_rows.extend(await self.bot.uex.get_fuel_prices(id_terminal=",".join(map(str, ids))))
            except UexApiError as exc:
                logger.info("Fuel snapshot skipped terminal batch %s: %s", ids, exc)
            await asyncio.sleep(FUEL_REQUEST_DELAY_SECONDS)

        changed, total = await self.bot.db.record_fuel_price_snapshot(fuel_rows)
        logger.info("Fuel-price snapshot: %d changed states across %d rows", changed, total)

    @snapshot_fuel_prices.before_loop
    async def before_fuel_snapshot(self) -> None:
        await self.bot.wait_until_ready()

    @tasks.loop(hours=REFERENCE_SNAPSHOT_HOURS)
    async def refresh_reference_data(self) -> None:
        try:
            terminals, commodities, refinery_yields = await asyncio.gather(
                self.bot.uex.get_terminals(),
                self.bot.uex.get_commodities(),
                self.bot.uex.get_refineries_yields(),
            )
            terminal_count = await self.bot.db.upsert_terminal_reference(terminals)
            commodity_count = await self.bot.db.upsert_commodity_reference(commodities)
            yield_count = await self.bot.db.record_refinery_yield_snapshot(refinery_yields)
            logger.info(
                "UEX reference refresh: %d terminals, %d commodities, %d refinery yields",
                terminal_count,
                commodity_count,
                yield_count,
            )
        except UexApiError as exc:
            logger.warning("UEX reference refresh failed: %s", exc)
        except Exception:
            logger.exception("UEX reference refresh failed unexpectedly")

    @refresh_reference_data.before_loop
    async def before_reference_refresh(self) -> None:
        await self.bot.wait_until_ready()


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Intelligence(bot))
