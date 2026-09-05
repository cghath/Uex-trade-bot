"""Marketplace cog tests: the delete-listing local-state ordering guard, and
/my-negotiations' item-link resolution via a per-row listing lookup."""
from __future__ import annotations

import asyncio
import sqlite3
from datetime import datetime, timezone

from cryptography.fernet import Fernet
import httpx

from bot.cogs.marketplace import ConfirmListingView, Marketplace
from bot.db.database import Database
from bot.uex.client import UexClient
from unittest.mock import AsyncMock


def _make_db(tmp_path) -> Database:
    return Database(tmp_path / "marketplace.sqlite3", Fernet(Fernet.generate_key()))


async def _yield_once(**kwargs):
    # A real interaction.response.edit_message() round-trips to Discord - forcing an actual
    # scheduler checkpoint here reproduces two concurrent callbacks genuinely being in
    # flight together, rather than one running to completion before the other even starts.
    await asyncio.sleep(0)


class _FakeResponse:
    async def defer(self, **kwargs):
        pass

    async def edit_message(self, **kwargs):
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


def test_my_negotiations_links_item_via_listing_lookup(tmp_path):
    """/marketplace_negotiations doesn't return id_item - the command must resolve it per
    row via get_marketplace_listings(id=id_listing) and link the item name, falling back
    to a plain name for a negotiation whose listing lookup comes back empty."""
    async def run():
        db = _make_db(tmp_path)
        await db.init()
        user_id = 654
        await db.set_user_secret_key(user_id, "sk_test")

        def handler(request: httpx.Request) -> httpx.Response:
            if "marketplace_negotiations_messages" in request.url.path:
                raise AssertionError("should not fetch messages for /my-negotiations")
            if "marketplace_negotiations" in request.url.path:
                return httpx.Response(200, json={"status": "ok", "data": [
                    {"id": 1, "id_listing": 10, "listing_title": "Laranite", "is_listing_advertiser": 1,
                     "price": "1500", "currency": "UEC", "date_closed": None},
                    {"id": 2, "id_listing": 20, "listing_title": "Unresolvable Item", "is_listing_advertiser": 0,
                     "price": "500", "currency": "UEC", "date_closed": 1700000000},
                ]})
            if "marketplace_listings" in request.url.path:
                params = dict(request.url.params)
                if params.get("id") == "10":
                    return httpx.Response(200, json={"status": "ok", "data": [{"id": 10, "id_item": 55}]})
                return httpx.Response(200, json={"status": "ok", "data": []})
            raise AssertionError(f"unexpected request: {request.url}")

        client = UexClient(app_token="test", base_url="https://uex.test")
        await client._client.aclose()
        client._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))

        bot = type("FakeBot", (), {})()
        bot.db = db
        bot.uex = client
        cog = Marketplace.__new__(Marketplace)
        cog.bot = bot
        interaction = _FakeInteraction(user_id)

        try:
            await cog.my_negotiations.callback(cog, interaction)

            (message,), _ = interaction.followup.sent[0]
            assert "[Laranite](https://uexcorp.space/marketplace/home/?id_item=55&mode=list)" in message
            assert "Unresolvable Item" in message
            assert "[Unresolvable Item]" not in message
        finally:
            await client.aclose()

    asyncio.run(run())


def test_delete_listing_shows_a_confirmation_preview_before_deleting_anything(tmp_path):
    """/marketplace-delete-listing must never delete on a single command - it shows a
    preview (title/price) and a Confirm/Cancel view first, since this is a real, public,
    unrecoverable UEX listing and a mistyped listing_id has no undo."""
    async def run():
        db = _make_db(tmp_path)
        await db.init()
        user_id = 654
        await db.set_user_secret_key(user_id, "sk_test")

        def handler(request: httpx.Request) -> httpx.Response:
            if request.method == "GET" and "marketplace_listings" in request.url.path:
                return httpx.Response(200, json={"status": "ok", "data": [
                    {"id": 999, "title": "Laranite", "price": "150", "currency": "UEC", "unit": "unit"},
                ]})
            raise AssertionError(f"unexpected request: {request.method} {request.url}")

        client = UexClient(app_token="test", base_url="https://uex.test")
        await client._client.aclose()
        client._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))

        bot = type("FakeBot", (), {})()
        bot.db = db
        bot.uex = client
        cog = Marketplace.__new__(Marketplace)
        cog.bot = bot
        interaction = _FakeInteraction(user_id)

        try:
            await cog.marketplace_delete_listing.callback(cog, interaction, 999)

            assert len(interaction.followup.sent) == 1
            _, kwargs = interaction.followup.sent[0]
            assert "Delete this listing?" in kwargs["embed"].title
            assert "Laranite" in kwargs["embed"].description
            assert kwargs["view"] is not None  # Confirm/Cancel - nothing deleted yet
        finally:
            await client.aclose()

    asyncio.run(run())


