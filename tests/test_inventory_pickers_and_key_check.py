"""Audit UX-10 and UX-11.

- UX-10: six inventory commands took a raw stack or job number that the player had to copy
  out of /inventory or an old DM first. Each now autocompletes the player's own stacks, or
  only the jobs that command can act on.
- UX-11: /link-uex-account saved any key without checking it, so a wrong one "linked" and
  only failed later, in whichever command used it first. And linking silently put the
  player on the server's public /leaderboard. The key is now checked with UEX first, the
  confirmation names the UEX account, and the leaderboard is disclosed.
"""
import asyncio
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock

import httpx
import pytest
from cryptography.fernet import Fernet

from bot.cogs.account import Account, LinkUexModal
from bot.cogs.personal_inventory import (
    PersonalInventory,
    cancel_post_job_autocomplete,
    confirm_sale_job_autocomplete,
    floor_job_autocomplete,
    inventory_stack_autocomplete,
)
from bot.db.database import Database
from bot.uex.client import UexClient
from bot.uex.exceptions import UexApiError, UexAuthError

# ---- UX-10: pick a stack or job instead of typing its number ------------------------------


@pytest.mark.parametrize("command, param, autocomplete", [
    ("inventory_set_minimum", "inventory_id", inventory_stack_autocomplete),
    ("inventory_remove", "inventory_id", inventory_stack_autocomplete),
    ("inventory_post_now", "inventory_id", inventory_stack_autocomplete),
    ("inventory_confirm_sale", "job_id", confirm_sale_job_autocomplete),
    ("inventory_cancel_post", "job_id", cancel_post_job_autocomplete),
    ("inventory_resolve_floor", "job_id", floor_job_autocomplete),
])
def test_every_inventory_command_that_takes_a_number_offers_a_list(command, param, autocomplete):
    assert getattr(PersonalInventory, command)._params[param].autocomplete is autocomplete


def _typing(user_id, db):
    return NS(user=NS(id=user_id), client=NS(db=db))


def test_the_stack_list_shows_only_the_players_own_stacks_and_filters_as_they_type(tmp_path):
    async def run():
        db = Database(tmp_path / "stacks.sqlite3", Fernet(Fernet.generate_key()))
        await db.init()
        laranite = await db.add_inventory_item(user_id=7, id_item=1, id_category=2, item_name="Laranite", item_slug=None,
                                               quantity=32, quality=650, location="Area18", minimum_price=100)
        gold = await db.add_inventory_item(user_id=7, id_item=3, id_category=2, item_name="Gold", item_slug=None,
                                           quantity=5, quality=0, location="Lorville", minimum_price=None)
        await db.add_inventory_item(user_id=8, id_item=3, id_category=2, item_name="Gold", item_slug=None,
                                    quantity=9, quality=0, location="Orison", minimum_price=None)
        async with db.connect() as con:
            await con.execute("UPDATE personal_inventory SET reserved_quantity = 8 WHERE id = ?", (laranite,))
            await con.commit()
        everything = await inventory_stack_autocomplete(_typing(7, db), "")
        typed_name = await inventory_stack_autocomplete(_typing(7, db), "lar")
        typed_number = [await inventory_stack_autocomplete(_typing(7, db), typed) for typed in (f"{gold}", f"#{gold}")]
        return laranite, gold, everything, typed_name, typed_number

    laranite, gold, everything, typed_name, typed_number = asyncio.run(run())
    assert [(c.name, c.value) for c in everything] == [
        (f"#{gold} Gold · ×5 · Lorville", gold),
        (f"#{laranite} Laranite · q650 · ×32 (8 listed) · Area18", laranite),
    ]
    assert [c.value for c in typed_name] == [laranite]
    # A typed number is the stack's own number - not the "32" in Laranite's ×32.
    assert [[c.value for c in choices] for choices in typed_number] == [[gold], [gold]]


