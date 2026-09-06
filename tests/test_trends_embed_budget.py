"""A08: per-field/name truncation (1024/256 chars) doesn't protect Discord's separate,
combined 6000-character total-embed-text limit - many individually-legal route fields can
still sum well past it, and Discord rejects the ENTIRE send in that case, silently losing
every route, not just the overflow ones. These exercise _send_ranked_routes end-to-end
against a realistic warning-heavy fixture (stale health, illegal/volatile commodity risk,
missing practical-route infra) that pushes ten routes well past 6000 characters total even
though no single field is anywhere near its own 1024-char cap.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock

from bot.cogs.trends import Trends
from bot.uex.trends import ScoredRouteEntry


def _interaction():
    return NS(
        user=NS(id=1),
        response=NS(defer=AsyncMock()),
        followup=NS(send=AsyncMock()),
    )


def _make_cog(num_routes: int) -> tuple[Trends, object]:
    references = {
        i: dict(
            terminal_name=f"Terminal {i}", max_container_size=8, has_freight_elevator=0,
            has_loading_dock=0, is_player_owned=1, is_refuel=1, is_repair=1, is_cargo_center=1,
        )
        for i in range(1, 2 * num_routes + 1)
    }
    health = {
        i: dict(
            terminal_name=f"Terminal {i}", last_update_days=5, last_update_days_limit=3,
            last_update_days_percentage=0, prices_updated_percentage=0,
        )
        for i in range(1, 2 * num_routes + 1)
    }
    risk = dict(is_illegal=1, is_explosive=1, is_volatile_time=1, is_volatile_qt=1, is_buggy=1)
    db = NS(
        get_default_ship=AsyncMock(return_value="Ship"),
        get_terminal_references_by_ids=AsyncMock(return_value=references),
        get_terminal_data_health_by_ids=AsyncMock(return_value=health),
        get_route_market_signals_by_ids=AsyncMock(return_value={}),
        get_commodity_references=AsyncMock(return_value={i: risk for i in range(1, num_routes + 1)}),
    )
    uex = NS(
        get_vehicles=AsyncMock(return_value=[dict(name="Ship", scu=100)]),
        get_commodities_status=AsyncMock(return_value={}),
    )
    cog = Trends.__new__(Trends)
    cog.bot = NS(db=db, uex=uex)
    return cog, db


def _routes(num_routes: int) -> list[ScoredRouteEntry]:
    return [
        ScoredRouteEntry(
            commodity_name=f"Commodity {i}", id_commodity=i,
            origin_terminal_name=f"Origin {i}", destination_terminal_name=f"Destination {i}",
            price_origin=100, price_destination=200, price_margin=50, price_roi=100,
            distance=10, score=100, scu_origin=100, scu_destination=100, status_origin=1,
            status_destination=1, origin_terminal_id=2 * i - 1, destination_terminal_id=2 * i,
        )
        for i in range(1, num_routes + 1)
    ]


def test_warning_heavy_routes_fit_the_total_embed_limit(tmp_path):
    async def run():
        cog, _ = _make_cog(10)
        inter = _interaction()
        await cog._send_ranked_routes(
            inter, entries=_routes(10), updated_at=None, ship=None,
            title="Top routes", footer_note="Collected data", log_label="test", display_limit=10,
        )
        embed = inter.followup.send.call_args.kwargs["embed"]
        assert len(embed) <= 6000, (len(embed), len(embed.fields))

    asyncio.run(run())


def test_truncated_routes_are_disclosed_not_silently_dropped(tmp_path):
    """Whatever routes don't fit must be visibly noted, not just quietly absent - a user
    comparing "/top-routes said 10" against "the embed only shows 6" needs to know why."""
    async def run():
        cog, _ = _make_cog(10)
        inter = _interaction()
        await cog._send_ranked_routes(
            inter, entries=_routes(10), updated_at=None, ship=None,
            title="Top routes", footer_note="Collected data", log_label="test", display_limit=10,
        )
        embed = inter.followup.send.call_args.kwargs["embed"]
        route_fields = len(embed.fields)
        if route_fields < 10:
            assert "omitted" in (embed.footer.text or "").lower()

    asyncio.run(run())


def test_a_small_number_of_routes_is_never_truncated():
    """Regression guard: the budget must not kick in for an ordinary, small result set."""
    async def run():
        cog, _ = _make_cog(2)
        inter = _interaction()
        await cog._send_ranked_routes(
            inter, entries=_routes(2), updated_at=None, ship=None,
            title="Top routes", footer_note="Collected data", log_label="test", display_limit=10,
        )
        embed = inter.followup.send.call_args.kwargs["embed"]
        assert len(embed.fields) == 2
        assert "omitted" not in (embed.footer.text or "").lower()

    asyncio.run(run())


def test_route_budget_accounts_for_the_final_footer():
    """Follow-up review finding: _add_chunked_fields reserved only 100 characters while
    packing fields, but the real footer (explanation + refresh timestamp + ship note) is
    attached AFTER the loop and can be much longer than that reserve - confirmed to let the
    final assembled embed land at 6,009 characters (254-character footer) with a
    longer-terminal-name fixture. The fix sets the real footer BEFORE the loop so the
    budget check already accounts for it. Sweeping terminal-name padding from 0 to 150
    chars covers the exact boundary where the original bug only showed up near the limit."""
    async def run():
        for padding in range(0, 151):
            cog, _ = _make_cog(10)
            entries = _routes(10)
            for entry in entries:
                entry.origin_terminal_name += "x" * padding
                entry.destination_terminal_name += "x" * padding
            inter = _interaction()
            await cog._send_ranked_routes(
                inter, entries=entries, updated_at=datetime.now(timezone.utc),
                ship=None, title="Top routes", footer_note="Collected data",
                log_label="test", display_limit=10,
            )
            embed = inter.followup.send.call_args.kwargs["embed"]
            assert len(embed) <= 6000, (padding, len(embed), len(embed.footer.text), len(embed.fields))

    asyncio.run(run())


def test_top_routes_never_shows_a_route_without_its_risk_warning():
    """Follow-up review finding: _add_chunked_fields' old per-chunk budget check could add
    a route's first chunk and then refuse its second, leaving the route visible with its
    trailing cargo-risk warning silently missing - a visible route with no visible warning
    reads as "checked and safe." The fix (see test_prices_chunked_fields.py) makes adding a
    logical field all-or-nothing; this confirms the effect end-to-end: every route field
    that IS shown carries its risk warning, across a sweep of terminal-name paddings."""
    async def run():
        for padding in range(0, 101, 10):
            cog, db = _make_cog(10)
            db.get_default_ship.return_value = "RSI Constellation Taurus"
            cog.bot.uex.get_vehicles.return_value = [dict(name="RSI Constellation Taurus", scu=100)]
            cog.bot.uex.get_commodities_status.return_value = {
                "buy": [dict(code=1, name_short="High Supply")],
                "sell": [dict(code=1, name_short="Low Inventory")],
            }
            entries = _routes(10)
            for entry in entries:
                entry.origin_terminal_name += "x" * padding
                entry.destination_terminal_name += "x" * padding
                entry.price_origin = 5000
                entry.price_destination = 25000
            inter = _interaction()
            await cog._send_ranked_routes(
                inter, entries=entries, updated_at=None, ship=None,
                title="Top routes", footer_note="Collected data", log_label="test", display_limit=10,
            )
            embed = inter.followup.send.call_args.kwargs["embed"]
            groups: dict[str, str] = {}
            for field in embed.fields:
                route_number = field.name.split(".")[0]
                groups[route_number] = groups.get(route_number, "") + field.value
            assert all("Cargo risk:" in text for text in groups.values()), (
                padding, [(number, "Cargo risk:" in text) for number, text in groups.items()],
            )

    asyncio.run(run())
