"""Backend-agnostic storage exceptions.

Every storage adapter catches its own library's exceptions and re-raises
as StorageError so endpoint code never imports botocore / minio / etc.
"""

from __future__ import annotations

from typing import Final


class StorageError(Exception):
    """Raised by any storage adapter on operation failure."""

    def __init__(self, code: str, message: str, http_status: int = 502):
        super().__init__(message)
        self.code = code
        self.message = message
        self.http_status = http_status


class StorageTransportError(StorageError):
    """Connection / timeout errors talking to the storage backend."""

    def __init__(self, message: str, http_status: int = 502):
        super().__init__("TransportError", message, http_status)


class StorageOperationNotSupported(StorageError):
    """Raised when the current backend doesn't support the operation."""

    def __init__(self, operation: str, backend: str):
        super().__init__(
            "NotSupported",
            f"'{operation}' is not supported by the {backend} backend",
            501,
        )


# ── Helpers to convert botocore exceptions ──────────────────────────


_CLIENT_ERROR_STATUS: Final[dict[str, int]] = {
    "NoSuchBucket": 404,
    "NoSuchKey": 404,
    "NoSuchUpload": 404,
    "BucketNotEmpty": 409,
    "BucketAlreadyExists": 409,
    "BucketAlreadyOwnedByYou": 409,
    "AccessDenied": 403,
    "InvalidBucketName": 400,
    "InvalidArgument": 400,
    "InvalidPart": 400,
    "EntityTooSmall": 400,
    "InvalidPartOrder": 400,
    "RequestTimeout": 408,
    "SlowDown": 503,
    "ServiceUnavailable": 503,
}


def from_client_error(exc: Exception) -> StorageError:
    """Convert a botocore ClientError to StorageError."""
    response = getattr(exc, "response", {})
    error = response.get("Error", {})
    code = error.get("Code", "Unknown")
    message = error.get("Message", str(exc))
    status = response.get("ResponseMetadata", {}).get("HTTPStatusCode", 502)
    http_status = _CLIENT_ERROR_STATUS.get(code, status)
    return StorageError(code, message, http_status)


def from_transport_error(exc: Exception) -> StorageTransportError:
    """Convert a botocore transport/connection error to StorageTransportError."""
    from botocore.exceptions import EndpointConnectionError, ReadTimeoutError

    if isinstance(exc, EndpointConnectionError):
        return StorageTransportError("S3 endpoint unreachable", 502)
    if isinstance(exc, ReadTimeoutError):
        return StorageTransportError("S3 read timed out", 504)
    return StorageTransportError(f"S3 connection error: {exc}", 502)
