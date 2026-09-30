"""Blueprint search - the pure logic in bot/uex/blueprints.py.

Fixtures in tests/fixtures are trimmed copies of real Star Citizen Wiki API responses (4.10.0-LIVE):
`blueprint_names.tsv` is every blueprint the API attaches to a mission (680 uuids, 679 distinct
names - one name is shared by two uuids), `blueprint_missions_sample.json` is twelve real missions
chosen for their edge cases, `blueprint_detail_25pct.json` is the one real blueprint page whose
mission is NOT a guaranteed reward.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from bot.uex.blueprints import (
    BlueprintIndex,
    BlueprintRef,
    describe_chance,
    group_line,
    group_missions,
    normalize_name,
    parse_chances,
    parse_mission,
    parse_missions,
    snapshot_is_current,
    sync_result_is_plausible,
)

FIXTURES = Path(__file__).parent / "fixtures"


def _refs() -> list[BlueprintRef]:
    pairs = [line.split("\t", 1) for line in (FIXTURES / "blueprint_names.tsv").read_text(encoding="utf-8").splitlines() if line]
    return [BlueprintRef(uuid, name) for uuid, name in pairs]


def _missions_rows() -> list[dict]:
    return json.loads((FIXTURES / "blueprint_missions_sample.json").read_text(encoding="utf-8"))


REFS = _refs()
INDEX = BlueprintIndex(REFS)


# -- parsing -----------------------------------------------------------------------------------


def test_every_real_fixture_mission_parses_with_its_full_pool():
    rows = _missions_rows()
    parsed = parse_missions(rows)
    assert len(parsed) == len(rows) == 12
    for row, mission in zip(rows, parsed):
        assert mission.uuid == row["uuid"] and mission.pool_size == len(row["blueprints"])


def test_missing_and_null_fields_stay_unknown_instead_of_becoming_zero_or_text():
    by_uuid = {m.uuid: m for m in parse_missions(_missions_rows())}
    no_rank = next(m for m in by_uuid.values() if m.rank_name is None)
    assert no_rank.rank_name is None
    no_rep = next(r for r in _missions_rows() if r["reputation_amount"] is None)
    assert by_uuid[no_rep["uuid"]].reputation is None, "unknown reputation must not be reported as 0"
    assert not hasattr(no_rank, "reward_min"), "the API's cash fields are null for every blueprint mission - none are modelled"


def test_illegal_and_pool_extremes_are_preserved():
    parsed = parse_missions(_missions_rows())
    assert any(m.illegal for m in parsed) and any(not m.illegal for m in parsed)
    assert min(m.pool_size for m in parsed) == 1 and max(m.pool_size for m in parsed) >= 18


def test_rows_that_cannot_describe_a_blueprint_contract_are_skipped_not_guessed():
    good = _missions_rows()[0]
    bad = [
        None, "text", {}, {**good, "uuid": None}, {**good, "title": "  "}, {**good, "blueprints": []},
        {**good, "blueprints": None}, {**good, "blueprints": [{"name": "X"}, {"uuid": "u"}, "junk", None]},
    ]
    assert parse_missions(bad) == []
    assert len(parse_missions(bad + [good])) == 1


def test_a_mission_the_api_lists_twice_is_kept_once():
    """LIVE FINDING: the real has_blueprints listing returned 786 rows for 784 distinct missions (two
    identical repeats). Left in, the snapshot's PRIMARY KEY rejected the entire sync."""
    rows = _missions_rows()
    doubled = rows + [rows[0], rows[3]]
    parsed = parse_missions(doubled)
    assert [m.uuid for m in parsed] == [r["uuid"] for r in rows]
    variant = {**rows[0], "title": "Same uuid, different text"}
    assert parse_missions([rows[0], variant])[0].title == rows[0]["title"], "first occurrence wins, deterministically"


def test_duplicate_pool_entries_collapse_and_giver_falls_back_to_faction():
    row = {**_missions_rows()[0], "mission_giver": None, "faction": {"name": "Some Faction"}}
    row["blueprints"] = row["blueprints"] + row["blueprints"]
    mission = parse_mission(row)
    assert mission.pool_size == len(_missions_rows()[0]["blueprints"])
    assert mission.giver == "Some Faction"
    assert parse_mission({**row, "faction": None, "mission_giver": ""}).giver == "Unknown giver"


# -- snapshot policy ---------------------------------------------------------------------------

NOW = datetime(2026, 9, 18, 12, 0)


