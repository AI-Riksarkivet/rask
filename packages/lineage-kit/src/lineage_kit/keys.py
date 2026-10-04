"""The public keys each signing identity publishes, read through an injected fetch and served from a short-lived cache ([[LH-064]], [[XC-078]]).

A verifier needs the keys its signer publishes: the secret `signing-public-<identity>`, field `keys`, current key first. Every door
that verifies reads them the same way, so the reader is one class and only HOW a secret is fetched differs between services. That
is the `fetch` callable a caller injects: a service reads through its own Dapr sidecar, whose client lives in service-kit, and
lineage-kit cannot import service-kit while service-kit cannot import lineage-kit, so the seam between them is a function.

`fetch(secret_name)` answers the secret's fields, or `{}` when the secret could not be read. An unreadable list and an absent one
are one answer, `KeySourceUnavailableError`: either way nothing is known about a signature, and a verifier that refused on a store
blip would destroy honest events.
"""

from __future__ import annotations

import threading
import time
from collections import OrderedDict
from collections.abc import Callable, Mapping, Sequence
from typing import Final, NamedTuple

from lineage_kit.signing import KeySourceUnavailableError, PublicKeySource, parse_published_keys


#: An identity publishes its keys in the secret named `<prefix><identity>`, as the comma-separated text of its `keys` field.
_PUBLISHED_LIST_PREFIX: Final = "signing-public-"
_KEYS_FIELD: Final = "keys"

#: How long one successful read of an identity's list is served without asking again. SHORT, because the list is
#: the trust anchor: a key removed from it stops verifying within this.
KEY_TTL_SECONDS: Final = 60.0

#: The least time between two reads forced by a key id the cached list does not hold, so a stream of forged key ids
#: costs one read per interval rather than one per event. A key id is judged on a list read AFTER IT WAS FIRST MET
#: (`PublishedKeys.read_for_unknown_key_id`), not after the delivery in hand arrived: a redelivery arrives later than
#: the delivery before it, so a rule keyed on the arrival is deferred again by every read another event made in the
#: interval before it. The interval therefore only paces reads. It is far BELOW the sidecar's first redelivery (the
#: chart's `pubsubDeliveryRetry` is a constant 120 s), so when a key id comes round again a read has either begun
#: since it was first met or ended long enough ago to repeat, and the redelivery ends in a verdict.
KEY_REFRESH_INTERVAL_SECONDS: Final = 30.0

#: How many key ids ONE identity remembers having met unknown, each with when it was first met. The ids are the events'
#: own text, so the memory is bounded, and per identity so that a flood against one identity cannot push another's out.
#: An id is met twice, at its delivery and at the sidecar's redelivery 120 s later, where it is decided, so the memory
#: has to hold the ids of two redelivery intervals. 8192 ids (162 bytes each, measured on CPython 3.13: 1.3 MB per
#: identity) hold a flood of about 34 DIFFERENT ids a second: measured through the real reader with the sidecar's retry
#: schedule emulated, none are parked at 30 a second and 88% at 36. Honest traffic adds one id per rotation, while the
#: cached list predates the new key. An id pushed out is met afresh, which costs its event one more retry cycle and
#: never a refusal on a list that predates the key.
MAX_UNKNOWN_KEY_IDS: Final = 8192


class _Read(NamedTuple):
    keys: tuple[str, ...]
    #: When the read BEGAN. An unknown key id is judged only on a read that began after the key id was first met, since
    #: a list read earlier may predate the key that signed it.
    started: float
    #: When it ended: the age the TTL and the refresh interval are counted from.
    finished: float


