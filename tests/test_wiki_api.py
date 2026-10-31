"""bot/wiki_api.py against an httpx.MockTransport - no network.

The guarantee: the client returns the COMPLETE set or raises WikiApiError. A short page, a wrong total,
an error mid-crawl, or a non-JSON body must never come back as a smaller-but-plausible list, because the
caller would then replace a good snapshot with it.
"""
from __future__ import annotations

import asyncio

import httpx
import pytest

from bot.wiki_api import (
    MAX_ATTEMPTS,
    MAX_PAGES,
    WikiApiClient,
    WikiApiError,
    WikiDuplicateNameError,
    base_vehicle_row,
    WikiUnavailableError,
)

UUID = "c098e722-902a-435b-83f8-a96cec36a012"


def _row(i: int, version: str = "4.10.0-LIVE.1") -> dict:
    return {"uuid": f"m{i}", "title": f"Mission {i}", "game_version": version, "blueprints": [{"name": "X", "uuid": UUID}]}


def _page(rows: list[dict], *, page: int, last: int, total: int) -> httpx.Response:
    return httpx.Response(200, json={"data": rows, "meta": {"current_page": page, "last_page": last, "total": total}})


class _Harness:
    def __init__(self, handler):
        self.requests: list[httpx.Request] = []
        self.sleeps: list[float] = []
        self._handler = handler

        async def sleep(seconds: float) -> None:
            self.sleeps.append(seconds)

        def transport_handler(request: httpx.Request) -> httpx.Response:
            self.requests.append(request)
            return self._handler(request, len(self.requests))

        self.client = WikiApiClient(transport=httpx.MockTransport(transport_handler), request_delay=0.4, sleep=sleep)

    def run(self, coro_fn):
        # The client stays open between calls (a MockTransport holds no sockets), so one harness can
        # drive several requests, as the cog's long-lived client does.
        return asyncio.run(coro_fn(self.client))


def _page_number(request: httpx.Request) -> int:
    return int(request.url.params.get("page[number]", "1"))


def test_game_version_reads_the_first_row_and_sends_a_one_row_request():
    h = _Harness(lambda req, n: _page([_row(1, "4.10.1-LIVE.9")], page=1, last=786, total=786))
    assert h.run(lambda c: c.get_game_version()) == "4.10.1-LIVE.9"
    params = h.requests[0].url.params
    assert params["filter[has_blueprints]"] == "1" and params["page[size]"] == "1"
    assert "python" not in h.requests[0].headers["user-agent"].lower() and "uex-trading-bot" in h.requests[0].headers["user-agent"]


@pytest.mark.parametrize("body", [{"data": []}, {"data": [{"uuid": "x"}]}, {"data": [{"game_version": "  "}]}, {"nope": 1}])
def test_game_version_raises_when_it_cannot_be_read(body):
    h = _Harness(lambda req, n: httpx.Response(200, json=body))
    with pytest.raises(WikiApiError):
        h.run(lambda c: c.get_game_version())


def test_all_pages_are_assembled_in_order_with_a_pause_between_them():
    rows = [_row(i) for i in range(5)]
    chunks = {1: rows[:2], 2: rows[2:4], 3: rows[4:]}
    h = _Harness(lambda req, n: _page(chunks[_page_number(req)], page=_page_number(req), last=3, total=5))
    got = h.run(lambda c: c.get_blueprint_missions())
    assert [r["uuid"] for r in got] == [f"m{i}" for i in range(5)]
    assert [_page_number(r) for r in h.requests] == [1, 2, 3]
    assert h.sleeps == [0.4, 0.4], "spaced out between pages, none after the last"
    assert all(r.url.params["filter[has_blueprints]"] == "1" for r in h.requests)


def test_a_page_that_comes_back_short_is_an_error_not_a_smaller_list():
    chunks = {1: [_row(0), _row(1)], 2: [_row(2)]}  # the API says 5 in total
    h = _Harness(lambda req, n: _page(chunks[_page_number(req)], page=_page_number(req), last=2, total=5))
    with pytest.raises(WikiApiError, match="reports a total of 5"):
        h.run(lambda c: c.get_blueprint_missions())


def test_a_transient_failure_mid_crawl_is_retried_and_the_crawl_completes():
    chunks = {1: [_row(0)], 2: [_row(1)]}

    def handler(req, n):
        if _page_number(req) == 2 and sum(_page_number(r) == 2 for r in h.requests) == 1:
            return httpx.Response(503)
        return _page(chunks[_page_number(req)], page=_page_number(req), last=2, total=2)

    h = _Harness(handler)
    assert [r["uuid"] for r in h.run(lambda c: c.get_blueprint_missions())] == ["m0", "m1"]
    assert 2.0 in h.sleeps, "backed off before retrying"


