"""Negotiation-message DM alert tests: registration, DB dedup/upsert helpers, and a full
enable -> seed -> poll -> notify cycle against a faked UEX API."""
from __future__ import annotations

import asyncio

from cryptography.fernet import Fernet
import discord
from discord.ext import commands
import httpx

from bot.cogs.negotiation_alerts import NegotiationAlerts
from bot.db.database import Database
from bot.main import INITIAL_COGS
from bot.uex.client import UexClient


def _make_db(tmp_path) -> Database:
    return Database(tmp_path / "negotiation_alerts.sqlite3", Fernet(Fernet.generate_key()))


def test_negotiation_alerts_cog_is_registered_and_exposes_its_command():
    async def run():
        bot = commands.Bot(command_prefix="!unused", intents=discord.Intents.none())
        try:
            await bot.add_cog(NegotiationAlerts(bot))
            assert {command.name for command in bot.tree.get_commands()} == {"negotiation-alerts"}
        finally:
            await bot.remove_cog("NegotiationAlerts")
            await bot.close()

    assert "bot.cogs.negotiation_alerts" in INITIAL_COGS
    asyncio.run(run())


def test_negotiation_alert_settings_toggle(tmp_path):
    async def run():
        db = _make_db(tmp_path)
        await db.init()
        await db.set_negotiation_alerts_enabled(111, True)
        await db.set_negotiation_alerts_enabled(222, True)
        assert set(await db.list_negotiation_alert_user_ids()) == {111, 222}

        await db.set_negotiation_alerts_enabled(111, False)
        assert await db.list_negotiation_alert_user_ids() == [222]

        # Re-enabling after disabling must not fail on the earlier PRIMARY KEY row.
        await db.set_negotiation_alerts_enabled(111, True)
        assert set(await db.list_negotiation_alert_user_ids()) == {111, 222}

    asyncio.run(run())


def test_negotiation_last_modified_is_upserted_per_user_and_negotiation(tmp_path):
    async def run():
        db = _make_db(tmp_path)
        await db.init()
        await db.set_negotiation_last_modified(1, 500, 1000)
        await db.set_negotiation_last_modified(1, 501, 2000)
        assert await db.get_negotiation_last_modified(1) == {500: 1000, 501: 2000}

        await db.set_negotiation_last_modified(1, 500, 1500)
        assert await db.get_negotiation_last_modified(1) == {500: 1500, 501: 2000}
        # A different user's high-water marks must stay independent.
        assert await db.get_negotiation_last_modified(2) == {}

    asyncio.run(run())


def test_negotiation_message_seen_is_deduplicated(tmp_path):
    async def run():
        db = _make_db(tmp_path)
        await db.init()
        assert await db.is_negotiation_message_seen(9001) is False
        await db.mark_negotiation_message_seen(9001)
        assert await db.is_negotiation_message_seen(9001) is True
        await db.mark_negotiation_message_seen(9001)  # must not raise on a duplicate mark
        assert await db.is_negotiation_message_seen(9001) is True

    asyncio.run(run())


def test_enable_seeds_baseline_then_only_new_messages_from_the_other_party_notify(tmp_path):
    async def run():
        db = _make_db(tmp_path)
        await db.init()
        user_id = 555
        await db.set_user_secret_key(user_id, "sk_test")

        negotiation_row = {
            "id": 42,
            "listing_title": "Laranite - Q0 - 100 available",
            "is_listing_advertiser": 1,
            "advertiser_username": "me_the_seller",
            "client_username": "some_buyer",
            "date_modified": 1000,
        }
        # Messages served for negotiation 42 change between the seed call and the poll
        # call, simulating a genuinely new message arriving afterward.
        message_sets = [
            [  # at enable time: one old message from each side, nothing new yet
                {"id": 1, "message": "Interested in 50 units?", "user_username": "some_buyer", "date_added": 100},
                {"id": 2, "message": "Sure, what price?", "user_username": "me_the_seller", "date_added": 200},
            ],
            [  # by the time the poll runs: the same two, plus a genuinely new one
                {"id": 1, "message": "Interested in 50 units?", "user_username": "some_buyer", "date_added": 100},
                {"id": 2, "message": "Sure, what price?", "user_username": "me_the_seller", "date_added": 200},
                {"id": 3, "message": "1200 UEC each works", "user_username": "some_buyer", "date_added": 300},
            ],
        ]
        calls = {"negotiations": 0, "messages": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            if "marketplace_negotiations_messages" in request.url.path:
                index = min(calls["messages"], len(message_sets) - 1)
                calls["messages"] += 1
                return httpx.Response(200, json={"status": "ok", "data": message_sets[index]})
            if "marketplace_negotiations" in request.url.path:
                calls["negotiations"] += 1
                row = dict(negotiation_row)
                row["date_modified"] = 1000 if calls["negotiations"] == 1 else 2000
                return httpx.Response(200, json={"status": "ok", "data": [row]})
            raise AssertionError(f"unexpected request: {request.url}")

        client = UexClient(app_token="test", base_url="https://uex.test")
        await client._client.aclose()
        client._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))

        bot = type("FakeBot", (), {})()
        bot.db = db
        bot.uex = client
        sent_dms: list[tuple[int, str]] = []

        cog = NegotiationAlerts.__new__(NegotiationAlerts)
        cog.bot = bot

        async def _fake_notify(target_user_id: int, message: str) -> None:
            sent_dms.append((target_user_id, message))

        cog._notify_user = _fake_notify

        try:
            # Enabling seeds the two pre-existing messages as seen without notifying.
            await db.set_negotiation_alerts_enabled(user_id, True)
            seeded = await cog._seed_baseline(user_id, "sk_test")
            assert seeded == 1
            assert sent_dms == []
            assert await db.is_negotiation_message_seen(1) is True
            assert await db.is_negotiation_message_seen(2) is True

            # A poll cycle later, UEX reports date_modified advanced and a third message
            # exists. Only that new, other-party message should trigger a DM.
            await cog.poll_negotiation_messages()
            assert len(sent_dms) == 1
            notified_user, text = sent_dms[0]
            assert notified_user == user_id
            assert "1200 UEC each" in text
            assert "some_buyer" in text

            # A second poll with no further change must not re-notify.
            sent_dms.clear()
            await cog.poll_negotiation_messages()
            assert sent_dms == []
        finally:
            await client.aclose()

    asyncio.run(run())
