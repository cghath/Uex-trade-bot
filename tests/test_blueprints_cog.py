"""/blueprint-search end to end: the real WikiApiClient, Blueprints cog and Database, with only the HTTP
boundary faked (httpx.MockTransport serving the real-data fixtures) and Discord's interaction stubbed.

Guarantees under test (CONTRIBUTING.md test plan):
  1. Sync policy - re-sync on a new game version or a week-old snapshot, skip otherwise; a first search
     acknowledges Discord BEFORE any network work; two simultaneous first searches crawl once.
  2. A bad sync never costs good data - server error, truncated crawl, empty answer, shrunken answer, or a
     DB failure all leave the old snapshot searchable, and the NEXT cycle recovers (through the real loop body).
  3. Answers - exact / typo / ambiguous / unknown / unavailable each give the right kind of reply; the mission's
     reward chance shows as 'always' or '25%'; a failed detail lookup degrades to 'unavailable', never blocks.
  4. Discord limits - oversized results fall back to text pages that still carry the disclosure, and so does a
     rejected embed send. Autocomplete is bounded and never syncs.
"""
from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock

import discord
import httpx
from cryptography.fernet import Fernet

from bot.cogs import blueprints as blueprints_module
from bot.cogs.blueprints import Blueprints, POOL_DISCLOSURE, blueprint_autocomplete
from bot.db.database import Database
from bot.uex.blueprints import BlueprintMission, BlueprintRef, parse_missions
from bot.wiki_api import WikiApiClient

FIXTURES = Path(__file__).parent / "fixtures"
ROWS = json.loads((FIXTURES / "blueprint_missions_sample.json").read_text(encoding="utf-8"))
VERSION = "4.10.0-LIVE.12519617"
NEW_VERSION = "4.10.1-LIVE.12660092"
QUARTER_MISSION = "9bd0c215-ece8-42b6-9145-f50664b252d1"  # the real 25%-chance contract
BATTERY = "Prism Laser Shotgun Battery (20 cap)"


async def _nosleep(_seconds: float) -> None:
    return None


