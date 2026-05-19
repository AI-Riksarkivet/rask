"""HCPError hierarchy — maps HTTP status codes to typed exceptions."""

from __future__ import annotations


class HCPError(Exception):
    """Base error for all rahcp operations."""

    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.status_code = status_code

    def __repr__(self) -> str:
        if self.status_code:
            return f"{type(self).__name__}({self.status_code}, {self.message!r})"
        return f"{type(self).__name__}({self.message!r})"


class AuthenticationError(HCPError):
    """401 — bad credentials or expired token."""


class NotFoundError(HCPError):
    """404 — tenant, namespace, or object not found."""


class ConflictError(HCPError):
    """409 — resource already exists."""


class RetryableError(HCPError):
    """408, 429, 500, 503, 504 — transient failure after all retries exhausted."""


class UpstreamError(HCPError):
    """502 — backend's upstream service is unreachable. Not retried."""


def error_for_status(status_code: int, message: str) -> HCPError:
    """Map an HTTP status code to the appropriate HCPError subclass."""
    cls = _STATUS_MAP.get(status_code, HCPError)
    return cls(message, status_code=status_code)


_STATUS_MAP: dict[int, type[HCPError]] = {
    401: AuthenticationError,
    403: AuthenticationError,
    404: NotFoundError,
    409: ConflictError,
    408: RetryableError,
    429: RetryableError,
    500: RetryableError,
    502: UpstreamError,
    503: RetryableError,
    504: RetryableError,
}
