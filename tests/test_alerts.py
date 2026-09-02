"""Tests for the merged /alert-list and /alert-remove commands.

alerts.py, stock_alerts.py, and marketplace_alerts.py had no test coverage before this file -
these commands used to be split three ways (alert-*, stock-alert-*, marketplace-alert-*) and
were merged so /alert-list and /alert-remove cover all three alert types. The riskiest new
logic is /alert-remove's composite-id dispatch (price:<id> / stock:<id> / marketplace:<id>) -
each alert type has its own independently auto-incrementing id, so a routing bug there would
silently delete (or fail to delete) the wrong row instead of raising.
"""
from __future__ import annotations

import asyncio

from cryptography.fernet import Fernet

from bot.cogs.alerts import Alerts
from bot.db.database import Database


def _make_db(tmp_path) -> Database:
    return Database(tmp_path / "alerts.sqlite3", Fernet(Fernet.generate_key()))


class _FakeResponse:
    def __init__(self):
        self.messages = []

    async def send_message(self, *args, **kwargs):
        self.messages.append((args, kwargs))


class _FakeInteraction:
    def __init__(self, user_id):
        self.user = type("U", (), {"id": user_id})()
        self.response = _FakeResponse()


async def _seed_one_of_each(db: Database, user_id: int) -> None:
    await db.add_price_alert(
        guild_id=1, channel_id=2, user_id=user_id,
        commodity_name="Gold", direction="sell_at_least", target_price=10.0,
    )
    await db.add_stock_alert(
        guild_id=1, channel_id=2, user_id=user_id,
        commodity_name="Laranite", ship_query=None, scope="global",
    )
    await db.add_marketplace_alert(
        user_id=user_id, keyword="Cutlass Black", operation="sell",
        target_price=None, min_quality=None, max_quality=None,
    )


def test_alert_list_reports_no_active_alerts_when_all_three_types_are_empty(tmp_path):
    async def run():
        db = _make_db(tmp_path)
        await db.init()
        bot = type("FakeBot", (), {})()
        bot.db = db
        cog = Alerts.__new__(Alerts)
        cog.bot = bot
        interaction = _FakeInteraction(user_id=1)

        await cog.alert_list.callback(cog, interaction)

        (text,), kwargs = interaction.response.messages[0]
        assert text == "You have no active alerts."
        assert kwargs.get("ephemeral") is True

    asyncio.run(run())


def test_alert_list_shows_all_three_alert_types_grouped_into_sections(tmp_path):
    async def run():
        db = _make_db(tmp_path)
        await db.init()
        user_id = 42
        await _seed_one_of_each(db, user_id)
        bot = type("FakeBot", (), {})()
        bot.db = db
        cog = Alerts.__new__(Alerts)
        cog.bot = bot
        interaction = _FakeInteraction(user_id)

        await cog.alert_list.callback(cog, interaction)

        (text,), kwargs = interaction.response.messages[0]
        assert kwargs.get("ephemeral") is True
        assert "**Price alerts**" in text and "Gold" in text
        assert "**Stock (restock) alerts**" in text and "Laranite" in text
        assert "**Marketplace alerts**" in text and "Cutlass Black" in text

    asyncio.run(run())


def test_alert_remove_picker_includes_all_three_types_with_disambiguating_labels(tmp_path):
    async def run():
        db = _make_db(tmp_path)
        await db.init()
        user_id = 7
        await _seed_one_of_each(db, user_id)
        bot = type("FakeBot", (), {})()
        bot.db = db
        cog = Alerts.__new__(Alerts)
        cog.bot = bot
        interaction = _FakeInteraction(user_id)

        await cog.alert_remove.callback(cog, interaction)

        _, kwargs = interaction.response.messages[0]
        view = kwargs["view"]
        ids = {item["id"] for item in view.alerts}
        assert len(view.alerts) == 3
        assert any(i.startswith("price:") for i in ids)
        assert any(i.startswith("stock:") for i in ids)
        assert any(i.startswith("marketplace:") for i in ids)
        labels = {item["id"]: item["label"] for item in view.alerts}
        assert "(price)" in next(v for k, v in labels.items() if k.startswith("price:"))
        assert "(restock)" in next(v for k, v in labels.items() if k.startswith("stock:"))
        assert "(marketplace)" in next(v for k, v in labels.items() if k.startswith("marketplace:"))

    asyncio.run(run())


