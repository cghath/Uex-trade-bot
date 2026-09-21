"""Blueprint search: which contracts award a given crafting blueprint.

Pure, dependency-free logic (no Discord, no I/O) over rows from the Star Citizen Wiki API's
`/api/missions?filter[has_blueprints]=1` and `/api/blueprints/{uuid}` (bot/wiki_api.py fetches
them, bot/cogs/blueprints.py wires this to Discord and the database).

What the data does and does not say - verified against the live API, not inferred from names:

* A mission's `blueprints` list is its reward POOL: the blueprints the contract can grant.
  The API publishes no per-item weight, so "1 of N" is the most this module ever claims - never
  "1/N odds".
* `chance` (only on a blueprint's detail page, one row per unlocking mission) is the chance the
  MISSION's blueprint reward triggers at all: every blueprint in one mission's pool carries the
  identical value (confirmed on "Additional Resources For Research": all 7 items 0.25), and 610
  of 611 sampled rows were 1.0. It is not the odds of one particular blueprint.
* Two missions can share a title yet award different pools (10 vs 7 items for that same contract),
  so results are grouped by pool, not by title.
* Cash reward fields are null/0 for every blueprint mission, so none are shown - a missing figure
  is not "0 aUEC".
"""
from __future__ import annotations

import math
import re
import unicodedata
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Literal

_UUID_RE = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")

MAX_CANDIDATES = 10
AUTOCOMPLETE_LIMIT = 25
# A lone typo-corrected match is accepted, but a query this short can't have been "corrected"
# into anything meaningful - it either matches a name outright or it doesn't.
MIN_FUZZY_QUERY_LENGTH = 4
# Snapshot refresh policy (see snapshot_is_current).
SNAPSHOT_MAX_AGE = timedelta(days=7)
# A sync whose mission count falls below this share of the stored one is rejected as a
# truncated/garbled upstream response rather than trusted over good data.
MIN_SYNC_SHARE_OF_PREVIOUS = 0.5


# -- rows -> records ---------------------------------------------------------------------------


@dataclass(frozen=True)
class BlueprintRef:
    uuid: str
    name: str


@dataclass(frozen=True)
class BlueprintMission:
    uuid: str
    title: str
    giver: str
    debug_name: str | None
    rank_name: str | None  # minimum standing needed to accept it; None when the API gave none
    rank_index: int | None
    reputation: int | None  # None = unknown, 0 = confirmed zero
    star_systems: tuple[str, ...]
    illegal: bool
    reward_scope: str | None
    game_version: str | None
    pool: tuple[BlueprintRef, ...]

    @property
    def pool_size(self) -> int:
        return len(self.pool)


@dataclass(frozen=True)
class SnapshotState:
    """What the stored snapshot says about itself. `synced_at` is naive UTC, matching how the rest
    of the database stores timestamps."""
    game_version: str
    synced_at: datetime
    mission_count: int
    blueprint_count: int


def _text(value: Any) -> str | None:
    if isinstance(value, str):
        stripped = value.strip()
        return stripped or None
    return None


def _int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)) and math.isfinite(value):
        return int(value)
    return None


def _blueprint_uuid(entry: dict) -> str | None:
    """The id `/api/blueprints/{uuid}` accepts. A mission's blueprint entry carries two uuids and the
    obvious one is the wrong one: `uuid` is the crafted ITEM's id (46 of 46 sampled entries matched the
    blueprint page's `output_item_uuid`, never its `uuid`), and using it made every detail lookup 404.
    The blueprint's own id is the last path segment of `link`. Only if `link` is unusable does this fall
    back to `uuid` - still a stable identity for matching, though its detail lookup will then fail
    soft ('reward chance unavailable')."""
    link = entry.get("link")
    if isinstance(link, str):
        tail = link.rstrip("/").rsplit("/", 1)[-1]
        if _UUID_RE.fullmatch(tail):
            return tail.lower()
    return _text(entry.get("uuid"))


