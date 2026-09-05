"""Regression tests for three audit findings (A01, A03, A04) in personal_inventory.py's
guarded automatic UEX posting - A03/A04 both involve state read once at the top of an
async flow going stale by the time a write actually happens, several awaits later; A01 is
the shared client's write-status handling, exercised here through the real cancellation
path that depends on it."""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock

import httpx
from cryptography.fernet import Fernet

from bot.cogs.personal_inventory import PersonalInventory
from bot.db.database import Database
from bot.uex.client import UexClient
from bot.uex.inventory import PriceRecommendation


def _make_db(tmp_path, name: str) -> Database:
    return Database(tmp_path / name, Fernet(Fernet.generate_key()))


def _cog(db, uex) -> PersonalInventory:
    instance = PersonalInventory.__new__(PersonalInventory)
    instance.bot = NS(db=db, uex=uex)
    instance._notify_user = AsyncMock()
    return instance


def _interaction(user_id: int = 1):
    return NS(
        user=NS(id=user_id),
        response=NS(send_message=AsyncMock(), defer=AsyncMock(), edit_message=AsyncMock()),
        followup=NS(send=AsyncMock()),
    )


async def _fixture(tmp_path, name: str, *, custom: bool = False):
    db = _make_db(tmp_path, name)
    await db.init()
    await db.set_user_secret_key(1, "fake-test-key")
    inv = await db.add_inventory_item(
        user_id=1, id_item=1, id_category=36, item_name="Audit item", item_slug=None,
        quantity=5, quality=0, location="Test location", minimum_price=100,
    )
    spec = dict(inventory_id=inv, quantity=5, scheduled_for=datetime.now(timezone.utc))
    if custom:
        spec.update(pricing_strategy="custom", custom_price=200)
    (job_id,) = await db.create_inventory_post_jobs(1, [spec])
    return db, inv, job_id


def test_rejected_delete_must_not_release_inventory(tmp_path):
    """A01: DELETE /marketplace_listings rejecting with status=user_not_verified (a real
    documented status, not in _AUTH_ERROR_STATUSES and not literally "error") used to fall
    through client.py's generic non-write status handling and return normally, so
    _cancel_listed_job reported a successful deletion and released the reservation even
    though the public listing was never actually deleted."""
    async def run():
        db, inv, job_id = await _fixture(tmp_path, "rejected_delete.sqlite3")
        await db.claim_inventory_post_job(job_id)
        await db.mark_inventory_post_listed(
            job_id, listing_id=999, listing_url=None, posted_price=200, date_expiration=None
        )

        def handler(request: httpx.Request) -> httpx.Response:
            if request.method == "DELETE":
                return httpx.Response(
                    403, json={"status": "user_not_verified", "message": "User account not verified", "data": None}
                )
            return httpx.Response(200, json={"status": "ok", "data": [{"id": 999, "in_stock": 5, "is_sold_out": 0}]})

        uex = UexClient("fake", base_url="https://audit.invalid")
        await uex._client.aclose()
        uex._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        try:
            job = await db.get_inventory_post_job(1, job_id)
            ok, message = await _cog(db, uex)._cancel_listed_job(job, secret_key="fake")
            entry = await db.get_inventory_item(1, inv)
            assert not ok, message
            assert entry["reserved_quantity"] == 5, entry["reserved_quantity"]
        finally:
            await uex.aclose()

    asyncio.run(run())


def test_floor_increase_during_pricing_is_respected(tmp_path):
    """A04: _post_one_job used the job row's own minimum_price - a snapshot taken before
    claim_inventory_post_job() marks it 'posting', at which point set_inventory_minimum_price
    stops updating it. Raising the floor while the pricing fetch is in flight must still be
    honored by the actual POST, not silently ignored."""
    async def run():
        db, inv, job_id = await _fixture(tmp_path, "floor.sqlite3")
        await db.claim_inventory_post_job(job_id)
        uex = NS(post_marketplace_advertise=AsyncMock(return_value={"id_listing": 999}))
        cog = _cog(db, uex)

        async def price(**kwargs):
            # Simulates the floor being raised by /inventory-set-minimum while this fetch's
            # network round trips are still in flight.
            await db.set_inventory_minimum_price(1, inv, 500)
            return PriceRecommendation(100, "Low", (), False)

        cog._fetch_live_price = price
        job = await db.get_inventory_post_job(1, job_id)
        await cog._post_one_job(job, notify=False)

        sent_price = uex.post_marketplace_advertise.call_args.kwargs["price"]
        current_floor = (await db.get_inventory_item(1, inv))["minimum_price"]
        assert sent_price >= current_floor, (sent_price, current_floor)

    asyncio.run(run())