def test_a_persistent_failure_mid_crawl_raises_after_bounded_attempts_and_returns_no_partial_data():
    def handler(req, n):
        if _page_number(req) == 2:
            return httpx.Response(500)
        return _page([_row(0)], page=1, last=2, total=2)

    h = _Harness(handler)
    with pytest.raises(WikiApiError, match="failed after"):
        h.run(lambda c: c.get_blueprint_missions())
    assert sum(_page_number(r) == 2 for r in h.requests) == MAX_ATTEMPTS


def test_retry_after_is_honoured_and_capped():
    responses = [httpx.Response(429, headers={"Retry-After": "5"}), httpx.Response(429, headers={"Retry-After": "9999"}),
                 _page([_row(0)], page=1, last=1, total=1)]
    h = _Harness(lambda req, n: responses[n - 1])
    assert len(h.run(lambda c: c.get_blueprint_missions())) == 1
    assert h.sleeps == [5.0, 30.0], "Retry-After respected, but never an unbounded wait"


def test_network_errors_are_retried_then_raised_as_wiki_api_error():
    def handler(req, n):
        raise httpx.ConnectError("boom")

    h = _Harness(handler)
    with pytest.raises(WikiApiError, match="failed after"):
        h.run(lambda c: c.get_game_version())
    assert len(h.requests) == MAX_ATTEMPTS


@pytest.mark.parametrize("status", [400, 401, 403, 404, 422])
def test_client_errors_are_final_and_not_retried(status):
    h = _Harness(lambda req, n: httpx.Response(status))
    with pytest.raises(WikiApiError, match=str(status)):
        h.run(lambda c: c.get_game_version())
    assert len(h.requests) == 1 and h.sleeps == []


@pytest.mark.parametrize("response", [
    httpx.Response(200, content=b"<html>maintenance</html>"), httpx.Response(200, json=[1, 2]),
    httpx.Response(200, json={"data": "nope"}), httpx.Response(200, json={"meta": {}}),
])
def test_a_body_that_is_not_the_expected_shape_is_an_error(response):
    h = _Harness(lambda req, n: response)
    with pytest.raises(WikiApiError):
        h.run(lambda c: c.get_blueprint_missions())


def test_an_absurd_page_count_is_refused_instead_of_crawled():
    h = _Harness(lambda req, n: _page([_row(0)], page=1, last=MAX_PAGES + 1, total=10_000))
    with pytest.raises(WikiApiError, match="refusing"):
        h.run(lambda c: c.get_blueprint_missions())
    assert len(h.requests) == 1


def _meta_page(rows: list[dict], **meta) -> httpx.Response:
    return httpx.Response(200, json={"data": rows, "meta": meta})


@pytest.mark.parametrize("response", [
    httpx.Response(200, json={"data": [{"uuid": "m0"}]}),                                        # no meta at all
    httpx.Response(200, json={"data": [{"uuid": "m0"}], "meta": "nope"}),                        # meta not an object
    _meta_page([_row(0)], current_page=1, total=1),                                              # no last_page
    _meta_page([_row(0)], current_page=1, last_page=1),                                          # no total
    _meta_page([_row(0)], last_page=1, total=1),                                                 # no current_page
    _meta_page([_row(0)], current_page=2, last_page=1, total=1),                                 # wrong page came back
    _meta_page([_row(0)], current_page=1, last_page="1", total=1),                               # string, not a count
    _meta_page([_row(0)], current_page=1, last_page=True, total=1),                              # bool is not a count
    _meta_page([_row(0)], current_page=1, last_page=0, total=1),
    _meta_page([_row(0)], current_page=1, last_page=1, total=-1),
    _meta_page([_row(0)], current_page=1, last_page=1, total=None),
    _meta_page([_row(0)], current_page=1, last_page=1, total=1, per_page=0),
])
def test_missing_or_malformed_pagination_meta_fails_closed_instead_of_assuming_one_page(response):
    """Regression: the old code defaulted an unusable `meta` to 'one page, no total', so a truncated or
    reshaped response looked like a complete crawl and could replace a good snapshot."""
    h = _Harness(lambda req, n: response)
    with pytest.raises(WikiApiError):
        h.run(lambda c: c.get_blueprint_missions())


