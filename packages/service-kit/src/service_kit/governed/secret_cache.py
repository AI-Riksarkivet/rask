"""Reads of the Dapr secret store, each served for a bounded, jittered time ([[XC-082]], ADR 0166).

A STORE THE PODS NEVER RE-READ MAKES A ROTATION AN OUTAGE: a credential replaced in OpenBao is still the one every
running pod signs with, until a restart, and once the old one is revoked every call under it fails. So every value read
from the store is served for at most `SECRET_TTL_SECONDS` after the store returned it, and re-read after that.

THE TTL IS JITTERED DOWNWARD. Each entry lives a random fraction shorter than the TTL, by up to `SECRET_TTL_JITTER`, so
the TTL stays the bound on how stale a served value can be, and replicas that read together do not all re-read
together. The same shape as Lakekeeper's secret cache (`crates/lakekeeper/src/service/cache_ttl.rs`, 600 s base).

FAILS CLOSED. A load that raises is never cached and never extends what is held, so a value the store stopped
confirming is dropped when its TTL passes, and a field the store does not hold is asked for again on the next read
rather than remembered as absent.

SINGLE-FLIGHT PER READ. Concurrent callers that find one read missing or expired wait on one load rather than each
asking the store. A read is keyed on the store, the secret AND the fields asked for, and holds only those fields: the
`lance` bundle carries every service's credential, and this process keeps no more of it than it uses.
"""

from __future__ import annotations

import secrets
import threading
import time
from collections.abc import Callable, Mapping
from typing import Final

from pydantic import BaseModel, ConfigDict


#: The longest a value read from the store is served before it is read again.
SECRET_TTL_SECONDS: Final = 600.0

#: The most an entry's lifetime is shortened by, as a fraction of the TTL: entries live between 300 and 600 seconds.
SECRET_TTL_JITTER: Final = 0.5

_JITTER: Final = secrets.SystemRandom()


class StoreRead(BaseModel):
    """One read of the secret store: these fields of this secret, from this store."""

    model_config = ConfigDict(frozen=True)

    store: str
    secret: str
    fields: tuple[str, ...]


class _Entry(BaseModel):
    model_config = ConfigDict(frozen=True)

    values: dict[str, str]
    loaded_at: float
    ttl: float


class SecretCache:
    """Store reads, each held until its jittered TTL passes."""

    def __init__(self, *, ttl_seconds: float = SECRET_TTL_SECONDS, jitter: float = SECRET_TTL_JITTER, clock: Callable[[], float] = time.monotonic) -> None:
        """Build an empty cache.

        Args:
            ttl_seconds: The longest a value is served after the store returned it.
            jitter: The most an entry's lifetime is shortened by, as a fraction of ``ttl_seconds``.
            clock: Monotonic seconds; the TTL is measured on it.

        Raises:
            ValueError: ``ttl_seconds`` is not positive, or ``jitter`` is outside ``[0, 1)``.
        """
        if ttl_seconds <= 0:
            raise ValueError(f"ttl_seconds must be positive, got {ttl_seconds}")
        if not 0 <= jitter < 1:
            raise ValueError(f"jitter must be in [0, 1) so every entry lives a while, got {jitter}")
        self._ttl = ttl_seconds
        self._jitter = jitter
        self._clock = clock
        self._entries: dict[StoreRead, _Entry] = {}
        self._locks: dict[StoreRead, threading.Lock] = {}
        self._locks_guard = threading.Lock()

    def peek(self, read: StoreRead) -> dict[str, str] | None:
        """What the store last returned for ``read``, while that is under its TTL; ``None`` otherwise. Never reads the store."""
        entry = self._entries.get(read)
        if entry is None or self._clock() - entry.loaded_at >= entry.ttl:
            return None
        return dict(entry.values)

    def get(self, read: StoreRead, load: Callable[[], Mapping[str, str]]) -> dict[str, str]:
        """``read``'s values: the held ones while under their TTL, else what ``load`` returns.

        Blocking: a caller that misses waits for the one load in flight for ``read``.

        Raises:
            Exception: Whatever ``load`` raises. Nothing is cached then.
        """
        if (held := self.peek(read)) is not None:
            return held
        with self._lock_for(read):
            if (held := self.peek(read)) is not None:
                return held
            return self._load(read, load)

    def refresh(self, read: StoreRead, load: Callable[[], Mapping[str, str]]) -> dict[str, str]:
        """Load ``read`` now, whatever is held.

        Raises:
            Exception: Whatever ``load`` raises. What is held stays until its own TTL passes.
        """
        with self._lock_for(read):
            return self._load(read, load)

    def due(self, read: StoreRead) -> bool:
        """Whether ``read`` should be read again: nothing is held, or half the held value's TTL is used."""
        entry = self._entries.get(read)
        return entry is None or self._clock() - entry.loaded_at >= entry.ttl / 2

    def clear(self) -> None:
        """Forget every read."""
        self._entries.clear()

    def _load(self, read: StoreRead, load: Callable[[], Mapping[str, str]]) -> dict[str, str]:
        values = dict(load())
        ttl = self._ttl * (1 - _JITTER.random() * self._jitter)
        self._entries[read] = _Entry(values=values, loaded_at=self._clock(), ttl=ttl)
        return dict(values)

    def _lock_for(self, read: StoreRead) -> threading.Lock:
        with self._locks_guard:
            return self._locks.setdefault(read, threading.Lock())


#: This process's reads of the secret store. A test replaces it to drive the TTL with its own clock.
STORE_READS = SecretCache()
