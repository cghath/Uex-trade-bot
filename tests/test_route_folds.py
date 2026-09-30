"""Three route commands folded into their parents (audit UX-8): /routes-from and
/route-on-the-way became /top-routes' origin/destination options, /route-from-multi became
/multi-stop-route's origin option. The twins' own behaviour is tested, under the new
options, in test_routes_from.py, test_route_on_the_way.py and test_route_send_shape.py;
this covers what changed in the merge."""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import httpx

from bot.cogs.help import CATEGORIES
from bot.uex.client import UexClient
from tests.route_results import route_results
from tests.test_honest_labels import _all_commands
from tests.test_route_on_the_way import _entry, _FakeInteraction, _make_cog, _make_db


async def _top_routes(tmp_path, entries, *, preferred_system=None, **options):
    db = _make_db(tmp_path)
    await db.init()
    await db.upsert_terminal_reference([
        {"id": 1, "name": "Area18", "star_system_name": "Stanton"},
        {"id": 2, "name": "Port Tressler", "star_system_name": "Stanton"},
        {"id": 3, "name": "Baijini Point", "star_system_name": "Stanton"},
    ])
    if preferred_system:
        await db.set_trading_preferences(1, preferred_system=preferred_system)
    client = UexClient(app_token="test", base_url="https://uex.test")
    await client._client.aclose()
    client._client = httpx.AsyncClient(transport=httpx.MockTransport(
        lambda request: httpx.Response(200, json={"status": "ok", "data": []})))
    bot = type("FakeBot", (), {"db": db, "uex": client, "get_cog": lambda self, name: None})()
    cog = _make_cog(bot)
    cog._top_scored_routes_lock = asyncio.Lock()
    cog._top_scored_routes = entries
    cog._top_scored_routes_updated_at = datetime.now(timezone.utc)
    interaction = _FakeInteraction(1)
    try:
        await cog.top_routes.callback(cog, interaction, **options)
    finally:
        await client.aclose()
    return interaction


_TO_TRESSLER = _entry(id_commodity=1, origin_id=1, origin_name="Area18", destination_id=2,
                      destination_name="Port Tressler", score=100)
_BAIJINI_TO_TRESSLER = _entry(id_commodity=2, origin_id=3, origin_name="Baijini Point", destination_id=2,
                              destination_name="Port Tressler", score=90)
_TO_BAIJINI = _entry(id_commodity=3, origin_id=1, origin_name="Area18", destination_id=3,
                     destination_name="Baijini Point", score=80)


def test_top_routes_can_show_only_routes_ending_at_a_terminal(tmp_path):
    interaction = asyncio.run(_top_routes(tmp_path, [_TO_TRESSLER, _BAIJINI_TO_TRESSLER, _TO_BAIJINI],
                                          destination="Port Tressler"))
    results = route_results(interaction.followup.sent)
    titles = " ".join(results.titles)
    assert "Commodity 1" in titles and "Commodity 2" in titles and "Commodity 3" not in titles
    assert "Best Routes to Port Tressler" in results.header


def test_a_saved_star_system_is_skipped_once_both_ends_are_named(tmp_path):
    # Every terminal here is in Stanton, so a saved Pyro filter rules the route out...
    unpinned = asyncio.run(_top_routes(tmp_path / "a", [_TO_TRESSLER], preferred_system="Pyro"))
    assert route_results(unpinned.followup.sent) is None
    # ...but with both ends named there's nothing left for it to restrict.
    pinned = asyncio.run(_top_routes(tmp_path / "b", [_TO_TRESSLER], preferred_system="Pyro",
                                     origin="Area18", destination="Port Tressler"))
    assert "Commodity 1" in " ".join(route_results(pinned.followup.sent).titles)


def test_the_three_folded_commands_are_gone_and_intro_lists_only_real_commands():
    real = _all_commands()
    assert {"routes-from", "route-on-the-way", "route-from-multi"}.isdisjoint(real)
    listed = {name for _title, _summary, names in CATEGORIES for name in names}
    assert listed <= real, sorted(listed - real)
