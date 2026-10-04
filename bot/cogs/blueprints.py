"""Blueprint Search (/blueprint-search): which contracts award a crafting blueprint.

Data comes from the Star Citizen Wiki API (bot/wiki_api.py), which re-extracts the game files each
patch, so nothing here is updated by hand: a background check every REFRESH_HOURS asks the API which
game version it serves (one tiny request) and re-syncs only when that changed or the snapshot is a week
old (bot/uex/blueprints.py snapshot_is_current). The snapshot lives in SQLite so a restart doesn't
re-download it and so a failed sync can never lose good data (Database.replace_blueprint_snapshot is
all-or-nothing, and sync_result_is_plausible refuses a truncated response before it gets that far).

`search` is the one entry point the slash command calls.
"""
from __future__ import annotations

import asyncio
import functools
import logging
import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Awaitable, Callable, Literal

import discord
from discord import app_commands
from discord.ext import commands, tasks

from bot.uex.blueprints import (
    BlueprintIndex,
    BlueprintMission,
    contracts_header,
    describe_chance,
    first_contracts,
    giver_block,
    group_missions,
    parse_chances,
    parse_missions,
    snapshot_is_current,
    summarize_contracts,
    sync_result_is_plausible,
)
from bot.uex.route_presentation import chunk_lines
from bot.wiki_api import WikiApiClient, WikiApiError
from bot.uex.blueprint_crafting import Recipe, UNAVAILABLE, craft_count, obtainable_qualities
from bot.cogs.blueprint_planner import BlueprintResultView, CraftLaunchView, ShoppingView, ShoppingService
from bot.discord_ui import LAYOUT_TEXT_LIMIT  # a result over it goes as text pages

logger = logging.getLogger("uexbot.blueprints")

REFRESH_HOURS = 12  # the API itself caches responses for 12h, so checking more often gains nothing
DETAIL_CACHE_SECONDS = 6 * 3600
DETAIL_CACHE_MAX = 300
TEXT_PAGE_LIMIT = 1900  # under Discord's 2000-char message cap
MAX_TEXT_PAGES = 5
# The longest real blueprint name is well under this; anything longer is not a name, and a player's raw query
# is echoed back in "no match"/"which one?" replies, so it is bounded and made mention-safe first.
MAX_QUERY_CHARS = 100
_NO_MENTIONS = discord.AllowedMentions.none()


def _echo(text: str) -> str:
    """A player's query made safe to repeat in a bot message: whitespace collapsed, length-bounded, and with
    @everyone/@here/user/role mentions and markdown neutralised so the bot can't be made to ping anyone."""
    return discord.utils.escape_markdown(discord.utils.escape_mentions(" ".join(text.split())[:MAX_QUERY_CHARS]))


def _text_pages(text: str) -> tuple[str, ...]:
    """Any plain-text reply split to fit Discord's message limit - every text result goes through this,
    so no reply can exceed 2,000 characters however long its parts are."""
    return tuple(chunk_lines(text.split("\n"), TEXT_PAGE_LIMIT))


class SnapshotRejected(Exception):
    """A fetched snapshot failed its sanity check (empty, or far smaller than the stored one)."""


@dataclass(frozen=True)
class SearchResult:
    """`pages` is ALWAYS the complete plain-text rendering (each page fits one Discord message), so
    it doubles as the fallback when the layout is too large or its send fails. `blocks` (the
    layout's header, then one per giver) and `crafting` exist only for a `found` result whose
    whole list fits one layout."""
    status: Literal["found", "ambiguous", "none", "unavailable"]
    pages: tuple[str, ...]
    name: str | None = None
    blocks: tuple[str, ...] = ()
    crafting: str = ""
    # Structured extras so callers never have to parse `pages` back apart: for
    # "ambiguous" the ranked candidate names and how many more matched; for "none" the near-miss
    # suggestions (possibly empty).
    candidates: tuple[str, ...] = ()
    more: int = 0
    # For "found": how many distinct contract groups did NOT fit in the posted list (0 = everything is shown).
    omitted: int = 0
    recipe: Recipe | None = None
    craft_quantity: int = 1


