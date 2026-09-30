"""Audit REL-8: commands that wrote to the DB before responding. A write can wait on a lock
(up to the 30s busy_timeout) past Discord's 3-second window; the player then sees "did not
respond", retries, and gets a duplicate - or, for /inventory-remove, removes twice. Every
one now defers first and replies through the followup. /alert-add and /stock-alert-add
are covered in test_alert_add_and_restock.py."""
import asyncio
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock

import pytest

from bot.cogs.account import Account, LinkUexModal
from bot.cogs.digest import Digest
from bot.cogs.marketplace_alerts import MarketplaceAlerts
from bot.cogs.negotiation_alerts import NegotiationAlerts
from bot.cogs.personal_inventory import PersonalInventory
from bot.cogs.scanner import Scanner
from bot.cogs.ships import Ships
from bot.cogs.trades import Trades
from bot.cogs.trading_preferences import TradingPreferences


class _RecordingDb:
    """Every method records whether the interaction had been deferred when it was called."""

    def __init__(self, interaction):
        self.calls = []
        self._interaction = interaction

    def __getattr__(self, name):
        async def method(*args, **kwargs):
            self.calls.append((name, self._interaction.response.defer.await_count))
            return None
        return method


def _interaction():
    return NS(user=NS(id=1, display_name="Pilot"), guild_id=9, channel_id=2,
              response=NS(defer=AsyncMock(), send_message=AsyncMock(side_effect=AssertionError("replied directly"))),
              followup=NS(send=AsyncMock()))


def _cog(cls, db):
    cog = cls.__new__(cls)
    cog.bot = NS(db=db, config=NS(scanner_steal_threshold=0.2))
    return cog


CHANNEL = NS(id=5, mention="#deals")

CASES = {
    "/trade-log-add": (Trades, lambda cog, i: Trades.trade_log_add.callback(
        cog, i, NS(value="buy"), "Gold", 10.0, 5.0), "log_trade"),
    "/unlink-uex-account": (Account, lambda cog, i: Account.unlink_uex_account.callback(cog, i),
                            "remove_user_secret_key"),
    "/set-scanner-channel": (Scanner, lambda cog, i: Scanner.set_scanner_channel.callback(cog, i, CHANNEL),
                             "set_scanner_channel"),
    "/set-digest-channel": (Digest, lambda cog, i: Digest.set_digest_channel.callback(cog, i, CHANNEL, 12),
                            "set_guild_digest_config"),
    "/digest-disable": (Digest, lambda cog, i: Digest.digest_disable.callback(cog, i), "disable_guild_digest"),
    "/marketplace-alert-add": (MarketplaceAlerts, lambda cog, i: MarketplaceAlerts.marketplace_alert_add.callback(
        cog, i, "Laranite", NS(value="sell")), "add_marketplace_alert"),
    "/inventory-set-minimum": (PersonalInventory, lambda cog, i: PersonalInventory.inventory_set_minimum.callback(
        cog, i, 1, 100), "set_inventory_minimum_price"),
    "/inventory-remove": (PersonalInventory, lambda cog, i: PersonalInventory.inventory_remove.callback(
        cog, i, 1, 2), "remove_inventory_quantity"),
    "/inventory-confirm-sale": (PersonalInventory, lambda cog, i: PersonalInventory.inventory_confirm_sale.callback(
        cog, i, 1, 0), "confirm_ambiguous_inventory_sale"),
    "/negotiation-alerts off": (NegotiationAlerts, lambda cog, i: NegotiationAlerts.negotiation_alerts.callback(
        cog, i, False), "set_negotiation_alerts_enabled"),
    "/clear-default-ship": (Ships, lambda cog, i: Ships.clear_default_ship.callback(cog, i), "clear_default_ship"),
    "/clear-trading-preferences": (TradingPreferences, lambda cog, i: TradingPreferences.clear_trading_preferences
                                   .callback(cog, i), "clear_trading_preferences"),
}


@pytest.mark.parametrize("command", list(CASES))
def test_the_command_defers_before_its_db_write_and_replies_privately(command):
    cls, call, write = CASES[command]

    async def run():
        interaction = _interaction()
        db = _RecordingDb(interaction)
        await call(_cog(cls, db), interaction)
        return interaction, db

    interaction, db = asyncio.run(run())
    interaction.response.defer.assert_awaited_once_with(ephemeral=True)
    assert (write, 1) in db.calls, f"{write} ran before the defer: {db.calls}"
    assert all(deferred == 1 for _, deferred in db.calls), db.calls
    interaction.followup.send.assert_awaited()
    assert all(call.kwargs.get("ephemeral") is True for call in interaction.followup.send.await_args_list)


def test_the_link_account_form_defers_before_saving_the_key():
    async def run():
        interaction = _interaction()
        db = _RecordingDb(interaction)
        modal = LinkUexModal(NS(db=db, uex=NS(get_user_profile=AsyncMock(return_value={"username": "pilot"}))))
        modal.secret_key_input._value = "  secret-key  "
        await modal.on_submit(interaction)
        return interaction, db

    interaction, db = asyncio.run(run())
    # A modal submit needs thinking=True for the deferred reply to be a private message.
    interaction.response.defer.assert_awaited_once_with(ephemeral=True, thinking=True)
    assert db.calls == [("set_user_secret_key", 1)]
    assert "linked" in interaction.followup.send.await_args.args[0]
