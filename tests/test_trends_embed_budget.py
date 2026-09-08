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
        get_terminal_market_observations_by_ids=AsyncMock(return_value={}),
        get_commodity_references=AsyncMock(return_value={i: risk for i in range(1, num_routes + 1)}),
        get_route_progression_track_record=AsyncMock(return_value={}),
    )
    uex = NS(
        get_vehicles=AsyncMock(return_value=[dict(name="Ship", scu=100)]),
        get_commodities_status=AsyncMock(return_value={}),
    )
    cog = Trends.__new__(Trends)
    cog.bot = NS(db=db, uex=uex, get_cog=lambda name: None)
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


def _route_embed_calls(mock) -> list:
    """Every route now sends as its own message (see /best-route's identical per-route-
    message restructuring in prices.py) - the first embed-bearing call is always the intro
    (no fields, just title/footer), so route embeds are every embed call AFTER that one."""
    embed_calls = [call for call in mock.call_args_list if call.kwargs.get("embed") is not None]
    return embed_calls[1:]


def _plain_messages(mock) -> list[str]:
    return [call.args[0] for call in mock.call_args_list if call.args]


def test_warning_heavy_routes_fit_the_total_embed_limit(tmp_path):
    async def run():
        cog, _ = _make_cog(10)
        inter = _interaction()
        await cog._send_ranked_routes(
            inter, entries=_routes(10), updated_at=None, ship=None,
            title="Top routes", footer_note="Collected data", log_label="test", display_limit=10,
        )
        for call in inter.followup.send.call_args_list:
            embed = call.kwargs.get("embed")
            if embed is not None:
                assert len(embed) <= 6000, (len(embed), len(embed.fields))

    asyncio.run(run())


def test_truncated_routes_are_disclosed_not_silently_dropped(tmp_path):
    """Whatever routes don't fit must be visibly noted, not just quietly absent - a user
    comparing "/top-routes said 10" against "only 6 route messages arrived" needs to know
    why. Each route embed that fails to fit its own message is skipped (not sent at all),
    and a trailing plain-text message discloses the count."""
    async def run():
        cog, _ = _make_cog(10)
        inter = _interaction()
        await cog._send_ranked_routes(
            inter, entries=_routes(10), updated_at=None, ship=None,
            title="Top routes", footer_note="Collected data", log_label="test", display_limit=10,
        )
        route_count = len(_route_embed_calls(inter.followup.send))
        if route_count < 10:
            assert any("omitted" in msg.lower() for msg in _plain_messages(inter.followup.send))

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
        assert len(_route_embed_calls(inter.followup.send)) == 2
        assert not any("omitted" in msg.lower() for msg in _plain_messages(inter.followup.send))

    asyncio.run(run())


def test_top_routes_evidence_levels_distinguish_zero_unknown_and_inferred():
    """Evidence-Level Labels: /top-routes used to render nothing at all for a missing
    scu_origin/scu_destination figure, visually identical to omitting a confirmed-zero
    figure for space. Three routes for the same commodity, one per tier: a live 0 (must
    read as a real report, not silence), a genuinely unknown figure with no history at
    all, and a missing figure backed by enough collected history to infer from."""
    async def run():
        cog, db = _make_cog(3)
        db.get_terminal_market_observations_by_ids = AsyncMock(return_value={
            (3, 6): [
                dict(observed_at="2020-01-01 00:00:00", price_buy=10, scu_buy=50,
                     price_sell=0, scu_sell=0, status_sell=None),
                dict(observed_at="2020-01-02 00:00:00", price_buy=10, scu_buy=0,
                     price_sell=0, scu_sell=0, status_sell=None),
            ],
        })
        entries = [
            ScoredRouteEntry(
                commodity_name="Zero Co", id_commodity=1,
                origin_terminal_name="Origin 1", destination_terminal_name="Destination 1",
                price_origin=100, price_destination=200, price_margin=50, price_roi=100,
                distance=10, score=100, scu_origin=0, scu_destination=100,
                status_origin=1, status_destination=1, origin_terminal_id=1, destination_terminal_id=2,
            ),
            ScoredRouteEntry(
                commodity_name="Unknown Co", id_commodity=2,
                origin_terminal_name="Origin 2", destination_terminal_name="Destination 2",
                price_origin=100, price_destination=200, price_margin=50, price_roi=100,
                distance=10, score=90, scu_origin=None, scu_destination=100,
                status_origin=1, status_destination=1, origin_terminal_id=3, destination_terminal_id=4,
            ),
            ScoredRouteEntry(
                commodity_name="Inferred Co", id_commodity=3,
                origin_terminal_name="Origin 3", destination_terminal_name="Destination 3",
                price_origin=100, price_destination=200, price_margin=50, price_roi=100,
                distance=10, score=80, scu_origin=None, scu_destination=100,
                status_origin=1, status_destination=1, origin_terminal_id=6, destination_terminal_id=7,
            ),
        ]
        inter = _interaction()
        await cog._send_ranked_routes(
            inter, entries=entries, updated_at=None, ship=None,
            title="Top routes", footer_note="Collected data", log_label="test", display_limit=10,
        )
        by_route: dict[str, str] = {}
        for call in _route_embed_calls(inter.followup.send):
            embed = call.kwargs["embed"]
            by_route[embed.title] = "".join(f.value or "" for f in embed.fields)
        zero_text = next(v for k, v in by_route.items() if "Zero Co" in k)
        unknown_text = next(v for k, v in by_route.items() if "Unknown Co" in k)
        inferred_text = next(v for k, v in by_route.items() if "Inferred Co" in k)

        # _make_cog's shared health fixture reports every terminal as "stale" (see this
        # file's own docstring) - so the live 0 SCU value lands in the "aging" tier here,
        # not "current". The point being pinned is that it's shown as a real quantity at
        # all, distinct from the unknown route's total absence of one.
        assert "**0 SCU**" in zero_text
        assert "no information reported" in unknown_text
        assert "**0 SCU**" not in unknown_text, "an unknown figure must never render as a literal zero"
        assert "historically available" in inferred_text
        assert "no information reported" not in inferred_text

    asyncio.run(run())


