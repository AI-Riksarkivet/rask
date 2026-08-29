"""TrackerProtocol — the interface all tracker backends must implement."""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from tracker.models import TransferStatus


@runtime_checkable
class TrackerProtocol(Protocol):
    """Interface for transfer state tracking.

    Any object implementing these methods can be used as a tracker in bulk transfer operations. The
    default implementation is :class:`~tracker.sqlite.SqliteTracker`.

    It declares everything the backends' own documented usage calls — which once excluded ``flush()``
    and the ``with`` block, so code written against the protocol could not call the method the
    docstrings told it to use.
    """

    def done_keys(self) -> set[str]: ...

    def error_entries(self) -> list[tuple[str, int]]: ...

    def error_details(self) -> list[tuple[str, int, str | None]]: ...

    def delete(self, key: str) -> None:
        """Flush the buffer, then delete the entry for ``key`` if present."""
        ...

    def mark(
        self,
        key: str,
        size: int,
        status: TransferStatus,
        *,
        error: str = "",
        etag: str | None = None,
        validated: bool = False,
        verified: bool = False,
    ) -> None: ...

    def flush(self) -> None:
        """Write all buffered marks to the database."""
        ...

    def commit(self) -> None: ...

    def close(self) -> None: ...

    def __enter__(self) -> TrackerProtocol: ...

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None: ...

    def summary(self) -> dict[str, int]: ...

    def unverified_keys(self) -> list[tuple[str, int, str | None]]: ...

    def unvalidated_keys(self) -> list[tuple[str, int]]: ...