def test_each_job_list_offers_only_the_jobs_its_command_can_act_on():
    jobs = [
        {"id": 1, "inventory_id": 10, "quantity": 4, "status": "pending", "auto_relist": 1},
        {"id": 2, "inventory_id": 10, "quantity": 4, "status": "listed", "auto_relist": 1},
        {"id": 3, "inventory_id": 11, "quantity": 2, "status": "listed", "auto_relist": 0},
        {"id": 4, "inventory_id": 11, "quantity": 2, "status": "needs_confirmation", "auto_relist": 1},
        {"id": 5, "inventory_id": 11, "quantity": 2, "status": "posting", "auto_relist": 1},
    ]
    db = NS(list_active_inventory_jobs=AsyncMock(return_value=jobs),
            list_inventory=AsyncMock(return_value=[{"id": 10, "item_name": "Gold"}, {"id": 11, "item_name": "Laranite"}]))

    def offered(autocomplete, typed=""):
        return [(c.value, c.name) for c in asyncio.run(autocomplete(_typing(7, db), typed))]

    assert offered(confirm_sale_job_autocomplete) == [(4, "#4 Laranite ×2 · needs confirming")]
    assert [value for value, _ in offered(cancel_post_job_autocomplete)] == [1, 2, 3, 4]
    assert offered(floor_job_autocomplete) == [(3, "#3 Laranite ×2 · listed")], "paused at its floor"
    assert [value for value, _ in offered(cancel_post_job_autocomplete, "gold")] == [1, 2]


# ---- UX-11: check the key, name the account, disclose the leaderboard -----------------------

def _link(profile=None, error=None):
    async def run():
        db = NS(set_user_secret_key=AsyncMock())
        uex = NS(get_user_profile=AsyncMock(return_value=profile, side_effect=error))
        modal = LinkUexModal(NS(db=db, uex=uex))
        modal.secret_key_input._value = "  secret-key  "
        interaction = NS(user=NS(id=7), response=NS(defer=AsyncMock()), followup=NS(send=AsyncMock()))
        await modal.on_submit(interaction)
        return db, uex, interaction.followup.send.await_args

    return asyncio.run(run())


def test_a_key_uex_rejects_is_not_linked():
    db, uex, reply = _link(error=UexAuthError("invalid_secret_key"))
    uex.get_user_profile.assert_awaited_once_with("secret-key")
    db.set_user_secret_key.assert_not_awaited()
    assert reply.args[0].startswith("UEX didn't accept that key, so nothing was linked.")
    assert reply.kwargs["ephemeral"] is True


def test_an_accepted_key_is_linked_under_its_uex_name_with_the_leaderboard_disclosed():
    db, _, reply = _link(profile={"username": "star_*pilot*", "name": "Star Pilot"})
    db.set_user_secret_key.assert_awaited_once_with(7, "secret-key")
    text = reply.args[0]
    assert text.startswith("Your UEX account is linked as **star\\_\\*pilot\\***.")
    assert "puts you on this server's /leaderboard" in text and "/unlink-uex-account takes you off" in text


def test_a_key_that_cant_be_checked_because_uex_is_down_is_linked_with_a_warning():
    db, _, reply = _link(error=UexApiError("503"))
    db.set_user_secret_key.assert_awaited_once_with(7, "secret-key")
    assert "UEX couldn't be reached to check the key" in reply.args[0]


def test_account_status_discloses_the_leaderboard():
    async def run():
        cog = Account.__new__(Account)
        cog.bot = NS(db=NS(has_linked_uex_account=AsyncMock(return_value=True)))
        interaction = NS(user=NS(id=7), response=NS(send_message=AsyncMock()))
        await Account.uex_account_status.callback(cog, interaction)
        return interaction.response.send_message.await_args.args[0]

    assert "/leaderboard" in asyncio.run(run())


@pytest.mark.parametrize("payload, expected", [
    ({"status": "ok", "data": {"username": "pilot"}}, {"username": "pilot"}),
    ({"status": "ok", "data": [{"username": "pilot"}]}, {"username": "pilot"}),
    ({"status": "invalid_secret_key", "data": None}, UexAuthError),
])
def test_the_profile_lookup_sends_the_players_key_to_uex_user(payload, expected):
    seen = []

    def handler(request):
        seen.append((request.url.path, request.headers.get("secret-key")))
        return httpx.Response(200 if payload["status"] == "ok" else 401, json=payload)

    async def run():
        client = UexClient(app_token="test", base_url="https://uex.test")
        await client._client.aclose()
        client._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        try:
            return await client.get_user_profile("sk_player")
        finally:
            await client.aclose()

    if expected is UexAuthError:
        with pytest.raises(UexAuthError):
            asyncio.run(run())
    else:
        assert asyncio.run(run()) == expected
    assert seen == [("/user", "sk_player")]
