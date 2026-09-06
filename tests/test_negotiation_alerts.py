"""Negotiation-message DM alert tests: registration, DB dedup/upsert helpers, and a full
enable -> seed -> poll -> notify cycle against a faked UEX API."""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

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


class _FakeResponse:
    async def defer(self, **kwargs):
        pass

    async def send_message(self, *args, **kwargs):
        raise AssertionError("send_message should not be used once deferred")


class _FakeFollowup:
    def __init__(self):
        self.sent = []

    async def send(self, *args, **kwargs):
        self.sent.append((args, kwargs))


class _FakeInteraction:
    def __init__(self, user_id):
        self.user = type("U", (), {"id": user_id})()
        self.response = _FakeResponse()
        self.followup = _FakeFollowup()


async def _cog_with_client(tmp_path, handler):
    db = _make_db(tmp_path)
    await db.init()
    client = UexClient(app_token="test", base_url="https://uex.test")
    await client._client.aclose()
    client._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    bot = type("FakeBot", (), {})()
    bot.db = db
    bot.uex = client
    cog = NegotiationAlerts.__new__(NegotiationAlerts)
    cog.bot = bot
    return db, client, cog


def test_enabling_does_not_turn_on_when_the_baseline_fetch_fails(tmp_path):
    """The exact bug this guards: an invalid secret key must not leave the feature
    enabled with an empty baseline, or the first later poll with a valid key floods
    the user's entire negotiation history as if it were all brand new."""
    async def run():
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(400, json={"status": "user_not_found", "message": "user_not_found", "http_code": 400})

        db, client, cog = await _cog_with_client(tmp_path, handler)
        try:
            user_id = 42
            await db.set_user_secret_key(user_id, "sk_bad")
            interaction = _FakeInteraction(user_id)

            await cog.negotiation_alerts.callback(cog, interaction, True)

            assert await db.list_negotiation_alert_user_ids() == [], (
                "enabling must not stick when the baseline fetch fails"
            )
            (message,), _ = interaction.followup.sent[0]
            assert "couldn't enable" in message.lower()
        finally:
            await client.aclose()

    asyncio.run(run())


def test_enabling_turns_on_only_after_a_successful_baseline_seed(tmp_path):
    async def run():
        def handler(request: httpx.Request) -> httpx.Response:
            if "marketplace_negotiations_messages" in request.url.path:
                return httpx.Response(200, json={"status": "ok", "data": []})
            return httpx.Response(
                200,
                json={"status": "ok", "data": [{"id": 1, "date_modified": 1000, "listing_title": "x"}]},
            )

        db, client, cog = await _cog_with_client(tmp_path, handler)
        try:
            user_id = 43
            await db.set_user_secret_key(user_id, "sk_good")
            interaction = _FakeInteraction(user_id)

            await cog.negotiation_alerts.callback(cog, interaction, True)

            assert await db.list_negotiation_alert_user_ids() == [user_id]
            (message,), _ = interaction.followup.sent[0]
            assert "now **on**" in message
            assert "Checked 1 existing negotiation" in message
        finally:
            await client.aclose()

    asyncio.run(run())


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
        assert await db.is_negotiation_message_seen(1, 9001) is False
        await db.mark_negotiation_message_seen(1, 9001)
        assert await db.is_negotiation_message_seen(1, 9001) is True
        await db.mark_negotiation_message_seen(1, 9001)  # must not raise on a duplicate mark
        assert await db.is_negotiation_message_seen(1, 9001) is True

    asyncio.run(run())


def test_negotiation_message_seen_is_scoped_per_user(tmp_path):
    """A06: a bare message_id primary key made one user's seen-state (from their own
    baseline seed or delivered notification) silently suppress a DIFFERENT user's still-
    pending notification for the exact same message - e.g. two different Discord users each
    independently watching the same negotiation from opposite sides."""
    async def run():
        db = _make_db(tmp_path)
        await db.init()
        await db.mark_negotiation_message_seen(1, 9001)
        assert await db.is_negotiation_message_seen(1, 9001) is True
        assert await db.is_negotiation_message_seen(2, 9001) is False

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

        async def _fake_notify(target_user_id: int, message: str) -> bool:
            sent_dms.append((target_user_id, message))
            return True

        cog._notify_user = _fake_notify

        try:
            # Enabling seeds the two pre-existing messages as seen without notifying.
            await db.set_negotiation_alerts_enabled(user_id, True)
            seeded = await cog._seed_baseline(user_id, "sk_test")
            assert seeded == 1
            assert sent_dms == []
            assert await db.is_negotiation_message_seen(user_id, 1) is True
            assert await db.is_negotiation_message_seen(user_id, 2) is True

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