@pytest.mark.parametrize(
    ("stored_version", "age", "remote", "expected"),
    [
        ("4.10.0", timedelta(hours=1), "4.10.0", True),       # same version, fresh: nothing to do
        ("4.10.0", timedelta(hours=1), "4.10.1", False),      # patch landed: re-sync
        ("4.10.0", timedelta(days=7) - timedelta(seconds=1), "4.10.0", True),   # just under the age cap
        ("4.10.0", timedelta(days=7), "4.10.0", False),       # at the cap: refresh (API may fix data without a version bump)
        ("4.10.0", timedelta(days=30), "4.10.0", False),
        (None, timedelta(hours=1), "4.10.0", False),          # nothing stored yet
    ],
)
def test_snapshot_is_current_matrix(stored_version, age, remote, expected):
    assert snapshot_is_current(stored_version, NOW - age, remote, NOW) is expected
    assert snapshot_is_current("4.10.0", None, "4.10.0", NOW) is False, "an unknown sync time is never trusted"


@pytest.mark.parametrize(
    ("new", "previous", "expected"),
    [(0, None, False), (0, 700, False), (5, None, True), (400, 786, True), (393, 786, True), (392, 786, False), (10, 786, False)],
)
def test_sync_result_is_plausible_rejects_empty_and_truncated_responses(new, previous, expected):
    assert sync_result_is_plausible(new, previous) is expected


# -- drop chance -------------------------------------------------------------------------------


def test_real_blueprint_page_yields_the_mission_chance_by_uuid():
    detail = json.loads((FIXTURES / "blueprint_detail_25pct.json").read_text(encoding="utf-8"))
    assert parse_chances(detail) == {"9bd0c215-ece8-42b6-9145-f50664b252d1": 0.25}
    assert parse_chances({"data": detail}) == parse_chances(detail), "the API's data envelope is accepted"


def test_out_of_domain_or_untrusted_chances_are_dropped_not_coerced():
    good_url = "https://api.star-citizen.wiki/missions/9bd0c215-ece8-42b6-9145-f50664b252d1"
    for bad in (0, -0.5, 1.5, "1", "0.25", None, True, float("nan"), float("inf")):
        assert parse_chances({"unlocking_missions": [{"web_url": good_url, "chance": bad}]}) == {}, bad
    slug = "https://api.star-citizen.wiki/missions/some-slug-5"
    assert parse_chances({"unlocking_missions": [{"web_url": slug, "chance": 1}, {"chance": 1}, "junk"]}) == {}
    assert parse_chances(None) == {} and parse_chances({"unlocking_missions": None}) == {}


def test_describe_chance_is_about_the_reward_and_never_claims_per_item_odds():
    assert describe_chance([1.0]) == "always grants one"
    assert describe_chance([1.0, 1.0]) == "always grants one"
    assert describe_chance([0.25]) == "25% chance to grant one"
    assert describe_chance([0.25, 1.0]) == "25%-100% chance to grant one (varies by variant)"
    assert describe_chance([0.5, 0.5]) == "50% chance to grant one"
    for text in (describe_chance([1.0]), describe_chance([0.25])):
        assert "1/" not in text and "each" not in text


def test_describe_chance_declines_unless_every_variant_is_known():
    assert describe_chance([]) is None
    assert describe_chance([None]) is None
    assert describe_chance([1.0, None]) is None, "a claim about x2 variants must not rest on checking only one"


# -- name matching -----------------------------------------------------------------------------


def _name(query: str) -> str | None:
    match = INDEX.match(query)
    return match.name if match.status == "resolved" else None


def test_normalize_name_folds_quotes_case_punctuation_and_typography():
    assert normalize_name('Prism "Deep Sea" Laser Shotgun') == "prism deep sea laser shotgun"
    assert normalize_name("Prism Laser‑Shotgun") == "prism laser shotgun"
    assert normalize_name("Killshot “Dominion Camo” Rifle") == "killshot dominion camo rifle"
    assert normalize_name("  !!!  ") == ""


def test_exact_names_resolve_regardless_of_case_quotes_and_punctuation():
    assert _name('prism deep sea laser shotgun') == 'Prism "Deep Sea" Laser Shotgun'
    assert _name("PRISM LASER SHOTGUN") == "Prism Laser Shotgun"
    assert _name("Prism Laser Shotgun") == "Prism Laser Shotgun"
    assert INDEX.match("Prism Laser Shotgun").corrected is False


def test_a_shared_name_requires_an_explicit_blueprint_identity():
    seen = {}
    for ref in REFS:
        seen.setdefault(normalize_name(ref.name), []).append(ref.uuid)
    shared = {n: u for n, u in seen.items() if len(u) > 1}
    assert set(shared) == {"broadspec", "fullforce"}
    for name, uuids in shared.items():
        match = INDEX.match(name)
        assert match.status == "ambiguous"
        assert len(match.candidates) == len(uuids)
        for uuid in uuids:
            assert INDEX.match(uuid).uuids == (uuid,)
        for candidate in match.candidates:
            assert len(INDEX.match(candidate).uuids) == 1