async def blueprint_autocomplete(interaction: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
    """Reads the in-memory index only - never triggers a sync, since Discord gives autocomplete
    ~3 seconds to answer."""
    cog = interaction.client.get_cog("Blueprints")
    if cog is None:
        return []
    index = await cog.get_index(sync_if_missing=False)
    if index is None:
        return []
    return [app_commands.Choice(name=name[:100], value=name[:100]) for name in index.autocomplete(current)]


class Blueprints(commands.Cog):
    def __init__(self, bot: commands.Bot, *, client: WikiApiClient | None = None, start_refresh: bool = True) -> None:
        self.bot = bot
        self._client = client or WikiApiClient()
        self._sync_lock = asyncio.Lock()
        self._index: BlueprintIndex | None = None
        self._detail_cache: dict[tuple[str, str], tuple[float, dict]] = {}
        self._quality_cache: dict[str, tuple[float, tuple[int, ...]]] = {}
        self.shopping = ShoppingService(bot)
        if start_refresh:
            self.refresh_snapshot.start()

    def cog_unload(self) -> None:
        self.refresh_snapshot.cancel()
        try:
            asyncio.get_running_loop().create_task(self._client.aclose())
        except RuntimeError:  # no running loop (interpreter shutdown) - nothing left to close politely
            pass

    # -- snapshot sync --------------------------------------------------------------------------

    @tasks.loop(hours=REFRESH_HOURS)
    async def refresh_snapshot(self) -> None:
        try:
            outcome = await self.sync_snapshot()
            logger.info("Blueprint snapshot check: %s", outcome)
        except (WikiApiError, SnapshotRejected) as exc:
            logger.warning("Blueprint snapshot refresh failed (keeping the previous snapshot): %s", exc)
        except Exception:
            logger.exception("Blueprint snapshot refresh failed unexpectedly")

    @refresh_snapshot.before_loop
    async def before_refresh_snapshot(self) -> None:
        await self.bot.wait_until_ready()

    async def sync_snapshot(self, *, force: bool = False) -> Literal["synced", "current"]:
        """Bring the stored snapshot up to date. Raises WikiApiError (couldn't fetch a complete answer)
        or SnapshotRejected (fetched, but implausible) - in both cases the previous snapshot is untouched.
        Serialised by a lock so the background loop and a player's first search can't both crawl."""
        async with self._sync_lock:
            state = await self.bot.db.get_blueprint_snapshot_state()
            remote_version = await self._client.get_game_version()
            now = datetime.now(timezone.utc).replace(tzinfo=None)
            if not force and snapshot_is_current(
                state.game_version if state else None, state.synced_at if state else None, remote_version, now,
            ):
                return "current"
            rows = await self._client.get_blueprint_missions()
            # The version probe and the crawl are separate requests. If they disagree (the API's cache flipped
            # between them, or a patch landed mid-crawl) the rows can't be trusted to be the version we'd
            # label them with - saving them would mark old data as the new patch, and the snapshot would then
            # read as "current" for a week. Reject and retry on the next cycle.
            row_versions = {row.get("game_version") for row in rows if isinstance(row, dict)}
            if row_versions != {remote_version}:
                raise SnapshotRejected(
                    f"rows report game version(s) {sorted(str(v) for v in row_versions)} but the API served "
                    f"{remote_version!r} a moment earlier - keeping the previous snapshot, will retry"
                )
            missions = parse_missions(rows)
            if not sync_result_is_plausible(len(missions), state.mission_count if state else None):
                raise SnapshotRejected(
                    f"{len(missions)} usable missions from {len(rows)} rows "
                    f"(previous snapshot: {state.mission_count if state else 'none'})"
                )
            counts = await self.bot.db.replace_blueprint_snapshot(missions, game_version=remote_version, synced_at=now)
            self._index = None
            logger.info("Blueprint snapshot synced: %d missions, %d blueprints (game %s)", *counts, remote_version)
            return "synced"

    async def get_index(self, *, sync_if_missing: bool) -> BlueprintIndex | None:
        if self._index is not None:
            return self._index
        refs = await self.bot.db.get_blueprint_refs()
        if not refs and sync_if_missing:
            try:
                await self.sync_snapshot()
            except (WikiApiError, SnapshotRejected) as exc:
                logger.warning("On-demand blueprint sync failed: %s", exc)
                return None
            refs = await self.bot.db.get_blueprint_refs()
        if not refs:
            return None
        self._index = BlueprintIndex(refs)
        return self._index

    # -- drop chance (per blueprint, fetched lazily) ---------------------------------------------

    async def _detail_for(self, uuid: str, version: str) -> dict | None:
        key = (uuid, version)
        cached = self._detail_cache.get(key)
        if cached and time.monotonic() - cached[0] < DETAIL_CACHE_SECONDS:
            return cached[1]
        try:
            detail = await self._client.get_blueprint_detail(uuid)
            if detail.get("uuid") != uuid or detail.get("game_version") != version:
                raise WikiApiError("Blueprint detail and mission snapshot identity/version disagree")
        except WikiApiError as exc:
            logger.warning("Blueprint detail lookup failed for %s: %s", uuid, exc)
            return None
        if len(self._detail_cache) >= DETAIL_CACHE_MAX:
            self._detail_cache.pop(min(self._detail_cache, key=lambda k: self._detail_cache[k][0]))
        self._detail_cache[key] = (time.monotonic(), detail)
        return detail

    async def quality_options(self, recipe: Recipe) -> dict[str, tuple[int, ...]]:
        result = {}
        for item in recipe.inputs:
            if not item.ore_uuid:
                result[item.path] = item.quality_values
                continue
            cached = self._quality_cache.get(item.ore_uuid)
            if cached and time.monotonic() - cached[0] < DETAIL_CACHE_SECONDS:
                result[item.path] = cached[1]
                continue
            try:
                detail = await self._client.get_commodity_detail(item.ore_uuid)
                values = obtainable_qualities(detail, item.ore_uuid)
            except WikiApiError:
                values = ()
            result[item.path] = values
            if values:
                if len(self._quality_cache) >= DETAIL_CACHE_MAX:
                    self._quality_cache.pop(next(iter(self._quality_cache)))
                self._quality_cache[item.ore_uuid] = (time.monotonic(), values)
        return result

    async def cog_load(self) -> None:
        self.bot.add_view(ShoppingView(self.shopping))

    # -- search ---------------------------------------------------------------------------------

    async def search(self, query: str, *, sync_if_missing: bool = True, craft_quantity: int = 1) -> SearchResult:
        query = " ".join(str(query).split())[:MAX_QUERY_CHARS]
        count_match = re.fullmatch(r"craft\s+(\d+)\s+(.+)", query, re.IGNORECASE)
        if count_match:
            craft_quantity, query = int(count_match[1]), count_match[2]
        try:
            craft_count(craft_quantity)
        except ValueError as exc:
            return SearchResult("none", _text_pages(str(exc)))
        shown_query = _echo(query)
        index = await self.get_index(sync_if_missing=sync_if_missing)
        if index is None:
            return SearchResult(
                "unavailable",
                _text_pages("Blueprint data isn't available right now - the Star Citizen Wiki API couldn't be reached. Try again in a bit."),
            )
        match = index.match(query)
        if match.status == "ambiguous":
            shown = "\n".join(f"• {name}" for name in match.candidates)
            more = match.total_candidates - len(match.candidates)
            tail = f"\n…and {more} more." if more > 0 else ""
            return SearchResult("ambiguous", _text_pages(
                f"“{shown_query}” matches several blueprints - which one do you mean?\n{shown}{tail}\n"
                "Pick one from the autocomplete list or type more of its name."
            ), candidates=match.candidates, more=max(more, 0))
        if match.status == "none":
            hint = ""
            if match.candidates:
                hint = "\nDid you mean: " + ", ".join(match.candidates) + "?"
            return SearchResult("none", _text_pages(f"No blueprint matches “{shown_query}”.{hint}"), None,
                                candidates=match.candidates)

        missions = await self.bot.db.get_blueprint_missions(list(match.uuids))
        if not missions:
            return SearchResult("none", _text_pages(f"No contracts in the current data award {match.name}."), match.name)
        state = await self.bot.db.get_blueprint_snapshot_state()
        detail = await self._detail_for(match.uuids[0], state.game_version) if state is not None else None
        recipe = None
        if detail is not None and all(m.game_version == state.game_version for m in missions):
            try:
                recipe = Recipe.parse(detail)
            except ValueError:
                logger.warning("Unsupported crafting recipe for %s", match.uuids[0])
        else:
            detail = None
        return self._render(match.name, shown_query if match.corrected else None, missions,
                            parse_chances(detail), state, recipe, craft_quantity)

    def _render(
        self, name: str, corrected_from: str | None, missions: list[BlueprintMission],
        chances: dict[str, float], state, recipe: Recipe | None = None, craft_quantity: int = 1,
    ) -> SearchResult:
        groups = group_missions(missions)
        summary = summarize_contracts(
            groups, [describe_chance([chances.get(uuid) for uuid in group.mission_uuids]) for group in groups])
        header = discord.utils.escape_mentions(
            contracts_header(name, summary, state.game_version if state is not None else None, corrected_from))
        # The crafting details live behind Configure crafting (the owner's call, 2026-10-03): the
        # reply only says where to find them, and how many copies when it's more than one.
        if recipe is None:
            crafting = layout_crafting = f"-# {UNAVAILABLE}"
        else:
            title = "**Crafting**" if craft_quantity == 1 else f"**Crafting {craft_quantity:,} copies**"
            layout_crafting = f"{title}\nSee the materials and how their quality changes the stats."
            crafting = (f"{title}\nTo see the materials and how their quality changes the stats, "
                        "use **Configure crafting** below.")

        def assemble(shown: int) -> list[str]:
            lines = header.split("\n")
            omitted = summary.count - shown
            if omitted:
                lines.append(
                    f"Showing {shown} of {summary.count} contracts - {omitted} more didn't fit in Discord. "
                    "Search a more specific name to narrow it down."
                )
            for section in first_contracts(summary.givers, shown):
                lines.extend(["", *discord.utils.escape_mentions(giver_block(section)).split("\n")])
            lines.extend(["", *crafting.split("\n")])
            return chunk_lines(lines, TEXT_PAGE_LIMIT)

        # Everything if it fits; otherwise the LARGEST prefix of the list that fits, with an explicit "showing X
        # of Y". Never silently drop the middle: the summary claims what was shown.
        shown = summary.count
        pages = assemble(shown)
        if len(pages) > MAX_TEXT_PAGES:
            low, high = 0, shown - 1
            while low < high:
                mid = (low + high + 1) // 2
                if len(assemble(mid)) <= MAX_TEXT_PAGES:
                    low = mid
                else:
                    high = mid - 1
            shown = low
            pages = assemble(shown)
        omitted = summary.count - shown

        blocks = (header, *(discord.utils.escape_mentions(giver_block(section)) for section in summary.givers))
        # The layout only ever shows the whole list; a result cut short as text goes as text, with its notice.
        fits = omitted == 0 and sum(map(len, blocks)) + len(layout_crafting) <= LAYOUT_TEXT_LIMIT
        return SearchResult("found", tuple(pages), name, blocks if fits else (), layout_crafting if fits else "",
                            omitted=omitted, recipe=recipe, craft_quantity=craft_quantity)

    async def deliver(self, send: Callable[..., Awaitable], result: SearchResult) -> None:
        """Send a result via `send` (a followup.send with wait=True, or a channel.send - either
        returns the message, which the craft buttons need to grey out when they expire).
        Prefers the layout; if it doesn't fit (Discord's 40 components or 4,000 characters), or
        Discord rejects it, sends the complete text pages instead - same facts, same disclosures."""
        if result.blocks:
            try:
                layout = BlueprintResultView(self, result.blocks, result.crafting, result.recipe, result.craft_quantity)
                if layout.content_length() > LAYOUT_TEXT_LIMIT:
                    raise ValueError(f"{layout.content_length()} characters")
            except ValueError as exc:
                logger.warning("Blueprint layout doesn't fit (%s); sending text", exc)
            else:
                try:
                    layout.message = await send(view=layout, allowed_mentions=_NO_MENTIONS)
                    return
                except discord.HTTPException as exc:
                    layout.stop()
                    logger.warning("Blueprint layout send failed (%s); falling back to text", exc)
        view = CraftLaunchView(self, result.recipe, result.craft_quantity) if result.recipe else None
        extras = {"view": view} if view else {}
        for index, page in enumerate(result.pages):
            last = index == len(result.pages) - 1
            sent = await send(content=page, allowed_mentions=_NO_MENTIONS, **(extras if last else {}))
            if last and view is not None:
                view.message = sent

    # -- command --------------------------------------------------------------------------------

    @app_commands.command(name="blueprint-search", description="Find which contracts award a crafting blueprint.")
    @app_commands.describe(
        blueprint="Blueprint name - partial names and small typos are fine, e.g. 'killshot rifle'",
        craft_quantity="How many copies to craft; material quantities scale to this number.",
    )
    @app_commands.autocomplete(blueprint=blueprint_autocomplete)
    async def blueprint_search(
        self, interaction: discord.Interaction, blueprint: app_commands.Range[str, 1, MAX_QUERY_CHARS],
        craft_quantity: app_commands.Range[int, 1, 10000] = 1,
    ) -> None:
        # The very first search can trigger a full sync (~9 requests) - acknowledge before any of it.
        await interaction.response.defer()
        result = await self.search(blueprint, craft_quantity=craft_quantity)
        await self.deliver(functools.partial(interaction.followup.send, wait=True), result)

    @app_commands.command(name="blueprint-list", description="Open your private combined blueprint shopping list.")
    async def blueprint_list(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        await self.shopping.open(interaction)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Blueprints(bot))
