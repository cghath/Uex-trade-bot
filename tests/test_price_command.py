"""/price end to end: the buying-capacity figure added to "Best places to SELL" - how much
a terminal is currently buying from you (UEX's scu_sell field), not a fixed capacity - and
its always-present freshness dot (bot/uex/data_health.py's freshness_emoji)."""
from __future__ import annotations

import asyncio
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock

from cryptography.fernet import Fernet

from bot.cogs.prices import Prices
from bot.db.database import Database
from bot.uex.data_health import FRESHNESS_LEGEND
from bot.uex.supply_demand import SELL_SIDE_STATUS_CLARIFIER


class _FakeInteraction:
    def __init__(self) -> None:
        self.response = NS(defer=AsyncMock())
        self.followup = NS(send=AsyncMock())


def _cog(db, *, price_rows):
    cog = Prices.__new__(Prices)
    cog.bot = NS(
        db=db,
        uex=NS(
            get_commodities_prices=AsyncMock(return_value=price_rows),
            get_commodities_status=AsyncMock(return_value={"buy": [], "sell": []}),
        ),
    )
    return cog


def test_price_shows_buying_capacity_next_to_each_sell_location(tmp_path):
    """No data-health row exists for this terminal at all, so its freshness dot must be
    the "unknown" one (⚪), not silently omitted or defaulted to looking fresh."""
    async def run():
        db = Database(tmp_path / "price.sqlite3", Fernet(Fernet.generate_key()))
        await db.init()
        rows = [
            {"id_terminal": 1, "terminal_name": "Terminal A", "commodity_name": "Gold",
             "price_sell": 100.0, "scu_sell": 250, "status_sell": 1},
        ]
        cog = _cog(db, price_rows=rows)
        interaction = _FakeInteraction()

        await cog.price.callback(cog, interaction, commodity="Gold")

        embed = interaction.followup.send.call_args.kwargs["embed"]
        fields = {f.name: f.value for f in embed.fields}
        assert "buying 250 SCU ⚪" in fields["Best places to SELL"]
        assert FRESHNESS_LEGEND in embed.footer.text
        assert SELL_SIDE_STATUS_CLARIFIER in embed.footer.text

    asyncio.run(run())


def test_price_shows_a_green_dot_for_confirmed_fresh_data(tmp_path):
    async def run():
        db = Database(tmp_path / "price_fresh.sqlite3", Fernet(Fernet.generate_key()))
        await db.init()
        await db.record_terminal_data_health_snapshot([
            {"id_terminal": 1, "type": "commodity", "terminal_name": "Terminal A",
             "prices_total": 10, "prices_updated": 10, "prices_updated_percentage": 100,
             "last_update_days": 0, "last_update_days_limit": 10, "last_update_days_percentage": 90},
        ])
        rows = [
            {"id_terminal": 1, "terminal_name": "Terminal A", "commodity_name": "Gold",
             "price_sell": 100.0, "scu_sell": 250, "status_sell": 1},
        ]
        cog = _cog(db, price_rows=rows)
        interaction = _FakeInteraction()

        await cog.price.callback(cog, interaction, commodity="Gold")

        embed = interaction.followup.send.call_args.kwargs["embed"]
        fields = {f.name: f.value for f in embed.fields}
        assert "buying 250 SCU 🟢" in fields["Best places to SELL"]

    asyncio.run(run())


def test_price_shows_a_red_dot_and_no_duplicate_warning_for_confirmed_stale_data(tmp_path):
    """The freshness dot alone carries the age signal for a displayed SCU figure - the
    old ⚠️-prefixed warning note this command used to append is not shown alongside it,
    which would otherwise repeat the same information twice on one line."""
    async def run():
        db = Database(tmp_path / "price_stale.sqlite3", Fernet(Fernet.generate_key()))
        await db.init()
        await db.record_terminal_data_health_snapshot([
            {"id_terminal": 1, "type": "commodity", "terminal_name": "Terminal A",
             "prices_total": 10, "prices_updated": 10, "prices_updated_percentage": 100,
             "last_update_days": 14, "last_update_days_limit": 10, "last_update_days_percentage": 0},
        ])
        rows = [
            {"id_terminal": 1, "terminal_name": "Terminal A", "commodity_name": "Gold",
             "price_sell": 100.0, "scu_sell": 250, "status_sell": 1},
        ]
        cog = _cog(db, price_rows=rows)
        interaction = _FakeInteraction()

        await cog.price.callback(cog, interaction, commodity="Gold")

        embed = interaction.followup.send.call_args.kwargs["embed"]
        fields = {f.name: f.value for f in embed.fields}
        line = fields["Best places to SELL"]
        assert "buying 250 SCU 🔴" in line
        assert "⚠️" not in line, f"expected the dot alone, no duplicate warning text: {line!r}"

    asyncio.run(run())