def test_a_page_size_that_contradicts_the_page_count_is_rejected():
    h = _Harness(lambda req, n: _meta_page([_row(0)], current_page=1, last_page=5, total=10, per_page=100))
    with pytest.raises(WikiApiError, match="per page but 5 pages"):
        h.run(lambda c: c.get_blueprint_missions())


def test_a_short_page_in_the_middle_of_the_crawl_is_caught_when_the_page_size_is_known():
    def handler(req, n):
        return _meta_page([_row(0)], current_page=_page_number(req), last_page=2, total=101, per_page=100)

    h = _Harness(handler)
    with pytest.raises(WikiApiError, match="returned 1 rows, expected 100"):
        h.run(lambda c: c.get_blueprint_missions())
    assert len(h.requests) == 1, "stops at the bad page instead of crawling on"


def test_pagination_that_shifts_mid_crawl_is_rejected_as_a_mixed_snapshot():
    """The API's data changing between page 1 and page 2 (a new game patch landing) would otherwise
    yield rows from two different versions that still add up to the second page's own total."""
    def handler(req, n):
        page = _page_number(req)
        return _meta_page([_row(page)], current_page=page, last_page=2, total=2 if page == 1 else 3)

    h = _Harness(handler)
    with pytest.raises(WikiApiError, match="changed mid-crawl"):
        h.run(lambda c: c.get_blueprint_missions())

    def page_count_shift(req, n):
        page = _page_number(req)
        return _meta_page([_row(page)], current_page=page, last_page=2 if page == 1 else 3, total=2)

    with pytest.raises(WikiApiError, match="changed mid-crawl"):
        _Harness(page_count_shift).run(lambda c: c.get_blueprint_missions())


def test_a_consistent_multi_page_crawl_with_a_page_size_passes():
    rows = [_row(i) for i in range(5)]

    def handler(req, n):
        page = _page_number(req)
        return _meta_page(rows[(page - 1) * 2: page * 2], current_page=page, last_page=3, total=5, per_page=2)

    got = _Harness(handler).run(lambda c: c.get_blueprint_missions())
    assert [r["uuid"] for r in got] == [f"m{i}" for i in range(5)]


def test_an_empty_but_well_formed_result_is_returned_empty_for_the_caller_to_judge():
    """The client reports what the API says; refusing to replace a snapshot with an empty one is the
    sync's job, not the transport's."""
    h = _Harness(lambda req, n: _meta_page([], current_page=1, last_page=1, total=0))
    assert h.run(lambda c: c.get_blueprint_missions()) == []


def test_blueprint_detail_validates_the_uuid_before_any_request_and_unwraps_the_envelope():
    h = _Harness(lambda req, n: httpx.Response(200, json={"data": {"uuid": UUID, "unlocking_missions": []}}))
    for bad in ("", "../missions", "not-a-uuid", UUID + "/x"):
        with pytest.raises(WikiApiError):
            h.run(lambda c, b=bad: c.get_blueprint_detail(b))
    assert h.requests == [], "a bad uuid must never reach the network (no path injection)"
    assert h.run(lambda c: c.get_blueprint_detail(UUID))["uuid"] == UUID
    assert h.requests[0].url.path.endswith(f"/blueprints/{UUID}")


def test_blueprint_detail_accepts_a_bare_object_and_rejects_other_shapes():
    h = _Harness(lambda req, n: httpx.Response(200, json={"uuid": UUID}))
    assert h.run(lambda c: c.get_blueprint_detail(UUID)) == {"uuid": UUID}
    h2 = _Harness(lambda req, n: httpx.Response(200, json={"data": [1]}))
    with pytest.raises(WikiApiError):
        h2.run(lambda c: c.get_blueprint_detail(UUID))


# -- get_vehicle_ports --------------------------------------------------------------------------

def _vehicle(name: str, ports: list) -> dict:
    return {"uuid": f"v-{name}", "name": name, "ports": ports}


def test_vehicle_ports_returns_the_exact_case_insensitive_name_match_only():
    rows = [_vehicle("Avenger Stalker", [{"name": "hardpoint_power_plant"}]), _vehicle("Avenger Titan", [{"name": "x"}])]
    h = _Harness(lambda req, n: httpx.Response(200, json={"data": rows}))
    ports = h.run(lambda c: c.get_vehicle_ports("avenger stalker"))
    assert ports == [{"name": "hardpoint_power_plant"}]
    assert h.requests[0].url.params["filter[name]"] == "avenger stalker"


