"""Client for the Star Citizen Wiki API (https://api.star-citizen.wiki) - the source of blueprint and
contract data for /blueprint-search.

Kept separate from bot/uex/client.py on purpose: that one is the only thing that talks to UEX and
carries UEX's auth, rate limit and cache rules; this is a different, volunteer-run, unauthenticated
service with its own etiquette. The API states no rate limit, so this stays deliberately small: a full
sync is ~9 requests per game patch (see bot/cogs/blueprints.py), spaced out, and per-blueprint detail
lookups are cached by the caller.

Every failure surfaces as WikiApiError so callers can catch one type and keep their old data.
"""
from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import Awaitable, Callable
from typing import Any

import httpx

logger = logging.getLogger("uexbot.wiki_api")

BASE_URL = "https://api.star-citizen.wiki/api"
USER_AGENT = "uex-trading-bot (Discord bot blueprint lookups; https://github.com/cghath/Uex-trade-bot)"
PAGE_SIZE = 100
MAX_ATTEMPTS = 3
MAX_RETRY_AFTER_SECONDS = 30.0
# A safety valve: 786 blueprint missions are ~8 pages today. A response claiming wildly more is a bug
# or a changed API, not something to crawl.
MAX_PAGES = 40
_UUID_RE = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")


class WikiApiError(Exception):
    """The wiki API could not give a usable, complete answer."""


def _is_count(value: Any) -> bool:
    """A real integer - `bool` is an int subclass, and `true` is not a page number."""
    return isinstance(value, int) and not isinstance(value, bool)


