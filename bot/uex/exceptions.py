"""Exceptions raised by the UEX API client."""


class UexApiError(Exception):
    """Base class for UEX API errors."""


class UexRateLimitError(UexApiError):
    """Raised when UEX reports 'requests_limit_reached'."""


class UexAuthError(UexApiError):
    """Raised for missing/invalid app token or secret_key."""


class UexNotFoundError(UexApiError):
    """Raised when the API returns no matching data for a lookup."""


def describe_uex_api_error(exc: UexApiError) -> str:
    """User-facing text for a failed UEX call - distinguishes transient (rate limit)
    from actionable (auth) failures instead of surfacing the same raw message for
    every failure class."""
    if isinstance(exc, UexRateLimitError):
        return f"UEX is rate-limiting requests right now - try again in a minute. ({exc})"
    if isinstance(exc, UexAuthError):
        return (
            f"UEX rejected the request as unauthorized ({exc}). If you've linked a UEX "
            "account, try /unlink-uex-account then /link-uex-account again with a fresh key."
        )
    return f"UEX didn't return a valid response just now - this is usually temporary, try again in a moment. ({exc})"