def test_vehicle_ports_returns_empty_when_no_row_or_more_than_one_row_matches_exactly():
    h_none = _Harness(lambda req, n: httpx.Response(200, json={"data": [_vehicle("Avenger Titan", [])]}))
    assert h_none.run(lambda c: c.get_vehicle_ports("Avenger Stalker")) == []

    # filter[name] matches by substring - "Avenger" alone must not silently pick one variant.
    rows = [_vehicle("Avenger Stalker", [{"name": "a"}]), _vehicle("Avenger Titan", [{"name": "b"}])]
    h_ambiguous = _Harness(lambda req, n: httpx.Response(200, json={"data": rows}))
    assert h_ambiguous.run(lambda c: c.get_vehicle_ports("Avenger")) == []


def test_vehicle_ports_tolerates_a_missing_or_malformed_ports_field():
    h = _Harness(lambda req, n: httpx.Response(200, json={"data": [{"name": "Avenger Stalker"}]}))
    assert h.run(lambda c: c.get_vehicle_ports("Avenger Stalker")) == []


# -- get_item_detail ----------------------------------------------------------------------------

def test_item_detail_validates_the_uuid_before_any_request_and_checks_identity():
    h = _Harness(lambda req, n: httpx.Response(200, json={"data": {"uuid": UUID, "name": "PowerBolt"}}))
    for bad in ("", "../items", "not-a-uuid", UUID + "/x"):
        with pytest.raises(WikiApiError):
            h.run(lambda c, b=bad: c.get_item_detail(b))
    assert h.requests == [], "a bad uuid must never reach the network (no path injection)"

    detail = h.run(lambda c: c.get_item_detail(UUID))
    assert detail == {"uuid": UUID, "name": "PowerBolt"}
    assert h.requests[0].url.path.endswith(f"/items/{UUID}")


def test_item_detail_rejects_a_mismatched_or_missing_identity():
    h = _Harness(lambda req, n: httpx.Response(200, json={"data": {"uuid": "some-other-uuid"}}))
    with pytest.raises(WikiApiError):
        h.run(lambda c: c.get_item_detail(UUID))

    h2 = _Harness(lambda req, n: httpx.Response(200, json={"data": [1]}))
    with pytest.raises(WikiApiError):
        h2.run(lambda c: c.get_item_detail(UUID))


def test_vehicle_loadout_also_returns_the_ships_own_port_tags():
    row = dict(_vehicle("Avenger Titan", [{"name": "hp"}]), port_tags=["AEGS_Avenger_Base", 3])
    h = _Harness(lambda req, n: httpx.Response(200, json={"data": [row]}))
    assert h.run(lambda c: c.get_vehicle_loadout("Avenger Titan")) == ([{"name": "hp"}], ["AEGS_Avenger_Base"])
    h_none = _Harness(lambda req, n: httpx.Response(200, json={"data": []}))
    assert h_none.run(lambda c: c.get_vehicle_loadout("Avenger Titan")) == ([], [])


def test_vehicle_loadout_raises_for_a_name_no_single_row_is_the_base_of():
    """Rows of one name with no class_name to tell them apart (or the PYAM Exec pairs, whose
    class names don't extend each other): never a guess, and never an empty answer the finder
    would show as "no slots"."""
    rows = [_vehicle("Cutlass Black", [{"name": "a"}]), _vehicle("cutlass black ", [{"name": "b"}]),
            _vehicle("Cutlass Black PYAM Exec", [{"name": "c"}])]
    h = _Harness(lambda req, n: httpx.Response(200, json={"data": rows}))
    with pytest.raises(WikiDuplicateNameError) as caught:
        h.run(lambda c: c.get_vehicle_loadout("Cutlass Black"))
    assert caught.value.count == 2 and caught.value.vehicle_name == "Cutlass Black"
    assert isinstance(caught.value, WikiApiError)
    # A substring-only match (the PYAM Exec) is still just one exact row away from resolving.
    assert h.run(lambda c: c.get_vehicle_loadout("Cutlass Black PYAM Exec")) == ([{"name": "c"}], [])