class WikiApiClient:
    def __init__(
        self, *, base_url: str = BASE_URL, transport: httpx.AsyncBaseTransport | None = None,
        timeout: float = 30.0, request_delay: float = 0.4,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._client = httpx.AsyncClient(
            base_url=base_url, transport=transport, timeout=timeout, follow_redirects=True,
            headers={"Accept": "application/json", "User-Agent": USER_AGENT},
        )
        self._request_delay = request_delay
        self._sleep = sleep

    async def aclose(self) -> None:
        await self._client.aclose()

    async def _get_json(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        """GET with bounded retries on the failures worth retrying (network errors, 429, 5xx),
        honouring Retry-After. Any other 4xx, a non-JSON body, or a non-object body is final."""
        last: Exception | None = None
        for attempt in range(1, MAX_ATTEMPTS + 1):
            delay = float(2 ** attempt)
            try:
                response = await self._client.get(path, params=params)
            except httpx.HTTPError as exc:
                last = exc
            else:
                if response.status_code == 429 or response.status_code >= 500:
                    last = WikiApiError(f"HTTP {response.status_code} from {path}")
                    retry_after = response.headers.get("Retry-After", "")
                    if retry_after.isdigit():
                        delay = min(float(retry_after), MAX_RETRY_AFTER_SECONDS)
                elif response.status_code != 200:
                    raise WikiApiError(f"HTTP {response.status_code} from {path}")
                else:
                    try:
                        body = response.json()
                    except ValueError as exc:
                        raise WikiApiError(f"{path} returned invalid JSON") from exc
                    if not isinstance(body, dict):
                        raise WikiApiError(f"{path} returned an unexpected JSON shape")
                    return body
            if attempt < MAX_ATTEMPTS:
                logger.info("Wiki API %s failed (%s); retrying in %.0fs", path, last, delay)
                await self._sleep(delay)
        raise WikiApiError(f"{path} failed after {MAX_ATTEMPTS} attempts: {last}") from last

    @staticmethod
    def _rows(body: dict[str, Any], path: str) -> list[dict[str, Any]]:
        rows = body.get("data")
        if not isinstance(rows, list):
            raise WikiApiError(f"{path} response has no data list")
        return rows

    async def get_game_version(self) -> str:
        """The game version the API currently serves - one tiny request, so a daily check is cheap."""
        path = "/missions"
        body = await self._get_json(path, {"filter[has_blueprints]": "1", "page[size]": 1})
        rows = self._rows(body, path)
        version = rows[0].get("game_version") if rows and isinstance(rows[0], dict) else None
        if not isinstance(version, str) or not version.strip():
            raise WikiApiError("could not read a game version from the missions endpoint")
        return version.strip()

    @staticmethod
    def _page_meta(body: dict[str, Any], path: str, page: int) -> tuple[int, int, int | None]:
        """(last_page, total, per_page) from a page's `meta`, or WikiApiError. Fails closed: with no
        usable pagination metadata there is nothing to prove the crawl is complete, and quietly
        assuming "one page" would turn a truncated response into a plausible-looking snapshot."""
        meta = body.get("meta")
        if not isinstance(meta, dict):
            raise WikiApiError(f"{path} response has no pagination meta - can't verify the crawl is complete")
        current, last_page, total, per_page = (meta.get(k) for k in ("current_page", "last_page", "total", "per_page"))
        if not _is_count(current) or current != page:
            raise WikiApiError(f"{path} page {page} came back labelled page {current!r}")
        if not _is_count(last_page) or last_page < 1:
            raise WikiApiError(f"{path} reports an invalid page count: {last_page!r}")
        if last_page > MAX_PAGES:
            raise WikiApiError(f"{path} reports {last_page} pages - refusing to crawl that many")
        if not _is_count(total) or total < 0:
            raise WikiApiError(f"{path} reports an invalid total: {total!r}")
        if per_page is not None:
            if not _is_count(per_page) or per_page < 1:
                raise WikiApiError(f"{path} reports an invalid page size: {per_page!r}")
            if last_page != max(1, -(-total // per_page)):
                raise WikiApiError(f"{path} reports {total} rows at {per_page} per page but {last_page} pages")
        return last_page, total, per_page

    async def get_blueprint_missions(self) -> list[dict[str, Any]]:
        """Every mission that awards blueprints, or WikiApiError - never a partial list. Completeness
        is checked against the API's own pagination `meta` (which must be present, self-consistent
        and unchanged from page to page), so a page that silently returned short, a response with no
        meta, or a pagination shift mid-crawl can't be mistaken for the whole set."""
        path = "/missions"
        rows: list[dict[str, Any]] = []
        page = 1
        expected: tuple[int, int] | None = None
        while expected is None or page <= expected[0]:
            body = await self._get_json(
                path, {"filter[has_blueprints]": "1", "page[size]": PAGE_SIZE, "page[number]": page},
            )
            page_rows = self._rows(body, path)
            last_page, total, per_page = self._page_meta(body, path, page)
            if expected is None:
                expected = (last_page, total)
            elif expected != (last_page, total):
                raise WikiApiError(
                    f"{path} pagination changed mid-crawl (pages {expected[0]} -> {last_page}, "
                    f"total {expected[1]} -> {total}) - refusing a mixed snapshot"
                )
            if per_page is not None and page < last_page and len(page_rows) != per_page:
                raise WikiApiError(f"{path} page {page} returned {len(page_rows)} rows, expected {per_page}")
            rows.extend(page_rows)
            page += 1
            if page <= last_page and self._request_delay:
                await self._sleep(self._request_delay)
        if len(rows) != expected[1]:
            raise WikiApiError(f"{path} returned {len(rows)} rows but reports a total of {expected[1]}")
        return rows

    async def get_blueprint_detail(self, blueprint_uuid: str) -> dict[str, Any]:
        """One blueprint's detail page (its unlocking missions with their reward chance)."""
        if not _UUID_RE.fullmatch(blueprint_uuid or ""):
            raise WikiApiError(f"not a blueprint uuid: {blueprint_uuid!r}")
        body = await self._get_json(f"/blueprints/{blueprint_uuid}")
        detail = body.get("data", body)
        if not isinstance(detail, dict):
            raise WikiApiError("blueprint detail has an unexpected shape")
        return detail

    async def get_commodity_detail(self, ore_uuid: str) -> dict[str, Any]:
        """Quantized obtainable qualities for a recipe's ore UUID, never its name."""
        if not _UUID_RE.fullmatch(ore_uuid or ""):
            raise WikiApiError("not a commodity uuid")
        body = await self._get_json(f"/commodities/{ore_uuid}")
        detail = body.get("data")
        if not isinstance(detail, dict) or detail.get("uuid") != ore_uuid:
            raise WikiApiError("commodity detail identity mismatch")
        return detail

    async def get_vehicle_ports(self, vehicle_name: str) -> list[dict[str, Any]]:
        """One ship's hardpoint/component slots ("ports": type, size range, default
        equipped item), for the Ship Parts Finder - UEX has no equivalent data (its own
        `id_vehicle` FK on `/items` is populated almost exclusively for cosmetic Liveries,
        confirmed empirically against live catalog data, not functional components).

        `filter[name]` on this API matches by substring, not exact name (a query of
        "Avenger" returns every Avenger variant) - so this only accepts an exact,
        case-insensitive `name` match against the results and returns [] otherwise. Callers
        must pass an already-resolved canonical ship name (e.g. via
        `bot/uex/ships.py: resolve_ship` against UEX's own vehicle list) rather than a raw
        user query, the same "resolve to exactly one candidate first" convention every other
        name-matching helper in this codebase follows.
        """
        ports, _ = await self.get_vehicle_loadout(vehicle_name)
        return ports

    async def get_vehicle_loadout(self, vehicle_name: str) -> tuple[list[dict[str, Any]], list[str]]:
        """get_vehicle_ports plus the ship's own `port_tags` (e.g. ['AEGS_Avenger_Base']) -
        a ship-specific part like the 'Reliant Toshima Turret' carries `required_tags`
        (['MISC_Reliant_Base']) that only a ship with those tags satisfies. ([], []) when
        the name doesn't resolve to exactly one ship."""
        body = await self._get_json("/vehicles", {"filter[name]": vehicle_name, "page[size]": 10})
        rows = self._rows(body, "/vehicles")
        target = vehicle_name.strip().lower()
        exact = [row for row in rows if (row.get("name") or "").strip().lower() == target]
        if len(exact) != 1:
            return [], []
        ports = exact[0].get("ports")
        tags = exact[0].get("port_tags")
        return (ports if isinstance(ports, list) else [],
                [t for t in tags if isinstance(t, str)] if isinstance(tags, list) else [])

    async def get_item_detail(self, item_uuid: str) -> dict[str, Any]:
        """One component's real stats (e.g. `power_plant.power_segment_generation`) plus
        its UEX-sourced price/terminal listings (`uex_prices.purchase`), by Star Citizen
        UUID - verified live that `uex_prices.purchase[].terminal_id` is the exact same id
        UEX's own `/terminals` uses (3/3 real terminals cross-checked by id and name), not
        an independently-scraped copy. Component hardware prices change far less often than
        commodity market prices, so this embedded copy is used as the price/stats source
        directly rather than a separate live UEX lookup per candidate item."""
        if not _UUID_RE.fullmatch(item_uuid or ""):
            raise WikiApiError(f"not an item uuid: {item_uuid!r}")
        body = await self._get_json(f"/items/{item_uuid}")
        detail = body.get("data")
        if not isinstance(detail, dict) or detail.get("uuid") != item_uuid:
            raise WikiApiError("item detail identity mismatch")
        return detail

    async def find_item_detail_by_name(self, item_name: str) -> dict[str, Any] | None:
        """A component's detail by exact name, for when UEX's uuid for it doesn't exist on
        the wiki (most radars: 'Fleming', 'Capston', 'Observer-Go' all have a UEX uuid the
        wiki doesn't know, but the same name does resolve). `filter[name]` matches by
        substring, so - same as get_vehicle_ports - only a single exact, case-insensitive
        name match counts; zero or several returns None rather than a guess."""
        exact = await self.find_item_variants_by_name(item_name)
        if len(exact) != 1 or not _UUID_RE.fullmatch(exact[0].get("uuid") or ""):
            return None
        return await self.get_item_detail(exact[0]["uuid"])

    async def find_item_variants_by_name(self, item_name: str) -> list[dict[str, Any]]:
        """Every wiki item whose name is exactly `item_name` (case-insensitive). One shop
        name can be several game items: 'VariPuck S4 Gimbal Mount' is 6, one generic and
        five ship-specific ones (Polaris, M80, ...) with their own `required_tags` - and
        UEX's uuid for it points at the Polaris-only one. The list rows are full records,
        required_tags included, so no per-variant detail call is needed."""
        target = (item_name or "").strip().lower()
        if not target:
            return []
        body = await self._get_json("/items", {"filter[name]": item_name.strip(), "page[size]": 25})
        rows = self._rows(body, "/items")
        return [row for row in rows
                if isinstance(row, dict) and (row.get("name") or "").strip().lower() == target]