def test_a_blueprints_identity_is_the_uuid_in_its_link_not_the_crafted_items_uuid():
    """LIVE FINDING: a mission's blueprint entry has two uuids. `uuid` is the crafted ITEM's id; using it
    as the blueprint id made every /api/blueprints/{uuid} lookup 404. The real id is in `link`."""
    blueprint_id = "e3097998-28ef-48ad-a220-2ddaa1a85ce3"
    item_id = "c098e722-902a-435b-83f8-a96cec36a012"
    row = {**_missions_rows()[0], "blueprints": [
        {"name": "Killshot Rifle", "uuid": item_id, "link": f"https://api.star-citizen.wiki/api/blueprints/{blueprint_id.upper()}/"},
    ]}
    (ref,) = parse_mission(row).pool
    assert ref.uuid == blueprint_id, "link-derived, lower-cased for consistent comparison"

    no_link = {**row, "blueprints": [{"name": "Killshot Rifle", "uuid": item_id}]}
    assert parse_mission(no_link).pool[0].uuid == item_id, "with no usable link, fall back to a stable id (detail lookup will fail soft)"
    bad_link = {**row, "blueprints": [{"name": "Killshot Rifle", "uuid": item_id, "link": "https://x/api/blueprints/not-a-uuid"}]}
    assert parse_mission(bad_link).pool[0].uuid == item_id
    for real in _missions_rows():
        for entry, ref in zip(real["blueprints"], parse_mission(real).pool):
            assert ref.uuid == entry["link"].rsplit("/", 1)[-1] != entry["uuid"], "real fixture rows: id comes from the link"


def test_an_exact_name_beats_the_longer_variants_that_contain_it():
    assert _name("Arclight Pistol") == "Arclight Pistol"
    assert _name("arclight pistol battery 30 cap") == "Arclight Pistol Battery (30 cap)"


def test_a_real_base_item_wins_over_its_colored_variants_but_a_shared_fragment_is_ambiguous():
    """The game has a plain 'Antium Arms' AND 'Antium Arms Maroon/Moss Camo/...': the exact name is
    the answer for 'antium arms'. A fragment that no single name fully specifies is a real
    ambiguity - list the candidates, never pick one."""
    exact = INDEX.match("antium arms")
    assert (exact.status, exact.name) == ("resolved", "Antium Arms")
    assert _name("antium arms maroon") == "Antium Arms Maroon"

    family = INDEX.match("antium")
    assert family.status == "ambiguous" and family.name is None and family.total_candidates >= 8
    assert {"Antium Arms", "Antium Arms Maroon", "Antium Core Maroon"} <= set(family.candidates) or len(family.candidates) == 10
    assert all("Antium" in name for name in family.candidates)

    colour = INDEX.match("moss camo")
    assert colour.status == "ambiguous" and all("Moss Camo" in n for n in colour.candidates)
    assert INDEX.match("killshot").status == "ambiguous"


def test_typos_are_corrected_only_when_one_name_fits():
    for typo, real in (
        ("kilshot rifle", "Killshot Rifle"), ("arclght pistol", "Arclight Pistol"),
        ("arclihgt pistol", "Arclight Pistol"), ("antium arms maron", "Antium Arms Maroon"),
        ("deadrig shotgn", "Deadrig Shotgun"),
    ):
        match = INDEX.match(typo)
        assert (match.status, match.name, match.corrected) == ("resolved", real, True), typo


def test_prefix_of_a_word_resolves_when_unique():
    assert _name("arclight pistol batt") == "Arclight Pistol Battery (30 cap)"


def test_model_numbers_and_numerals_must_match_exactly_because_one_character_names_a_different_item():
    assert _name("omnisky vi cannon") == "Omnisky VI Cannon"
    assert INDEX.match("omnisky vx cannon").status == "none"
    assert INDEX.match("klein s7 mining laser").status == "none", "S7 is not a misspelling of Klein-S1/S2"
    assert _name("klein s1 mining laser") == "Klein-S1 Mining Laser"
    assert _name("klein s2 mining laser") == "Klein-S2 Mining Laser", "the game really has both - each must resolve to itself"


def test_unknown_input_is_none_and_offers_near_names_only_when_half_the_words_match():
    assert INDEX.match("").status == "none" and INDEX.match("!!!").status == "none"
    assert INDEX.match("xylophone").candidates == ()
    near = INDEX.match("zzzzzz rifle")
    assert near.status == "none" and near.candidates and all("rifle" in n.lower() for n in near.candidates)
    assert len(near.candidates) <= 5


