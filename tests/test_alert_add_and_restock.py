"""Audit UX-5, UX-1 and REL-8 (for the two alert-add commands).

- UX-5: /alert-add and /stock-alert-add saved whatever commodity text was typed, so a typo
  made an alert that could never fire. The name is now resolved against UEX's list and
  the canonical name saved; a typo is refused with suggestions.
- REL-8: both wrote to the DB before responding, so a slow DB could make a player see
  "did not respond", retry, and get a duplicate. Both now defer first.
- UX-1: a stock alert's check sent one ping per newly stocked terminal - on its first
  check, every terminal already in stock. Now it's one message per alert per check.
"""
import asyncio
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, MagicMock

from bot.cogs.alerts import Alerts
from bot.cogs.stock_alerts import StockAlerts
from bot.uex.exceptions import UexApiError
from bot.uex.stock_alerts import RESTOCK_MESSAGE_MAX_TERMINALS, format_restock_message
from bot.uex.trading import resolve_tradeable_commodity, suggest_commodity_names, unknown_commodity_message

COMMODITIES = [
    {"name": "Gold", "is_buyable": 1, "is_sellable": 1},
    {"name": "Laranite", "is_buyable": 1, "is_sellable": 1},
    {"name": "Laranite (Raw)", "is_buyable": 0, "is_sellable": 1},
    {"name": "Agricium", "is_buyable": 1, "is_sellable": 1},
    {"name": "Agricultural Supplies", "is_buyable": 1, "is_sellable": 1},
    {"name": "Jaclium (Ore)", "is_buyable": 0, "is_sellable": 0},
]


# ---- resolving a typed name -----------------------------------------------------------------

def test_an_exact_name_resolves_whatever_its_case():
    assert resolve_tradeable_commodity(COMMODITIES, "  gOLD ")["name"] == "Gold"
    assert resolve_tradeable_commodity(COMMODITIES, "laranite")["name"] == "Laranite", "exact beats a longer match"


def test_a_unique_partial_name_resolves_and_an_ambiguous_one_does_not():
    assert resolve_tradeable_commodity(COMMODITIES, "supplies")["name"] == "Agricultural Supplies"
    assert resolve_tradeable_commodity(COMMODITIES, "agri") is None


def test_typos_and_untradeable_commodities_do_not_resolve():
    assert resolve_tradeable_commodity(COMMODITIES, "Goldd") is None
    assert resolve_tradeable_commodity(COMMODITIES, "Jaclium") is None
    assert resolve_tradeable_commodity(COMMODITIES, "   ") is None


def test_suggestions_cover_both_an_ambiguous_name_and_a_misspelling():
    assert suggest_commodity_names(COMMODITIES, "agri") == ["Agricium", "Agricultural Supplies"]
    assert suggest_commodity_names(COMMODITIES, "Laranit") == ["Laranite", "Laranite (Raw)"]
    assert suggest_commodity_names(COMMODITIES, "Laranate")[0] == "Laranite", "a misspelling gets the nearest name"
    assert "**Gold**" in unknown_commodity_message(COMMODITIES, "Goldd")
    assert "Did you mean" not in unknown_commodity_message(COMMODITIES, "zzzzqqq")


# ---- the two add commands -------------------------------------------------------------------

def _interaction(order):
    async def defer(**kwargs):
        order.append(("defer", kwargs))

    async def send(content=None, **kwargs):
        order.append(("send", content))

    return NS(guild_id=1, channel_id=2, user=NS(id=3), response=NS(defer=defer),
              followup=NS(send=send))


def _bot(order, *, commodities=COMMODITIES, uex_error=None):
    async def get_commodities():
        order.append(("get_commodities", None))
        if uex_error:
            raise uex_error
        return commodities

    async def add_price_alert(**kwargs):
        order.append(("add_price_alert", kwargs))
        return 41

    async def add_stock_alert(**kwargs):
        order.append(("add_stock_alert", kwargs))
        return 42

    return NS(uex=NS(get_commodities=get_commodities),
              db=NS(add_price_alert=add_price_alert, add_stock_alert=add_stock_alert))


def _alert_add(commodity, **bot_kwargs):
    order = []
    cog = Alerts.__new__(Alerts)
    cog.bot = _bot(order, **bot_kwargs)
    asyncio.run(Alerts.alert_add.callback(cog, _interaction(order), commodity,
                                          NS(value="sell_at_least"), 5000.0))
    return order


def _stock_alert_add(commodity, scope=None, **bot_kwargs):
    order = []
    cog = StockAlerts.__new__(StockAlerts)
    cog.bot = _bot(order, **bot_kwargs)
    asyncio.run(StockAlerts.stock_alert_add.callback(cog, _interaction(order), commodity, None, scope))
    return order


def test_price_alert_saves_the_canonical_name_and_responds_before_any_slow_work():
    order = _alert_add("gold")
    assert [step for step, _ in order] == ["defer", "get_commodities", "add_price_alert", "send"]
    assert order[2][1]["commodity_name"] == "Gold"
    assert "**Gold**" in order[3][1]