class FakeWiki:
    """The wiki API as an httpx handler. `log` records what was asked, in order."""

    def __init__(self, rows=None, version=VERSION):
        self.rows = [dict(r) for r in (rows if rows is not None else ROWS)]
        self.version = version
        self.log: list[str] = []
        self.events: list[str] | None = None
        self.missions_status = 200
        self.detail_status = 200
        self.claim_total: int | None = None
        # index -> the game_version stamped on that crawled row (default: the version the probe reported),
        # so a test can make the crawl disagree with the probe, as when a patch lands between the two.
        self.row_stamp = None
        self.chances = {QUARTER_MISSION: 0.25}

    def handler(self, request: httpx.Request) -> httpx.Response:
        if self.events is not None:
            self.events.append("http")
        path, params = request.url.path, request.url.params
        if path.endswith("/missions"):
            size, page = int(params.get("page[size]", 100)), int(params.get("page[number]", 1))
            if self.missions_status != 200:
                self.log.append("missions-error")
                return httpx.Response(self.missions_status)
            if size == 1:
                self.log.append("version")
                first = [dict(self.rows[0], game_version=self.version)] if self.rows else []
                return httpx.Response(200, json={"data": first, "meta": {"last_page": 1, "total": len(self.rows)}})
            self.log.append(f"missions:{page}")
            rows = [dict(r, game_version=self.row_stamp(i) if self.row_stamp else self.version)
                    for i, r in enumerate(self.rows)]
            last = max(1, -(-len(rows) // size))
            total = self.claim_total if self.claim_total is not None else len(rows)
            return httpx.Response(200, json={"data": rows[(page - 1) * size: page * size],
                                             "meta": {"current_page": page, "last_page": last, "total": total}})
        uuid = path.rsplit("/", 1)[-1]
        self.log.append(f"detail:{uuid}")
        # Like the real API: /blueprints/{uuid} knows only the BLUEPRINT id (the last segment of an entry's
        # `link`) - the entry's own `uuid` is the crafted item's id and 404s. A client that confuses the two
        # gets 'reward chance unavailable', which the chance tests below then catch.
        known = {b["link"].rstrip("/").rsplit("/", 1)[-1] for r in self.rows for b in r["blueprints"]}
        if uuid not in known:
            return httpx.Response(404)
        if self.detail_status != 200:
            return httpx.Response(self.detail_status)
        unlocking = [
            {"title": r["title"], "debug_name": r["debug_name"], "reward_scope": r["reward_scope"],
             "chance": self.chances.get(r["uuid"], 1),
             "web_url": f"https://api.star-citizen.wiki/missions/{r['uuid']}"}
            for r in self.rows if any(b["link"].rstrip("/").rsplit("/", 1)[-1] == uuid for b in r["blueprints"])
        ]
        return httpx.Response(200, json={"data": {"uuid": uuid, "game_version": self.version, "unlocking_missions": unlocking}})

    def count(self, prefix: str) -> int:
        return sum(entry.startswith(prefix) for entry in self.log)


async def _make(tmp_path: Path, wiki: FakeWiki, *, seed: bool = False):
    db = Database(tmp_path / "cog.sqlite3", Fernet(Fernet.generate_key()))
    await db.init()
    client = WikiApiClient(transport=httpx.MockTransport(wiki.handler), request_delay=0, sleep=_nosleep)
    cog = Blueprints(NS(db=db, wait_until_ready=AsyncMock()), client=client, start_refresh=False)
    if seed:
        await db.replace_blueprint_snapshot(parse_missions(ROWS), game_version=VERSION, synced_at=_utcnow())
    return cog, db


def _utcnow() -> datetime:
    return datetime.now().replace(microsecond=0)  # naive; the exact zone is irrelevant to these comparisons' margins


class FakeFollowup:
    def __init__(self, fail_embeds: bool = False):
        self.sent: list[dict] = []
        self.fail_embeds = fail_embeds

    async def send(self, **kwargs):
        if self.fail_embeds and "embed" in kwargs:
            raise discord.HTTPException(NS(status=400, reason="Bad Request"), "Invalid Form Body")
        self.sent.append(kwargs)


def _interaction(cog, *, events=None, fail_embeds=False):
    async def defer(**_):
        if events is not None:
            events.append("defer")

    return NS(response=NS(defer=defer), followup=FakeFollowup(fail_embeds), client=NS(get_cog=lambda name: cog))


def _search_command(cog, interaction, query):
    return Blueprints.blueprint_search.callback(cog, interaction, query)


def _text_of(sent: list[dict]) -> str:
    parts = []
    for kwargs in sent:
        if "content" in kwargs:
            parts.append(kwargs["content"])
        if "embed" in kwargs:
            e = kwargs["embed"]
            parts.append(e.title + "\n" + (e.description or "") + "\n" + "\n".join(f.value for f in e.fields) + "\n" + (e.footer.text or ""))
    return "\n".join(parts)


# -- 1. sync policy ----------------------------------------------------------------------------


def test_a_first_search_acknowledges_discord_before_any_network_work_then_answers(tmp_path):
    async def run():
        wiki = FakeWiki()
        wiki.events = []
        cog, _ = await _make(tmp_path, wiki)
        interaction = _interaction(cog, events=wiki.events)
        await _search_command(cog, interaction, BATTERY)
        return wiki, interaction

    wiki, interaction = asyncio.run(run())
    assert wiki.events[0] == "defer", "the interaction must be acknowledged before the first-use sync crawls the API"
    assert wiki.count("missions:") == 1 and wiki.count("version") == 1
    assert "Blueprint: " + BATTERY in _text_of(interaction.followup.sent)


def test_a_crawl_where_the_api_repeats_a_mission_still_syncs_with_the_distinct_count(tmp_path):
    """The real API repeats a couple of missions in its has_blueprints listing (found by the live smoke
    test). The crawl's own completeness check counts them (rows == meta.total); the sync must then store
    each mission once instead of dying on the snapshot's primary key."""
    async def run():
        wiki = FakeWiki(rows=ROWS + [ROWS[0], ROWS[1]])
        cog, db = await _make(tmp_path, wiki)
        return await cog.sync_snapshot(), await db.get_blueprint_snapshot_state()

    outcome, state = asyncio.run(run())
    assert outcome == "synced" and state.mission_count == len(ROWS)


def test_sync_is_skipped_when_the_game_version_is_unchanged_and_the_snapshot_is_fresh(tmp_path):
    async def run():
        wiki = FakeWiki()
        cog, _ = await _make(tmp_path, wiki, seed=True)
        return wiki, await cog.sync_snapshot()

    wiki, outcome = asyncio.run(run())
    assert outcome == "current" and wiki.log == ["version"], "only the one-row version probe - no crawl"


def test_a_new_game_version_resyncs_and_the_search_sees_the_new_data_immediately(tmp_path):
    kept, dropped = ROWS[:6], ROWS[6:]
    kept_names = {b["name"] for r in kept for b in r["blueprints"]}
    only_before = sorted({b["name"] for r in dropped for b in r["blueprints"]} - kept_names)
    assert only_before, "fixture must contain blueprints that only the dropped missions award"

    async def run():
        wiki = FakeWiki()
        cog, db = await _make(tmp_path, wiki, seed=True)
        before = await cog.search(only_before[0])  # warms the in-memory index too
        wiki.rows, wiki.version = [dict(r) for r in kept], NEW_VERSION  # the patch removed half the contracts
        outcome = await cog.sync_snapshot()
        return before, outcome, await db.get_blueprint_snapshot_state(), await cog.search(only_before[0]), wiki

    before, outcome, state, after, wiki = asyncio.run(run())
    assert before.status == "found"
    assert outcome == "synced" and state.game_version == NEW_VERSION and state.mission_count == len(kept)
    assert wiki.count("missions:") == 1
    assert after.status == "none", "the stale in-memory index must be dropped, not served after the patch"


def test_a_week_old_snapshot_resyncs_even_though_the_version_string_did_not_change(tmp_path):
    async def run():
        wiki = FakeWiki()
        cog, db = await _make(tmp_path, wiki)
        await db.replace_blueprint_snapshot(parse_missions(ROWS), game_version=VERSION,
                                            synced_at=datetime.now() - timedelta(days=8))
        return wiki, await cog.sync_snapshot()

    wiki, outcome = asyncio.run(run())
    assert outcome == "synced" and wiki.count("missions:") == 1


def test_two_simultaneous_first_searches_crawl_the_api_once(tmp_path):
    async def run():
        wiki = FakeWiki()
        cog, _ = await _make(tmp_path, wiki)
        results = await asyncio.gather(cog.search(BATTERY), cog.search("antium arms maroon"))
        return wiki, results

    wiki, results = asyncio.run(run())
    assert wiki.count("missions:") == 1, "the sync lock must serialise them; the second finds a current snapshot"
    assert [r.status for r in results] == ["found", "found"]


# -- 2. a bad sync never costs good data --------------------------------------------------------


def _bad_sync_cases():
    def server_error(w): w.missions_status = 503
    def truncated(w): w.claim_total = len(w.rows) + 5
    def empty(w): w.rows = []
    def shrunken(w): w.rows = w.rows[:5]
    # The probe says the NEW version, but the crawl comes back stamped with the old one (the API's cache flipped
    # between the two requests) - saving it would label old data as the new patch and read as "current" for a week.
    def stale_crawl(w): w.row_stamp = lambda i: VERSION
    def mixed_crawl(w): w.row_stamp = lambda i: NEW_VERSION if i % 2 else VERSION
    return {
        "server-error": server_error, "truncated-crawl": truncated, "empty-answer": empty,
        "shrunken-answer": shrunken, "crawl-older-than-probe": stale_crawl, "crawl-mixes-versions": mixed_crawl,
    }


def test_every_kind_of_bad_sync_leaves_the_old_snapshot_searchable_and_the_next_cycle_recovers(tmp_path):
    for label, break_wiki in _bad_sync_cases().items():
        async def run(label=label, break_wiki=break_wiki):
            wiki = FakeWiki()
            workdir = tmp_path / label
            workdir.mkdir()
            cog, db = await _make(workdir, wiki, seed=True)
            before = (await db.get_blueprint_snapshot_state(), await db.get_blueprint_refs())
            wiki.version = NEW_VERSION  # a patch landed, so the loop will try to sync...
            break_wiki(wiki)            # ...and the API misbehaves
            await cog.refresh_snapshot.coro(cog)  # the REAL loop body: must swallow the failure
            after_failure = (await db.get_blueprint_snapshot_state(), await db.get_blueprint_refs())
            answer = await cog.search(BATTERY)

            wiki.rows, wiki.missions_status, wiki.claim_total = [dict(r) for r in ROWS], 200, None  # API recovers
            wiki.row_stamp = None
            await cog.refresh_snapshot.coro(cog)  # next cycle
            return before, after_failure, answer, await db.get_blueprint_snapshot_state()

        before, after_failure, answer, recovered = asyncio.run(run())
        assert after_failure == before, f"{label}: the snapshot changed despite the failed sync"
        assert answer.status == "found", f"{label}: old data must still answer searches"
        assert recovered.game_version == NEW_VERSION, f"{label}: the next cycle did not recover"


def test_a_database_failure_during_the_swap_is_contained_and_retried_next_cycle(tmp_path):
    async def run():
        wiki = FakeWiki()
        cog, db = await _make(tmp_path, wiki, seed=True)
        wiki.version = NEW_VERSION
        real = db.replace_blueprint_snapshot
        db.replace_blueprint_snapshot = AsyncMock(side_effect=RuntimeError("disk full"))
        await cog.refresh_snapshot.coro(cog)  # must not raise: an uncaught error would kill the loop for good
        stuck = await db.get_blueprint_snapshot_state()
        db.replace_blueprint_snapshot = real
        await cog.refresh_snapshot.coro(cog)
        return stuck, await db.get_blueprint_snapshot_state()

    stuck, healed = asyncio.run(run())
    assert stuck.game_version == VERSION and healed.game_version == NEW_VERSION


def test_first_use_while_the_api_is_down_says_so_instead_of_failing_the_command(tmp_path):
    async def run():
        wiki = FakeWiki()
        wiki.missions_status = 503
        cog, _ = await _make(tmp_path, wiki)
        interaction = _interaction(cog)
        await _search_command(cog, interaction, BATTERY)
        return interaction.followup.sent

    sent = asyncio.run(run())
    assert len(sent) == 1 and "embed" not in sent[0] and "isn't available right now" in sent[0]["content"]


# -- 3. answers ---------------------------------------------------------------------------------


def test_a_found_blueprint_shows_its_contracts_with_the_25_percent_and_always_labels(tmp_path):
    async def run():
        cog, _ = await _make(tmp_path, FakeWiki(), seed=True)
        interaction = _interaction(cog)
        await _search_command(cog, interaction, BATTERY)
        return interaction.followup.sent

    (sent,) = asyncio.run(run())
    embed = sent["embed"]
    body = "\n".join(f.value for f in embed.fields)
    assert embed.title == f"Blueprint: {BATTERY}"
    assert "Additional Resources For Research" in body and "25% chance to grant one" in body
    assert "pool of 7" in body, "the contract's pool size is shown, not per-item odds"
    assert POOL_DISCLOSURE in embed.footer.text and VERSION in embed.footer.text
    assert "1/" not in body


def test_detail_lookups_use_the_blueprint_id_from_the_link_never_the_crafted_items_id(tmp_path):
    """Regression for the live 404s: the client must ask /blueprints/{id} with the blueprint's own id."""
    async def run():
        wiki = FakeWiki()
        cog, _ = await _make(tmp_path, wiki, seed=False)
        await cog.sync_snapshot()
        result = await cog.search(BATTERY)
        return wiki, result

    wiki, result = asyncio.run(run())
    item_ids = {b["uuid"] for r in ROWS for b in r["blueprints"]}
    asked = {entry.split(":", 1)[1] for entry in wiki.log if entry.startswith("detail:")}
    assert asked and not (asked & item_ids), "a lookup used a crafted-item id, which the real API 404s"
    assert "25% chance to grant one" in "".join(f.value for f in result.embed.fields)


def test_an_always_rewarded_contract_says_so(tmp_path):
    async def run():
        cog, _ = await _make(tmp_path, FakeWiki(), seed=True)
        return await cog.search("antium arms maroon")

    result = asyncio.run(run())
    assert result.status == "found" and "always grants one" in "".join(f.value for f in result.embed.fields)


def test_a_typo_is_corrected_and_says_what_it_did(tmp_path):
    async def run():
        cog, _ = await _make(tmp_path, FakeWiki(), seed=True)
        return await cog.search("antium arms maron")

    result = asyncio.run(run())
    assert result.status == "found" and result.name == "Antium Arms Maroon"
    assert "you typed “antium arms maron”" in result.embed.description


def test_an_ambiguous_name_lists_real_candidates_and_never_picks_one(tmp_path):
    async def run():
        cog, db = await _make(tmp_path, FakeWiki(), seed=True)
        return await cog.search("prism"), {r.name for r in await db.get_blueprint_refs()}

    result, names = asyncio.run(run())
    assert result.status == "ambiguous" and result.embed is None and result.name is None
    listed = [line[2:] for line in result.pages[0].splitlines() if line.startswith("• ")]
    assert len(listed) >= 2 and set(listed) <= names


def test_an_unknown_name_gets_a_plain_no_match_and_a_near_one_gets_did_you_mean(tmp_path):
    async def run():
        cog, _ = await _make(tmp_path, FakeWiki(), seed=True)
        return await cog.search("xylophone"), await cog.search("zzzzzz antium")

    nothing, near = asyncio.run(run())
    assert nothing.status == "none" and "No blueprint matches" in nothing.pages[0] and "Did you mean" not in nothing.pages[0]
    assert near.status == "none" and "Did you mean" in near.pages[0] and "Antium" in near.pages[0]


def test_a_failed_detail_lookup_still_answers_and_marks_the_chance_unavailable(tmp_path):
    async def run():
        wiki = FakeWiki()
        wiki.detail_status = 500
        cog, _ = await _make(tmp_path, wiki, seed=True)
        interaction = _interaction(cog)
        await _search_command(cog, interaction, BATTERY)
        return interaction.followup.sent

    (sent,) = asyncio.run(run())
    body = "\n".join(f.value for f in sent["embed"].fields)
    assert "reward chance unavailable" in body and "Additional Resources For Research" in body
    assert "always" not in body and "25%" not in body, "no chance may be invented when the lookup failed"


def test_the_detail_lookup_is_cached_within_its_ttl_and_refetched_after(tmp_path, monkeypatch):
    async def run():
        wiki = FakeWiki()
        cog, _ = await _make(tmp_path, wiki, seed=True)
        await cog.search(BATTERY)
        await cog.search(BATTERY)
        first = wiki.count("detail:")
        clock = {"now": 1_000_000.0}
        monkeypatch.setattr(blueprints_module.time, "monotonic", lambda: clock["now"])
        cog._detail_cache.clear()
        await cog.search(BATTERY)
        clock["now"] += blueprints_module.DETAIL_CACHE_SECONDS + 1
        await cog.search(BATTERY)
        return first, wiki.count("detail:")

    first, total = asyncio.run(run())
    assert first == 1, "the second search reuses the cached detail page"
    assert total == 3, "and it is fetched again once the cache entry has expired"


# -- 4. Discord limits --------------------------------------------------------------------------


def _bulk_missions(n: int) -> list[BlueprintMission]:
    target = BlueprintRef("11111111-1111-4111-8111-111111111111", "Bulk Test Blueprint")
    other = [BlueprintRef(f"22222222-2222-4222-8222-{i:012d}", f"Filler {i}") for i in range(3)]
    return [
        BlueprintMission(
            uuid=f"00000000-0000-4000-8000-{i:012d}", title=f"A Deliberately Long Contract Title Number {i} {'x' * 60}",
            giver=f"Mission Giver Corporation Number {i}", debug_name=None, rank_name="Sr. Contractor", rank_index=3,
            reputation=100, star_systems=("Stanton", "Pyro"), illegal=bool(i % 2), reward_scope="Salvage",
            game_version=VERSION, pool=(target, *other[: i % 3 + 1]),
        )
        for i in range(n)
    ]


def test_an_oversized_result_falls_back_to_text_pages_that_keep_the_disclosure(tmp_path):
    async def run():
        wiki = FakeWiki()
        cog, db = await _make(tmp_path, wiki)
        await db.replace_blueprint_snapshot(_bulk_missions(60), game_version=VERSION, synced_at=_utcnow())
        wiki.detail_status = 404
        interaction = _interaction(cog)
        await _search_command(cog, interaction, "Bulk Test Blueprint")
        return interaction.followup.sent

    sent = asyncio.run(run())
    assert all("embed" not in kwargs for kwargs in sent), "60 givers cannot fit one embed"
    assert 2 <= len(sent) <= blueprints_module.MAX_TEXT_PAGES
    assert all(len(kwargs["content"]) <= 2000 for kwargs in sent)
    assert POOL_DISCLOSURE in sent[-1]["content"], "the disclosure must survive on the last page"
    assert "ILLEGAL" in "".join(k["content"] for k in sent), "warnings survive the fallback too"


def test_a_rejected_embed_send_falls_back_to_the_same_facts_as_text(tmp_path):
    async def run():
        cog, _ = await _make(tmp_path, FakeWiki(), seed=True)
        interaction = _interaction(cog, fail_embeds=True)
        await _search_command(cog, interaction, BATTERY)
        return interaction.followup.sent

    sent = asyncio.run(run())
    text = "\n".join(k["content"] for k in sent)
    assert all("embed" not in k for k in sent)
    assert "25% chance to grant one" in text and POOL_DISCLOSURE in text and VERSION in text


def test_a_result_too_long_for_five_pages_shows_a_prefix_and_says_exactly_how_much_was_left_out(tmp_path):
    """Regression: the old rendering dropped groups from the middle without saying so, while the summary
    and the model's tool text both claimed the whole list was shown."""
    async def run():
        wiki = FakeWiki()
        cog, db = await _make(tmp_path, wiki)
        await db.replace_blueprint_snapshot(_bulk_missions(400), game_version=VERSION, synced_at=_utcnow())
        wiki.detail_status = 404
        result = await cog.search("Bulk Test Blueprint")
        interaction = _interaction(cog)
        await cog.deliver(interaction.followup.send, result)
        return result, interaction.followup.sent

    result, sent = asyncio.run(run())
    text = "\n".join(result.pages)
    shown = 400 - result.omitted
    assert result.status == "found" and 0 < result.omitted < 400 and result.embed is None
    assert len(result.pages) <= blueprints_module.MAX_TEXT_PAGES and all(len(p) <= 2000 for p in result.pages)
    assert f"Showing {shown} of 400 contract groups - {result.omitted} more didn't fit" in result.pages[0]
    assert text.count("Deliberately Long Contract Title Number") == shown, "the notice's count is what is actually listed"
    # Groups display sorted by giver name (a string sort, so "Number 10" precedes "Number 2"): what is shown must be
    # exactly the first `shown` of that order - a prefix with no gap in the middle.
    display_order = sorted(range(400), key=lambda i: f"Mission Giver Corporation Number {i}".lower())
    listed = {i for i in range(400) if f"Contract Title Number {i} " in text}
    assert listed == set(display_order[:shown])
    assert POOL_DISCLOSURE in result.pages[-1], "the pool disclosure survives truncation"
    assert all("embed" not in kwargs for kwargs in sent), "an embed can only show the full list, so truncation goes as text"


def test_a_complete_result_reports_nothing_omitted(tmp_path):
    async def run():
        cog, _ = await _make(tmp_path, FakeWiki(), seed=True)
        return await cog.search(BATTERY)

    result = asyncio.run(run())
    assert result.omitted == 0 and result.embed is not None and "Showing" not in "\n".join(result.pages)


def test_no_reply_can_ping_anyone_however_the_query_is_written(tmp_path):
    """A player's raw query is echoed in 'no match' / 'did you mean' replies. Escaped mentions stop it
    working as a ping, and every send also carries allowed_mentions=none as the backstop."""
    hostile = "@everyone @here <@123456789012345678> <@&123456789012345678> <#123456789012345678>"

    async def run():
        cog, _ = await _make(tmp_path, FakeWiki(), seed=True)
        interaction = _interaction(cog)
        await _search_command(cog, interaction, hostile)
        # ...and the same guarantee when the embed is rejected and the text path runs instead.
        typo = _interaction(cog, fail_embeds=True)
        await _search_command(cog, typo, "antium arms maron @everyone")
        found = _interaction(cog)
        await _search_command(cog, found, BATTERY)
        return interaction.followup.sent, typo.followup.sent, found.followup.sent

    for sent in asyncio.run(run()):
        assert sent, "something was sent"
        assert all(kwargs.get("allowed_mentions") is not None for kwargs in sent)
        assert all(kwargs["allowed_mentions"].everyone is False and kwargs["allowed_mentions"].users is False
                   and kwargs["allowed_mentions"].roles is False for kwargs in sent)
    plain = asyncio.run(run())[0]
    text = _text_of(plain)
    assert "@everyone" not in text and "@here" not in text and "<@1234" not in text and "<@&1234" not in text
    assert "@​everyone" in text, "the query is still echoed, just defused"


def test_an_echoed_query_is_bounded_whitespace_collapsed_and_the_reply_still_fits_one_message(tmp_path):
    async def run():
        cog, _ = await _make(tmp_path, FakeWiki(), seed=True)
        # Every character escapes to two, so a 100-char query can double in the reply.
        return await cog.search("*_~`|>" * 500), await cog.search("xylophone\n\n\n   \t  quartz")

    long, spaced = asyncio.run(run())
    assert long.status == "none" and all(len(p) <= 2000 for p in long.pages)
    assert len(long.pages[0]) < 500, "a 3,000-character query is cut to the bound before it is echoed"
    assert "xylophone quartz" in spaced.pages[0] and "\n\n" not in spaced.pages[0].split("“", 1)[1].split("”", 1)[0]


def test_the_echo_helper_bounds_and_defuses_its_own_input():
    """`_echo` is also the last line of defence for any future caller, so it doesn't lean on search() bounding first."""
    echoed = blueprints_module._echo("x" * 5000)
    assert len(echoed) == blueprints_module.MAX_QUERY_CHARS
    assert blueprints_module._echo("a \n\t  b") == "a b"
    assert "@​everyone" in blueprints_module._echo("hi @everyone")


def test_search_bounds_the_query_before_it_reaches_the_fuzzy_matcher(tmp_path, monkeypatch):
    """The model can pass any string as the tool argument; a 10,000-character query must not be fuzzy-matched
    against 700 names token by token."""
    from bot.uex.blueprints import BlueprintIndex

    seen: list[str] = []
    real = BlueprintIndex.match

    def spy(self, query, *args, **kwargs):
        seen.append(query)
        return real(self, query, *args, **kwargs)

    monkeypatch.setattr(BlueprintIndex, "match", spy)

    async def run():
        cog, _ = await _make(tmp_path, FakeWiki(), seed=True)
        return await cog.search("antium " * 2000)

    asyncio.run(run())
    assert len(seen) == 1 and len(seen[0]) <= blueprints_module.MAX_QUERY_CHARS


def test_a_result_cut_short_as_text_never_also_ships_a_full_embed(tmp_path, monkeypatch):
    """The embed can only show the whole list. If the text pages had to omit groups, an embed that happens to
    fit would show everything while `omitted` (and so the model's tool text) says some were left out - the
    two must never disagree. Shrinks the page limits so a small, embed-sized result gets truncated as text."""
    monkeypatch.setattr(blueprints_module, "TEXT_PAGE_LIMIT", 300)
    monkeypatch.setattr(blueprints_module, "MAX_TEXT_PAGES", 3)

    async def run():
        wiki = FakeWiki()
        cog, db = await _make(tmp_path, wiki)
        await db.replace_blueprint_snapshot(_bulk_missions(8), game_version=VERSION, synced_at=_utcnow())
        wiki.detail_status = 404
        return await cog.search("Bulk Test Blueprint")

    result = asyncio.run(run())
    assert result.omitted > 0, "the shrunken limits must actually force truncation for this to test anything"
    assert result.embed is None


def test_the_command_option_itself_refuses_an_over_long_query():
    option = Blueprints.blueprint_search.parameters[0]
    assert (option.min_value, option.max_value) == (1, blueprints_module.MAX_QUERY_CHARS)


def test_every_plain_text_reply_is_split_to_fit_a_discord_message():
    pages = blueprints_module._text_pages("\n".join(f"line {i} " + "x" * 90 for i in range(300)) + "\n" + "y" * 5000)
    assert len(pages) > 5 and all(len(page) <= 1900 for page in pages)
    assert "".join(pages).count("y") == 5000, "an over-long single line is split, never dropped"


def test_autocomplete_is_bounded_discord_safe_and_never_triggers_a_sync(tmp_path):
    async def run():
        wiki = FakeWiki()
        cog, db = await _make(tmp_path, wiki)
        empty = await blueprint_autocomplete(_interaction(cog), "anti")
        await db.replace_blueprint_snapshot(parse_missions(ROWS), game_version=VERSION, synced_at=_utcnow())
        wiki.log.clear()
        choices = await blueprint_autocomplete(_interaction(cog), "antium")
        return empty, choices, wiki.log

    empty, choices, log = asyncio.run(run())
    assert empty == [] and log == [] and len(choices) <= 25 and choices
    assert all(len(c.name) <= 100 and c.name == c.value for c in choices)
    assert all("Antium" in c.name for c in choices)