def test_a_typo_too_far_to_auto_correct_still_gets_a_did_you_mean_with_the_plain_item_first():
    """'arclite' is 3 edits from 'arclight' - beyond the automatic-correction budget - but a
    suggestion is only a question, so it may be looser. The plain item outranks its named variants."""
    match = INDEX.match("arclite pistol")
    assert match.status == "none" and match.name is None
    assert match.candidates[0] == "Arclight Pistol" and len(match.candidates) <= 5


def test_a_very_short_typo_query_is_declined_rather_than_corrected():
    assert INDEX.match("ryf").status == "none"


def test_a_single_character_slip_never_resolves_to_a_different_real_blueprint():
    """Property check over every real name: corrupt it three different ways; the result may be the
    right name, an ambiguity that lists it, or 'none' - but NEVER a confident wrong item. This is the
    guarantee that makes automatic correction safe; how often it recovers is checked separately."""
    wrong: list[tuple[str, str, str]] = []
    recovered = attempts = 0
    for name in INDEX.names[::3]:  # every third name: ~500 corruptions, ~6s (the full 1,550 measured 99.9% recovered, 0 wrong)
        words = name.split(" ")
        longest = max(range(len(words)), key=lambda i: len(words[i]))
        word = words[longest]
        if len(word) < 5 or not word.isalpha():
            continue
        corruptions = {
            "drop": word[:2] + word[3:],
            "swap": word[:1] + word[2] + word[1] + word[3:],
            "sub": word[:2] + ("q" if word[2] != "q" else "z") + word[3:],
        }
        for kind, bad_word in corruptions.items():
            query = " ".join(words[:longest] + [bad_word] + words[longest + 1:])
            match = INDEX.match(query)
            attempts += 1
            if match.status == "resolved":
                if match.name == name:
                    recovered += 1
                else:
                    wrong.append((kind, query, match.name))
            elif match.status == "ambiguous" and name in match.candidates:
                recovered += 1
    assert not wrong, f"confident wrong resolutions: {wrong[:8]}"
    assert attempts > 400
    assert recovered / attempts >= 0.97, f"recovered only {recovered}/{attempts}"


def test_every_real_name_resolves_to_itself():
    for name in INDEX.names:
        match = INDEX.match(name)
        assert match.status == "resolved" and match.name == name, name


def test_autocomplete_is_bounded_ranked_and_discord_safe():
    assert len(INDEX.autocomplete("")) == 25
    assert all(len(name) <= 100 for name in INDEX.names), "Discord choice names/values are capped at 100 chars"
    hits = INDEX.autocomplete("kilshot")
    assert hits and hits[0].startswith("Killshot") and len(hits) <= 25
    assert INDEX.autocomplete("antium arm")[0].startswith("Antium Arms")
    assert INDEX.autocomplete("zzzzzzzz") == []
    assert len(INDEX.autocomplete("a")) <= 25


# -- grouping and lines ------------------------------------------------------------------------


def test_identical_looking_missions_collapse_but_a_different_pool_stays_separate():
    missions = parse_missions(_missions_rows())
    same_title = [m for m in missions if m.title == "Additional Resources For Research"]
    assert len(same_title) == 2 and same_title[0].pool_size != same_title[1].pool_size
    groups = group_missions(missions)
    assert sum(g.title == "Additional Resources For Research" for g in groups) == 2, "different pools = different groups"

    clone = parse_mission({**_missions_rows()[0], "uuid": "00000000-0000-4000-8000-000000000001"})
    original = parse_missions(_missions_rows())[0]
    collapsed = [g for g in group_missions([original, clone]) if g.title == original.title]
    assert len(collapsed) == 1 and len(collapsed[0].mission_uuids) == 2


def test_group_line_carries_pool_chance_rank_and_warnings_in_one_line():
    missions = parse_missions(_missions_rows())
    illegal = next(g for g in group_missions(missions) if g.illegal)
    line = group_line(illegal, "always grants one")
    assert "ILLEGAL" in line and "always grants one" in line and f"pool of {illegal.pool_size}" in line
    assert "\n" not in line and line.startswith("• **")


def test_group_line_never_invents_missing_facts():
    missions = parse_missions(_missions_rows())
    unknown_rep = next(g for g in group_missions(missions) if g.reputation is None)
    line = group_line(unknown_rep, None)
    assert "reward chance unavailable" in line and "rep" not in line
    no_rank = next(g for g in group_missions(missions) if g.rank_name is None)
    assert "needs" not in group_line(no_rank, None)
