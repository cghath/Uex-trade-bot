"""Audit UX-13, MSG-9 and MSG-10 - the plain-text lists players read.

- UX-13: /my-favorites, /my-negotiations, /trade-log and /uex-trades joined their lines and
  sent them as-is. Over Discord's 2,000-character limit the send is refused, so the whole
  command failed. They now keep whole lines and count the rest.
- MSG-9: /uex-trades printed UEX's unix timestamps raw.
- MSG-10: /uex-trades told the player their key "may be invalid or expired" for any
  failure, a UEX outage included.
"""
import asyncio
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock

import httpx
from cryptography.fernet import Fernet

from bot.cogs.marketplace import Marketplace
from bot.cogs.trades import Trades
from bot.db.database import Database
from bot.delivery import MAX_MESSAGE_CHARS, fit_lines
from bot.uex.client import UexClient
from bot.uex.exceptions import UexApiError, UexAuthError

# ---- fit_lines ----------------------------------------------------------------------------


def test_short_lists_are_joined_unchanged():
    assert fit_lines(["a", "b"], footer="-# hint") == "a\nb\n-# hint"


def test_a_long_list_keeps_whole_lines_counts_the_rest_and_keeps_its_footer():
    lines = [f"line {i:03d} " + "x" * 90 for i in range(60)]
    text = fit_lines(lines, footer="-# hint")
    assert len(text) <= MAX_MESSAGE_CHARS
    kept = [line for line in text.splitlines() if line.startswith("line ")]
    assert kept == lines[:len(kept)], "only whole lines, in order"
    assert text.splitlines()[-2] == f"…and {60 - len(kept)} more."
    assert text.endswith("-# hint")


def test_a_single_line_longer_than_the_limit_still_gives_a_valid_message():
    text = fit_lines(["y" * 5000])
    assert text == "…and 1 more."


# ---- the commands -------------------------------------------------------------------------

class _Followup:
    def __init__(self):
        self.sent = []

    async def send(self, *args, **kwargs):
        self.sent.append((args, kwargs))


def _interaction():
    sent = []

    async def send_message(*args, **kwargs):
        sent.append((args, kwargs))

    async def defer(**kwargs):
        return None

    return NS(user=NS(id=7), response=NS(defer=defer, send_message=send_message), followup=_Followup(),
              direct=sent)


def test_trade_log_with_many_long_entries_still_sends():
    entries = [{"id": i, "logged_at": "2026-09-29 12:00:00", "operation": "sell", "quantity_scu": 696.0,
                "commodity_name": "Recycled Material Composite", "unit_price": 12345.67,
                "terminal_name": "Shubin Mining Facility SMCa-6 - Hurston - Aberdeen"} for i in range(50)]
    cog = Trades.__new__(Trades)
    cog.bot = NS(db=NS(get_trade_log=AsyncMock(return_value=entries)))
    interaction = _interaction()
    asyncio.run(Trades.trade_log.callback(cog, interaction, 50))
    (message,), kwargs = interaction.direct[0]
    assert len(message) <= MAX_MESSAGE_CHARS and "more." in message and kwargs["ephemeral"] is True


def test_trade_log_limit_is_bounded():
    param = next(p for p in Trades.trade_log.parameters if p.name == "limit")
    assert (param.min_value, param.max_value) == (1, 50)


def _uex_trades(rows=None, error=None):
    cog = Trades.__new__(Trades)
    uex = NS(get_user_trades=AsyncMock(return_value=rows, side_effect=error))
    cog.bot = NS(db=NS(get_user_secret_key=AsyncMock(return_value="sk")), uex=uex)
    interaction = _interaction()
    asyncio.run(Trades.uex_trades.callback(cog, interaction))
    return interaction.followup.sent[-1][0][0]


def test_uex_trades_shows_real_dates_not_raw_timestamps():
    message = _uex_trades(rows=[{"operation": "buy", "scu": 32, "commodity_name": "Gold", "price": 5000,
                                 "date_added": 1790000000}])
    assert "<t:1790000000:f>" in message and "(1790000000)" not in message


def test_uex_trades_only_blames_the_key_for_an_auth_failure():
    outage = _uex_trades(error=UexApiError("503 x3"))
    assert "invalid or expired" not in outage and "/link-uex-account" not in outage
    assert "usually temporary" in outage
    rejected = _uex_trades(error=UexAuthError("invalid_secret_key"))
    assert "/link-uex-account" in rejected


def test_a_full_favourites_list_with_long_titles_stays_under_the_limit(tmp_path):
    def handler(request):
        if "marketplace_favorites" in request.url.path:
            return httpx.Response(200, json={"status": "ok", "data": [
                {"id": i, "id_listing": 1000 + i, "title": "Extremely Long Listing Title " * 6,
                 "price": "123456789", "currency": "UEC", "is_sold_out": 1} for i in range(15)]})
        return httpx.Response(200, json={"status": "ok", "data": []})

    async def run():
        db = Database(tmp_path / "fav.sqlite3", Fernet(Fernet.generate_key()))
        await db.init()
        await db.set_user_secret_key(7, "sk_test")
        client = UexClient(app_token="test", base_url="https://uex.test")
        await client._client.aclose()
        client._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        cog = Marketplace.__new__(Marketplace)
        cog.bot = NS(db=db, uex=client)
        interaction = _interaction()
        try:
            await Marketplace.my_favorites.callback(cog, interaction)
        finally:
            await client.aclose()
        return interaction.followup.sent[-1][0][0]

    message = asyncio.run(run())
    assert len(message) <= MAX_MESSAGE_CHARS
    assert "more." in message and message.endswith("Pick one below for its full details.")