def test_new_message_notification_links_the_item_via_the_listing_lookup(tmp_path):
    """id_item isn't on the negotiation itself - the DM must resolve it from id_listing via
    get_marketplace_listings, exactly once (not once per message), and never let that lookup
    block the notification if it fails."""
    async def run():
        db = _make_db(tmp_path)
        await db.init()
        user_id = 777
        await db.set_user_secret_key(user_id, "sk_test")
        await db.set_negotiation_alerts_enabled(user_id, True)
        await db.set_negotiation_last_modified(user_id, 88, 1000)

        negotiation_row = {
            "id": 88, "id_listing": 5150, "listing_title": "Laranite - Q0",
            "is_listing_advertiser": 1, "advertiser_username": "me", "client_username": "buyer",
            "date_modified": 2000,
        }
        listing_lookups = []

        def handler(request: httpx.Request) -> httpx.Response:
            if "marketplace_negotiations_messages" in request.url.path:
                return httpx.Response(200, json={"status": "ok", "data": [
                    {"id": 1, "message": "1000 UEC?", "user_username": "buyer", "date_added": 50},
                    {"id": 2, "message": "1100 then", "user_username": "buyer", "date_added": 60},
                ]})
            if "marketplace_negotiations" in request.url.path:
                return httpx.Response(200, json={"status": "ok", "data": [negotiation_row]})
            if "marketplace_listings" in request.url.path:
                listing_lookups.append(dict(request.url.params))
                return httpx.Response(200, json={"status": "ok", "data": [{"id": 5150, "id_item": 55}]})
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

        async def _fake_notify(target_user_id: int, message: str) -> bool:
            sent_dms.append((target_user_id, message))
            return True

        cog._notify_user = _fake_notify

        try:
            await cog.poll_negotiation_messages()
            assert len(sent_dms) == 2, "both new messages from the other party should DM"
            for _, text in sent_dms:
                assert "[Laranite - Q0](https://uexcorp.space/marketplace/home/?id_item=55&mode=list)" in text
            # Two messages in the same negotiation/call must resolve id_item once, not twice.
            assert len(listing_lookups) == 1
            assert listing_lookups[0]["id"] == "5150"
        finally:
            await client.aclose()

    asyncio.run(run())


def test_a_failed_messages_fetch_does_not_advance_the_checkpoint_and_is_retried(tmp_path):
    """The exact bug this guards: if the per-negotiation messages fetch fails on one poll
    while date_modified has already advanced, the checkpoint must not advance to match it -
    otherwise the next poll sees date_modified <= checkpoint, skips the negotiation entirely,
    and whatever arrived during the failure is never checked again."""
    async def run():
        db = _make_db(tmp_path)
        await db.init()
        user_id = 900
        await db.set_user_secret_key(user_id, "sk_test")
        await db.set_negotiation_alerts_enabled(user_id, True)
        await db.set_negotiation_last_modified(user_id, 77, 1000)

        negotiation_row = {
            "id": 77, "listing_title": "Test listing", "is_listing_advertiser": 1,
            "advertiser_username": "me", "client_username": "buyer", "date_modified": 2000,
        }
        state = {"messages_should_fail": True}

        def handler(request: httpx.Request) -> httpx.Response:
            if "marketplace_negotiations_messages" in request.url.path:
                if state["messages_should_fail"]:
                    return httpx.Response(500, json={"status": "error", "message": "boom", "http_code": 500})
                return httpx.Response(200, json={"status": "ok", "data": [
                    {"id": 1, "message": "1000 UEC?", "user_username": "buyer", "date_added": 50},
                ]})
            if "marketplace_negotiations" in request.url.path:
                return httpx.Response(200, json={"status": "ok", "data": [negotiation_row]})
            raise AssertionError(f"unexpected request: {request.url}")

        client = UexClient(app_token="test", base_url="https://uex.test")
        await client._client.aclose()
        client._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))

        from types import SimpleNamespace as NS

        bot = type("FakeBot", (), {})()
        bot.db = db
        bot.uex = client
        # A working recipient - this test is only about the messages-fetch failure gating
        # the checkpoint, not delivery success/failure, so delivery must actually succeed
        # here or the checkpoint-advance assertion below would be exercising the wrong gate.
        bot.get_user = lambda _: NS(send=AsyncMock())
        cog = NegotiationAlerts.__new__(NegotiationAlerts)
        cog.bot = bot

        try:
            await cog.poll_negotiation_messages()
            assert (await db.get_negotiation_last_modified(user_id))[77] == 1000

            state["messages_should_fail"] = False
            await cog.poll_negotiation_messages()
            assert (await db.get_negotiation_last_modified(user_id))[77] == 2000
        finally:
            await client.aclose()

    asyncio.run(run())


