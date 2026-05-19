"""TrackerProtocol — the interface all tracker backends must implement."""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from rahcp_tracker.models import TransferStatus


@runtime_checkable
class TrackerProtocol(Protocol):
    """Interface for transfer state tracking.

    Any object implementing these methods can be used as a tracker
    in bulk transfer operations.  The default implementation is
    :class:`~rahcp_tracker.sqlite.SqliteTracker`.
    """

    def done_keys(self) -> set[str]: ...

    def error_entries(self) -> list[tuple[str, int]]: ...

    def mark(
        self,
        key: str,
        size: int,
        status: TransferStatus,
        error: str = "",
        *,
        etag: str | None = None,
        validated: bool = False,
        verified: bool = False,
    ) -> None: ...

    def commit(self) -> None: ...

    def close(self) -> None: ...

    def summary(self) -> dict[str, int]: ...

    def unverified_keys(self) -> list[tuple[str, int, str | None]]: ...

    def unvalidated_keys(self) -> list[tuple[str, int]]: ...