# Live wiki class names (2026-10-02): every pair a ship UEX lists, plus the PYAM Exec kind.
EDITIONS = {
    "Cutlass Black": ["DRAK_Cutlass_Black", "DRAK_Cutlass_Black_BIS2950"],
    "Carrack": ["ANVL_Carrack", "ANVL_Carrack_BIS2950"],
    "Polaris": ["RSI_Polaris", "RSI_Polaris_Collector_Military"],
    "Hammerhead": ["AEGS_Hammerhead_GS", "AEGS_Hammerhead"],
    "Idris-P": ["AEGS_Idris_P_FW_25", "AEGS_Idris_P", "AEGS_Idris_P_TSG"],
    "F8C Lightning": ["ANVL_Lightning_F8C_Plat", "ANVL_Lightning_F8C"],
    "Zeus Mk II CL": ["RSI_Zeus_CL", "RSI_Zeus_CL_Collector_Indust"],
}


@pytest.mark.parametrize("name,class_names", EDITIONS.items(), ids=list(EDITIONS))
def test_a_ship_with_editions_resolves_to_its_plain_row(name, class_names):
    rows = [{"uuid": f"v-{c}", "name": name, "class_name": c} for c in class_names]
    base = min(class_names, key=len)
    assert base_vehicle_row(rows)["class_name"] == base, "whatever order the wiki lists them in"
    assert base_vehicle_row(list(reversed(rows)))["class_name"] == base


def test_no_base_row_means_no_guess():
    pyam = [{"class_name": "DRAK_Corsair_Exec_Military"}, {"class_name": "DRAK_Corsair_Exec_StealthIndustrial"}]
    assert base_vehicle_row(pyam) is None, "neither extends the other"
    assert base_vehicle_row([{"class_name": "X"}, {"class_name": "X"}]) is None, "two rows of one class name"
    assert base_vehicle_row([{"class_name": "X"}, {}]) is None, "a row with no class name can't be placed"
    assert base_vehicle_row([{}, {"class_name": "_Edition"}]) is None, "nor be the base"
    # A prefix only counts at a word boundary: 'RSI_Zeus' isn't the base of 'RSI_ZeusX'.
    assert base_vehicle_row([{"class_name": "RSI_Zeus"}, {"class_name": "RSI_ZeusX"}]) is None
    assert base_vehicle_row([{"class_name": "Only"}]) == {"class_name": "Only"}


def test_vehicle_loadout_reads_the_plain_ships_slots_not_an_editions():
    rows = [dict(_vehicle("Cutlass Black", [{"name": "edition"}]), class_name="DRAK_Cutlass_Black_BIS2950"),
            dict(_vehicle("Cutlass Black", [{"name": "plain"}]), class_name="DRAK_Cutlass_Black",
                 port_tags=["DRAK_Cutlass_Base"])]
    h = _Harness(lambda req, n: httpx.Response(200, json={"data": rows}))
    assert h.run(lambda c: c.get_vehicle_loadout("Cutlass Black")) == ([{"name": "plain"}], ["DRAK_Cutlass_Base"])


# -- get_vehicle_stock_ports (/ship-loadout's nested stock items) -------------------------------

VEHICLE_UUID = "0079c5d5-1678-4f8c-85ba-18ca8f642af6"  # the Avenger Titan's, live


def _stock_handler(detail_body, *, rows=None):
    rows = rows if rows is not None else [{"uuid": VEHICLE_UUID, "name": "Avenger Titan", "ports": None}]

    def handler(req, n):
        if req.url.path.endswith("/vehicles"):
            return httpx.Response(200, json={"data": rows})
        return httpx.Response(200, json=detail_body)
    return handler


def test_vehicle_stock_ports_reads_the_single_vehicle_endpoint_by_the_exact_rows_uuid():
    nested = [{"name": "hardpoint_weapon_class2_nose", "equipped_item_uuid": "gimbal",
               "ports": [{"name": "hardpoint_class_2", "equipped_item_uuid": "revenant"}]}]
    h = _Harness(_stock_handler({"data": {"uuid": VEHICLE_UUID, "ports": nested}}))
    assert h.run(lambda c: c.get_vehicle_stock_ports("avenger titan")) == nested
    assert h.requests[0].url.params["filter[name]"] == "avenger titan"
    assert h.requests[1].url.path.endswith(f"/vehicles/{VEHICLE_UUID}")


def test_vehicle_stock_ports_reads_the_plain_ship_of_a_name_with_editions():
    nested = [{"name": "hardpoint_power_plant", "equipped_item_uuid": "pp"}]
    rows = [{"uuid": OTHER_UUID, "name": "Cutlass Black", "class_name": "DRAK_Cutlass_Black_BIS2950"},
            {"uuid": VEHICLE_UUID, "name": "Cutlass Black", "class_name": "DRAK_Cutlass_Black"}]
    h = _Harness(_stock_handler({"data": {"uuid": VEHICLE_UUID, "ports": nested}}, rows=rows))
    assert h.run(lambda c: c.get_vehicle_stock_ports("Cutlass Black")) == nested
    assert h.requests[1].url.path.endswith(f"/vehicles/{VEHICLE_UUID}")