def test_alert_remove_dispatches_each_prefix_to_its_own_table_only(tmp_path):
    """The core regression guard: removing one alert type must not touch the other two,
    even though all three tables assign ids independently (all could be #1 at once)."""
    async def run():
        db = _make_db(tmp_path)
        await db.init()
        user_id = 99
        await _seed_one_of_each(db, user_id)
        bot = type("FakeBot", (), {})()
        bot.db = db
        cog = Alerts.__new__(Alerts)
        cog.bot = bot
        interaction = _FakeInteraction(user_id)

        await cog.alert_remove.callback(cog, interaction)
        _, kwargs = interaction.response.messages[0]
        view = kwargs["view"]
        composite_ids = {item["id"].split(":")[0]: item["id"] for item in view.alerts}

        picker_interaction = _FakeInteraction(user_id)
        confirmation = await view.remove_callback(picker_interaction, composite_ids["price"])
        assert confirmation == f"Price alert #{composite_ids['price'].split(':')[1]} removed."

        # Only the price alert should be gone; stock and marketplace must be untouched.
        assert await db.list_user_alerts(user_id) == []
        assert len(await db.list_user_stock_alerts(user_id)) == 1
        assert len(await db.list_user_marketplace_alerts(user_id)) == 1

        confirmation = await view.remove_callback(picker_interaction, composite_ids["stock"])
        assert confirmation == f"Stock alert #{composite_ids['stock'].split(':')[1]} removed."
        assert await db.list_user_stock_alerts(user_id) == []
        assert len(await db.list_user_marketplace_alerts(user_id)) == 1

        confirmation = await view.remove_callback(picker_interaction, composite_ids["marketplace"])
        assert confirmation == f"Marketplace alert #{composite_ids['marketplace'].split(':')[1]} removed."
        assert await db.list_user_marketplace_alerts(user_id) == []

    asyncio.run(run())


def test_alert_list_truncates_instead_of_exceeding_discords_message_limit(tmp_path):
    """Discord rejects a plain message content over 2000 chars outright - a user with enough
    alerts across all three types must get a truncated, still-sendable list, not an
    uncaught discord.HTTPException."""
    async def run():
        db = _make_db(tmp_path)
        await db.init()
        user_id = 314
        for i in range(120):
            await db.add_price_alert(
                guild_id=1, channel_id=2, user_id=user_id,
                commodity_name=f"Commodity{i}", direction="sell_at_least", target_price=10.0,
            )
        bot = type("FakeBot", (), {})()
        bot.db = db
        cog = Alerts.__new__(Alerts)
        cog.bot = bot
        interaction = _FakeInteraction(user_id)

        await cog.alert_list.callback(cog, interaction)

        (text,), _ = interaction.response.messages[0]
        assert len(text) <= 2000
        assert "truncated" in text
        assert "120 alerts total" in text

    asyncio.run(run())


def test_alert_remove_reports_already_removed_for_a_stale_id(tmp_path):
    async def run():
        db = _make_db(tmp_path)
        await db.init()
        user_id = 5
        await _seed_one_of_each(db, user_id)
        bot = type("FakeBot", (), {})()
        bot.db = db
        cog = Alerts.__new__(Alerts)
        cog.bot = bot
        interaction = _FakeInteraction(user_id)

        await cog.alert_remove.callback(cog, interaction)
        _, kwargs = interaction.response.messages[0]
        view = kwargs["view"]
        price_id = next(item["id"] for item in view.alerts if item["id"].startswith("price:"))

        picker_interaction = _FakeInteraction(user_id)
        await view.remove_callback(picker_interaction, price_id)
        confirmation = await view.remove_callback(picker_interaction, price_id)
        assert "already removed" in confirmation

    asyncio.run(run())
