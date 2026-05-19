"""Backend-agnostic storage abstraction layer."""

from app.services.storage.errors import (
    StorageError,
    StorageOperationNotSupported,
    StorageTransportError,
)
from app.services.storage.factory import create_cached_storage, create_storage
from app.services.storage.protocol import StorageProtocol

__all__ = [
    "StorageError",
    "StorageOperationNotSupported",
    "StorageProtocol",
    "StorageTransportError",
    "create_cached_storage",
    "create_storage",
]