def test_price_alert_refuses_a_typo_without_saving_anything():
    order = _alert_add("Goldd")
    assert "add_price_alert" not in [step for step, _ in order]
    assert "Did you mean **Gold**" in order[-1][1]


def test_price_alert_is_not_saved_when_uex_cant_check_the_name():
    order = _alert_add("Gold", uex_error=UexApiError("503"))
    assert "add_price_alert" not in [step for step, _ in order]
    assert "no alert was set" in order[-1][1]


def test_stock_alert_saves_the_canonical_name_and_keeps_a_personal_reply_private():
    order = _stock_alert_add("laranite", scope=NS(value="personal"))
    assert [step for step, _ in order] == ["defer", "get_commodities", "add_stock_alert", "send"]
    assert order[0][1] == {"ephemeral": True}
    assert order[2][1]["commodity_name"] == "Laranite" and order[2][1]["scope"] == "personal"


def test_stock_alert_refuses_a_typo_without_saving_anything():
    order = _stock_alert_add("Laranit")
    assert order[0] == ("defer", {"ephemeral": True}), "a DM alert (the default) replies privately"
    assert "add_stock_alert" not in [step for step, _ in order]
    assert "Couldn't find a tradeable commodity called **Laranit**" in order[-1][1]


# ---- one restock message per alert per check ------------------------------------------------

def _terminal(i, price, scu=100, name=None):
    return {"id_terminal": i, "terminal_name": name or f"Terminal {i}", "price_buy": price, "scu_buy": scu}


def test_a_single_restock_keeps_the_familiar_wording():
    text = format_restock_message(7, "Gold", [_terminal(1, 55.0)], 96)
    assert text.startswith("stock alert #7: **Gold** is back in stock at **Terminal 1** — 55.00 aUEC/unit")
    assert "fills your full 96 SCU hold" in text


def test_many_restocks_become_one_message_cheapest_first_with_the_rest_counted():
    terminals = [_terminal(i, 100.0 - i) for i in range(1, 15)]  # 14 terminals, later ones cheaper
    text = format_restock_message(7, "Gold", terminals, None)
    lines = text.splitlines()
    assert lines[0] == "stock alert #7: **Gold** is in stock at 14 terminals:"
    assert lines[1].startswith("• **Terminal 14** — 86.00") and lines[RESTOCK_MESSAGE_MAX_TERMINALS].startswith("• **Terminal 5**")
    assert "…and 4 more." in lines
    assert text.count("/set-trading-preferences") == 1, "the no-ship hint appears once, not on every line"


def test_a_known_ship_puts_the_cargo_fit_on_each_line():
    text = format_restock_message(7, "Gold", [_terminal(1, 10.0, scu=50), _terminal(2, 11.0, scu=500)], 96)
    assert "(fills 50 of your 96 SCU hold)" in text and "(fills your full 96 SCU hold)" in text
    assert "/set-trading-preferences" not in text


def test_the_combined_message_stays_well_inside_discords_limit():
    long_name = "Shubin Mining Facility SMCa-6 - Hurston - Aberdeen - Klescher Rehabilitation Facility Annex"
    terminals = [_terminal(i, 1_234_567.89, scu=123_456, name=f"{long_name} {i}") for i in range(40)]
    assert len(format_restock_message(123456, "Recycled Material Composite", terminals, 98_304)) < 1900


def _poller(send_error=None):
    alert = {"id": 7, "user_id": 10, "channel_id": 5, "commodity_name": "Gold", "scope": "personal",
             "ship_query": None}
    db = NS(list_active_stock_alerts=AsyncMock(return_value=[alert]),
            get_stock_alert_terminal_state=AsyncMock(return_value={}),
            upsert_stock_alert_terminal_state=AsyncMock(),
            get_default_ship=AsyncMock(return_value=None))
    uex = NS(get_commodities_prices=AsyncMock(return_value=[
        {"id_terminal": i, "terminal_name": f"T{i}", "price_buy": 5 + i, "scu_buy": 100} for i in range(1, 6)
    ]))
    user = NS(sent=[])

    async def send(content=None, **kwargs):
        user.sent.append(content)
        if send_error is not None:
            raise send_error

    user.send = send
    cog = StockAlerts.__new__(StockAlerts)
    cog.bot = NS(get_channel=MagicMock(return_value=None), get_user=MagicMock(return_value=user),
                 fetch_user=AsyncMock(return_value=user), db=db, uex=uex)
    asyncio.run(cog._poll_stock_alerts_once())
    return user, sorted(call.args[1] for call in db.upsert_stock_alert_terminal_state.await_args_list)


def test_a_first_check_with_five_stocked_terminals_sends_one_message():
    user, saved = _poller()
    assert len(user.sent) == 1 and "is in stock at 5 terminals" in user.sent[0]
    assert saved == [1, 2, 3, 4, 5]


def test_an_unsent_combined_message_leaves_every_terminal_to_retry():
    import discord
    user, saved = _poller(send_error=discord.HTTPException(NS(status=503, reason="x"), {"message": "x"}))
    assert len(user.sent) == 1 and saved == []
