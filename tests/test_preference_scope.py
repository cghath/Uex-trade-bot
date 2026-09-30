"""Saved trading preferences: which commands read them, and saying so (audit MSG-5, MSG-6, MSG-8).

- MSG-6: the option descriptions and /my-trading-preferences named the wrong commands (e.g.
  "all 4 route commands" for a filter eight read), and /intelligence-brief read none.
- MSG-8: /mixed-routes, /multi-stop-route and /route-from-multi never said a saved
  auto-load, system or risk filter was shaping their results. Every route command's footer
  now has the same "Filters: ..." line, with the saved ones marked.
- MSG-5: /my-trading-preferences called the ship "maybe renamed" when UEX was down."""
from __future__ import annotations

import asyncio
import inspect
import re
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock

from bot.cogs import intelligence_brief as intelligence_brief_module
from bot.cogs import prices as prices_module
from bot.cogs.intelligence_brief import IntelligenceBrief
from bot.cogs.prices import Prices
from bot.cogs.trading_preferences import TradingPreferences
from bot.cogs.trends import Trends
from bot.uex.exceptions import UexApiError
from bot.uex.mixed_routes import MixedCargoItem
from bot.uex.multi_stop_routes import MultiStopLeg, MultiStopRoute
from bot.uex.trading_preferences import (
    DEFAULT_TRADING_PREFERENCES,
    PREFERENCE_READERS,
    ROUTE_COMMANDS,
    format_trading_preferences,
    preference_scope,
)
from tests.route_results import route_results
from tests.test_route_send_shape import _FakeInteraction

SAVED = dict(DEFAULT_TRADING_PREFERENCES, auto_load_only=True, preferred_system="Stanton", risk_tolerance="low")


def _command_sources() -> dict[str, str]:
    """Each route command's code: its callback plus any of its cog's own methods it calls."""
    sources = {}
    for cog in (Prices, Trends, IntelligenceBrief):
        for command in cog.__cog_app_commands__:
            if command.name not in ROUTE_COMMANDS:
                continue
            source = inspect.getsource(command.callback)
            for helper in set(re.findall(r"self\.(_\w+)\(", source)):
                if inspect.isfunction(method := getattr(cog, helper, None)):
                    source += inspect.getsource(method)
            sources[command.name] = source
    return sources


def test_each_preference_names_the_commands_that_really_read_it():
    sources = _command_sources()
    assert set(sources) == set(ROUTE_COMMANDS)
    for key, readers in PREFERENCE_READERS.items():
        in_code = {name for name, source in sources.items() if f'prefs["{key}"]' in source}
        assert in_code == set(readers), (key, sorted(in_code ^ set(readers)))


def test_the_descriptions_say_which_commands_each_preference_reaches():
    assert preference_scope("budget") == "every route command except /best-route and /diminishing-returns"
    assert preference_scope("auto_load_only") == "every route command"
    assert preference_scope("preferred_system") == "every route command"
    assert preference_scope("space_only") == "every route command except /best-route and /top-routes"

    params = {param.name: str(param.description)
              for param in TradingPreferences.set_trading_preferences.parameters}
    assert preference_scope("budget") in params["budget"]
    assert preference_scope("auto_load_only") in params["auto_load_only"]
    assert preference_scope("preferred_system") in params["system"]
    assert "all 4" not in " ".join(params.values())

    shown = format_trading_preferences(dict(DEFAULT_TRADING_PREFERENCES))
    for key in PREFERENCE_READERS:
        assert f"({preference_scope(key)})" in shown, key


def test_my_trading_preferences_doesnt_call_a_uex_outage_a_renamed_ship():
    async def run():
        replies = {}
        for label, vehicles in (("down", AsyncMock(side_effect=UexApiError("down"))),
                                ("gone", AsyncMock(return_value=[dict(name="Other Ship", scu=10)]))):
            cog = TradingPreferences.__new__(TradingPreferences)
            db = NS(get_trading_preferences=AsyncMock(return_value=dict(DEFAULT_TRADING_PREFERENCES, ship_name="Cutlass")))
            cog.bot = NS(db=db, uex=NS(get_vehicles=vehicles))
            interaction = NS(user=NS(id=1), response=NS(defer=AsyncMock()), followup=NS(send=AsyncMock()))
            await cog.my_trading_preferences.callback(cog, interaction)
            replies[label] = interaction.followup.send.call_args.args[0]

        assert "didn't load" in replies["down"] and "renamed" not in replies["down"]
        assert "maybe renamed" in replies["gone"]

    asyncio.run(run())


