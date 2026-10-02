"""Verifying the signature an event carries against the public keys its signer publishes ([[LH-064]]).

The bus door authenticates the sidecar that delivered an event, never the producer that wrote it, so the author
stamped inside is a claim until a signature proves it. A producer signs with an Ed25519 key only its own sidecar can
read; lineage holds PUBLIC keys only, so a compromised lineage can admit or refuse events and cannot forge one.

WHAT THIS MODULE OWNS is the part the kit cannot: reading each identity's published list through this pod's Dapr
sidecar (`signing-public-<identity>`, field `keys`), and turning the kit's outcomes into the three answers a door
needs. `lineage_kit.signing.verify_signature` owns what a valid signature is.

* VERIFIED: the event's signer and the key that signed it are known. The caller may act on what the signature attests.
* REFUSED (`UnverifiedEventError`, a `PermissionDeniedError`): no listed signer's published key verifies the event.
  Final, so every door answers it without a retry.
* OUTAGE (`ServiceUnavailableError`): the list could not be read, or was read before this event arrived and may
  predate the key that signed it. Says nothing about the signature, so the delivery is retried and a staged object
  stays staged. Refusing on an outage would destroy honest events during a store blip.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from typing import Any, Final, NamedTuple

from fastapi import Request
from fastapi.concurrency import run_in_threadpool
from lance_namespace import ServiceUnavailableError

from lineage.core.config import LineageSettings
from lineage.core.metrics import record_signature_refused, record_signature_verified
from lineage.models import UnverifiedEventError, run_id_from_payload
from lineage_kit.signing import (
    KeySourceUnavailableError,
    PublicKeySource,
    SignatureError,
    VerifiedSignature,
    parse_published_keys,
    signature_of,
    verify_signature,
)
from service_kit.governed.secrets import fetch_dapr_secret


log = logging.getLogger(__name__)

#: ONE attempt, and short. Verification sits on the delivery path of every signed event, and the boot-time retry
#: `fetch_dapr_secret` defaults to (about two minutes) would hold a worker that long per event through a store
#: blip. An outage is answered RETRY instead, and the sidecar's redelivery is the retry.
KEY_READ_TIMEOUT_SECONDS: Final = 2.0

#: How long one successful read of an identity's list is served without asking again. SHORT, because the list is
#: the trust anchor: a key removed from it stops verifying within this.
KEY_TTL_SECONDS: Final = 60.0

#: The least time between two reads forced by a key id the cached list does not hold. BELOW the sidecar's first
#: redelivery (the chart's `pubsubDeliveryRetry` is a constant 120 s), so one redelivery of an event that met an
#: unknown key id always ends in a verdict, and a stream of forged key ids costs one read per interval rather than
#: one per event.
KEY_REFRESH_INTERVAL_SECONDS: Final = 30.0

#: Bounds what an event's own text can put into a log line or a recorded refusal: the kit's messages quote the
#: identity and algorithm the event claims.
_MAX_REASON_CHARS: Final = 300


class _Read(NamedTuple):
    keys: tuple[str, ...]
    #: When the read BEGAN. A verdict about an event rests only on a read that began after the event arrived, since a
    #: list read earlier may predate the key that signed it.
    started: float
    #: When it ended: the age the TTL and the refresh interval are counted from.
    finished: float


class PublishedKeys:
    """What each listed signer publishes, read through this pod's Dapr sidecar and cached by what the store said.

    Successes only: a list that could not be read is never cached, so an outage is answered afresh on the next call
    and a half-seeded store cannot pin a missing list for a TTL. Reads of one identity are SINGLE-FLIGHT, each
    behind its own lock: concurrent callers share the read in progress rather than each asking the store.

    Never keyed by anything the event supplies: the kit reads an identity only after checking it against the
    chart-derived signer set, so the locks and the cache are bounded by that set.
    """

    def __init__(self, *, store: str, clock: Callable[[], float] = time.monotonic) -> None:
        self._store = store
        self._clock = clock
        self._reads: dict[str, _Read] = {}
        self._guard = threading.Lock()
        self._locks: dict[str, threading.Lock] = {}

    def for_event(self) -> PublicKeySource:
        """The key source for ONE verification, stamped with the moment it began."""
        return _KeysForOneEvent(self, since=self._clock())

    def cached(self, identity: str) -> Sequence[str]:
        """The identity's list from the cache while it is younger than the TTL, else from a fresh read."""
        with self._lock_for(identity):
            held = self._reads.get(identity)
            if held is not None and self._clock() - held.finished < KEY_TTL_SECONDS:
                return held.keys
            return self._read(identity).keys

    def read_since(self, identity: str, since: float) -> Sequence[str]:
        """The identity's list as read AFTER ``since``, which is what an unknown key id is judged on.

        A read that began after the event arrived is returned as it is, so a verdict never rests on a stale list.
        A list read moments ago but BEFORE the event arrived cannot decide it either, and reading again for every
        such event would hand a forger the store; that is declined with `KeySourceUnavailableError`, which the door
        answers RETRY. A rate limit delays a verdict and never decides one.
        """
        with self._lock_for(identity):
            held = self._reads.get(identity)
            if held is not None and held.started >= since:
                return held.keys
            if held is not None and self._clock() - held.finished < KEY_REFRESH_INTERVAL_SECONDS:
                raise KeySourceUnavailableError(f"the public keys {identity!r} publishes were read moments before this event arrived; retry after the interval")
            return self._read(identity).keys

    def _lock_for(self, identity: str) -> threading.Lock:
        with self._guard:
            lock = self._locks.get(identity)
            if lock is None:
                lock = self._locks[identity] = threading.Lock()
            return lock

    def _read(self, identity: str) -> _Read:
        """One bounded read. Unreadable and absent are one answer: either way nothing is known about the signature."""
        started = self._clock()
        bundle = fetch_dapr_secret(self._store, f"signing-public-{identity}", timeout=KEY_READ_TIMEOUT_SECONDS, retries=1)
        keys = tuple(parse_published_keys(bundle.get("keys", "")))
        if not keys:
            raise KeySourceUnavailableError(f"the public keys {identity!r} publishes could not be read")
        read = _Read(keys=keys, started=started, finished=self._clock())
        self._reads[identity] = read
        return read