def test_vehicle_stock_ports_is_empty_without_one_exact_ship_and_never_requests_a_bad_uuid():
    titan = {"uuid": VEHICLE_UUID, "name": "Cutlass Black"}
    # Two rows of one name and nothing to tell them apart: no guess.
    h_two = _Harness(_stock_handler({}, rows=[titan, dict(titan, uuid=OTHER_UUID)]))
    assert h_two.run(lambda c: c.get_vehicle_stock_ports("Cutlass Black")) == []
    for bad in (None, "../items", 7):
        h_bad = _Harness(_stock_handler({}, rows=[dict(titan, uuid=bad)]))
        assert h_bad.run(lambda c: c.get_vehicle_stock_ports("Cutlass Black")) == []
        assert len(h_bad.requests) == 1, "a missing or malformed uuid never reaches the network"


def test_vehicle_stock_ports_rejects_another_ships_detail_and_tolerates_no_ports():
    h = _Harness(_stock_handler({"data": {"uuid": OTHER_UUID, "ports": [{"name": "x"}]}}))
    with pytest.raises(WikiApiError):
        h.run(lambda c: c.get_vehicle_stock_ports("Avenger Titan"))
    h_none = _Harness(_stock_handler({"data": {"uuid": VEHICLE_UUID, "ports": None}}))
    assert h_none.run(lambda c: c.get_vehicle_stock_ports("Avenger Titan")) == []


# -- find_item_variants_by_name / find_item_detail_by_name -------------------------------------

OTHER_UUID = "22222222-2222-4222-8222-222222222222"


def test_item_variants_are_every_exact_name_match_only():
    rows = [{"uuid": UUID, "name": "VariPuck S4 Gimbal Mount", "required_tags": []},
            {"uuid": OTHER_UUID, "name": "VariPuck S4 Gimbal Mount", "required_tags": ["RSI_Polaris"]},
            {"uuid": "x", "name": "VariPuck S4 Gimbal Mount Pro"}]
    h = _Harness(lambda req, n: httpx.Response(200, json={"data": rows}))
    variants = h.run(lambda c: c.find_item_variants_by_name("variPuck s4 gimbal mount"))
    assert [v["uuid"] for v in variants] == [UUID, OTHER_UUID]
    assert h.run(lambda c: c.find_item_variants_by_name("  ")) == []


def test_item_detail_by_name_needs_exactly_one_exact_match():
    def handler(req, n):
        if req.url.path.endswith("/items"):
            name = req.url.params["filter[name]"]
            rows = {"Fleming": [{"uuid": UUID, "name": "Fleming"}, {"uuid": "y", "name": "Fleming Pro"}],
                    "VariPuck": [{"uuid": UUID, "name": "VariPuck"}, {"uuid": OTHER_UUID, "name": "VariPuck"}]}
            return httpx.Response(200, json={"data": rows.get(name, [])})
        return httpx.Response(200, json={"data": {"uuid": UUID, "name": "Fleming", "type": "Radar"}})

    h = _Harness(handler)
    assert h.run(lambda c: c.find_item_detail_by_name("Fleming"))["type"] == "Radar"
    assert h.run(lambda c: c.find_item_detail_by_name("VariPuck")) is None, "two same-named items: no guess"
    assert h.run(lambda c: c.find_item_detail_by_name("Nothing")) is None


def test_a_wiki_that_never_answers_raises_the_unavailable_subclass():
    """Exhausted retries (network errors, 429s or 5xx) mean "the wiki didn't answer", which a
    caller caching misses must tell apart from a definite "no such item" (ship parts: REL-1)."""
    h = _Harness(lambda req, n: httpx.Response(503))
    with pytest.raises(WikiUnavailableError):
        h.run(lambda c: c.get_item_detail(UUID))
    assert len(h.requests) == MAX_ATTEMPTS


def test_a_definite_404_is_a_plain_wiki_error_not_an_outage():
    h = _Harness(lambda req, n: httpx.Response(404))
    with pytest.raises(WikiApiError) as info:
        h.run(lambda c: c.get_item_detail(UUID))
    assert not isinstance(info.value, WikiUnavailableError)
    assert len(h.requests) == 1, "not retried"
