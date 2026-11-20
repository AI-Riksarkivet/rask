"""Storage options that answer as a service's do while it holds no store session (``lakehouse.storage.store_session``)."""

from __future__ import annotations

from lakehouse.storage.credential import StoreCredentialUnavailableError
from lakehouse.storage.options import StorageOptions


def no_store_session() -> StorageOptions:
    """A service's storage options with no unexpired store session held, as its settings answer them then.

    Raises:
        StoreCredentialUnavailableError: Always.
    """
    raise StoreCredentialUnavailableError("no unexpired store session is held")
