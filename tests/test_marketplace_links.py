"""Tests for the UEX Marketplace item-link helpers (bot/uex/marketplace.py) shared by
every cog that displays a Marketplace item name - see marketplace_item_link's docstring
for why some rows (e.g. /marketplace_negotiations) fall back to a plain, unlinked name."""
from __future__ import annotations

from bot.uex.marketplace import marketplace_item_link, marketplace_item_url


def test_marketplace_item_url_builds_the_canonical_page():
    assert marketplace_item_url(55) == "https://uexcorp.space/marketplace/home/?id_item=55&mode=list"


def test_marketplace_item_link_wraps_name_when_id_is_known():
    assert marketplace_item_link("Laranite", 55) == (
        "[Laranite](https://uexcorp.space/marketplace/home/?id_item=55&mode=list)"
    )


def test_marketplace_item_link_falls_back_to_plain_name_without_an_id():
    assert marketplace_item_link("Laranite", None) == "Laranite"
