"""What a source and a sink ARE — the two shapes every adapter in this package satisfies.

The package shipped four adapters (`FSSource`/`FSSink`, `S3Source`/`S3Sink`) and two factories that
return one of each, and nothing anywhere stated the contract: `build_source`/`build_sink` were
annotated `Any` with a `noqa`, so the seam's shape lived only in the head of whoever last read all
four classes. That is also what let the duplicated implementations drift.

Runtime-checkable so a test can assert conformance; structural, so an adapter that lives in a caller
(a runner's own read-through cache, say) satisfies it without importing anything from here.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Protocol, runtime_checkable


@runtime_checkable
class Source(Protocol):
    """Something a workload reads bytes out of, keyed by an opaque string."""

    def keys(self) -> Iterable[str]:
        """The keys this source offers — an iterable, materialised lazily."""
        ...

    def read(self, key: str) -> bytes:
        """The bytes at ``key``. Raises :class:`~storage.errors.ObjectNotFoundError` when absent."""
        ...


@runtime_checkable
class Sink(Protocol):
    """Somewhere a workload writes bytes, keyed the same way."""

    def existing_keys(self, suffix: str = "") -> Iterable[str]:
        """Keys already present (optionally filtered by suffix) — what makes a run resumable."""
        ...

    def write(self, key: str, data: bytes) -> None:
        """Write ``data`` at ``key``, creating whatever intermediate structure the backend needs."""
        ...
