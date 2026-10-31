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
import discord

from bot.cogs.marketplace import Marketplace
from bot.cogs.marketplace_alerts import MarketplaceAlerts
from bot.cogs.scanner import Scanner, ScannerOffView
from bot.db.database import Database
from bot.uex.marketplace import (
    QUALITY_MAX, compute_marketplace_movers, format_quality_range, group_marketplace_history_by_currency_unit,
    marketplace_price_label, quality_range_error,
)


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


def test_a_minimum_quality_above_the_maximum_is_refused_before_anything_runs():
    """Audit LOGIC-7: min 900 / max 100 made an alert that could never fire and a search
    that was always empty."""
    assert quality_range_error(900, 100) is not None
    assert quality_range_error(100, 100) is None and quality_range_error(900, None) is None

    async def run(command, cog, *args):
        interaction = NS(guild_id=1, channel_id=2, user=NS(id=3),
                         response=NS(defer=AsyncMock(), send_message=AsyncMock()), followup=NS(send=AsyncMock()))
        await command.callback(cog, interaction, *args)
        return interaction

    alerts = MarketplaceAlerts.__new__(MarketplaceAlerts)
    alerts.bot = NS(db=NS(add_marketplace_alert=AsyncMock(return_value=9)))
    interaction = asyncio.run(run(MarketplaceAlerts.marketplace_alert_add, alerts,
                                  "Laranite", NS(value="sell"), None, 900.0, 100.0, None))
    alerts.bot.db.add_marketplace_alert.assert_not_awaited()
    assert "above the maximum" in interaction.response.send_message.await_args.args[0]
    assert interaction.response.send_message.await_args.kwargs == {"ephemeral": True}

    market = Marketplace.__new__(Marketplace)
    market.bot = NS(uex=NS(get_item_catalog=AsyncMock(return_value=[])))
    interaction = asyncio.run(run(Marketplace.marketplace_search, market, "Laranite", None, 900.0, 100.0))
    market.bot.uex.get_item_catalog.assert_not_awaited()
    interaction.response.defer.assert_not_awaited()
    assert "above the maximum" in interaction.response.send_message.await_args.args[0]


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


def _button_interaction(user_id, *, manage_channels=True):
    return NS(user=NS(id=user_id), permissions=discord.Permissions(manage_channels=manage_channels),
              response=NS(defer=AsyncMock(), edit_message=AsyncMock(), send_message=AsyncMock()),
              edit_original_response=AsyncMock())


def test_turn_off_button_turns_the_owners_scanner_off():
    async def run():
        interaction = _button_interaction(10)
        deferred_at_write = []

        async def clear(user_id):
            # Audit REL-6: acknowledged before the write, which can wait on a lock past 3s.
            deferred_at_write.append(interaction.response.defer.await_count)
            return True

        db = NS(clear_scanner_channel=AsyncMock(side_effect=clear))
        view = ScannerOffView(db, 10)
        await view.turn_off.callback(interaction)
        return db, interaction, view, deferred_at_write

    db, interaction, view, deferred_at_write = asyncio.run(run())
    db.clear_scanner_channel.assert_awaited_once_with(10)
    assert deferred_at_write == [1]
    assert "turned off" in interaction.edit_original_response.await_args.kwargs["content"]
    assert all(child.disabled for child in view.children)


def _turn_off_reply(*, manage_channels):
    async def run():
        view = ScannerOffView(NS(clear_scanner_channel=AsyncMock(return_value=True)), 10)
        interaction = _button_interaction(10, manage_channels=manage_channels)
        await view.turn_off.callback(interaction)
        return interaction.edit_original_response.await_args.kwargs["content"]  # deferred first (audit REL-6)

    return asyncio.run(run())


def test_turning_it_off_points_a_member_at_scan_now_not_the_admin_command():
    """A member who set the scanner up before /set-scanner-channel became admin-only isn't sent
    to it to turn the scanner back on (2026-10-03 audit UX-12)."""
    member = _turn_off_reply(manage_channels=False)
    assert "turned off" in member and "/scan-now" in member and "/set-scanner-channel" not in member
    assert "/set-scanner-channel" in _turn_off_reply(manage_channels=True)


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


# -- 2026-10-03 audit MSG-3: /marketplace-history in one currency and unit -------------------

def _history(price, currency, unit, when, operation="sell"):
    return {"date_added": when, "price": str(price), "currency": currency, "unit": unit, "operation": operation}