def test_top_routes_discloses_missing_distance_instead_of_silence():
    """Audit fix: travel_warning's has_real_distance was hardcoded True for every route in
    this loop regardless of whether that SPECIFIC route's distance field was actually
    populated - a route with distance=None got neither a real distance line nor any
    travel-time disclaimer, silently indistinguishable from a route where distance
    genuinely doesn't matter."""
    async def run():
        cog, _ = _make_cog(1)
        inter = _interaction()
        entries = [
            ScoredRouteEntry(
                commodity_name="No Distance Co", id_commodity=1,
                origin_terminal_name="Origin 1", destination_terminal_name="Destination 1",
                price_origin=100, price_destination=200, price_margin=50, price_roi=100,
                distance=None, score=100, scu_origin=100, scu_destination=100,
                status_origin=1, status_destination=1, origin_terminal_id=1, destination_terminal_id=2,
            ),
        ]
        await cog._send_ranked_routes(
            inter, entries=entries, updated_at=None, ship=None,
            title="Top routes", footer_note="Collected data", log_label="test", display_limit=10,
        )
        embed = inter.followup.send.call_args.kwargs["embed"]
        combined = "\n".join(field.value or "" for field in embed.fields)
        assert "not included in this ranking" in combined, combined

    asyncio.run(run())


def test_top_routes_now_warns_on_a_cross_system_route():
    """Centralized Route Presentation: /top-routes never had a cross-system warning at
    all (unlike /best-route's fallback branch and /mixed-routes, which always had one,
    and /multi-stop-route's per-leg lines) - a real gap only noticed once the shared
    bot.uex.route_presentation.travel_warning was written and the missing call site
    audited for. /top-routes has real UEX distance data for every route (r.distance),
    so it uses the has_real_distance=True style: silent when systems match, a warning
    only when they're known and differ."""
    async def run():
        cog, _ = _make_cog(1)
        cog.bot.db.get_terminal_references_by_ids = AsyncMock(return_value={
            1: dict(terminal_name="Terminal 1", star_system_name="Stanton"),
            2: dict(terminal_name="Terminal 2", star_system_name="Pyro"),
        })
        inter = _interaction()
        await cog._send_ranked_routes(
            inter, entries=_routes(1), updated_at=None, ship=None,
            title="Top routes", footer_note="Collected data", log_label="test", display_limit=10,
        )
        embed = inter.followup.send.call_args.kwargs["embed"]
        assert any("crosses systems" in (f.value or "") for f in embed.fields), embed.fields

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
            for call in inter.followup.send.call_args_list:
                embed = call.kwargs.get("embed")
                if embed is not None:
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
            groups: dict[str, str] = {}
            for call in _route_embed_calls(inter.followup.send):
                embed = call.kwargs["embed"]
                groups[embed.title] = "".join(f.value or "" for f in embed.fields)
            assert all("Cargo risk:" in text for text in groups.values()), (
                padding, [(title, "Cargo risk:" in text) for title, text in groups.items()],
            )

    asyncio.run(run())
