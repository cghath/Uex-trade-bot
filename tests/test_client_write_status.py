"""Regression test for UexClient's write (POST/DELETE) response handling.

Real defect (audit finding A01): a documented, non-"ok" status on a write endpoint (e.g.
DELETE /marketplace_listings returning status="user_not_verified") wasn't recognized as a
rejection unless it matched _AUTH_ERROR_STATUSES or the literal string "error" - every other
status fell through to the generic "log and return data" path built for GET's soft
"nothing matched" semantics (no_trades_found, invalid_type, etc.), so a rejected DELETE
returned normally with data=None as if the listing had actually been deleted. Unlike GET,
every documented non-"ok" status on a write endpoint is a genuine rejection - there is no
soft/empty-but-valid case for a POST or DELETE.
"""
from __future__ import annotations

import asyncio

import httpx
import pytest

from bot.uex.client import UexClient
from bot.uex.exceptions import UexApiError, UexRejectedError


def test_delete_with_undocumented_rejection_status_raises(tmp_path):
    """user_not_verified is a real documented DELETE /marketplace_listings rejection status
    (see docs/UEX_API_2.0_reference.md) that isn't in _AUTH_ERROR_STATUSES and isn't the
    literal "error" - it must still be treated as a failure, not a successful deletion."""
    async def run():
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                403,
                json={"status": "user_not_verified", "message": "User account not verified", "data": None},
            )

        client = UexClient("fake", base_url="https://client-test.invalid")
        await client._client.aclose()
        client._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        try:
            with pytest.raises(UexApiError):
                await client.delete_marketplace_listing(listing_id=999, secret_key="fake")
        finally:
            await client.aclose()

    asyncio.run(run())


def test_post_with_undocumented_rejection_status_raises(tmp_path):
    """Same defect class on the write side: POST /marketplace_advertise's own
    user_active_listings_limit_reached (a real documented status) must raise, not return
    the null data of a rejected listing as if it had been created."""
    async def run():
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json={"status": "user_active_listings_limit_reached", "message": "Too many active listings", "data": None},
            )

        client = UexClient("fake", base_url="https://client-test.invalid")
        await client._client.aclose()
        client._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        try:
            with pytest.raises(UexApiError):
                await client.post_marketplace_advertise(secret_key="fake", id_category=1)
        finally:
            await client.aclose()

    asyncio.run(run())


def test_delete_retried_after_network_error_is_ambiguous_not_rejected(tmp_path):
    """Follow-up review finding: DELETE is retried after a network-level failure (unlike
    POST, which never retries), so a "not found"-style status on the RETRY (e.g.
    listing_not_found) is genuinely uncertain - the first attempt's response was lost, so
    it may have already reached UEX and completed the deletion before the connection
    dropped. Raising UexRejectedError here (whose documented contract is "definitely
    nothing happened, no reconciliation needed") would be actively wrong in that case;
    this must be the plain, ambiguous UexApiError instead."""
    async def run():
        attempts = {"n": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            attempts["n"] += 1
            if attempts["n"] == 1:
                raise httpx.ReadError("connection dropped", request=request)
            return httpx.Response(
                200, json={"status": "listing_not_found", "message": "Listing not found", "data": None}
            )

        client = UexClient("fake", base_url="https://client-test.invalid")
        await client._client.aclose()
        client._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        try:
            with pytest.raises(UexApiError) as exc_info:
                await client.delete_marketplace_listing(listing_id=999, secret_key="fake")
            assert not isinstance(exc_info.value, UexRejectedError), (
                "a retried DELETE's ambiguous outcome must not be reported as a definite rejection"
            )
            assert attempts["n"] == 2
        finally:
            await client.aclose()

    asyncio.run(run())


def test_delete_rejected_on_the_first_attempt_is_still_a_definite_rejection(tmp_path):
    """Regression guard: the retry-ambiguity fix above must not weaken the original A01
    fix - a DELETE rejected on its FIRST attempt (no prior network error at all) is still
    a real, definite rejection, not downgraded to ambiguous just because DELETE is
    generally retry-eligible."""
    async def run():
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200, json={"status": "listing_not_found", "message": "Listing not found", "data": None}
            )

        client = UexClient("fake", base_url="https://client-test.invalid")
        await client._client.aclose()
        client._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        try:
            with pytest.raises(UexRejectedError):
                await client.delete_marketplace_listing(listing_id=999, secret_key="fake")
        finally:
            await client.aclose()

    asyncio.run(run())


def test_get_with_non_ok_status_still_returns_data_not_an_error(tmp_path):
    """The fix must stay scoped to write methods - a GET's own soft "nothing matched"
    statuses (e.g. no_trades_found) are still a valid, non-fatal empty result."""
    async def run():
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"status": "no_trades_found", "message": "", "data": []})

        client = UexClient("fake", base_url="https://client-test.invalid")
        await client._client.aclose()
        client._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        try:
            result = await client.get_terminals()
            assert result == []
        finally:
            await client.aclose()

    asyncio.run(run())