def test_custom_price_still_checked_against_a_floor_raised_during_posting(tmp_path):
    """The custom-price path re-validates against job['minimum_price'] at the top of
    _post_one_job, but that's the same stale snapshot - a floor raised after that check but
    before the POST (e.g. during an unrelated await elsewhere in the same call) must still
    be caught by the live re-check right before the payload is built."""
    async def run():
        db, inv, job_id = await _fixture(tmp_path, "floor_custom.sqlite3", custom=True)
        await db.claim_inventory_post_job(job_id)
        await db.set_inventory_minimum_price(1, inv, 500)
        uex = NS(post_marketplace_advertise=AsyncMock(return_value={"id_listing": 999}))
        cog = _cog(db, uex)

        job = await db.get_inventory_post_job(1, job_id)
        # The job row itself still shows the old floor/custom price (frozen at claim time).
        assert job["minimum_price"] == 100
        await cog._post_one_job(job, notify=False)

        sent_price = uex.post_marketplace_advertise.call_args.kwargs["price"]
        assert sent_price >= 500, sent_price

    asyncio.run(run())


def test_uncertain_post_cannot_relist_without_resolving_live_listing(tmp_path):
    """A03: an ambiguous POST (network error/lost response, or no id_listing returned)
    leaves the job's listing_id NULL - UEX's own acceptance of that POST was never
    confirmed, so a live, untracked listing may already exist. Confirming 0 sold must not
    automatically queue a brand new POST for the same stock while that's unresolved."""
    async def run():
        db, inv, job_id = await _fixture(tmp_path, "uncertain.sqlite3")
        await db.claim_inventory_post_job(job_id)
        await db.mark_inventory_post_failed(job_id, "Network timeout after POST", ambiguous=True)
        cog = _cog(db, NS())

        await cog.inventory_confirm_sale.callback(cog, _interaction(), job_id=job_id, quantity_sold=0)

        due = await db.list_due_inventory_post_jobs()
        assert not due, "a new automatic post was queued while the original POST's outcome is still unknown"

    asyncio.run(run())


def test_uncertain_post_message_tells_the_user_to_check_uex_manually(tmp_path):
    async def run():
        db, inv, job_id = await _fixture(tmp_path, "uncertain_msg.sqlite3")
        await db.claim_inventory_post_job(job_id)
        await db.mark_inventory_post_failed(job_id, "Network timeout after POST", ambiguous=True)
        cog = _cog(db, NS())
        inter = _interaction()

        await cog.inventory_confirm_sale.callback(cog, inter, job_id=job_id, quantity_sold=0)

        (message,), kwargs = inter.response.send_message.call_args
        assert "never confirmed" in message.lower()
        assert kwargs.get("ephemeral") is True

    asyncio.run(run())


def test_resolved_ambiguous_listing_can_still_auto_relist(tmp_path):
    """Contrast case: when the job DOES have a listing_id, needs_confirmation only ever
    arrives after UEX independently confirmed that listing no longer exists (both
    mark_inventory_post_needs_confirmation call sites only fire on an empty GET result) -
    so relisting the remainder in that case is safe and must not be blocked by the A03 fix."""
    async def run():
        db, inv, job_id = await _fixture(tmp_path, "resolved.sqlite3")
        await db.claim_inventory_post_job(job_id)
        await db.mark_inventory_post_listed(
            job_id, listing_id=999, listing_url=None, posted_price=150, date_expiration=None
        )
        await db.mark_inventory_post_needs_confirmation(
            job_id, "Listing disappeared from UEX without a final remaining-stock value"
        )
        cog = _cog(db, NS())

        await cog.inventory_confirm_sale.callback(cog, _interaction(), job_id=job_id, quantity_sold=0)

        due = await db.list_due_inventory_post_jobs()
        assert len(due) == 1, "a resolved (confirmed-gone) listing's remainder should still auto-relist"

    asyncio.run(run())