def parse_mission(row: Any) -> BlueprintMission | None:
    """One `/api/missions` row -> BlueprintMission, or None when it can't describe a usable
    blueprint contract (no id, no title, or an empty/garbled pool). Tolerates any missing or
    null field - the API returns null for many of them on many rows."""
    if not isinstance(row, dict):
        return None
    uuid = _text(row.get("uuid"))
    title = _text(row.get("title"))
    if not uuid or not title:
        return None
    pool: list[BlueprintRef] = []
    seen: set[str] = set()
    for entry in row.get("blueprints") or []:
        if not isinstance(entry, dict):
            continue
        bp_uuid, bp_name = _blueprint_uuid(entry), _text(entry.get("name"))
        if bp_uuid and bp_name and bp_uuid not in seen:
            seen.add(bp_uuid)
            pool.append(BlueprintRef(bp_uuid, bp_name))
    if not pool:
        return None
    faction = row.get("faction") if isinstance(row.get("faction"), dict) else {}
    systems = tuple(s for s in (row.get("star_systems") or []) if isinstance(s, str) and s.strip())
    return BlueprintMission(
        uuid=uuid,
        title=title,
        giver=_text(row.get("mission_giver")) or _text(faction.get("name")) or "Unknown giver",
        debug_name=_text(row.get("debug_name")),
        rank_name=_text(row.get("min_standing_name")),
        rank_index=_int(row.get("rank_index")),
        reputation=_int(row.get("reputation_amount")),
        star_systems=systems,
        illegal=row.get("illegal") is True or _text(row.get("legality_label")) == "Illegal",
        reward_scope=_text(row.get("reward_scope")),
        game_version=_text(row.get("game_version")),
        pool=tuple(pool),
    )


def parse_missions(rows: Iterable[Any]) -> list[BlueprintMission]:
    """Usable missions, one per uuid. The API's `has_blueprints` listing really does repeat a mission
    (found live: 786 rows, 784 distinct uuids - two identical rows), so the first occurrence wins.
    Without this the snapshot's PRIMARY KEY rejects the whole sync."""
    seen: set[str] = set()
    missions: list[BlueprintMission] = []
    for row in rows:
        mission = parse_mission(row)
        if mission is not None and mission.uuid not in seen:
            seen.add(mission.uuid)
            missions.append(mission)
    return missions


def snapshot_is_current(
    stored_version: str | None, stored_at: datetime | None, remote_version: str,
    now: datetime, max_age: timedelta = SNAPSHOT_MAX_AGE,
) -> bool:
    """Skip a re-sync only when the game version is unchanged AND the snapshot is recent. The
    age cap exists because the API can correct its data mid-patch without changing the
    version string, so an unchanged version alone would never refresh it."""
    if not stored_version or stored_at is None:
        return False
    return stored_version == remote_version and (now - stored_at) < max_age


def sync_result_is_plausible(new_count: int, previous_count: int | None) -> bool:
    """Guards the snapshot against being replaced by a truncated or empty upstream response."""
    if new_count <= 0:
        return False
    if previous_count and new_count < previous_count * MIN_SYNC_SHARE_OF_PREVIOUS:
        return False
    return True


def mission_uuid_from_url(url: Any) -> str | None:
    if not isinstance(url, str):
        return None
    tail = url.rstrip("/").rsplit("/", 1)[-1]
    return tail.lower() if _UUID_RE.fullmatch(tail) else None


def parse_chances(detail: Any) -> dict[str, float]:
    """`{mission_uuid: chance}` from a blueprint detail response's `unlocking_missions` rows.
    A chance outside (0, 1] or non-numeric is dropped (unknown), never coerced."""
    chances: dict[str, float] = {}
    body = detail.get("data", detail) if isinstance(detail, dict) else None
    if not isinstance(body, dict):
        return chances
    for row in body.get("unlocking_missions") or []:
        if not isinstance(row, dict):
            continue
        uuid = mission_uuid_from_url(row.get("web_url"))
        chance = row.get("chance")
        if uuid and isinstance(chance, (int, float)) and not isinstance(chance, bool) \
                and math.isfinite(chance) and 0 < chance <= 1:
            chances[uuid] = float(chance)
    return chances


# -- name matching -----------------------------------------------------------------------------

_QUOTES = re.compile("[\"'`‘’“”′]")


