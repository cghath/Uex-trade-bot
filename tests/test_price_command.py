"""/price end to end: the buying-capacity figure added to "Best places to SELL" - how much
a terminal is currently buying from you (UEX's scu_sell field), not a fixed capacity - and
its always-present freshness label (bot/uex/data_health.py's freshness_label: a dot plus
the real elapsed-days figure, since two terminals both showing "fresh" can differ by
several days and the dot alone can't tell them apart)."""
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
        line = fields["Best places to SELL"]
        assert line.startswith("⚪ **Terminal A**"), f"expected the dot to lead the line: {line!r}"
        assert "buying 250 SCU" in line
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
        line = fields["Best places to SELL"]
        assert line.startswith("🟢 (0d) **Terminal A**"), (
            f"expected the dot plus the real elapsed-days figure to lead the line: {line!r}"
        )
        assert "buying 250 SCU" in line

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
        assert line.startswith("🔴 (14d) **Terminal A**"), f"expected the dot plus age to lead the line: {line!r}"
        assert "buying 250 SCU" in line
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
        assert line.startswith("⚪ **Terminal A**"), (
            f"expected a leading freshness dot even with no SCU figure to show: {line!r}"
        )

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
        line = fields["Best places to SELL"]
        assert "buying" not in line
        assert "holds" not in line

    asyncio.run(run())


def test_price_falls_back_to_on_hand_stock_when_no_buying_amount_is_reported(tmp_path):
    """UEX often reports a terminal's own current inventory (scu_sell_stock) even when it
    has no specific 'amount actually bought' figure (scu_sell == 0) - real live case: an
    'Out of Stock' terminal that clearly wants to buy, but nobody's logged a transaction
    size there recently. Shown as a distinctly-worded fallback, never as if it were the
    real buying figure - it's the opposite signal (more on-hand stock = closer to full)."""
    async def run():
        db = Database(tmp_path / "price_stock_fallback.sqlite3", Fernet(Fernet.generate_key()))
        await db.init()
        rows = [
            {"id_terminal": 1, "terminal_name": "Terminal A", "commodity_name": "Gold",
             "price_sell": 100.0, "scu_sell": 0, "scu_sell_stock": 505, "status_sell": 1},
        ]
        cog = _cog(db, price_rows=rows)
        interaction = _FakeInteraction()

        await cog.price.callback(cog, interaction, commodity="Gold")

        embed = interaction.followup.send.call_args.kwargs["embed"]
        fields = {f.name: f.value for f in embed.fields}
        line = fields["Best places to SELL"]
        assert "buying" not in line
        assert "holds ~505 SCU already" in line
        assert "on-hand stock" in embed.footer.text

    asyncio.run(run())


def test_price_prefers_the_real_buying_amount_over_the_stock_fallback(tmp_path):
    """When UEX DOES report a real scu_sell figure, the on-hand-stock fallback must not
    also be shown - the real buying amount always wins when both are present."""
    async def run():
        db = Database(tmp_path / "price_stock_not_shown.sqlite3", Fernet(Fernet.generate_key()))
        await db.init()
        rows = [
            {"id_terminal": 1, "terminal_name": "Terminal A", "commodity_name": "Gold",
             "price_sell": 100.0, "scu_sell": 1178, "scu_sell_stock": 505, "status_sell": 3},
        ]
        cog = _cog(db, price_rows=rows)
        interaction = _FakeInteraction()

        await cog.price.callback(cog, interaction, commodity="Gold")

        embed = interaction.followup.send.call_args.kwargs["embed"]
        fields = {f.name: f.value for f in embed.fields}
        line = fields["Best places to SELL"]
        assert "buying 1,178 SCU" in line
        assert "holds" not in line

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
        line = fields["Best places to BUY"]
        assert line.startswith("🟢 (0d) **Terminal B**"), f"expected the dot plus age to lead the line: {line!r}"

    asyncio.run(run())
