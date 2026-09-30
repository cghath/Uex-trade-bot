"""Audit REL-15: on a tag-restricted slot (PDC slots, remote turrets) most parts fail the
tag check, and each one's fitting-variant lookup used to fire at once, up to ~86 wiki
calls in one burst. A wiki outage there also read as "no variants", quietly dropping the
part with no outage note. Lookups now go DETAIL_BATCH_SIZE at a time, once per name, and
an unanswered one is counted in the note and skipped for WIKI_OUTAGE_RETRY_SECONDS."""
import asyncio
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock

from bot.cogs import ship_parts_finder
from bot.cogs.ship_parts_finder import DETAIL_BATCH_SIZE, WIKI_OUTAGE_RETRY_SECONDS, ShipPartsFinder
from bot.uex.ship_parts import ShipPort
from bot.wiki_api import WikiApiError, WikiUnavailableError

PDC = ShipPort(name="hardpoint_pdc_left", port_type="Turret", size_min=2, size_max=2,
               tags=frozenset({"RSI_Polaris"}), required_tags=frozenset())


def _restricted(name):
    """A candidate whose UEX uuid led to another ship's variant, so it fails the tag check."""
    return {"name": name, "size": 2, "required_tags": ["ANVL_Carrack"], "_uex_row": {"size": "2"},
            "_detail_loaded": True}


def _generic(name):
    return {"uuid": f"generic-{name}", "name": name, "size": 2, "required_tags": []}


def _cog(find_variants):
    wiki = NS(find_item_variants_by_name=find_variants)
    return ShipPartsFinder(NS(), wiki_client=wiki, start_refresh=False)


def test_variant_lookups_go_a_batch_at_a_time_not_all_at_once():
    state = {"in_flight": 0, "peak": 0}

    async def find_variants(name):
        state["in_flight"] += 1
        state["peak"] = max(state["peak"], state["in_flight"])
        await asyncio.sleep(0)
        state["in_flight"] -= 1
        return [_generic(name)]

    async def run():
        cog = _cog(AsyncMock(side_effect=find_variants))
        candidates = [_restricted(f"PDC Mount {i}") for i in range(86)]
        await cog._swap_in_fitting_variants(candidates, PDC)
        return cog, candidates

    cog, candidates = asyncio.run(run())
    assert state["peak"] == DETAIL_BATCH_SIZE
    assert cog._wiki.find_item_variants_by_name.await_count == 86
    assert all(c["uuid"].startswith("generic-") for c in candidates), "every part got its fitting variant"


def test_a_name_shared_by_several_parts_is_looked_up_once():
    async def find_variants(name):
        await asyncio.sleep(0)  # a real request yields, so same-name lookups would overlap
        return [_generic(name)]

    async def run():
        cog = _cog(AsyncMock(side_effect=find_variants))
        candidates = [_restricted("VariPuck S2 Gimbal Mount") for _ in range(5)]
        await cog._swap_in_fitting_variants(candidates, PDC)
        return cog, candidates

    cog, candidates = asyncio.run(run())
    assert cog._wiki.find_item_variants_by_name.await_count == 1
    assert all(c["uuid"] == "generic-VariPuck S2 Gimbal Mount" for c in candidates)


def test_an_unanswered_variant_lookup_is_flagged_and_skipped_for_the_outage_window(monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr(ship_parts_finder, "time", NS(monotonic=lambda: clock[0]))
    down = {"now": True}

    async def find_variants(name):
        if down["now"]:
            raise WikiUnavailableError("503 x3")
        return [_generic(name)]

    async def run():
        cog = _cog(AsyncMock(side_effect=find_variants))
        first = [_restricted("Mount A")]
        await cog._swap_in_fitting_variants(first, PDC)
        calls_after_outage = cog._wiki.find_item_variants_by_name.await_count

        down["now"] = False
        clock[0] += 60  # still inside the retry window: not asked again
        second = [_restricted("Mount A")]
        await cog._swap_in_fitting_variants(second, PDC)
        calls_inside_window = cog._wiki.find_item_variants_by_name.await_count

        clock[0] += WIKI_OUTAGE_RETRY_SECONDS  # past it: asked again, and it answers
        third = [_restricted("Mount A")]
        await cog._swap_in_fitting_variants(third, PDC)
        return first, second, third, calls_after_outage, calls_inside_window, cog

    first, second, third, after_outage, inside_window, cog = asyncio.run(run())
    assert first[0]["_detail_unanswered"] is True and second[0]["_detail_unanswered"] is True
    assert (after_outage, inside_window) == (1, 1)
    assert third[0]["uuid"] == "generic-Mount A" and not third[0].get("_detail_unanswered")
    assert cog._wiki.find_item_variants_by_name.await_count == 2


def test_a_definite_wiki_error_is_cached_like_any_answer():
    async def run():
        cog = _cog(AsyncMock(side_effect=WikiApiError("identity mismatch")))
        for _ in range(3):
            candidates = [_restricted("Mount B")]
            await cog._swap_in_fitting_variants(candidates, PDC)
        return cog, candidates

    cog, candidates = asyncio.run(run())
    assert cog._wiki.find_item_variants_by_name.await_count == 1
    assert not candidates[0].get("_detail_unanswered"), "a definite answer isn't an outage"


def test_an_outage_on_a_tag_restricted_slot_shows_in_the_browsers_wiki_note():
    """End to end: the part is still left out (its fit can't be confirmed), but the count
    that drives "The Star Citizen Wiki didn't respond for N parts here" includes it."""
    async def run():
        catalog = [{"id": 1, "uuid": "u1", "category": "Turrets", "size": "2", "name": "PDC Mount"}]
        uex = NS(get_item_catalog=AsyncMock(return_value=catalog),
                 get_items_prices_all=AsyncMock(return_value=[
                     {"id_item": 1, "price_buy": 5000, "id_terminal": 5, "terminal_name": "Shop 5"}]),
                 get_terminal_distance=AsyncMock(return_value=None))
        wiki = NS(get_item_detail=AsyncMock(return_value=_restricted("PDC Mount") | {"uuid": "u1"}),
                  find_item_detail_by_name=AsyncMock(return_value=None),
                  find_item_variants_by_name=AsyncMock(side_effect=WikiUnavailableError("503 x3")))
        cog = ShipPartsFinder(NS(uex=uex, db=NS(get_terminal_references_by_ids=AsyncMock(return_value={}))),
                              wiki_client=wiki, start_refresh=False)
        return await cog.candidates_for_port(PDC, category="Turrets")

    candidates = asyncio.run(run())
    assert list(candidates) == []
    assert candidates.wiki_unavailable == 1
