"""Exceptions raised by the UEX API client."""


class UexApiError(Exception):
    """Base class for UEX API errors."""


class UexRateLimitError(UexApiError):
    """Raised when UEX reports 'requests_limit_reached'."""


class UexAuthError(UexApiError):
    """Raised for missing/invalid app token or secret_key."""


class UexNotFoundError(UexApiError):
    """Raised when the API returns no matching data for a lookup."""