def normalize_name(text: str) -> str:
    """Case-, quote-, punctuation- and typography-insensitive form: `Prism "Deep Sea" Laser
    Shotgun` -> `prism deep sea laser shotgun`. Quotes are dropped (not split on) so an
    apostrophe inside a word doesn't break it; NFKC folds the no-break spaces and hyphens some
    models emit."""
    folded = unicodedata.normalize("NFKC", text).casefold()
    return re.sub(r"[\W_]+", " ", _QUOTES.sub("", folded)).strip()


def _osa_distance(a: str, b: str) -> int:
    """Optimal-string-alignment (Damerau) distance: an adjacent transposition counts as one edit."""
    rows = [list(range(len(b) + 1))]
    for i, ca in enumerate(a, 1):
        row = [i]
        for j, cb in enumerate(b, 1):
            cost = 0 if ca == cb else 1
            value = min(rows[-1][j] + 1, row[j - 1] + 1, rows[-1][j - 1] + cost)
            if i > 1 and j > 1 and ca == b[j - 2] and a[i - 2] == cb:
                value = min(value, rows[-2][j - 2] + 1)
            row.append(value)
        rows.append(row)
    return rows[-1][-1]


def _allowed_edits(token: str) -> int:
    """Typo budget by length. Short tokens (model numbers, roman numerals: `s1`, `vi`, `mh1`) and
    anything containing a digit must match exactly - one changed character there names a
    DIFFERENT item, not a misspelling."""
    if not token.isalpha():
        return 0
    return 0 if len(token) <= 3 else 1 if len(token) <= 6 else 2


_TokenKind = Literal["exact", "prefix", "fuzzy"]
_TOKEN_SCORE = {"exact": 1.0, "prefix": 0.9}


def _score_token(query_token: str, name_token: str) -> tuple[_TokenKind, float] | None:
    if query_token == name_token:
        return "exact", 1.0
    if len(query_token) >= 3 and query_token.isalpha() and name_token.startswith(query_token):
        return "prefix", _TOKEN_SCORE["prefix"]
    budget = _allowed_edits(query_token)
    if budget and abs(len(query_token) - len(name_token)) <= budget and name_token.isalpha():
        distance = _osa_distance(query_token, name_token)
        if distance <= budget:
            return "fuzzy", 0.8 * (1 - distance / max(len(query_token), len(name_token)))
    return None


MIN_SUGGESTION_COVERAGE = 0.5
MIN_LOOSE_SIMILARITY = 0.6


def _loose_token_score(query_token: str, name_token: str) -> float:
    """Similarity in [0, 1] for 'did you mean' ranking only - never used to auto-correct. Exact and
    prefix matches keep their normal scores; alphabetic words otherwise score by edit similarity
    when it clears MIN_LOOSE_SIMILARITY (so 'arclite' ~ 'arclight'). Anything containing a digit
    is exact-only, same as strict matching."""
    strict = _score_token(query_token, name_token)
    if strict is not None:
        return strict[1]
    if query_token.isalpha() and name_token.isalpha() and len(query_token) >= 4:
        similarity = 1 - _osa_distance(query_token, name_token) / max(len(query_token), len(name_token))
        if similarity >= MIN_LOOSE_SIMILARITY:
            return similarity
    return 0.0


@dataclass(frozen=True)
class _Group:
    norm: str
    tokens: tuple[str, ...]
    display: str
    uuids: tuple[str, ...]


@dataclass(frozen=True)
class _Scored:
    group: _Group
    score: float
    extra_tokens: int
    fuzzy: bool

    @property
    def complete(self) -> bool:
        return self.extra_tokens == 0


@dataclass(frozen=True)
class BlueprintMatch:
    """`resolved`: `name`/`uuids` say which single blueprint UUID,
    `corrected` is True when a typo had to be fixed to get there. `ambiguous`: `candidates` are
    the ranked names to ask the player about. `none`: `candidates` are near suggestions (may be
    empty)."""
    status: Literal["resolved", "ambiguous", "none"]
    name: str | None = None
    uuids: tuple[str, ...] = ()
    candidates: tuple[str, ...] = ()
    total_candidates: int = 0
    corrected: bool = False


