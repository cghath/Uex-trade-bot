"""Audit findings MSG-1, MSG-2 and UX-3.

MSG-1: marketplace quality was described as UEX's documented 0-100, but real listings use
the game's 0-1000, and an alert with no max quality showed "100". MSG-2: /marketplace-movers
labelled every price "UEC", even for items traded in WIF or MGS. UX-3: there was no way to
turn the deal scanner off.
"""
import asyncio
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock

from cryptography.fernet import Fernet

from bot.cogs.marketplace import Marketplace
from bot.cogs.marketplace_alerts import MarketplaceAlerts
from bot.cogs.scanner import ScannerOffView
from bot.db.database import Database
from bot.uex.marketplace import QUALITY_MAX, compute_marketplace_movers, format_quality_range


# -- MSG-1: the 0-1000 quality scale ---------------------------------------------------------

def test_an_unset_quality_bound_shows_the_real_scale_limit():
    assert format_quality_range(None, None) == "0-1000"
    assert format_quality_range(800, None) == "800-1000"
    assert format_quality_range(None, 499) == "0-499"


def test_quality_options_accept_the_real_0_to_1000_scale():
    for command in (MarketplaceAlerts.marketplace_alert_add, Marketplace.marketplace_search):
        for name in ("min_quality", "max_quality"):
            param = command._params[name]
            assert (param.min_value, param.max_value) == (0, QUALITY_MAX), (command.name, name)
            assert "0-1000" in str(param.description) and "0-100 " not in str(param.description)
            assert len(str(param.description)) <= 100, "Discord's own limit"


# -- MSG-2: each mover in its own currency ---------------------------------------------------

def _trend(name, currency, current, month):
    return {"item_name": name, "id_item": 1, "price_avg_sell": str(current), "price_avg_month_sell": str(month),
            "currency": currency}


def test_movers_keep_each_rows_own_currency():
    gainers, _ = compute_marketplace_movers([_trend("Hadanite", "WIF", 150, 100), _trend("Laranite", None, 120, 100)])
    assert {m.item_name: m.currency for m in gainers} == {"Hadanite": "WIF", "Laranite": "UEC"}


def test_marketplace_movers_command_shows_wif_not_uec():
    async def run():
        cog = Marketplace.__new__(Marketplace)
        cog.bot = NS(uex=NS(get_marketplace_trends=AsyncMock(return_value=[_trend("Hadanite", "WIF", 150, 100)])))
        interaction = NS(response=NS(defer=AsyncMock()), followup=NS(send=AsyncMock()))
        await Marketplace.marketplace_movers.callback(cog, interaction)
        return interaction.followup.send.await_args.kwargs["embed"]

    embed = asyncio.run(run())
    text = "\n".join(field.value for field in embed.fields)
    assert "150 WIF" in text and "UEC" not in text


# -- UX-3: turning the scanner off -----------------------------------------------------------

def test_clearing_the_scanner_stops_polling_and_keeps_seen_deals(tmp_path):
    async def run():
        db = Database(tmp_path / "scanner.sqlite3", Fernet(Fernet.generate_key()))
        await db.init()
        await db.set_scanner_channel(10, 555)
        await db.mark_scanner_listing_seen(10, 77)
        first = await db.clear_scanner_channel(10)
        second = await db.clear_scanner_channel(10)
        return first, second, await db.list_scanner_watchers(), await db.get_seen_scanner_listing_ids(10)

    first, second, watchers, seen = asyncio.run(run())
    assert (first, second) == (True, False)
    assert watchers == []
    assert seen == {77}, "turning it back on won't re-send a deal already shown"


def _button_interaction(user_id):
    return NS(user=NS(id=user_id), response=NS(edit_message=AsyncMock(), send_message=AsyncMock()))


def test_turn_off_button_turns_the_owners_scanner_off():
    async def run():
        db = NS(clear_scanner_channel=AsyncMock(return_value=True))
        view = ScannerOffView(db, 10)
        interaction = _button_interaction(10)
        await view.turn_off.callback(interaction)
        return db, interaction, view

    db, interaction, view = asyncio.run(run())
    db.clear_scanner_channel.assert_awaited_once_with(10)
    assert "turned off" in interaction.response.edit_message.await_args.kwargs["content"]
    assert all(child.disabled for child in view.children)


def test_turn_off_button_ignores_anyone_else():
    async def run():
        db = NS(clear_scanner_channel=AsyncMock())
        view = ScannerOffView(db, 10)
        await view.turn_off.callback(_button_interaction(99))
        return db

    asyncio.run(run()).clear_scanner_channel.assert_not_awaited()


def test_an_expired_turn_off_button_is_greyed_out():
    async def run():
        view = ScannerOffView(NS(), 10)
        view.origin = NS(edit_original_response=AsyncMock())
        await view.on_timeout()
        return view

    view = asyncio.run(run())
    view.origin.edit_original_response.assert_awaited_once()
    assert all(child.disabled for child in view.children)
