"""Marketplace cog tests: the delete-listing local-state ordering guard, and
/my-negotiations' item-link resolution via a per-row listing lookup."""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone

from cryptography.fernet import Fernet
import httpx

from bot.cogs.marketplace import Marketplace
from bot.db.database import Database
from bot.uex.client import UexClient


def _make_db(tmp_path) -> Database:
    return Database(tmp_path / "marketplace.sqlite3", Fernet(Fernet.generate_key()))


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

            (message,), _ = interaction.followup.sent[0]
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