def test_price_hides_buying_capacity_when_status_confirms_no_demand(tmp_path):
    """Regression guard: showing a stale positive scu_sell next to a status label that
    already says 'Maximum Inventory (No Demand)' would directly contradict itself - the
    same buy/sell status inversion this codebase has hit before. The freshness dot still
    shows even though the SCU figure itself is suppressed - it describes this terminal's
    data in general, not specifically the (now-hidden) capacity number."""
    async def run():
        db = Database(tmp_path / "price_no_demand.sqlite3", Fernet(Fernet.generate_key()))
        await db.init()
        rows = [
            {"id_terminal": 1, "terminal_name": "Terminal A", "commodity_name": "Gold",
             "price_sell": 100.0, "scu_sell": 250, "status_sell": 7},
        ]
        cog = _cog(db, price_rows=rows)
        interaction = _FakeInteraction()

        await cog.price.callback(cog, interaction, commodity="Gold")

        embed = interaction.followup.send.call_args.kwargs["embed"]
        fields = {f.name: f.value for f in embed.fields}
        line = fields["Best places to SELL"]
        assert "buying" not in line
        assert "⚪" in line, f"expected a freshness dot even with no SCU figure to show: {line!r}"

    asyncio.run(run())


def test_price_omits_buying_capacity_when_scu_sell_is_not_reported(tmp_path):
    async def run():
        db = Database(tmp_path / "price_no_scu.sqlite3", Fernet(Fernet.generate_key()))
        await db.init()
        rows = [
            {"id_terminal": 1, "terminal_name": "Terminal A", "commodity_name": "Gold",
             "price_sell": 100.0},
        ]
        cog = _cog(db, price_rows=rows)
        interaction = _FakeInteraction()

        await cog.price.callback(cog, interaction, commodity="Gold")

        embed = interaction.followup.send.call_args.kwargs["embed"]
        fields = {f.name: f.value for f in embed.fields}
        assert "buying" not in fields["Best places to SELL"]

    asyncio.run(run())


def test_price_shows_a_freshness_dot_on_buy_section_entries_too(tmp_path):
    """Regression: a user-reported screenshot showed dots appearing only on the one sell
    row that happened to have a live SCU figure - every other row, on both sides, had none
    at all. Every listed terminal in both "Best places to SELL" and "Best places to BUY"
    must carry its own freshness dot, independent of whether a capacity figure is shown."""
    async def run():
        db = Database(tmp_path / "price_buy_dot.sqlite3", Fernet(Fernet.generate_key()))
        await db.init()
        await db.record_terminal_data_health_snapshot([
            {"id_terminal": 2, "type": "commodity", "terminal_name": "Terminal B",
             "prices_total": 10, "prices_updated": 10, "prices_updated_percentage": 100,
             "last_update_days": 0, "last_update_days_limit": 10, "last_update_days_percentage": 90},
        ])
        rows = [
            {"id_terminal": 2, "terminal_name": "Terminal B", "commodity_name": "Gold",
             "price_buy": 50.0},
        ]
        cog = _cog(db, price_rows=rows)
        interaction = _FakeInteraction()

        await cog.price.callback(cog, interaction, commodity="Gold")

        embed = interaction.followup.send.call_args.kwargs["embed"]
        fields = {f.name: f.value for f in embed.fields}
        assert "🟢" in fields["Best places to BUY"]

    asyncio.run(run())
