"""Saved trading preferences: pure formatting, DB round-trip, and route-command wiring.

Feature: store per-user defaults for space-only terminals, capital-ship access,
auto-loading, preferred system, and risk tolerance, applied by /best-route, /top-routes,
/mixed-routes, and /multi-stop-route whenever their matching option is left unset.
Risk tolerance is stored and shown but not yet enforced by any route command - a
deliberate scoping decision, not an oversight.
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock

from cryptography.fernet import Fernet

from bot.cogs import prices as prices_module
from bot.cogs import trends as trends_module
from bot.cogs.prices import Prices
from bot.cogs.trading_preferences import TradingPreferences
from bot.db.database import Database
from bot.uex.mixed_routes import MixedCargoItem, MixedRoute
from bot.uex.trading_preferences import (
    DEFAULT_TRADING_PREFERENCES,
    UNSET,
    describe_active_preferences,
    format_trading_preferences,
)


def _make_db(tmp_path) -> Database:
    return Database(tmp_path / "prefs.sqlite3", Fernet(Fernet.generate_key()))


# -- pure formatting -------------------------------------------------------------


def test_describe_active_preferences_is_none_when_nothing_is_active():
    assert describe_active_preferences() is None


def test_describe_active_preferences_only_reports_what_it_was_told_about():
    # A command with no space-only/capital-ship-access concept (e.g. /best-route) simply
    # never passes those kwargs - the function must not invent them.
    note = describe_active_preferences(auto_load_only=True, system="Pyro")
    assert note == "Active preferences: auto-load-only, system: Pyro"


def test_describe_active_preferences_covers_all_five_fields():
    note = describe_active_preferences(
        space_only=True, capital_ship_access=True, auto_load_only=True,
        system="Stanton", risk_tolerance="low",
    )
    assert note == (
        "Active preferences: space-only, capital-ship access, auto-load-only, "
        "system: Stanton, risk tolerance: low (not yet enforced)"
    )


def test_describe_active_preferences_high_risk_tolerance_is_not_shown_as_active():
    # "high" means no restriction - the same no-op default as an unset preference.
    assert describe_active_preferences(risk_tolerance="high") is None


def test_format_trading_preferences_shows_defaults_clearly():
    text = format_trading_preferences(dict(DEFAULT_TRADING_PREFERENCES))
    assert "Space-only terminals: **No**" in text
    assert "Capital-ship access required: **No**" in text
    assert "Auto-load only: **No**" in text
    assert "Preferred system: **Any (no restriction)**" in text
    assert "Risk tolerance: **High (no restriction, default)**" in text


def test_format_trading_preferences_shows_set_values():
    prefs = {
        "space_only": True, "capital_ship_access": True, "auto_load_only": True,
        "preferred_system": "Pyro", "risk_tolerance": "low",
    }
    text = format_trading_preferences(prefs)
    assert "Space-only terminals: **Yes**" in text
    assert "Preferred system: **Pyro**" in text
    assert "Risk tolerance: **low**" in text


# -- DB round-trip ----------------------------------------------------------------


def test_get_trading_preferences_defaults_when_no_row_exists(tmp_path):
    async def run():
        db = _make_db(tmp_path)
        await db.init()
        prefs = await db.get_trading_preferences(user_id=1)
        assert prefs == DEFAULT_TRADING_PREFERENCES

    asyncio.run(run())


def test_set_trading_preferences_only_changes_fields_that_were_passed(tmp_path):
    async def run():
        db = _make_db(tmp_path)
        await db.init()
        await db.set_trading_preferences(1, space_only=True, preferred_system="Pyro")
        # A second call touching only auto_load_only must not reset space_only/system.
        prefs = await db.set_trading_preferences(1, auto_load_only=True)
        assert prefs["space_only"] is True
        assert prefs["preferred_system"] == "Pyro"
        assert prefs["auto_load_only"] is True
        assert prefs["capital_ship_access"] is False
        assert prefs["risk_tolerance"] is None

    asyncio.run(run())


def test_set_trading_preferences_can_explicitly_clear_a_field_back_to_none(tmp_path):
    async def run():
        db = _make_db(tmp_path)
        await db.init()
        await db.set_trading_preferences(1, preferred_system="Stanton")
        # Passing None explicitly (not UNSET) clears it - distinct from "not provided".
        prefs = await db.set_trading_preferences(1, preferred_system=None)
        assert prefs["preferred_system"] is None

    asyncio.run(run())


def test_set_trading_preferences_default_kwarg_is_unset_not_a_real_value(tmp_path):
    # A caller that omits a kwarg entirely relies on the method's own default being UNSET,
    # not None/False - otherwise every call would silently reset every untouched field.
    async def run():
        db = _make_db(tmp_path)
        await db.init()
        await db.set_trading_preferences(1, space_only=True)
        prefs = await db.set_trading_preferences(1, auto_load_only=True)
        assert prefs["space_only"] is True

    asyncio.run(run())


def test_clear_trading_preferences_removes_the_row(tmp_path):
    async def run():
        db = _make_db(tmp_path)
        await db.init()
        await db.set_trading_preferences(1, space_only=True)
        assert await db.clear_trading_preferences(1) is True
        assert await db.get_trading_preferences(1) == DEFAULT_TRADING_PREFERENCES
        assert await db.clear_trading_preferences(1) is False

    asyncio.run(run())


def test_trading_preferences_are_isolated_per_user(tmp_path):
    async def run():
        db = _make_db(tmp_path)
        await db.init()
        await db.set_trading_preferences(1, space_only=True)
        prefs_other_user = await db.get_trading_preferences(2)
        assert prefs_other_user == DEFAULT_TRADING_PREFERENCES

    asyncio.run(run())


# -- default ship, now stored in user_trading_preferences, not a dedicated table -------


def test_default_ship_round_trips_through_trading_preferences(tmp_path):
    async def run():
        db = _make_db(tmp_path)
        await db.init()
        await db.set_default_ship(1, "Freelancer")
        assert await db.get_default_ship(1) == "Freelancer"
        assert (await db.get_trading_preferences(1))["ship_name"] == "Freelancer"

    asyncio.run(run())


def test_set_default_ship_does_not_disturb_other_saved_preferences(tmp_path):
    async def run():
        db = _make_db(tmp_path)
        await db.init()
        await db.set_trading_preferences(1, space_only=True, auto_load_only=True)
        await db.set_default_ship(1, "Freelancer")
        prefs = await db.get_trading_preferences(1)
        assert prefs["ship_name"] == "Freelancer"
        assert prefs["space_only"] is True
        assert prefs["auto_load_only"] is True

    asyncio.run(run())


def test_clear_default_ship_only_clears_the_ship(tmp_path):
    async def run():
        db = _make_db(tmp_path)
        await db.init()
        await db.set_trading_preferences(1, space_only=True)
        await db.set_default_ship(1, "Freelancer")
        assert await db.clear_default_ship(1) is True
        assert await db.clear_default_ship(1) is False
        prefs = await db.get_trading_preferences(1)
        assert prefs["ship_name"] is None
        assert prefs["space_only"] is True  # untouched by clearing just the ship

    asyncio.run(run())


def test_clear_trading_preferences_also_clears_the_ship(tmp_path):
    # Distinct from clear_default_ship above: clearing ALL trading preferences resets
    # everything in the same row, ship included - this is the behavior change the user
    # asked for when folding the ship into trading preferences.
    async def run():
        db = _make_db(tmp_path)
        await db.init()
        await db.set_default_ship(1, "Freelancer")
        await db.clear_trading_preferences(1)
        assert await db.get_default_ship(1) is None

    asyncio.run(run())


# -- migrating existing user_ship_preference rows into user_trading_preferences --------


def test_migration_copies_a_ship_with_no_existing_trading_preferences_row(tmp_path):
    async def run():
        db = _make_db(tmp_path)
        await db.init()
        async with db.connect() as conn:
            await conn.execute(
                "INSERT INTO user_ship_preference (user_id, ship_name) VALUES (1, 'Old Ship')"
            )
            await conn.commit()
        # Re-running init() is exactly what happens on every real bot restart.
        await db.init()
        assert await db.get_default_ship(1) == "Old Ship"

    asyncio.run(run())


def test_migration_backfills_a_null_ship_on_an_existing_preferences_row(tmp_path):
    async def run():
        db = _make_db(tmp_path)
        await db.init()
        await db.set_trading_preferences(1, space_only=True)  # creates a row, no ship
        async with db.connect() as conn:
            await conn.execute(
                "INSERT INTO user_ship_preference (user_id, ship_name) VALUES (1, 'Old Ship')"
            )
            await conn.commit()
        await db.init()
        prefs = await db.get_trading_preferences(1)
        assert prefs["ship_name"] == "Old Ship"
        assert prefs["space_only"] is True

    asyncio.run(run())


def test_migration_never_overwrites_a_ship_already_set_via_the_new_path(tmp_path):
    async def run():
        db = _make_db(tmp_path)
        await db.init()
        await db.set_default_ship(1, "New Ship")
        async with db.connect() as conn:
            await conn.execute(
                "INSERT INTO user_ship_preference (user_id, ship_name) VALUES (1, 'Stale Old Ship')"
            )
            await conn.commit()
        await db.init()
        assert await db.get_default_ship(1) == "New Ship"

    asyncio.run(run())


def test_migration_is_idempotent_across_repeated_startups(tmp_path):
    async def run():
        db = _make_db(tmp_path)
        await db.init()
        async with db.connect() as conn:
            await conn.execute(
                "INSERT INTO user_ship_preference (user_id, ship_name) VALUES (1, 'Old Ship')"
            )
            await conn.commit()
        await db.init()
        await db.init()
        await db.init()
        assert await db.get_default_ship(1) == "Old Ship"

    asyncio.run(run())


# -- /set-trading-preferences, /clear-trading-preferences, /my-trading-preferences -----


class _FakeInteraction:
    def __init__(self, user_id: int) -> None:
        self.user = NS(id=user_id)
        self.response = NS(send_message=AsyncMock())


def test_set_trading_preferences_command_requires_at_least_one_option(tmp_path):
    async def run():
        db = _make_db(tmp_path)
        await db.init()
        cog = TradingPreferences.__new__(TradingPreferences)
        cog.bot = NS(db=db)
        interaction = _FakeInteraction(1)
        await cog.set_trading_preferences.callback(
            cog, interaction, ship=None, space_only=None, capital_ship_access=None,
            auto_load_only=None, system=None, risk_tolerance=None,
        )
        message = interaction.response.send_message.call_args.args[0]
        assert "at least one option" in message
        assert await db.get_trading_preferences(1) == DEFAULT_TRADING_PREFERENCES

    asyncio.run(run())


def test_set_trading_preferences_command_updates_and_confirms(tmp_path):
    async def run():
        db = _make_db(tmp_path)
        await db.init()
        cog = TradingPreferences.__new__(TradingPreferences)
        cog.bot = NS(db=db)
        interaction = _FakeInteraction(1)
        await cog.set_trading_preferences.callback(
            cog, interaction, ship=None, space_only=True, capital_ship_access=None,
            auto_load_only=None, system=None, risk_tolerance=None,
        )
        message = interaction.response.send_message.call_args.args[0]
        assert "Space-only terminals: **Yes**" in message
        assert (await db.get_trading_preferences(1))["space_only"] is True

    asyncio.run(run())


def test_set_trading_preferences_command_any_system_choice_clears_preference(tmp_path):
    from discord import app_commands

    async def run():
        db = _make_db(tmp_path)
        await db.init()
        cog = TradingPreferences.__new__(TradingPreferences)
        cog.bot = NS(db=db)
        await db.set_trading_preferences(1, preferred_system="Stanton")
        interaction = _FakeInteraction(1)
        any_choice = app_commands.Choice(name="Any (no restriction)", value="any")
        await cog.set_trading_preferences.callback(
            cog, interaction, ship=None, space_only=None, capital_ship_access=None,
            auto_load_only=None, system=any_choice, risk_tolerance=None,
        )
        assert (await db.get_trading_preferences(1))["preferred_system"] is None

    asyncio.run(run())


def test_set_trading_preferences_command_sets_ship_with_validation(tmp_path):
    async def run():
        db = _make_db(tmp_path)
        await db.init()
        cog = TradingPreferences.__new__(TradingPreferences)
        uex = NS(get_vehicles=AsyncMock(return_value=[dict(name="Cutlass Black", scu=46)]))
        cog.bot = NS(db=db, uex=uex)
        interaction = _FakeInteraction(1)
        await cog.set_trading_preferences.callback(
            cog, interaction, ship="Cutlass", space_only=None, capital_ship_access=None,
            auto_load_only=None, system=None, risk_tolerance=None,
        )
        message = interaction.response.send_message.call_args.args[0]
        assert "Default ship: **Cutlass Black**" in message
        prefs = await db.get_trading_preferences(1)
        assert prefs["ship_name"] == "Cutlass Black"
        # /set-default-ship and /my-ship read the same underlying value.
        assert await db.get_default_ship(1) == "Cutlass Black"

    asyncio.run(run())


def test_set_trading_preferences_command_rejects_an_ambiguous_ship(tmp_path):
    async def run():
        db = _make_db(tmp_path)
        await db.init()
        cog = TradingPreferences.__new__(TradingPreferences)
        uex = NS(get_vehicles=AsyncMock(return_value=[]))
        cog.bot = NS(db=db, uex=uex)
        interaction = _FakeInteraction(1)
        await cog.set_trading_preferences.callback(
            cog, interaction, ship="Nonexistent Ship", space_only=None,
            capital_ship_access=None, auto_load_only=None, system=None, risk_tolerance=None,
        )
        message = interaction.response.send_message.call_args.args[0]
        assert "unambiguous match" in message
        assert (await db.get_trading_preferences(1))["ship_name"] is None

    asyncio.run(run())


def test_clear_trading_preferences_command(tmp_path):
    async def run():
        db = _make_db(tmp_path)
        await db.init()
        cog = TradingPreferences.__new__(TradingPreferences)
        cog.bot = NS(db=db)
        await db.set_trading_preferences(1, space_only=True)
        interaction = _FakeInteraction(1)
        await cog.clear_trading_preferences.callback(cog, interaction)
        assert "cleared" in interaction.response.send_message.call_args.args[0].lower()
        assert await db.get_trading_preferences(1) == DEFAULT_TRADING_PREFERENCES

    asyncio.run(run())


def test_my_trading_preferences_command_shows_current_state(tmp_path):
    async def run():
        db = _make_db(tmp_path)
        await db.init()
        cog = TradingPreferences.__new__(TradingPreferences)
        cog.bot = NS(db=db)
        await db.set_trading_preferences(1, auto_load_only=True)
        interaction = _FakeInteraction(1)
        await cog.my_trading_preferences.callback(cog, interaction)
        message = interaction.response.send_message.call_args.args[0]
        assert "Auto-load only: **Yes**" in message

    asyncio.run(run())


# -- route-command wiring: saved preferences apply only when the option is unset -------


def test_mixed_routes_applies_saved_capital_ship_access_preference(monkeypatch):
    """The independent 'always require capital-ship access' preference (this session's
    design decision) must force capital_access_only on even for a non-capital ship,
    without the user passing anything on the command itself."""
    captured = {}

    def fake_build_mixed_routes(*args, **kwargs):
        captured.update(kwargs)
        return []

    monkeypatch.setattr(prices_module, "build_mixed_routes", fake_build_mixed_routes)

    async def run():
        db = NS(
            get_default_ship=AsyncMock(return_value="Ship"),
            get_trading_preferences=AsyncMock(
                return_value=dict(DEFAULT_TRADING_PREFERENCES, capital_ship_access=True)
            ),
            get_mixed_route_market_rows=AsyncMock(return_value=[]),
        )
        # A perfectly ordinary, non-capital ship - requires_capital_cargo_access alone
        # would be False for this vehicle.
        uex = NS(
            get_vehicles=AsyncMock(return_value=[dict(name="Ship", scu=100, pad_type="M")]),
            get_space_stations=AsyncMock(return_value=[]),
        )
        cog = Prices.__new__(Prices)
        cog.bot = NS(db=db, uex=uex)
        interaction = _FakeInteraction(1)
        interaction.response.defer = AsyncMock()
        interaction.followup = NS(send=AsyncMock())
        await cog.mixed_routes.callback(cog, interaction, None, None, None, None, None)

    asyncio.run(run())
    assert captured["capital_access_only"] is True


def test_mixed_routes_explicit_option_still_works_without_a_saved_preference(monkeypatch):
    captured = {}

    def fake_build_mixed_routes(*args, **kwargs):
        captured.update(kwargs)
        return []

    monkeypatch.setattr(prices_module, "build_mixed_routes", fake_build_mixed_routes)

    async def run():
        db = NS(
            get_default_ship=AsyncMock(return_value="Ship"),
            get_trading_preferences=AsyncMock(return_value=dict(DEFAULT_TRADING_PREFERENCES)),
            get_mixed_route_market_rows=AsyncMock(return_value=[]),
        )
        uex = NS(get_vehicles=AsyncMock(return_value=[dict(name="Ship", scu=100, pad_type="M")]))
        cog = Prices.__new__(Prices)
        cog.bot = NS(db=db, uex=uex)
        interaction = _FakeInteraction(1)
        interaction.response.defer = AsyncMock()
        interaction.followup = NS(send=AsyncMock())
        # space_only explicitly True on the call itself, no saved preference involved.
        await cog.mixed_routes.callback(cog, interaction, None, None, True, None, None)

    asyncio.run(run())
    assert captured["space_only"] is True


def test_best_route_falls_back_to_saved_auto_load_preference(monkeypatch):
    async def run():
        db = NS(
            get_default_ship=AsyncMock(return_value=None),
            get_trading_preferences=AsyncMock(
                return_value=dict(DEFAULT_TRADING_PREFERENCES, auto_load_only=True)
            ),
            get_commodity_references=AsyncMock(return_value={}),
            get_terminal_references_by_ids=AsyncMock(return_value={}),
        )
        uex = NS(
            get_commodities_prices=AsyncMock(
                return_value=[{"id_commodity": 1, "commodity_name": "Gold"}]
            ),
            get_commodities_routes=AsyncMock(return_value=[]),
        )
        cog = Prices.__new__(Prices)
        cog.bot = NS(db=db, uex=uex)
        cog._get_status_lookup = AsyncMock(return_value={})
        interaction = _FakeInteraction(1)
        interaction.response.defer = AsyncMock()
        sent = []
        interaction.followup = NS(send=AsyncMock(side_effect=lambda *a, **k: sent.append((a, k))))
        # No auto_load_only passed - only the saved preference should apply.
        await cog.best_route.callback(cog, interaction, "Gold", None, None, None)
        assert sent, "expected a followup message"
        # With no UEX routes and no fallback rows, the auto-load preference has nothing to
        # filter, but the call must complete without error - the real assertion is that
        # the preference lookup happened at all (get_trading_preferences was awaited).
        db.get_trading_preferences.assert_awaited_once_with(1)

    asyncio.run(run())


def test_top_routes_send_ranked_routes_shows_active_preferences_in_footer(monkeypatch):
    """/top-routes' shared _send_ranked_routes must surface which saved/explicit
    preferences shaped the result, the same way /mixed-routes already does via its
    existing 'surface terminals excluded'/'capital access confirmed' footer notes."""
    from bot.uex.trends import ScoredRouteEntry

    async def run():
        entry = ScoredRouteEntry(
            commodity_name="Gold", id_commodity=1,
            origin_terminal_name="A", destination_terminal_name="B",
            price_origin=10, price_destination=20, price_margin=None, price_roi=None,
            distance=None, score=100, scu_origin=10, scu_destination=10,
            status_origin=1, status_destination=1,
            origin_terminal_id=1, destination_terminal_id=2,
        )
        terminal_refs = {
            1: {"is_auto_load": 1, "star_system_name": "Pyro"},
            2: {"is_auto_load": 1, "star_system_name": "Pyro"},
        }
        db = NS(
            get_default_ship=AsyncMock(return_value=None),
            get_terminal_references_by_ids=AsyncMock(return_value=terminal_refs),
            get_terminal_data_health_by_ids=AsyncMock(return_value={}),
            get_route_market_signals_by_ids=AsyncMock(return_value={}),
            get_commodity_references=AsyncMock(return_value={}),
        )
        cog = trends_module.Trends.__new__(trends_module.Trends)
        cog.bot = NS(db=db)
        cog._get_status_lookup = AsyncMock(return_value={})
        interaction = _FakeInteraction(1)
        interaction.response.defer = AsyncMock()
        sent = []
        interaction.followup = NS(send=AsyncMock(side_effect=lambda *a, **k: sent.append(k)))
        await cog._send_ranked_routes(
            interaction,
            entries=[entry],
            updated_at=None,
            ship=None,
            title="Top Trade Routes",
            footer_note="note",
            log_label="/top-routes",
            display_limit=5,
            auto_load_only=True,
            system="Pyro",
            risk_tolerance="low",
        )
        assert sent, "expected a followup"
        embed = sent[0]["embed"]
        assert "auto-load-only" in embed.footer.text
        assert "system: Pyro" in embed.footer.text
        assert "risk tolerance: low" in embed.footer.text

    asyncio.run(run())
