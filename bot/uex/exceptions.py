"""Exceptions raised by the UEX API client."""


class UexApiError(Exception):
    """Base class for UEX API errors. Also covers genuinely AMBIGUOUS outcomes - a
    network-level failure (timeout, connection drop), a non-JSON response, or exhausting
    retries - where UEX's actual handling of the request is unknown, not rejected."""


class UexRejectedError(UexApiError):
    """UEX responded and explicitly rejected the request with a real, documented status
    (a literal "error" status, or any other non-"ok" status on a write endpoint - see
    UexClient._request). Distinct from the ambiguous base class: this means UEX definitely
    received and rejected the request, so no write happened and no reconciliation is
    needed. Callers that need to tell a definite rejection from a genuinely uncertain
    outcome (e.g. deciding whether an inventory post might have silently gone through)
    should check `isinstance(exc, UexRejectedError)` rather than parsing message text -
    the message format is not a stable contract, confirmed by a past regression where a
    caller's string-prefix classifier silently broke against a new error message shape
    that still meant the exact same thing structurally.
    """


class UexRateLimitError(UexRejectedError):
    """Raised when UEX reports 'requests_limit_reached' - rate limiting happens before a
    write is processed, so nothing was created/deleted."""


class UexAuthError(UexRejectedError):
    """Raised for missing/invalid app token or secret_key - the request never got far
    enough to touch data."""


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
