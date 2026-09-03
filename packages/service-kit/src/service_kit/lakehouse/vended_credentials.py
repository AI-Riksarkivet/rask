"""Cached, per-table vended credentials for any writer that puts bytes on object storage directly.

A writer that puts fragments straight on object storage should sign them with a credential scoped to
one table prefix and short-lived, rather than a long-lived key that reaches the whole bucket — proven
enforced on RustFS 2026-09-03, where a credential vended for one table was refused on another with
403 AccessDenied, and where a read-tier credential answered PUT and DELETE with AccessDenied too.

IT LIVES IN SERVICE-KIT because it has two consumers in different processes: the ingest worker's
client-direct write, and the maintenance executor's compaction. `base_refs` and `work_items` moved
here before it, for the same reason and with the same consequence if they had not — a second copy of
a credential cache is a second answer to "when does this expire", and the one that is wrong reports a
403 that reads as a permission problem rather than as a stale credential.

Two facts decide the shape here and they pull against each other. An ingest run is long and its units
are many (`ingest/queue.py` speaks of "millions of units"), so vending per unit would put a catalog
round trip on the hot path of every write. But the credential EXPIRES — 900s by default — so vending
once and holding it means a long run starts failing halfway through with a 403 that reads as a
permission problem rather than an expiry.

So: cache per table, refresh while still valid. `expires_at_millis` comes back from the vending door
for exactly this.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from typing import Protocol

from pydantic import BaseModel, Field


logger = logging.getLogger(__name__)


class VendedCredential(BaseModel):
    """A scoped credential and WHEN IT DIES, together.

    The expiry travels with the options because only the vend knows it: a caller that had to supply it
    would be guessing, and a cache that never learned it would either re-vend on every write or hold a
    credential past its death and report the 403 as a permission problem.
    """

    options: dict[str, str]
    #: Epoch milliseconds, as the vending door reports it. Wall-clock, never a duration.
    expires_at_millis: float | None = None


class CredentialVendor(Protocol):
    """Asks the catalog for a scoped credential. ``None`` means none is on offer.

    A Protocol rather than the concrete client so the worker can be driven without a catalog, and so
    nothing here depends on how the credential is fetched.
    """

    def __call__(self, namespace: str, dataset: str, *, tier: str = ...) -> VendedCredential | None: ...


#: Refresh this long before expiry. Wide enough that a batch already in flight finishes on the old
#: credential, narrow enough not to re-vend constantly. Refreshing ON FAILURE instead would surface as
#: a mid-run 403 — indistinguishable, to whoever reads it, from a genuine authorization problem.
_REFRESH_MARGIN_SECONDS = 60.0


class _Entry(BaseModel):
    options: dict[str, str] | None
    #: Monotonic-clock deadline after which this entry must be re-vended. ``None`` = never expires,
    #: which is how a "nothing on offer" answer is remembered without re-asking on every write.
    refresh_after: float | None = Field(default=None)


class VendedCredentialCache:
    """One vended credential per table, refreshed before it expires.

    The vendor is injected (writing-python `design-patterns` → Dependency injection), so expiry can be
    driven in a test without a clock or a catalog.
    """

    def __init__(self, vendor: CredentialVendor, *, now: Callable[[], float] | None = None) -> None:
        self._vendor = vendor
        # WALL CLOCK, not `time.monotonic`. `expires_at_millis` is an epoch timestamp minted by the
        # store, so the deadline has to be compared on the same scale. Monotonic counts from an
        # arbitrary per-process origin: on a freshly started pod it is near zero, so every credential
        # reads as valid for decades; on a long-lived one it can exceed any real epoch and every
        # credential reads as already expired. Monotonic is right for measuring DURATIONS and wrong
        # for meeting someone else's deadline.
        self._now = now or time.time
        self._entries: dict[tuple[str, str], _Entry] = {}

    def storage_options(self, namespace: str, dataset: str) -> dict[str, str] | None:
        """The storage options to write this table with, or ``None`` to use the ambient credential.

        The expiry is read off the vend itself — only the vend knows it, so a caller that had to
        supply it would be guessing.
        """
        key = (namespace, dataset)
        entry = self._entries.get(key)
        if entry is not None and (entry.refresh_after is None or self._now() < entry.refresh_after):
            return entry.options

        vended = self._vendor(namespace, dataset, tier="write")
        options = vended.options if vended is not None else None
        expires_at_millis = vended.expires_at_millis if vended is not None else None
        # A "nothing on offer" answer is cached WITHOUT a deadline. `mode_b` is a deliberate posture,
        # and re-asking on every write would put a doomed round trip on the hot path of a deployment
        # that simply chose server-mediated access.
        # `max(..., now)` keeps the deadline out of the PAST: a credential that arrives already inside
        # the margin is used once and re-vended on the NEXT call, rather than re-vended in a loop here.
        refresh_after = None if options is None or expires_at_millis is None else max(expires_at_millis / 1000.0 - _REFRESH_MARGIN_SECONDS, self._now())
        self._entries[key] = _Entry(options=options, refresh_after=refresh_after)
        # LOGGED AT THE VEND, which is the only transition — a cached hit is silent, because this sits
        # behind writes counted in millions and a line per write would be a line per unit. Without it
        # the posture is unauditable in both directions: a scoped write and a fallback to the key that
        # reaches the whole bucket looked identical from outside, so an estate whose vending door had
        # silently stopped working was indistinguishable from a hardened one.
        #
        # NEVER the credential itself. What is useful is which table, whether it is scoped, and when it
        # expires; the secret in a log store would undo the scoping it is reporting on.
        if options is None:
            logger.info("write credential AMBIENT for %s$%s — nothing vended; these writes are signed by the process credential", namespace, dataset)
        else:
            logger.info(
                "write credential SCOPED for %s$%s — expires in %.0fs",
                namespace,
                dataset,
                0.0 if expires_at_millis is None else max(expires_at_millis / 1000.0 - self._now(), 0.0),
            )
        return options