HISTORY = [
    _history(1000, "UEC", "unit", 1_700_000_000), _history(1100, "UEC", "unit", 1_700_100_000),
    _history(1200, "uec ", "unit", 1_700_200_000), _history(5, "WIF", "unit", 1_700_150_000),
    _history(90_000, "UEC", "box", 1_700_250_000),
]


def test_history_is_split_by_currency_and_unit_biggest_first():
    groups = group_marketplace_history_by_currency_unit(HISTORY)
    assert [(key, len(rows)) for key, rows in groups] == [
        (("UEC", "unit"), 3), (("UEC", "box"), 1), (("WIF", "unit"), 1),
    ], "ties go to the group with the latest row"
    assert marketplace_price_label("WIF", "unit") == "WIF / unit"
    assert marketplace_price_label(None, None) == "price / unit"


def test_marketplace_history_charts_one_currency_and_unit_and_labels_it(monkeypatch):
    from bot.cogs import marketplace as marketplace_module

    drawn = {}

    def render(**kwargs):
        drawn.update(kwargs)
        return __import__("io").BytesIO(b"png")

    monkeypatch.setattr(marketplace_module, "render_price_history_chart", render)

    async def run():
        cog = Marketplace.__new__(Marketplace)
        cog.bot = NS(uex=NS(get_item_catalog=AsyncMock(return_value=[{"id": 5, "name": "Hadanite"}]),
                            get_marketplace_prices_history=AsyncMock(return_value=HISTORY)))
        interaction = NS(response=NS(defer=AsyncMock()), followup=NS(send=AsyncMock()))
        await Marketplace.marketplace_history.callback(cog, interaction, "Hadanite")
        return interaction.followup.send.await_args.kwargs["embed"]

    embed = asyncio.run(run())
    assert drawn["y_label"] == "UEC / unit"
    assert [row["price_sell"] for row in drawn["history_rows"]] == [1000, 1100, 1200], "no WIF or per-box price"
    assert "in UEC / unit" in embed.footer.text
    assert "2 price changes in other currencies or units not drawn" in embed.footer.text


# -- 2026-10-03 audit UX-12: /set-scanner-channel is for admins --------------------------------

def test_set_scanner_channel_is_hidden_from_members_and_checked_when_run():
    command = Scanner.set_scanner_channel
    assert command.default_permissions == discord.Permissions(manage_channels=True)
    assert command.checks, "a server that shows the command to members still can't let them run it"


def _set_channel(target_permissions, *, manage_channels=True):
    async def run():
        cog = Scanner.__new__(Scanner)
        cog.bot = NS(db=NS(set_scanner_channel=AsyncMock()), config=NS(scanner_steal_threshold=0.2))
        interaction = NS(user=NS(id=10), permissions=discord.Permissions(manage_channels=manage_channels),
                         response=NS(defer=AsyncMock(), send_message=AsyncMock()), followup=NS(send=AsyncMock()))
        channel = NS(id=5, mention="#deals", permissions_for=lambda member: target_permissions)
        await Scanner.set_scanner_channel.callback(cog, interaction, channel)
        return cog.bot.db, interaction

    return asyncio.run(run())


def test_aiming_the_deal_posts_needs_manage_channels_in_that_channel():
    db, interaction = _set_channel(discord.Permissions.none())
    db.set_scanner_channel.assert_not_awaited()
    reply = interaction.response.send_message.await_args
    assert "Manage Channels permission in #deals" in reply.args[0] and reply.kwargs["ephemeral"] is True

    db, _ = _set_channel(discord.Permissions(manage_channels=True))
    db.set_scanner_channel.assert_awaited_once_with(10, 5)


def _status(manage_channels):
    async def run():
        cog = Scanner.__new__(Scanner)
        cog.bot = NS(db=NS(get_scanner_channel=AsyncMock(return_value=None)))
        interaction = NS(user=NS(id=10), permissions=discord.Permissions(manage_channels=manage_channels),
                         response=NS(send_message=AsyncMock()))
        await Scanner.scanner_status.callback(cog, interaction)
        return interaction.response.send_message.await_args.args[0]

    return asyncio.run(run())


def test_scanner_status_points_a_member_at_scan_now_not_the_admin_command():
    assert "/scan-now" in _status(False) and "/set-scanner-channel" not in _status(False)
    assert "/set-scanner-channel" in _status(True)
