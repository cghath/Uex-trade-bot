"""UEX answers `/marketplace_listings?id=N` with the bare listing object, or `false` when
there's none - never a one-row list (checked live on 2026-10-01). get_marketplace_listings
must still hand every caller a list: a bare object reached `rows[0]` as `KeyError: 0` and
stopped inventory reconciliation on the Pi."""
from __future__ import annotations

import asyncio

import httpx
import pytest

from bot.uex.client import UexClient

LISTING = {"id": 175615, "id_item": 42, "in_stock": 1, "is_sold_out": 0}


def _listings(data):
    async def run():
        client = UexClient(app_token="test", base_url="https://uex.test")
        await client._client.aclose()
        client._client = httpx.AsyncClient(transport=httpx.MockTransport(
            lambda request: httpx.Response(200, json={"status": "ok", "data": data})
        ))
        try:
            return await client.get_marketplace_listings(id=175615, use_cache=False)
        finally:
            await client.aclose()

    return asyncio.run(run())


@pytest.mark.parametrize("data, expected", [
    (LISTING, [LISTING]),       # an id= lookup that found the listing
    (False, []),                # an id= lookup with no such listing
    ({}, []),
    ([LISTING, LISTING], [LISTING, LISTING]),  # id_item=/username= lookups are already lists
])
def test_listing_lookups_always_come_back_as_a_list(data, expected):
    assert _listings(data) == expected