def test_failed_dm_does_not_advance_the_poll_checkpoint(tmp_path):
    """Follow-up review finding: A07's fix made _notify_user's caller only mark a message
    seen on successful delivery, but _check_negotiation still returned True unconditionally
    - so poll_negotiation_messages advanced this negotiation's date_modified checkpoint
    regardless of whether delivery actually succeeded. Since the poller skips a negotiation
    whose date_modified hasn't advanced past the checkpoint, that meant a failed-delivery
    message was never retried on the NEXT poll either (the negotiation looks "already
    caught up" even though the one message that mattered was never delivered) - only some
    later, unrelated negotiation activity bumping date_modified further would surface it
    again. This exercises two real poll cycles (not _check_negotiation directly, which
    bypasses the checkpoint gate entirely) with the first delivery failing and the second
    succeeding."""
    async def run():
        db = _make_db(tmp_path)
        await db.init()
        user_id = 1
        await db.set_user_secret_key(user_id, "sk_test")
        await db.set_negotiation_alerts_enabled(user_id, True)
        negotiation = dict(
            id=7, id_listing=999, date_modified=100,
            is_listing_advertiser=1, advertiser_username="Alice", client_username="Bob",
        )
        uex = type("FakeUex", (), {})()
        uex.get_marketplace_negotiations = AsyncMock(return_value=[negotiation])
        uex.get_marketplace_negotiations_messages = AsyncMock(return_value=[
            dict(id=42, date_added=99, message="Interested", user_username="Bob")
        ])
        uex.get_marketplace_listings = AsyncMock(return_value=[])
        cog = NegotiationAlerts.__new__(NegotiationAlerts)
        cog.bot = type("FakeBot", (), {})()
        cog.bot.db = db
        cog.bot.uex = uex
        cog._notify_user = AsyncMock(side_effect=[False, True])

        await cog.poll_negotiation_messages.coro(cog)
        await cog.poll_negotiation_messages.coro(cog)

        assert cog._notify_user.await_count == 2, (
            cog._notify_user.await_count, await db.get_negotiation_last_modified(user_id)
        )
        assert await db.is_negotiation_message_seen(user_id, 42) is True

    asyncio.run(run())


def test_failed_dm_delivery_is_retried_not_permanently_discarded(tmp_path):
    """A07: _notify_user catches Discord HTTP failures (DMs closed, a transient outage),
    but the caller previously marked the message seen regardless of whether it actually
    reached the user - permanently discarding a notification the moment send() raised,
    with no way for it to ever go out. A failed delivery must leave the message unseen so
    the next poll cycle retries it."""
    async def run():
        db = _make_db(tmp_path)
        await db.init()
        negotiation = {
            "id": 7, "id_listing": 999, "date_modified": 100,
            "is_listing_advertiser": 1, "advertiser_username": "Alice", "client_username": "Bob",
        }
        message = {"id": 42, "date_added": 99, "message": "Interested", "user_username": "Bob"}
        from types import SimpleNamespace

        failure = discord.HTTPException(
            SimpleNamespace(status=503, reason="Service Unavailable"), {"message": "Temporary failure"}
        )

        class _FailThenSucceedUser:
            def __init__(self):
                self.send_calls = 0

            async def send(self, message):
                self.send_calls += 1
                if self.send_calls == 1:
                    raise failure

        user = _FailThenSucceedUser()
        uex = type("FakeUex", (), {})()

        async def get_messages(**kwargs):
            return [message]

        async def get_listings(**kwargs):
            return []

        uex.get_marketplace_negotiations_messages = get_messages
        uex.get_marketplace_listings = get_listings
        bot = type("FakeBot", (), {})()
        bot.db = db
        bot.uex = uex
        bot.get_user = lambda _: user
        cog = NegotiationAlerts.__new__(NegotiationAlerts)
        cog.bot = bot

        # First attempt: delivery fails - the message must stay unseen.
        await cog._check_negotiation(1, "fake", negotiation, 7)
        assert user.send_calls == 1
        assert await db.is_negotiation_message_seen(1, 42) is False

        # A later poll cycle retries the same still-unseen message and this time succeeds.
        await cog._check_negotiation(1, "fake", negotiation, 7)
        assert user.send_calls == 2
        assert await db.is_negotiation_message_seen(1, 42) is True

    asyncio.run(run())