class _KeysForOneEvent:
    """`PublicKeySource` for one verification: `published` may answer from the cache, `refresh` only from a read after `since`."""

    def __init__(self, keys: PublishedKeys, *, since: float) -> None:
        self._keys = keys
        self._since = since

    def published(self, identity: str) -> Sequence[str]:
        return self._keys.cached(identity)

    def refresh(self, identity: str) -> Sequence[str]:
        return self._keys.read_since(identity, self._since)


def _published_keys(request: Request, settings: LineageSettings) -> PublishedKeys:
    """This process's key reader, built on first use and kept on the app so its cache outlives one delivery."""
    state = request.app.state
    keys: PublishedKeys | None = getattr(state, "published_keys", None)
    if keys is None:
        keys = state.published_keys = PublishedKeys(store=settings.dapr_secret_store)
    return keys


def _bounded(text: str) -> str:
    return text if len(text) <= _MAX_REASON_CHARS else f"{text[:_MAX_REASON_CHARS]}..."


def _subject_of(arrived: Mapping[str, Any]) -> dict[str, str | None]:
    """Which event a log line is about: its run, or the dataset a static change names. Only strings, and bounded."""
    dataset = arrived.get("dataset")
    name = dataset.get("name") if isinstance(dataset, dict) else None
    run_id = run_id_from_payload(arrived)
    return {"run_id": _bounded(run_id) if run_id else None, "dataset": _bounded(name) if isinstance(name, str) else None}


async def verify_arrived_event(arrived: Mapping[str, Any], request: Request, settings: LineageSettings) -> VerifiedSignature | None:
    """Verify the signature on the event exactly as it arrived, or answer ``None`` when this estate enforces none.

    ``None`` is the unenforced estate (`settings.signing` lists no signer): nothing is verified, a `rask_signature`
    the event carries is ignored, and nothing may be admitted on one. A caller reads a signature's meaning from
    the returned `VerifiedSignature`, never from the event, so it acts on what THIS call established.

    OVER THE BYTES THAT ARRIVED, never a re-serialisation: the producer signed the document it wrote, and a model
    dump grows the fields the schema defaults. Verification runs in the threadpool because it reads the store
    through a blocking client.

    Raises:
        UnverifiedEventError: no listed signer's published key verifies the event. Final.
        ServiceUnavailableError: the identity's published keys could not be read, or could not be read afresh for an
            unknown key id. Retry.
    """
    signing = settings.signing
    if not signing.enforced:
        return None
    keys = _published_keys(request, settings)
    try:
        verified = await run_in_threadpool(verify_signature, arrived, source=keys.for_event(), signers=signing.signers, delegators=signing.delegators)
    except SignatureError as exc:
        record_signature_refused(exc.reason)
        claim = signature_of(arrived)
        log.warning(
            "lineage_signature_refused",
            extra={**_subject_of(arrived), "reason": exc.reason, "identity": _bounded(claim.identity) if claim else None},
        )
        raise UnverifiedEventError(_bounded(str(exc)), exc.reason) from exc
    except KeySourceUnavailableError as exc:
        log.warning("lineage_signature_keys_unavailable", extra={**_subject_of(arrived), "error": _bounded(str(exc))})
        raise ServiceUnavailableError(_bounded(str(exc))) from exc
    record_signature_verified(verified.identity)
    return verified