class PublishedKeys:
    """What each listed signer publishes, read through the injected `fetch` and cached by what the store said.

    Successes only: a list that could not be read is never cached, so an outage is answered afresh on the next call
    and a half-seeded store cannot pin a missing list for a TTL. Reads of one identity are SINGLE-FLIGHT, each
    behind its own lock: concurrent callers share the read in progress rather than each asking the store.

    The locks and the cache are keyed only by identities the caller configured: the kit reads an identity only after
    checking it against the signer set, so they are bounded by that set. The one thing keyed by the event's own text,
    the key ids it names, is a bounded memory under each identity (`MAX_UNKNOWN_KEY_IDS`).

    `fetch` BLOCKS (a sidecar's secret API is a blocking HTTP call), so an async door verifies in a worker thread.
    """

    def __init__(self, fetch: Callable[[str], Mapping[str, str]], *, clock: Callable[[], float] = time.monotonic) -> None:
        self._fetch = fetch
        self._clock = clock
        self._reads: dict[str, _Read] = {}
        #: Per identity, the key ids met unknown, least recently met first, each with when it was FIRST met. Touched
        #: only with that identity's lock held.
        self._sightings: dict[str, OrderedDict[str, float]] = {}
        self._guard = threading.Lock()
        self._locks: dict[str, threading.Lock] = {}

    def for_event(self, kid: str | None) -> PublicKeySource:
        """The key source for ONE verification, stamped with the moment it began and the key id its event names.

        ``None`` is an event whose facet names no key id. The kit refuses it from the event alone and asks for no
        key, so a source made for it is never asked.
        """
        return _KeysForOneEvent(self, arrived=self._clock(), kid=kid)

    def cached(self, identity: str) -> Sequence[str]:
        """The identity's list from the cache while it is younger than the TTL, else from a fresh read."""
        with self._lock_for(identity):
            held = self._reads.get(identity)
            if held is not None and self._clock() - held.finished < KEY_TTL_SECONDS:
                return held.keys
            return self._read(identity).keys

    def read_for_unknown_key_id(self, identity: str, kid: str | None, *, arrived: float) -> Sequence[str]:
        """The identity's list as read AFTER ``kid`` was first met unknown for it, which is what an unknown key id is judged on.

        ``arrived`` is when THIS delivery reached the verifier. The key id's first sighting is the earliest arrival
        remembered for it, so a redelivery is judged on any read that began since the FIRST delivery, whichever event
        caused the read, and never waits for one to begin after the redelivery itself. Nothing is decided early by
        that: a key is published before it signs anything, so a read that began after an event naming the key id
        arrived lists the key unless it has since left the list, and a key that has left is refused by design.

        A read that began after the first sighting is returned as it is. A list read earlier cannot decide the key id,
        and reading again for every such event would hand a forger the store: while the last read ended less than
        `KEY_REFRESH_INTERVAL_SECONDS` ago that is declined with `KeySourceUnavailableError`, which the door answers
        RETRY, and past it the store is read afresh. A rate limit delays a verdict and never decides one.
        """
        with self._lock_for(identity):
            first_met = self._record_sighting(identity, kid, arrived)
            held = self._reads.get(identity)
            if held is not None and held.started >= first_met:
                return held.keys
            if held is not None and self._clock() - held.finished < KEY_REFRESH_INTERVAL_SECONDS:
                raise KeySourceUnavailableError(
                    f"no read of the public keys {identity!r} publishes has begun since this key id was first met, "
                    "and the last one ended moments ago; retry after the interval"
                )
            return self._read(identity).keys

    def _record_sighting(self, identity: str, kid: str | None, arrived: float) -> float:
        """Record that ``kid`` was met unknown for ``identity`` at ``arrived``, and return when it was FIRST met.

        Called with the identity's lock held. Meeting an id again keeps its earliest moment and makes it the most
        recently met, so the id pushed out at the bound is the one met least recently. An event naming no key id has
        nothing to remember, and its first moment is its own arrival.
        """
        if kid is None:
            return arrived
        met = self._sightings.get(identity)
        if met is None:
            met = self._sightings[identity] = OrderedDict()
        first = min(met.get(kid, arrived), arrived)
        met[kid] = first
        met.move_to_end(kid)
        if len(met) > MAX_UNKNOWN_KEY_IDS:
            met.popitem(last=False)
        return first

    def _lock_for(self, identity: str) -> threading.Lock:
        with self._guard:
            lock = self._locks.get(identity)
            if lock is None:
                lock = self._locks[identity] = threading.Lock()
            return lock

    def _read(self, identity: str) -> _Read:
        """One read through the injected fetch. Unreadable and absent are one answer: either way nothing is known about the signature."""
        started = self._clock()
        bundle = self._fetch(f"{_PUBLISHED_LIST_PREFIX}{identity}")
        keys = tuple(parse_published_keys(bundle.get(_KEYS_FIELD, "")))
        if not keys:
            raise KeySourceUnavailableError(f"the public keys {identity!r} publishes could not be read")
        read = _Read(keys=keys, started=started, finished=self._clock())
        self._reads[identity] = read
        return read


class _KeysForOneEvent:
    """`PublicKeySource` for one verification: `published` may answer from the cache, `refresh` only from a read since the key id was first met."""

    def __init__(self, keys: PublishedKeys, *, arrived: float, kid: str | None) -> None:
        self._keys = keys
        self._arrived = arrived
        self._kid = kid

    def published(self, identity: str) -> Sequence[str]:
        return self._keys.cached(identity)

    def refresh(self, identity: str) -> Sequence[str]:
        return self._keys.read_for_unknown_key_id(identity, self._kid, arrived=self._arrived)