class BlueprintIndex:
    """Blueprint names ready for matching. Build once per snapshot, reuse for every query - the
    autocomplete calls it on each keystroke."""

    def __init__(self, refs: Iterable[BlueprintRef]) -> None:
        by_norm: dict[str, list[BlueprintRef]] = {}
        for ref in refs:
            norm = normalize_name(ref.name)
            if norm:
                by_norm.setdefault(norm, []).append(ref)
        self._groups: list[_Group] = []
        self._exact: dict[str, _Group] = {}
        self._by_uuid = {}
        for norm, members in by_norm.items():
            unique = {m.uuid: m for m in members}
            for uuid, member in sorted(unique.items()):
                display = member.name if len(unique) == 1 else f"{member.name[:60]} [{uuid}]"
                group = _Group(norm, tuple(norm.split()), display, (uuid,))
                self._groups.append(group)
                self._exact[normalize_name(display)] = group
                self._by_uuid[uuid.lower()] = group
        self._groups.sort(key=lambda g: g.display.lower())

    def __len__(self) -> int:
        return len(self._groups)

    @property
    def names(self) -> list[str]:
        return [g.display for g in self._groups]

    def _score(self, query_tokens: Sequence[str], group: _Group) -> _Scored | None:
        """Every query token must be matched by a distinct name token; the best available kind of
        match (exact > prefix > fuzzy) is taken for each."""
        available = list(group.tokens)
        total, fuzzy = 0.0, False
        for token in query_tokens:
            best: tuple[float, int, _TokenKind] | None = None
            for index, name_token in enumerate(available):
                scored = _score_token(token, name_token)
                if scored is not None and (best is None or scored[1] > best[0]):
                    best = (scored[1], index, scored[0])
            if best is None:
                return None
            total += best[0]
            fuzzy = fuzzy or best[2] == "fuzzy"
            del available[best[1]]
        extra = len(available)
        return _Scored(group, total / len(query_tokens) - 0.02 * extra, extra, fuzzy)

    def _candidates(self, query_tokens: Sequence[str]) -> list[_Scored]:
        found = [s for s in (self._score(query_tokens, g) for g in self._groups) if s is not None]
        return sorted(found, key=lambda s: (-s.score, s.group.display.lower()))

    def match(self, query: str) -> BlueprintMatch:
        by_uuid = self._by_uuid.get(query.strip().lower())
        if by_uuid is not None:
            return BlueprintMatch("resolved", by_uuid.display, by_uuid.uuids)
        norm = normalize_name(query)
        if not norm:
            return BlueprintMatch("none")
        exact = self._exact.get(norm)
        if exact is not None:
            return BlueprintMatch("resolved", exact.display, exact.uuids)
        same_name = [g for g in self._groups if g.norm == norm]
        if len(same_name) > 1:
            return BlueprintMatch("ambiguous", candidates=tuple(g.display for g in same_name[:MAX_CANDIDATES]),
                                  total_candidates=len(same_name))
        tokens = norm.split()
        candidates = self._candidates(tokens)
        if not candidates:
            return BlueprintMatch("none", candidates=self._suggestions(tokens))
        # A lone match, or exactly one name the query fully specifies (every word of the name is
        # accounted for - the others are longer variants of it), is the answer. Anything else is a
        # real ambiguity: ask, never guess.
        complete = [c for c in candidates if c.complete]
        chosen = candidates[0] if len(candidates) == 1 else complete[0] if len(complete) == 1 else None
        if chosen is not None:
            if chosen.fuzzy and len(norm.replace(" ", "")) < MIN_FUZZY_QUERY_LENGTH:
                return BlueprintMatch("none")
            return BlueprintMatch("resolved", chosen.group.display, chosen.group.uuids, corrected=chosen.fuzzy)
        return BlueprintMatch(
            "ambiguous", candidates=tuple(c.group.display for c in candidates[:MAX_CANDIDATES]),
            total_candidates=len(candidates),
        )

    def _suggestions(self, tokens: Sequence[str], limit: int = 5) -> tuple[str, ...]:
        """'Did you mean...' for a query nothing fully matched. Looser than automatic correction
        (a similarity ratio, not a fixed edit budget) because a suggestion is only ever a question
        the player answers - it can't silently name the wrong item. A name qualifies when at least
        half the query's words (or, for one word, that word) match it; shorter names rank first so
        the plain item beats its named variants."""
        ranked = []
        for group in self._groups:
            matched = 0.0
            available = list(group.tokens)
            for token in tokens:
                hits = [(s, i) for i, t in enumerate(available) if (s := _loose_token_score(token, t)) > 0]
                if hits:
                    best = max(hits)
                    matched += best[0]
                    del available[best[1]]
            coverage = matched / len(tokens)
            if coverage >= MIN_SUGGESTION_COVERAGE:
                ranked.append((coverage - 0.02 * len(available), group.display))
        ranked.sort(key=lambda pair: (-pair[0], pair[1].lower()))
        return tuple(name for _, name in ranked[:limit])

    def autocomplete(self, current: str, limit: int = AUTOCOMPLETE_LIMIT) -> list[str]:
        """Names for a Discord autocomplete dropdown: prefix/substring/typo-tolerant, best first."""
        norm = normalize_name(current)
        if not norm:
            return [g.display for g in self._groups[:limit]]
        ranked: list[str] = [c.group.display for c in self._candidates(norm.split())]
        for group in self._groups:
            if norm in group.norm and group.display not in ranked:
                ranked.append(group.display)
        return ranked[:limit]


