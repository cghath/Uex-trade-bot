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
from bot.cogs.personal_inventory import (
    FloorReachedView,
    LowerFloorModal,
    PersonalInventory,
    SetMinimumModal,
    SetMinimumPricesView,
)
from bot.cogs.scanner import Scanner
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


# Manage Channels in it: /set-scanner-channel checks the target channel (audit UX-12).
CHANNEL = NS(id=5, mention="#deals", permissions_for=lambda member: NS(manage_channels=True))

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


# -- Audit REL-6: the write paths entry 100's sweep missed -----------------------------------

class _PendingJobDb(_RecordingDb):
    async def get_inventory_post_job(self, user_id, job_id):
        self.calls.append(("get_inventory_post_job", self._interaction.response.defer.await_count))
        return {"id": job_id, "status": "pending"}


def test_inventory_cancel_post_defers_before_cancelling_a_pending_job():
    async def run():
        interaction = _interaction()
        db = _PendingJobDb(interaction)
        await PersonalInventory.inventory_cancel_post.callback(_cog(PersonalInventory, db), interaction, 4)
        return interaction, db

    interaction, db = asyncio.run(run())
    interaction.response.defer.assert_awaited_once_with(ephemeral=True)
    assert ("cancel_pending_inventory_post", 1) in db.calls, db.calls
    assert interaction.followup.send.await_args.kwargs == {"ephemeral": True}


def _form_interaction():
    interaction = _interaction()
    interaction.edit_original_response = AsyncMock()
    return interaction


def test_the_minimum_price_form_defers_before_saving_the_floor():
    async def run():
        interaction = _form_interaction()
        db = _RecordingDb(interaction)
        row = {"id": 3, "item_name": "Gold", "quantity": 5, "minimum_price": None}
        view = SetMinimumPricesView(_cog(PersonalInventory, db), 1, [row], [row])
        modal = SetMinimumModal(view, 3)
        modal.price_input._value = "500,000"
        await modal.on_submit(interaction)
        return interaction, db

    interaction, db = asyncio.run(run())
    interaction.response.defer.assert_awaited_once_with()
    assert db.calls == [("set_inventory_minimum_price", 1)]
    # The write found nothing (None), so the reply is a private followup, not an edit.
    assert "was not found" in interaction.followup.send.await_args.args[0]
    assert interaction.followup.send.await_args.kwargs == {"ephemeral": True}


def test_the_lower_floor_form_defers_before_resuming_the_job():
    async def run():
        interaction = _form_interaction()
        db = _RecordingDb(interaction)
        view = FloorReachedView(_cog(PersonalInventory, db), {"id": 5, "user_id": 1})
        modal = LowerFloorModal(view)
        modal.price_input._value = "700000"
        await modal.on_submit(interaction)
        return interaction, db

    interaction, db = asyncio.run(run())
    interaction.response.defer.assert_awaited_once_with()
    assert db.calls == [("resume_auto_relist_with_new_floor", 1)]
    interaction.edit_original_response.assert_awaited_once()