def test_delete_listing_cancel_does_not_call_delete(tmp_path):
    """Clicking Cancel on the confirmation view must never reach the UEX DELETE call."""
    async def run():
        db = _make_db(tmp_path)
        await db.init()
        user_id = 987
        await db.set_user_secret_key(user_id, "sk_test")

        def handler(request: httpx.Request) -> httpx.Response:
            if request.method == "GET" and "marketplace_listings" in request.url.path:
                return httpx.Response(200, json={"status": "ok", "data": [
                    {"id": 999, "title": "Laranite", "price": "150", "currency": "UEC", "unit": "unit"},
                ]})
            raise AssertionError(f"unexpected request: {request.method} {request.url} - Cancel must not delete")

        client = UexClient(app_token="test", base_url="https://uex.test")
        await client._client.aclose()
        client._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))

        bot = type("FakeBot", (), {})()
        bot.db = db
        bot.uex = client
        cog = Marketplace.__new__(Marketplace)
        cog.bot = bot
        interaction = _FakeInteraction(user_id)

        try:
            await cog.marketplace_delete_listing.callback(cog, interaction, 999)

            _, kwargs = interaction.followup.sent[0]
            view = kwargs["view"]
            cancel_interaction = _FakeInteraction(user_id)
            await view.cancel.callback(cancel_interaction)

            (message,), _ = cancel_interaction.followup.sent[0]
            assert "cancelled" in message.lower()
        finally:
            await client.aclose()

    asyncio.run(run())


def test_delete_listing_does_not_record_local_stock_when_uex_delete_fails(tmp_path):
    """The exact bug this guards: if UEX rejects the delete, local inventory state must be
    untouched - not partially updated based on a deletion that never actually happened."""
    async def run():
        db = _make_db(tmp_path)
        await db.init()
        user_id = 321
        await db.set_user_secret_key(user_id, "sk_test")
        inventory_id = await db.add_inventory_item(
            user_id=user_id, id_item=1, id_category=2, item_name="Test Item",
            item_slug=None, quantity=5, quality=0, location="Area18", minimum_price=100,
        )
        job_id = (await db.create_inventory_post_jobs(
            user_id,
            [{"inventory_id": inventory_id, "quantity": 5, "scheduled_for": datetime.now(timezone.utc)}],
        ))[0]
        assert await db.claim_inventory_post_job(job_id)
        await db.mark_inventory_post_listed(
            job_id, listing_id=999, listing_url=None, posted_price=150, date_expiration=None
        )
        before = await db.get_inventory_item(user_id, inventory_id)

        def handler(request: httpx.Request) -> httpx.Response:
            if request.method == "GET" and "marketplace_listings" in request.url.path:
                return httpx.Response(200, json={"status": "ok", "data": [
                    {"id": 999, "in_stock": 3, "is_sold_out": False},
                ]})
            if request.method == "DELETE":
                return httpx.Response(400, json={"status": "error", "message": "not_yours", "http_code": 400})
            raise AssertionError(f"unexpected request: {request.method} {request.url}")

        client = UexClient(app_token="test", base_url="https://uex.test")
        await client._client.aclose()
        client._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))

        bot = type("FakeBot", (), {})()
        bot.db = db
        bot.uex = client
        cog = Marketplace.__new__(Marketplace)
        cog.bot = bot
        interaction = _FakeInteraction(user_id)

        try:
            await cog.marketplace_delete_listing.callback(cog, interaction, 999)

            # The command only shows the preview - simulate the user clicking "Delete listing".
            _, kwargs = interaction.followup.sent[0]
            view = kwargs["view"]
            confirm_interaction = _FakeInteraction(user_id)
            await view.confirm.callback(confirm_interaction)

            (message,), _ = confirm_interaction.followup.sent[0]
            assert "couldn't delete" in message.lower()

            after = await db.get_inventory_item(user_id, inventory_id)
            assert after["quantity"] == before["quantity"]
            assert after["reserved_quantity"] == before["reserved_quantity"]
            job = await db.get_inventory_post_job(user_id, job_id)
            # Unchanged from mark_inventory_post_listed's own value - never overwritten with
            # the mocked in_stock=3 the UEX pre-delete check saw, since the delete failed.
            assert job["last_known_stock"] == 5
            assert job["status"] == "listed"
        finally:
            await client.aclose()

    asyncio.run(run())


