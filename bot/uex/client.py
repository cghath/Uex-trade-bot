"""Async client for the UEX Corp API 2.0 (https://uexcorp.space/api/documentation/).

Auth model:
- Most read endpoints (terminals, commodities, commodities_prices, items, ...) need no auth.
- Endpoints UEX marks "Bearer Token" need the app token created on the UEX "My Apps" page,
  sent as `Authorization: Bearer <token>`. This one app token is shared by the whole bot -
  it identifies the application to UEX, not an individual player.
- User-scoped endpoints (e.g. /user_trades) additionally need the *player's own* personal
  key, sent as a `secret-key` header. Since a bot can serve many Discord users who each have
  their own UEX account, every user-scoped call takes an explicit `secret_key` argument
  rather than relying on one bot-wide value - callers look up the right key per Discord user
  (see bot/db/database.py: get_user_secret_key) and pass it in.

Response envelope is always JSON: {"status": "ok" | "error" | "requests_limit_reached", "data": ..., "message": ...}
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

import httpx

from .exceptions import UexApiError, UexAuthError, UexRateLimitError

logger = logging.getLogger(__name__)

BASE_URL = "https://api.uexcorp.uk/2.0"

# In-memory TTL cache keyed by (path, sorted params) -> (expires_at, data).
# Mirrors the cache windows UEX itself documents per-endpoint, so we don't hammer
# an endpoint faster than its own data actually refreshes.
_DEFAULT_CACHE_TTL = 300  # 5 minutes, conservative default
_ENDPOINT_CACHE_TTL = {
    "terminals": 12 * 3600,
    "commodities": 12 * 3600,
    "commodities_prices": 30 * 60,
    "commodities_prices_all": 30 * 60,
    "commodities_routes": 30 * 60,
    "commodities_prices_history": 3600,
    "items": 12 * 3600,
    "items_prices": 30 * 60,
    "categories": 24 * 3600,
    "marketplace_trends": 3600,
    "vehicles": 12 * 3600,
    "commodities_status": 24 * 3600,
    "marketplace_prices_history": 3600,
    "marketplace_prices_averages": 3600,
    "marketplace_prices_averages_all": 3600,
}

# UEX status strings that specifically mean "the secret_key is missing/wrong/not allowed",
# as opposed to statuses like "no_trades_found" which just mean an empty (but valid) result.
_AUTH_ERROR_STATUSES = {
    "missing_secret_key",
    "invalid_secret_key",
    "user_not_found",
    "user_not_allowed",
}


class UexClient:
    def __init__(
        self,
        app_token: str,
        default_secret_key: str | None = None,
        base_url: str = BASE_URL,
        timeout: float = 15.0,
        max_retries: int = 3,
    ) -> None:
        self._app_token = app_token
        # Optional fallback secret_key (e.g. from .env), used only if a caller doesn't
        # pass one explicitly. In a multi-user bot, callers should always pass their own.
        self._default_secret_key = default_secret_key
        self._base_url = base_url.rstrip("/")
        self._max_retries = max_retries
        self._client = httpx.AsyncClient(timeout=timeout)
        self._cache: dict[tuple, tuple[float, Any]] = {}
        self._cache_lock = asyncio.Lock()

    async def aclose(self) -> None:
        await self._client.aclose()

    async def __aenter__(self) -> "UexClient":
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.aclose()

    # -- low level ---------------------------------------------------------

    def _headers(self, require_secret: bool = False, secret_key: str | None = None) -> dict[str, str]:
        headers = {
            "Authorization": f"Bearer {self._app_token}",
            "Accept": "application/json",
        }
        if require_secret:
            key = secret_key or self._default_secret_key
            if not key:
                raise UexAuthError(
                    "This endpoint needs a UEX secret_key for the calling user. "
                    "Link an account first (e.g. /link-uex-account)."
                )
            headers["secret-key"] = key
        return headers

    async def _get(
        self,
        path: str,
        params: dict[str, Any] | None = None,
        require_secret: bool = False,
        secret_key: str | None = None,
        use_cache: bool = True,
    ) -> Any:
        return await self._request(
            "GET", path, params=params, require_secret=require_secret, secret_key=secret_key, use_cache=use_cache
        )

    async def _post(
        self,
        path: str,
        json_body: dict[str, Any] | None = None,
        require_secret: bool = True,
        secret_key: str | None = None,
    ) -> Any:
        # Writes are never cached and always need the calling player's own secret_key -
        # posting a listing on someone's behalf without their key would be a real bug.
        return await self._request(
            "POST", path, json_body=json_body, require_secret=require_secret, secret_key=secret_key, use_cache=False
        )

    async def _delete(
        self,
        path: str,
        params: dict[str, Any] | None = None,
        require_secret: bool = True,
        secret_key: str | None = None,
    ) -> Any:
        return await self._request(
            "DELETE", path, params=params, require_secret=require_secret, secret_key=secret_key, use_cache=False
        )

    async def _request(
        self,
        method: str,
        path: str,
        params: dict[str, Any] | None = None,
        json_body: dict[str, Any] | None = None,
        require_secret: bool = False,
        secret_key: str | None = None,
        use_cache: bool = True,
    ) -> Any:
        params = {k: v for k, v in (params or {}).items() if v is not None}
        json_body = {k: v for k, v in (json_body or {}).items() if v is not None} if json_body is not None else None

        # Only GET responses are ever cached - a write's "response" isn't reusable data,
        # and caching it under a shared key could leak one user's write result to another.
        use_cache = use_cache and method == "GET"
        cache_key = (method, path, tuple(sorted(params.items())), require_secret, secret_key)

        if use_cache:
            async with self._cache_lock:
                cached = self._cache.get(cache_key)
                if cached and cached[0] > time.monotonic():
                    return cached[1]

        url = f"{self._base_url}/{path.strip('/')}"
        headers = self._headers(require_secret=require_secret, secret_key=secret_key)

        last_error: Exception | None = None
        for attempt in range(1, self._max_retries + 1):
            try:
                response = await self._client.request(method, url, params=params, json=json_body, headers=headers)
            except httpx.HTTPError as exc:
                # Never auto-retry a POST after a network-level failure (timeout, connection
                # drop): the request may have already reached UEX and created the listing -
                # retrying blind could create a duplicate. GET/DELETE are safe to retry since
                # they're idempotent (a lost GET response wastes time, not state; a repeated
                # DELETE just gets "listing_not_found" the second time).
                if method == "POST":
                    raise UexApiError(
                        f"Network error POSTing to {path} - the request may or may not have "
                        f"gone through. Check before retrying to avoid creating a duplicate: {exc}"
                    ) from exc
                last_error = exc
                await asyncio.sleep(min(2**attempt, 10))
                continue

            if response.status_code == 429:
                retry_after = float(response.headers.get("Retry-After", 2**attempt))
                logger.warning("UEX rate limit hit on %s, retrying in %.1fs", path, retry_after)
                await asyncio.sleep(retry_after)
                continue

            try:
                payload = response.json()
            except ValueError as exc:
                raise UexApiError(f"Non-JSON response from {path}: {response.text[:200]}") from exc

            status = payload.get("status")
            message = payload.get("message", "")
            http_code = payload.get("http_code")
            code_part = f" (http_code={http_code})" if http_code is not None else ""

            if status is None:
                # Doesn't even look like UEX's normal {status, data, message} envelope -
                # likely a raw failure from a proxy/gateway in front of the API.
                raise UexApiError(f"Unexpected response from {path} (HTTP {response.status_code}): {response.text[:200]}")

            if status == "requests_limit_reached":
                raise UexRateLimitError(f"UEX daily/per-minute quota reached on {path}")

            if status in _AUTH_ERROR_STATUSES or (
                status == "error" and ("secret_key" in message.lower() or "unauthorized" in message.lower())
            ):
                raise UexAuthError(f"UEX auth error on {path}: {status} {message}{code_part}".strip())

            if status == "error":
                raise UexApiError(f"UEX API error on {path}: {message}{code_part}")

            # Any other status (e.g. "no_trades_found", "invalid_type") is an endpoint-specific
            # "nothing matched" signal, not a fatal error - UEX sends these with non-2xx HTTP
            # codes even though `data` (usually []) is the real, valid answer. Log for visibility
            # but don't raise.
            if status != "ok":
                logger.info("UEX status '%s' on %s: %s", status, path, message)

            data = payload.get("data")

            if use_cache:
                ttl = _ENDPOINT_CACHE_TTL.get(path.strip("/").split("/")[0], _DEFAULT_CACHE_TTL)
                async with self._cache_lock:
                    self._cache[cache_key] = (time.monotonic() + ttl, data)

            return data

        raise UexApiError(f"Failed to reach UEX API at {path} after {self._max_retries} attempts") from last_error

    # -- public/reference data ----------------------------------------------

    async def get_terminals(self, **filters: Any) -> list[dict[str, Any]]:
        """Trading terminals/locations. Filters: id_star_system, id_planet, name, type, code, ..."""
        return await self._get("terminals", params=filters) or []

    async def get_commodities(self, **filters: Any) -> list[dict[str, Any]]:
        return await self._get("commodities", params=filters) or []

    async def get_commodities_prices(self, **filters: Any) -> list[dict[str, Any]]:
        """Buy/sell prices per terminal. Needs at least one filter, e.g. commodity_name='Gold'.

        Includes volatility_price_buy/sell and scu_buy_users_rows/scu_sell_users_rows -
        the latter being real player-submitted trade trip counts over the last 15 days,
        the closest thing UEX has to a "trade volume" signal.
        """
        return await self._get("commodities_prices", params=filters) or []

    async def get_commodities_prices_all(self, **filters: Any) -> list[dict[str, Any]]:
        """Every commodity at every terminal in one call. No filters required.

        Lacks the per-user volume/volatility fields that /commodities_prices has, but is
        one cheap bulk call - good for market-wide "current vs average price" comparisons.
        """
        return await self._get("commodities_prices_all", params=filters) or []

    async def get_commodities_routes(self, **filters: Any) -> list[dict[str, Any]]:
        """UEX's own precomputed buy->sell trade routes, with real distance (GM), ROI,
        profit, and a UEX quality score. Needs at least one of: id_commodity,
        id_terminal_origin, id_planet_origin, id_orbit_origin.
        """
        return await self._get("commodities_routes", params=filters) or []

    async def get_commodities_prices_history(self, **filters: Any) -> list[dict[str, Any]]:
        """Historical price snapshots for one commodity at one terminal.
        Requires id_terminal and id_commodity. Up to 500 rows, updated hourly.
        """
        return await self._get("commodities_prices_history", params=filters) or []

    async def get_items(self, **filters: Any) -> list[dict[str, Any]]:
        return await self._get("items", params=filters) or []

    async def get_items_prices(self, **filters: Any) -> list[dict[str, Any]]:
        return await self._get("items_prices", params=filters) or []

    async def get_companies(self, **filters: Any) -> list[dict[str, Any]]:
        return await self._get("companies", params=filters) or []

    async def get_vehicles(self, **filters: Any) -> list[dict[str, Any]]:
        """Ship/vehicle catalog. Includes `scu` (cargo capacity) and `container_sizes`.
        No auth required. Filter with id_company if needed.
        """
        return await self._get("vehicles", params=filters) or []

    async def get_categories(self, **filters: Any) -> list[dict[str, Any]]:
        """Marketplace listing categories. Filter with type='item'|'service'|'contract'."""
        return await self._get("categories", params=filters) or []

    async def get_commodities_status(self) -> dict[str, list[dict[str, Any]]]:
        """Definitions for the raw status_buy/status_sell codes on /commodities_prices and
        /commodities_routes rows. Shape is irregular vs. every other endpoint here - `data`
        is a dict `{"buy": [...], "sell": [...]}`, not a flat list, since buy-side and sell-side
        codes carry different names/colors for the same numeric code. Each row has: code,
        name, name_short, name_abbr, percentage_start, percentage_end, colors. See
        bot/uex/status.py for turning this into a fast code -> label lookup.
        """
        data = await self._get("commodities_status")
        if not isinstance(data, dict):
            return {"buy": [], "sell": []}
        return {"buy": data.get("buy") or [], "sell": data.get("sell") or []}

    # -- marketplace (player-to-player, separate from commodity/terminal trading) --

    async def get_marketplace_listings(self, **filters: Any) -> list[dict[str, Any]]:
        """Active public marketplace listings. No auth required. Filters: id, slug,
        username, id_item, operation ('buy'|'sell'). Without id_item, capped at 100 rows.
        """
        return await self._get("marketplace_listings", params=filters) or []

    async def get_marketplace_trends(self, **filters: Any) -> list[dict[str, Any]]:
        """Marketplace items with the most negotiation activity right now. No auth required."""
        return await self._get("marketplace_trends", params=filters) or []

    async def get_marketplace_negotiations(self, secret_key: str | None = None, **filters: Any) -> list[dict[str, Any]]:
        """The calling player's own marketplace deals (as buyer or seller)."""
        return await self._get(
            "marketplace_negotiations", params=filters, require_secret=True, secret_key=secret_key, use_cache=False
        ) or []

    async def post_marketplace_advertise(self, secret_key: str, **fields: Any) -> dict[str, Any]:
        """Create a REAL, public UEX marketplace listing as the given player. This is not
        reversible by the bot alone - the caller should confirm with the user before calling
        this. Required fields: id_category, operation ('buy'|'sell'), type
        ('item'|'service'|'contract'), unit, title, description, price, currency, language.
        """
        return await self._post("marketplace_advertise", json_body=fields, secret_key=secret_key)

    async def delete_marketplace_listing(self, listing_id: int, secret_key: str) -> Any:
        """Delete one of the calling player's own marketplace listings."""
        return await self._delete("marketplace_listings", params={"id": listing_id}, secret_key=secret_key)

    async def get_marketplace_prices_history(self, **filters: Any) -> list[dict[str, Any]]:
        """One row per Marketplace listing price CHANGE (not a fixed interval) - unlike
        /commodities_prices_history's regular hourly snapshots. No auth required. Needs at
        least one of: id_item, id_listing, id_terminal, id_star_system, id_category,
        item_uuid, item_name. Optional: operation, quality_tier, currency, game_version,
        date_start, date_end. Capped at 1000 rows.

        This is the endpoint with genuinely populated quality/quality_tier fields (0-7),
        unlike /marketplace_listings' own quality field which is almost always unset -
        see bot/uex/marketplace.py's QUALITY_TIER_CHOICES for the verified tier boundaries.
        """
        return await self._get("marketplace_prices_history", params=filters) or []

    async def get_marketplace_prices_averages(self, **filters: Any) -> list[dict[str, Any]]:
        """UEX's precomputed Marketplace price averages: one row per unique
        id_item + quality_tier + operation + currency + unit combination, each carrying
        price_avg (current, from active listings), price_avg_week (7-day rolling) and
        price_avg_month (30-day rolling), plus listings_count. No auth required; data
        refreshed hourly. Needs at least one of: id_item (up to 10, comma-separated),
        id_category, item_uuid, item_name. Optional narrowing: operation ('buy'|'sell'),
        quality_tier (0-7, same buckets as /marketplace_prices_history), currency,
        game_version.

        Two caveats worth knowing before trusting a row: UEX falls back to price_avg for
        the week/month figures when an item doesn't have enough history yet (so identical
        values may mean "no real 30-day baseline", not "perfectly stable price"), and this
        is a Marketplace endpoint, so numeric fields may arrive as JSON strings - run
        prices through bot/uex/marketplace.py's parse_uex_number, never assume floats.
        """
        return await self._get("marketplace_prices_averages", params=filters) or []

    async def get_marketplace_prices_averages_all(self, **filters: Any) -> list[dict[str, Any]]:
        """The full dump variant of /marketplace_prices_averages: every item's average-price
        rows in one call, no filters required. Same per-row shape (one row per item x
        quality_tier x operation x currency x unit). Used by the hourly Marketplace snapshot
        to learn which items trade at real quality tiers (>= 1) at all - the catalog and
        /marketplace_trends carry no quality information, so this is the one bulk signal for
        "does this item have an in-game quality".
        """
        return await self._get("marketplace_prices_averages_all", params=filters) or []

    async def get_marketplace_favorites(self, secret_key: str | None = None, **filters: Any) -> list[dict[str, Any]]:
        """The calling player's own favorited Marketplace listings (linked UEX account)."""
        return await self._get(
            "marketplace_favorites", params=filters, require_secret=True, secret_key=secret_key, use_cache=False
        ) or []

    # -- user-scoped (requires the calling player's own secret_key) -----------

    async def get_user_trades(self, secret_key: str | None = None, **filters: Any) -> list[dict[str, Any]]:
        return await self._get(
            "user_trades", params=filters, require_secret=True, secret_key=secret_key, use_cache=False
        ) or []
