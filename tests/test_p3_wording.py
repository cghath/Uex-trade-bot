"""Small wording fixes from the audit's P3 list (MSG-15, MSG-17 to MSG-20, UX-17)."""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock

from bot.cogs.diagnostics import Diagnostics
from bot.cogs.digest import Digest, _format_data_freshness
from bot.cogs.liquidity import LiquidityCog
from bot.cogs.ship_parts_finder import ShipPartsFinder
from bot.cogs.trends import _build_route_field
from bot.uex.item_finder import place_and_vendor_text
from bot.uex.ship_part_display import shop_text
from bot.uex.supply_demand import EvidenceLevel
from bot.uex.trends import ScoredRouteEntry
from bot.wiki_api import WikiApiError, WikiUnavailableError


def test_freshness_says_just_now_not_just_now_ago():  # MSG-17
    value = _format_data_freshness({"liquidity": "2026-08-25 15:00:00"},
                                   now=datetime(2026, 8, 25, 15, 0, 20, tzinfo=timezone.utc))
    assert "**Sellability Ratings:** just now" in value and "just now ago" not in value


def test_distances_are_written_in_gm():  # MSG-15
    entry = ScoredRouteEntry(
        commodity_name="Gold", id_commodity=1, origin_terminal_name="A", destination_terminal_name="B",
        price_origin=100, price_destination=200, price_margin=50, price_roi=100, distance=12.34, score=1,
        scu_origin=10, scu_destination=10, status_origin=1, status_destination=1,
        origin_terminal_id=1, destination_terminal_id=2,
    )
    evidence = EvidenceLevel(tier="current", quantity_scu=10)
    _name, value = _build_route_field(1, entry, None, None, {}, evidence, evidence)
    assert "12.3 Gm" in value and "GM" not in value


def test_the_rating_is_called_the_sellability_rating():  # MSG-18
    async def run():
        cog = LiquidityCog.__new__(LiquidityCog)
        cog.db = NS(get_top_liquidity_items=AsyncMock(return_value=[]))
        interaction = NS(response=NS(send_message=AsyncMock()))
        await LiquidityCog.liquidity_rank.callback(cog, interaction)
        return interaction.response.send_message.call_args.args[0]

    reply = asyncio.run(run())
    assert "Sellability Ratings" in reply and "liquidity" not in reply.lower()


def test_every_shop_command_names_a_shop_the_same_way():  # MSG-19
    assert place_and_vendor_text("GrimHEX", "Skutters", bold=True) == "**GrimHEX** (Skutters)"
    assert place_and_vendor_text("Pyro Gateway (Stanton)", "Ship Weapons", bold=True) == \
        "Ship Weapons at **Pyro Gateway (Stanton)**"
    assert place_and_vendor_text("Checkmate", None) == "Checkmate"
    assert shop_text("Ship Weapons - Pyro Gateway (Stanton)") == "Ship Weapons at Pyro Gateway (Stanton)"


def test_a_ship_slot_lookup_failure_never_shows_raw_error_text():  # MSG-20
    async def reply(error: Exception) -> str:
        cog = ShipPartsFinder.__new__(ShipPartsFinder)
        cog._ports_for_vehicle = AsyncMock(side_effect=error)
        return await cog._build_browser({"id": 1, "name": "Perseus"}, (5, "Area18"))

    down = asyncio.run(reply(WikiUnavailableError("HTTP 503 after 3 retries")))
    missing = asyncio.run(reply(WikiApiError("identity mismatch: uuid abc123")))
    assert "Couldn't reach the Star Citizen Wiki" in down and "503" not in down
    assert "doesn't list usable component slots" in missing and "abc123" not in missing


def test_server_settings_are_hidden_from_members_who_cant_use_them():  # UX-17
    for command in (Digest.set_digest_channel, Digest.digest_disable, Diagnostics.command_usage):
        assert command.default_permissions is not None and command.default_permissions.manage_guild, command.name
    assert Digest.digest_now.default_permissions is None, "posting the digest stays open to everyone"