def test_confirm_listing_view_only_posts_once_on_concurrent_double_click():
    """Real defect (audit finding A02): ConfirmListingView.confirm set self.resolved = True
    but never checked it first - two already-dispatched callbacks (a double-click, or a
    redelivered interaction) could both get past that line before either's edit_message
    round-trip disabled the button on Discord's side, so both reached the real POST. The
    fix must check-then-set: asyncio is single-threaded and nothing awaits between the
    check and the set, so the second callback to run always observes the first one's write
    and never gets past it."""
    from types import SimpleNamespace as NS

    def interaction():
        return NS(
            user=NS(id=1),
            response=NS(send_message=AsyncMock(), edit_message=AsyncMock(side_effect=_yield_once)),
            followup=NS(send=AsyncMock()),
        )

    async def run():
        uex = NS(post_marketplace_advertise=AsyncMock(return_value={"id_listing": 999}))
        bot = NS(uex=uex)
        view = ConfirmListingView(bot, "fake-secret", {"title": "Test"}, 1)

        first, second = interaction(), interaction()
        await asyncio.gather(view.confirm.callback(first), view.confirm.callback(second))

        assert uex.post_marketplace_advertise.await_count == 1, uex.post_marketplace_advertise.await_count
        assert second.response.send_message.await_count == 1
        (message,), _ = second.response.send_message.call_args
        assert "already resolved" in message.lower()

    asyncio.run(run())


def test_transient_database_error_does_not_kill_marketplace_collector():
    """A12: snapshot_item_activity's DB write sat outside its own try/except - an
    sqlite3.OperationalError (e.g. "database is locked" from another collector writing at
    the same moment) propagated out of the @tasks.loop coroutine uncaught. tasks.loop's own
    auto-reconnect only covers a specific set of network exceptions, not arbitrary ones, so
    this permanently stopped the whole hourly collector until the bot was restarted."""
    async def run():
        db = type("FakeDb", (), {})()
        db.upsert_marketplace_item_activity = AsyncMock(
            side_effect=sqlite3.OperationalError("database is locked")
        )
        db.update_liquidity_scores = AsyncMock(return_value=0)
        db.upsert_marketplace_tier_stats = AsyncMock()
        db.count_marketplace_item_activity = AsyncMock(return_value=0)
        db.count_marketplace_tier_stats = AsyncMock(return_value=(0, 0))
        uex = type("FakeUex", (), {})()
        uex.get_marketplace_trends = AsyncMock(
            return_value=[{"id_item": 1, "item_name": "Ore", "negotiations_count": 1}]
        )
        uex.get_marketplace_prices_averages_all = AsyncMock(return_value=[])
        bot = type("FakeBot", (), {})()
        bot.db = db
        bot.uex = uex
        bot.wait_until_ready = AsyncMock()
        instance = Marketplace.__new__(Marketplace)
        instance.bot = bot

        task = instance.snapshot_item_activity.start()
        try:
            await asyncio.wait_for(asyncio.shield(task), timeout=2)
        except (sqlite3.OperationalError, asyncio.TimeoutError):
            pass
        finally:
            instance.snapshot_item_activity.cancel()

        assert not instance.snapshot_item_activity.failed(), "collector task terminated after one database error"
        db.upsert_marketplace_item_activity.assert_awaited_once()

    asyncio.run(run())
