"""Picking a Marketplace listing by name instead of by id.

/marketplace-listing and /marketplace-delete-listing autocomplete the player's own listings
(found by the UEX username their linked key belongs to - the same name UEX puts on their
listings, checked live on 2026-10-01), and /marketplace-listing also their favorites and
open deals. /marketplace-search, /my-favorites and /my-negotiations put a "Show details
for..." menu under their results. Typing an id still works everywhere.
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock

import discord
import httpx
import pytest

import bot.autocomplete as autocomplete_module
from bot.cogs import marketplace as marketplace_module
from bot.cogs.marketplace import (
    ConfirmDeleteListingView,
    ListingDetailsView,
    Marketplace,
    any_listing_autocomplete,
    forget_listing_picks,
    own_listing_autocomplete,
)
from bot.uex.client import UexClient
from bot.uex.marketplace import (
    ListingChoice,
    listing_choice_label,
    listing_choices_from_rows,
    match_listing_choices,
    merge_listing_choices,
)

# Shaped like the live answers for the owner's account on 2026-10-01.
OWN_ROWS = [
    {"id": 175616, "title": "Oni Kiba Helmet - Q0 standard - 1 available", "price": "3594000", "currency": "UEC"},
    {"id": 175615, "title": "Ace Interceptor Helmet - Q0 standard - 1 available", "price": "1548000",
     "currency": "UEC"},
    {"id": 170683, "title": "05 comp board", "price": "2000000", "currency": "UEC"},
]
FAVORITE_ROWS = [
    {"id": 9, "id_listing": 168220, "title": "Pembroke Exploration Suit", "price": "95000", "currency": "UEC"},
]
DEAL_ROWS = [
    # A deal on the player's own listing: shows once, as theirs.
    {"id": 31, "id_listing": 175615, "listing_title": "Ace Interceptor Helmet", "price": "1500000",
     "currency": "UEC", "date_closed": None},
    {"id": 32, "id_listing": 171002, "listing_title": "Quantainium Crate", "price": "400000", "currency": "UEC",
     "date_closed": None},
    {"id": 33, "id_listing": 171002, "listing_title": "Quantainium Crate", "price": "380000", "currency": "UEC",
     "date_closed": None},
    {"id": 34, "id_listing": 150001, "listing_title": "Long Gone", "price": "1", "currency": "UEC",
     "date_closed": 1790000000},
]


@pytest.fixture(autouse=True)
def _fresh_pick_cache():
    marketplace_module._listing_picks_cache.clear()
    yield
    marketplace_module._listing_picks_cache.clear()


# -- the pure helpers (bot/uex/marketplace.py) -------------------------------------------

def test_choices_read_each_endpoints_own_listing_id():
    own = listing_choices_from_rows(OWN_ROWS, "yours")
    assert [(c.id, c.price, c.source) for c in own] == [
        (175616, 3594000.0, "yours"), (175615, 1548000.0, "yours"), (170683, 2000000.0, "yours")]
    # A favorite's or deal's own "id" is not the listing's: id_listing is.
    assert [c.id for c in listing_choices_from_rows(FAVORITE_ROWS, "favorite", id_key="id_listing")] == [168220]
    assert listing_choices_from_rows(DEAL_ROWS, "deal", id_key="id_listing")[1].title == "Quantainium Crate"
    assert listing_choices_from_rows([{"title": "no id"}, {"id": "0"}, {"id": None}], "yours") == []


def test_merge_keeps_each_listing_once_first_source_first():
    own = listing_choices_from_rows(OWN_ROWS, "yours")
    deals = listing_choices_from_rows(DEAL_ROWS, "deal", id_key="id_listing")
    merged = merge_listing_choices(own, deals)
    assert [(c.id, c.source) for c in merged] == [
        (175616, "yours"), (175615, "yours"), (170683, "yours"), (171002, "deal"), (150001, "deal")]


def test_labels_fit_discords_100_characters_and_keep_id_and_price():
    choice = listing_choices_from_rows(OWN_ROWS, "yours")[1]
    assert listing_choice_label(choice) == "#175615 Ace Interceptor Helmet - Q0 standard - 1 available · 1,548,000 UEC"
    assert listing_choice_label(choice, show_source=True).startswith("Yours · #175615 ")
    long = ListingChoice(1234567, "Extremely Long Title " * 10, 123456789.0, "UEC", "favorite")
    label = listing_choice_label(long, show_source=True)
    assert len(label) == 100
    assert label.startswith("Favorite · #1234567 Extremely") and label.endswith("… · 123,456,789 UEC")
    assert listing_choice_label(ListingChoice(5, "No price", None, "UEC", "yours")) == "#5 No price"


def test_matching_by_typed_id_or_by_words():
    choices = listing_choices_from_rows(OWN_ROWS, "yours")
    assert [i for _, i in match_listing_choices(choices, "")] == [175616, 175615, 170683]
    assert [i for _, i in match_listing_choices(choices, "1756")] == [175616, 175615]
    assert [i for _, i in match_listing_choices(choices, "#170")] == [170683]
    assert [i for _, i in match_listing_choices(choices, "HELMET")] == [175616, 175615]
    assert match_listing_choices(choices, "999") == []
    many = [ListingChoice(i, f"Item {i}", 1.0, "UEC", "yours") for i in range(1, 40)]
    assert len(match_listing_choices(many, "")) == 25


# -- UexClient.get_user_username ---------------------------------------------------------

def test_username_comes_from_the_linked_key_and_is_cached():
    requests = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"status": "ok", "data": {"username": "cheeno", "name": "cheeno"}})

    async def run():
        client = UexClient(app_token="test", base_url="https://uex.test")
        await client._client.aclose()
        client._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        try:
            first = await client.get_user_username("sk_one")
            second = await client.get_user_username("sk_one")
        finally:
            await client.aclose()
        return first, second

    assert asyncio.run(run()) == ("cheeno", "cheeno")
    assert len(requests) == 1, "the second keystroke reuses the cached username"
    assert requests[0].headers.get("secret-key") == "sk_one"


# -- the autocompletes -------------------------------------------------------------------

def _uex(**overrides):
    uex = NS(
        get_user_username=AsyncMock(return_value="cheeno"),
        get_marketplace_listings=AsyncMock(return_value=[dict(r) for r in OWN_ROWS]),
        get_marketplace_favorites=AsyncMock(return_value=[dict(r) for r in FAVORITE_ROWS]),
        get_marketplace_negotiations=AsyncMock(return_value=[dict(r) for r in DEAL_ROWS]),
    )
    for name, value in overrides.items():
        setattr(uex, name, value)
    return uex


def _autocomplete_interaction(uex, *, secret_key="sk_test", user_id=42):
    db = NS(get_user_secret_key=AsyncMock(return_value=secret_key))
    return NS(client=NS(db=db, uex=uex), user=NS(id=user_id))


def test_delete_lists_only_your_own_listings_by_name():
    uex = _uex()
    choices = asyncio.run(own_listing_autocomplete(_autocomplete_interaction(uex), ""))

    assert [(c.name, c.value) for c in choices] == [
        ("#175616 Oni Kiba Helmet - Q0 standard - 1 available · 3,594,000 UEC", 175616),
        ("#175615 Ace Interceptor Helmet - Q0 standard - 1 available · 1,548,000 UEC", 175615),
        ("#170683 05 comp board · 2,000,000 UEC", 170683),
    ]
    uex.get_marketplace_listings.assert_awaited_once_with(username="cheeno")
    uex.get_marketplace_favorites.assert_not_awaited()
    uex.get_marketplace_negotiations.assert_not_awaited()


def test_listing_lists_yours_then_favorites_then_open_deals_each_once():
    choices = asyncio.run(any_listing_autocomplete(_autocomplete_interaction(_uex()), ""))

    assert [c.value for c in choices] == [175616, 175615, 170683, 168220, 171002]
    assert choices[0].name.startswith("Yours · #175616")
    assert choices[3].name == "Favorite · #168220 Pembroke Exploration Suit · 95,000 UEC"
    assert choices[4].name.startswith("Deal · #171002 Quantainium Crate")
    assert all(len(c.name) <= 100 for c in choices)


def test_without_a_linked_account_there_is_nothing_to_list_but_an_id_can_still_be_typed():
    uex = _uex()
    assert asyncio.run(any_listing_autocomplete(_autocomplete_interaction(uex, secret_key=None), "175")) == []
    uex.get_user_username.assert_not_awaited()


def test_the_pick_list_is_reused_between_keystrokes_and_dropped_after_a_delete():
    async def run():
        uex = _uex()
        interaction = _autocomplete_interaction(uex)
        await own_listing_autocomplete(interaction, "")
        typed = await own_listing_autocomplete(interaction, "comp")
        assert [c.value for c in typed] == [170683]
        assert uex.get_marketplace_listings.await_count == 1
        forget_listing_picks(42)
        await own_listing_autocomplete(interaction, "")
        assert uex.get_marketplace_listings.await_count == 2

    asyncio.run(run())


def test_a_failed_source_still_lists_the_rest_but_is_not_kept():
    async def run():
        uex = _uex(get_marketplace_favorites=AsyncMock(side_effect=RuntimeError("UEX down")))
        interaction = _autocomplete_interaction(uex)
        first = await any_listing_autocomplete(interaction, "")
        assert [c.value for c in first] == [175616, 175615, 170683, 171002]
        await any_listing_autocomplete(interaction, "")
        assert uex.get_marketplace_favorites.await_count == 2, "an incomplete list is fetched again"

    asyncio.run(run())


def test_a_slow_source_is_left_out_rather_than_missing_discords_deadline(monkeypatch):
    monkeypatch.setattr(autocomplete_module, "AUTOCOMPLETE_BUDGET_SECONDS", 0.05)

    async def slow_favorites(**kwargs):
        await asyncio.sleep(0.5)
        return FAVORITE_ROWS

    async def run():
        uex = _uex(get_marketplace_favorites=slow_favorites)
        choices = await any_listing_autocomplete(_autocomplete_interaction(uex), "")
        assert 168220 not in [c.value for c in choices]
        assert marketplace_module._listing_picks_cache == {}
        await asyncio.sleep(0.6)  # let the background fetch finish before the loop closes

    asyncio.run(run())


# -- the "Show details for..." menu -----------------------------------------------------

def test_the_details_menu_offers_each_listing_once():
    deals = listing_choices_from_rows(DEAL_ROWS, "deal", id_key="id_listing")
    view = ListingDetailsView(NS(), deals)

    assert [o.value for o in view.select.options] == ["175615", "171002", "150001"]
    assert view.select.options[1].label == "Quantainium Crate"
    assert view.select.options[1].description == "#171002 · 400,000 UEC"
    assert view.timeout == marketplace_module.LISTING_DETAILS_TIMEOUT_SECONDS


def test_picking_a_listing_shows_its_details_privately():
    async def run():
        embed = discord.Embed(title="Quantainium Crate")
        cog = NS(listing_detail=AsyncMock(return_value=(embed, None)))
        view = ListingDetailsView(cog, listing_choices_from_rows(DEAL_ROWS, "deal", id_key="id_listing"))
        view.select._values = ["171002"]
        interaction = NS(response=NS(defer=AsyncMock()), followup=NS(send=AsyncMock()))

        await view.show_details(interaction)

        cog.listing_detail.assert_awaited_once_with(171002)
        interaction.response.defer.assert_awaited_once_with(ephemeral=True, thinking=True)
        interaction.followup.send.assert_awaited_once_with(embed=embed, ephemeral=True)

        cog.listing_detail = AsyncMock(return_value=(None, "No active listing found with id **171002**."))
        interaction.followup.send.reset_mock()
        await view.show_details(interaction)
        interaction.followup.send.assert_awaited_once_with("No active listing found with id **171002**.",
                                                           ephemeral=True)

    asyncio.run(run())


# -- the commands -----------------------------------------------------------------------

class _Followup:
    def __init__(self):
        self.sent = []

    async def send(self, *args, **kwargs):
        self.sent.append((args, kwargs))


def _command_interaction(user_id=42):
    return NS(user=NS(id=user_id), response=NS(defer=AsyncMock()), followup=_Followup())


def _cog(db=None, **uex):
    cog = Marketplace.__new__(Marketplace)
    cog.bot = NS(db=db or NS(get_user_secret_key=AsyncMock(return_value="sk_test")), uex=NS(**uex))
    return cog


def test_search_results_carry_a_details_menu():
    async def run():
        cog = _cog(get_item_catalog=AsyncMock(return_value=[]),
                   get_marketplace_listings=AsyncMock(return_value=[
                       {**OWN_ROWS[1], "operation": "sell", "in_stock": 1, "is_sold_out": 0, "user_username": "cheeno"}]))
        interaction = _command_interaction()
        await cog.marketplace_search.callback(cog, interaction, query="Ace Interceptor")
        return interaction.followup.sent

    (args, kwargs), = asyncio.run(run())
    assert isinstance(kwargs["view"], ListingDetailsView) and kwargs["wait"] is True
    assert [o.value for o in kwargs["view"].select.options] == ["175615"]
    assert kwargs["embed"].footer.text.endswith("pick one below for its full details")


def test_favorites_and_deals_carry_a_details_menu():
    async def run():
        cog = _cog(get_marketplace_favorites=AsyncMock(return_value=FAVORITE_ROWS),
                   get_marketplace_negotiations=AsyncMock(return_value=DEAL_ROWS),
                   get_marketplace_listings=AsyncMock(return_value=[]))
        favorites, deals = _command_interaction(), _command_interaction()
        await cog.my_favorites.callback(cog, favorites)
        await cog.my_negotiations.callback(cog, deals)
        return favorites.followup.sent[-1], deals.followup.sent[-1]

    (fav_args, fav_kwargs), (deal_args, deal_kwargs) = asyncio.run(run())
    assert [o.value for o in fav_kwargs["view"].select.options] == ["168220"]
    assert [o.value for o in deal_kwargs["view"].select.options] == ["175615", "171002", "150001"]
    assert fav_args[0].endswith("Pick one below for its full details.")
    assert deal_args[0].endswith("Pick one below for its full details.")


def test_a_list_with_nothing_to_pick_goes_out_without_a_menu():
    """Discord refuses a menu with no options - the whole reply would fail."""
    async def run():
        cog = _cog(get_marketplace_favorites=AsyncMock(return_value=[{"id": 9, "title": "No listing id"}]),
                   get_marketplace_listings=AsyncMock(return_value=[]))
        interaction = _command_interaction()
        await cog.my_favorites.callback(cog, interaction)
        return interaction.followup.sent

    (args, kwargs), = asyncio.run(run())
    assert "view" not in kwargs and "No listing id" in args[0]


def test_marketplace_listing_takes_the_picked_listing():
    async def run():
        cog = _cog(get_marketplace_listings=AsyncMock(return_value=[
            {**OWN_ROWS[2], "operation": "sell", "in_stock": 1, "user_username": "cheeno"}]))
        interaction = _command_interaction()
        await cog.marketplace_listing.callback(cog, interaction, 170683)
        cog.bot.uex.get_marketplace_listings.assert_awaited_once_with(id=170683, use_cache=False)
        return interaction.followup.sent

    (_, kwargs), = asyncio.run(run())
    assert kwargs["embed"].title == "05 comp board"
    assert kwargs["embed"].footer.text == "Listing #170683 · UEX Marketplace"


def test_a_delete_drops_the_players_cached_pick_list():
    marketplace_module._listing_picks_cache[(1, False)] = (float("inf"), [])
    marketplace_module._listing_picks_cache[(1, True)] = (float("inf"), [])
    marketplace_module._listing_picks_cache[(2, False)] = (float("inf"), [])

    async def run():
        db = NS(get_inventory_post_job_by_listing=AsyncMock(return_value=None),
                cancel_tracked_inventory_listing=AsyncMock(return_value=False))
        view = ConfirmDeleteListingView(NS(db=db, uex=NS(delete_marketplace_listing=AsyncMock())), 175615, "sk", 1)
        interaction = NS(user=NS(id=1), response=NS(edit_message=AsyncMock()), followup=NS(send=AsyncMock()))
        await view.confirm.callback(interaction)

    asyncio.run(run())
    assert list(marketplace_module._listing_picks_cache) == [(2, False)]


def test_a_delete_uex_still_shows_changes_nothing_and_says_so():
    async def run():
        db = NS(get_inventory_post_job_by_listing=AsyncMock(return_value=None),
                cancel_tracked_inventory_listing=AsyncMock(return_value=True))
        uex = NS(delete_marketplace_listing=AsyncMock(return_value=False))
        view = ConfirmDeleteListingView(NS(db=db, uex=uex), 175615, "sk", 1)
        interaction = NS(user=NS(id=1), response=NS(edit_message=AsyncMock()), followup=NS(send=AsyncMock()))
        await view.confirm.callback(interaction)
        return db, interaction

    db, interaction = asyncio.run(run())
    db.cancel_tracked_inventory_listing.assert_not_awaited()
    assert "still shows listing #175615" in interaction.followup.send.await_args.args[0]


def test_both_commands_pick_by_name_within_discords_limits():
    for command in (Marketplace.marketplace_listing, Marketplace.marketplace_delete_listing):
        (param,) = command.parameters
        assert param.name == "listing" and param.autocomplete
        assert len(str(param.description)) <= 100 and len(str(command.description)) <= 100
