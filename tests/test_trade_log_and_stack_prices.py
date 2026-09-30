"""A wrong trade-log entry can be removed, and its commodity/terminal autocomplete (audit
UX-16); an /inventory-sell batch takes a custom price per stack (audit UX-20)."""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace as NS

from cryptography.fernet import Fernet

from bot.cogs.personal_inventory import AuthorizeScheduleView, PersonalInventory, StackPricesModal
from bot.cogs.prices import commodity_name_autocomplete, terminal_name_autocomplete
from bot.cogs.trades import Trades
from bot.db.database import Database


def _db(tmp_path) -> Database:
    db = Database(tmp_path / "bot.sqlite3", Fernet(Fernet.generate_key()))
    asyncio.run(db.init())
    return db


class _Response:
    def __init__(self):
        self.messages, self.edits, self.sent_modal = [], [], None

    async def send_message(self, *args, **kwargs):
        self.messages.append((args, kwargs))

    async def edit_message(self, **kwargs):
        self.edits.append(kwargs)

    async def send_modal(self, modal):
        self.sent_modal = modal

    deferred = False

    async def defer(self, **kwargs):
        self.deferred = True


class _Interaction:
    def __init__(self, user_id, *, values=None):
        self.user = NS(id=user_id)
        self.response = _Response()
        self.followup = NS(sent=[], send=self._send)
        self.data = {"values": values or []}
        self.original_edits = []

    async def edit_original_response(self, **kwargs):
        self.original_edits.append(kwargs)

    async def _send(self, *args, **kwargs):
        self.followup.sent.append((args, kwargs))


# -- UX-16: the trade log -----------------------------------------------------------------


def test_trade_log_add_autocompletes_commodity_and_terminal():
    params = Trades.trade_log_add._params
    assert params["commodity"].autocomplete is commodity_name_autocomplete
    assert params["terminal"].autocomplete is terminal_name_autocomplete


def test_a_trade_can_be_removed_from_the_trade_log_menu(tmp_path):
    db = _db(tmp_path)

    async def run():
        gold = await db.log_trade(user_id=1, commodity_name="Gold", operation="buy", terminal_name="Area18",
                                  quantity_scu=10, unit_price=6.5)
        await db.log_trade(user_id=1, commodity_name="Laranite", operation="sell", quantity_scu=20, unit_price=30)
        cog = Trades.__new__(Trades)
        cog.bot = NS(db=db)
        interaction = _Interaction(1)
        await Trades.trade_log.callback(cog, interaction, 10)
        (text,), kwargs = interaction.response.messages[0]
        view = kwargs["view"]
        assert "Gold" in text and "Laranite" in text
        (menu,) = [item for item in view.children if hasattr(item, "options")]
        assert any(option.label.startswith(f"#{gold} BUY") for option in menu.options)

        pick = _Interaction(1, values=[str(gold)])
        acknowledged = []
        real_delete = db.delete_trade_log_entry

        async def delete(user_id, entry_id):
            acknowledged.append(pick.response.deferred)
            return await real_delete(user_id, entry_id)

        db.delete_trade_log_entry = delete
        await view._on_select(pick)
        assert acknowledged == [True], "the menu answers Discord before its DB write (audit REL-8)"
        content = pick.original_edits[0]["content"]
        assert content.startswith(f"Removed trade #{gold}.") and "Laranite" in content and "Gold" not in content
        return [entry["commodity_name"] for entry in await db.get_trade_log(1)]

    assert asyncio.run(run()) == ["Laranite"]


def test_nobody_can_remove_someone_elses_trade(tmp_path):
    db = _db(tmp_path)

    async def run():
        entry = await db.log_trade(user_id=1, commodity_name="Gold", operation="buy", quantity_scu=1, unit_price=1)
        assert await db.delete_trade_log_entry(2, entry) is False
        assert len(await db.get_trade_log(1)) == 1
        assert await db.delete_trade_log_entry(1, entry) is True
        return await db.get_trade_log(1)

    assert asyncio.run(run()) == []


# -- UX-20: a custom price per stack --------------------------------------------------------