def _mixed_route():
    source = dict(scu_buy=10, status_buy=1, star_system_name="Stanton")
    destination = dict(scu_sell=10, status_sell=1, star_system_name="Stanton")
    cargo = (MixedCargoItem(1, "Gold", 10, 100, 200, 10, 1000, 1000, source, destination),)
    return NS(origin_name="Origin", destination_name="Destination", origin_id=1, destination_id=2, cargo=cargo,
              cargo_scu=10, investment=1000, revenue=2000, profit=1000, roi_pct=100.0, is_exact=True)


def _multi_stop_route():
    source = dict(scu_buy=10, status_buy=1, star_system_name="Stanton")
    destination = dict(scu_sell=10, status_sell=1, star_system_name="Stanton")
    cargo = (MixedCargoItem(1, "Gold", 5, 100, 200, 10, 500, 500, source, destination),)
    return MultiStopRoute((MultiStopLeg(10, "Station A", 20, "Station B", cargo, 500, 1000, 500, True),), 500, 1000, 500)


def _prices_cog(prefs: dict) -> Prices:
    db = NS(
        get_default_ship=AsyncMock(return_value="Ship"),
        get_trading_preferences=AsyncMock(return_value=prefs),
        get_mixed_route_market_rows=AsyncMock(return_value=[]),
        get_terminal_data_health_by_ids=AsyncMock(return_value={}),
        resolve_terminal_id_by_name=AsyncMock(return_value=(10, "Station A")),
    )
    uex = NS(get_vehicles=AsyncMock(return_value=[dict(name="Ship", scu=100)]),
             get_terminal_distance=AsyncMock(return_value=dict(distance=10)))
    cog = Prices.__new__(Prices)
    cog.bot = NS(db=db, uex=uex, get_cog=lambda name: None)
    cog._get_status_lookup = AsyncMock(return_value={"buy": {}, "sell": {}})
    return cog


def test_mixed_cargo_footers_name_the_saved_filters(monkeypatch):
    monkeypatch.setattr(prices_module, "build_mixed_routes", lambda *a, **k: [_mixed_route()])
    monkeypatch.setattr(prices_module, "build_multi_stop_routes", lambda *a, **k: [_multi_stop_route()])

    async def footer(run_command) -> str:
        interaction = _FakeInteraction(1)
        await run_command(_prices_cog(dict(SAVED)), interaction)
        return route_results(interaction.followup.sent).embeds[0].footer.text

    async def run():
        expected = "Filters: auto-load-only (saved), system: Stanton (saved), risk tolerance: low (saved)"
        for name, run_command in (
            ("mixed-routes", lambda cog, i: cog.mixed_routes.callback(cog, i)),
            ("multi-stop-route", lambda cog, i: cog.multi_stop_route.callback(cog, i)),
            ("multi-stop-route origin", lambda cog, i: cog.multi_stop_route.callback(cog, i, origin="Station A")),
        ):
            assert expected in await footer(run_command), name

        # A filter set on the command itself isn't marked saved.
        passed = await footer(lambda cog, i: cog.mixed_routes.callback(cog, i, auto_load_only=True))
        assert "Filters: auto-load-only, system: Stanton (saved)" in passed

    asyncio.run(run())


def test_intelligence_brief_applies_saved_preferences(monkeypatch):
    seen = {}

    def build(rows, **kwargs):
        seen.update(kwargs)
        return []

    def tolerate(rows, tolerance):
        seen["risk_tolerance"] = tolerance
        return rows

    monkeypatch.setattr(intelligence_brief_module, "build_mixed_routes", build)
    monkeypatch.setattr(intelligence_brief_module, "within_risk_tolerance", tolerate)

    async def run():
        cog = IntelligenceBrief.__new__(IntelligenceBrief)
        cog.bot = NS(db=NS(get_mixed_route_market_rows=AsyncMock(return_value=[])),
                     uex=NS(get_vehicles=AsyncMock(return_value=[dict(name="Ship", scu=100)])))
        prefs = dict(SAVED, budget=5000.0, space_only=True)

        embed = await cog._routes_embed("Ship", prefs, budget=None, space_only=None)
        assert seen["budget"] == 5000.0 and seen["space_only"] is True
        assert seen["auto_load_only"] is True and seen["system"] == "Stanton" and seen["risk_tolerance"] == "low"
        assert "Your saved space-only, auto-load-only and system Stanton settings are on" in embed.description
        assert "set those options on this command" not in embed.description, "the brief has no such options"

        await cog._routes_embed("Ship", prefs, budget=100.0, space_only=False)
        assert seen["budget"] == 100.0 and seen["space_only"] is False, "the command's own options win"

    asyncio.run(run())
