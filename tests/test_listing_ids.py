"""Audit UX-4: /marketplace-listing and /marketplace-delete-listing take a listing id and say
it's shown in /marketplace-search - it wasn't. /my-favorites printed the favourite row's own
id and /my-negotiations the negotiation's, neither of which any command accepts. All three
now show the listing's id."""
import asyncio
from types import SimpleNamespace as NS

import httpx
from cryptography.fernet import Fernet

from bot.cogs.marketplace import Marketplace
from bot.db.database import Database
from bot.uex.client import UexClient


class _Followup:
    def __init__(self):
        self.sent = []

    async def send(self, *args, **kwargs):
        self.sent.append((args, kwargs))


class _Response:
    async def defer(self, **kwargs):
        pass


def _run(tmp_path, handler, command, *args):
    async def run():
        db = Database(tmp_path / "ids.sqlite3", Fernet(Fernet.generate_key()))
        await db.init()
        await db.set_user_secret_key(7, "sk_test")
        client = UexClient(app_token="test", base_url="https://uex.test")
        await client._client.aclose()
        client._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        cog = Marketplace.__new__(Marketplace)
        cog.bot = NS(db=db, uex=client)
        interaction = NS(user=NS(id=7), response=_Response(), followup=_Followup())
        try:
            await getattr(Marketplace, command).callback(cog, interaction, *args)
        finally:
            await client.aclose()
        return interaction.followup.sent

    return asyncio.run(run())


def _ok(data):
    return httpx.Response(200, json={"status": "ok", "data": data})


def test_search_results_show_each_listings_id(tmp_path):
    def handler(request):
        if request.url.path.endswith("/items"):
            return _ok([])
        if "marketplace_listings" in request.url.path:
            return _ok([{"id": 55512, "title": "Laranite x 32", "operation": "sell", "price": "450000",
                         "currency": "UEC", "user_username": "Pilot", "in_stock": 3}])
        return _ok([])

    sent = _run(tmp_path, handler, "marketplace_search", "Laranite")
    embed = sent[-1][1]["embed"]
    assert "listing #55512" in embed.fields[0].value
    assert "/marketplace-listing <id>" in embed.footer.text


def test_favorites_show_the_listing_id_not_the_favourite_rows_own(tmp_path):
    def handler(request):
        if "marketplace_favorites" in request.url.path:
            return _ok([{"id": 3, "id_listing": 777, "title": "Arrowhead Sniper", "price": "90000",
                         "currency": "UEC"}])
        if "marketplace_listings" in request.url.path:
            return _ok([{"id": 777, "id_item": 12}])
        return _ok([])

    message = _run(tmp_path, handler, "my_favorites")[-1][0][0]
    assert message.startswith("Listing #777 — ") and "#3 " not in message
    assert "/marketplace-listing <id>" in message


def test_negotiations_show_the_listing_id_not_the_negotiations_own(tmp_path):
    def handler(request):
        if "marketplace_negotiations" in request.url.path:
            return _ok([{"id": 4, "id_listing": 888, "listing_title": "Laranite", "is_listing_advertiser": 1,
                         "price": "1500", "currency": "UEC", "date_closed": None}])
        if "marketplace_listings" in request.url.path:
            return _ok([])
        return _ok([])

    message = _run(tmp_path, handler, "my_negotiations")[-1][0][0]
    assert message.startswith("Listing #888 selling — ") and "#4 " not in message