# -- presentation ------------------------------------------------------------------------------


def format_percent(chance: float) -> str:
    return f"{chance * 100:g}%"


def describe_chance(chances: Sequence[float | None]) -> str | None:
    """How likely the mission's blueprint reward is to trigger, from the per-mission chances
    (None = the detail lookup didn't cover that mission). Returns None unless EVERY mission in the
    group is known - a claim about "x2 variants" that only checked one of them would be a guess.
    Deliberately about the REWARD, not any one blueprint - see the module docstring."""
    if not chances or any(c is None for c in chances):
        return None
    known = [c for c in chances if c is not None]
    low, high = min(known), max(known)
    if low >= 1:
        return "always grants one"
    if low == high:
        return f"{format_percent(low)} chance to grant one"
    return f"{format_percent(low)}-{format_percent(high)} chance to grant one (varies by variant)"


@dataclass(frozen=True)
class MissionGroup:
    """Missions that look identical to a player: same title, giver, rank and pool. Several API
    rows often differ only by an internal variant id."""
    title: str
    giver: str
    rank_name: str | None
    rank_index: int | None
    reward_scope: str | None
    illegal: bool
    reputation: int | None
    star_systems: tuple[str, ...]
    pool_size: int
    mission_uuids: tuple[str, ...]


def group_missions(missions: Iterable[BlueprintMission]) -> list[MissionGroup]:
    buckets: dict[tuple, list[BlueprintMission]] = {}
    for m in missions:
        key = (m.title, m.giver, m.rank_name, m.reward_scope, m.illegal, frozenset(r.uuid for r in m.pool))
        buckets.setdefault(key, []).append(m)
    groups = []
    for members in buckets.values():
        first = members[0]
        systems = tuple(sorted({s for m in members for s in m.star_systems}))
        reps = {m.reputation for m in members if m.reputation is not None}
        groups.append(MissionGroup(
            title=first.title, giver=first.giver, rank_name=first.rank_name, rank_index=first.rank_index,
            reward_scope=first.reward_scope, illegal=first.illegal,
            reputation=max(reps) if reps else None, star_systems=systems,
            pool_size=first.pool_size, mission_uuids=tuple(sorted(m.uuid for m in members)),
        ))
    return sorted(groups, key=lambda g: (g.giver.lower(), g.rank_index if g.rank_index is not None else -1, g.title.lower()))


def group_line(group: MissionGroup, chance_text: str | None) -> str:
    """One player-facing line for a group, identical in the embed and the plain-text fallback."""
    parts = [f"**{group.title}**"]
    if group.rank_name:
        parts.append(f"needs {group.rank_name}")
    parts.append(f"pool of {group.pool_size}")
    parts.append(chance_text or "reward chance unavailable")
    if group.reputation:
        parts.append(f"+{group.reputation} rep")
    if group.star_systems:
        parts.append("/".join(group.star_systems))
    if group.illegal:
        parts.append("ILLEGAL")
    if len(group.mission_uuids) > 1:
        parts.append(f"x{len(group.mission_uuids)} variants")
    return "• " + " · ".join(parts)