async def _batch(db, count, *, minimum=1000):
    specs = []
    for n in range(count):
        inventory_id = await db.add_inventory_item(
            user_id=7, id_item=n + 1, id_category=2, item_name=f"Item {n + 1}", item_slug=None, quantity=2,
            quality=0, location="Area18", minimum_price=minimum,
        )
        specs.append({"inventory_id": inventory_id, "quantity": 2, "scheduled_for": datetime.now(timezone.utc),
                      "auto_relist": True, "minimum_price": minimum, "item_name": f"Item {n + 1}"})
    cog = PersonalInventory.__new__(PersonalInventory)
    cog.bot = NS(db=db)
    return AuthorizeScheduleView(cog, 7, specs), specs


async def _pick(view, value):
    interaction = _Interaction(7)
    view.choose_pricing_strategy._values = [value]
    await view.choose_pricing_strategy.callback(interaction)
    return interaction


async def _fill(modal, prices):
    for text_input, price in zip(modal.inputs, prices):
        text_input._value = price
    interaction = _Interaction(7)
    await modal.on_submit(interaction)
    return interaction


def test_a_seven_stack_batch_is_priced_five_then_two_and_each_job_keeps_its_own_price(tmp_path):
    db = _db(tmp_path)

    async def run():
        view, specs = await _batch(db, 7)
        first = (await _pick(view, "custom")).response.sent_modal
        assert isinstance(first, StackPricesModal) and len(first.inputs) == 5
        assert first.inputs[0].label == f"#{specs[0]['inventory_id']} Item 1"

        saved = await _fill(first, ["2,000", "2001", "2002", "2003", "2004"])
        assert saved.response.edits[0]["content"].startswith("Custom prices set for 5 of 7 stacks")
        assert view.price_button in view.children
        assert view.price_button.label == "Set prices for stacks 6-7 of 7"

        click = _Interaction(7)
        await view.price_button.callback(click)
        second = click.response.sent_modal
        assert len(second.inputs) == 2
        done = await _fill(second, ["3000", "3001"])
        assert "Item 7" in done.response.edits[0]["content"] and "3,001 UEC/unit" in done.response.edits[0]["content"]

        await view.confirm.callback(_Interaction(7))
        jobs = await db.list_active_inventory_jobs(7)
        return {job["inventory_id"]: job["custom_price"] for job in jobs}, specs

    prices, specs = asyncio.run(run())
    assert prices == dict(zip([s["inventory_id"] for s in specs], [2000, 2001, 2002, 2003, 2004, 3000, 3001]))


def test_a_price_below_a_stacks_minimum_saves_nothing_from_that_form(tmp_path):
    db = _db(tmp_path)

    async def run():
        view, _ = await _batch(db, 2, minimum=5000)
        modal = (await _pick(view, "custom")).response.sent_modal
        result = await _fill(modal, ["6000", "4000"])
        content = result.response.edits[0]["content"]
        assert "Nothing was saved from that form" in content and "#2 Item 2: below your minimum of 5,000" in content
        assert view.custom_prices == {}
        assert view.price_button in view.children and "Set custom prices" in content

    asyncio.run(run())


def test_authorize_refuses_a_custom_batch_until_every_stack_has_a_price(tmp_path):
    db = _db(tmp_path)

    async def run():
        view, _ = await _batch(db, 7)
        modal = (await _pick(view, "custom")).response.sent_modal
        await _fill(modal, ["2000"] * 5)
        attempt = _Interaction(7)
        await view.confirm.callback(attempt)
        assert "2 of 7 stacks still need a custom price" in attempt.response.edits[0]["content"]
        assert view.resolved is False
        return await db.list_active_inventory_jobs(7)

    assert asyncio.run(run()) == []


def test_custom_prices_can_be_changed_and_another_strategy_drops_them(tmp_path):
    db = _db(tmp_path)

    async def run():
        view, _ = await _batch(db, 2)
        modal = (await _pick(view, "custom")).response.sent_modal
        await _fill(modal, ["2000", "2500"])
        assert view.price_button.label == "Change custom prices"

        click = _Interaction(7)
        await view.price_button.callback(click)
        assert [text_input.default for text_input in click.response.sent_modal.inputs] == ["2000", "2500"]

        await _pick(view, "undercut")
        assert view.pricing_strategy == "undercut" and view.custom_prices == {}
        assert view.price_button not in view.children

    asyncio.run(run())
