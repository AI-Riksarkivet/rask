"""Which table id holds a dataset location — ``_locations/`` on the control root ([[LH-204]]).

THE ``dir`` BACKEND KEEPS NO LOCATION EXCLUSIVE. Its ``__manifest`` arbitrates only ADDs of an
``object_id`` (``lance_docs/ns_catalog/catalog/dir/index.md`` § Manifest Table Commits), so a second id
registered at a location another id holds commits, and two deregisters of one id both succeed. Measured
on pylance 12.0.0 (lh204 m1): eight barrier-threaded renames of one source, each retiring the source
before registering its destination, left eight live ids on one location in 4 of 4 rounds. With two ids
on one dataset a destructive drop of either deletes the bytes the other still resolves to.

THE STORE PICKS ONE HOLDER INSTEAD. A claim is one JSON record per location, keyed by the sha256 of the
path the location decodes to (:func:`service_kit.lakehouse.base_refs.decoded_path`, so the catalog's
percent-encoded spelling and the decoded directory are one key), created put-if-not-exists and handed
over under its ETag (:mod:`service_kit.lakehouse.records`) — the ``claim_bucket`` precedent applied to a
table location. Lakekeeper answers the same question inside its create transaction
(``crates/lakekeeper-storage-postgres/src/tabular/mod.rs``: a location overlapping another tabular's is
refused); this estate has no relational store, so the object store's conditional write is the
transaction.

A RELEASE IS A REPLACE UNDER THE ETAG IT READ, NEVER A DELETE ([[LH-458]]). Once a location's record exists,
every write to it is conditional on the record its writer judged: a release replaces its table's claim with
the same claim stamped ``released_at``, which :func:`holder` and :func:`take` read as a free location. A
hand-over landing between a release's read and its write fails the release's replace, and the release,
judging the claim it then reads, leaves the new holder's claim in place and answers that it released
nothing. The two conditional writes :mod:`service_kit.lakehouse.records` makes (create-if-absent and
replace-if-the-ETag-matches) are all the estate requires of its object store, and a conditional delete is
not one of them; an unconditional delete removes whatever claim was written after the read. So nothing
removes a released record, and a released location keeps one small one.

Who takes and releases: the catalog's register, undrop and rename doors take a claim, a recoverable drop
keeps it for the trashed id, and a destructive drop, a deregister and the maintenance purge release it.
A table made by ``create`` holds no claim — its location is a fresh ``<hash>_<object_id>`` directory —
and the register door's after-commit manifest check (``catalog.services.table_bases``) is what refuses a
second id there; the first door that attaches or moves it takes one.

A CLAIM WHOSE HOLDER IS GONE (a crash between a take and the attach, or a cascade drop, which destroys
its tables inside one native call) would wedge its location forever, so a taker may replace it once the
caller's ``is_gone`` predicate says the holder no longer resolves to the location. The predicate is the
caller's because only the catalog can describe a table; :data:`LEASE` bounds it from below so a holder
between its take and its attach — not yet in the manifest — is never mistaken for a gone one.

Every function is BLOCKING IO — callers threadpool it.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

from pydantic import BaseModel, Field

from service_kit.lakehouse import records
from service_kit.lakehouse.base_refs import decoded_path
from service_kit.lakehouse.objectfs import StorageOptions


_PREFIX = "_locations"

#: How long a claim protects a holder that does not (yet) resolve to its location. A take precedes the
#: attach by one backend call, milliseconds in practice; the lease is minutes so that a slow attach is
#: never overtaken, and short enough that a crashed one frees its location the same day.
LEASE = timedelta(minutes=15)

#: Rounds :func:`take` spends when the record disappears between its create and its read. No function here
#: deletes one, so only a delete from outside them does that; each step is arbitrated by the store, and the
#: bound turns a livelock into an error rather than a hang.
_TAKE_ATTEMPTS = 3


class ClaimStore(BaseModel):
    """Where the claims live: the control root and the credential that writes it."""

    control_root: str
    storage_options: StorageOptions = Field(default_factory=dict)


class LocationClaim(BaseModel):
    """One location's holder, or its last holder once released."""

    #: The decoded path the key is derived from.
    location: str
    #: The holder's canonical table id.
    table: str
    #: The holder's identifier segments, so a caller can describe it without knowing the delimiter.
    segments: list[str]
    claimed_at: datetime
    #: The request that took it, when the taker named one: only that request's retry is a retry.
    token: str | None = None
    #: When ``table`` released it. A released claim holds nothing: the location is free.
    released_at: datetime | None = None

    def within_lease(self, now: datetime) -> bool:
        return now - self.claimed_at < LEASE


class LocationHeldError(Exception):
    """The location is held by another table that still resolves to it."""

    def __init__(self, claim: LocationClaim) -> None:
        super().__init__(f"location {claim.location!r} is held by table {claim.table!r}")
        self.claim = claim


def claim_key(location: str) -> str:
    return f"{_PREFIX}/{hashlib.sha256(decoded_path(location).encode()).hexdigest()}.json"


def holder(store: ClaimStore, location: str) -> LocationClaim | None:
    """The claim on ``location``, or ``None`` when nobody holds it: no record, or a released claim.

    Raises:
        ValueError: The stored record is not a claim (pydantic's ``ValidationError`` is one).
    """
    found = records.read_json(store.control_root, store.storage_options, claim_key(location))
    if found is None:
        return None
    claim = LocationClaim.model_validate(found[0])
    return None if claim.released_at is not None else claim


def take(
    store: ClaimStore,
    location: str,
    table: str,
    segments: list[str],
    *,
    is_gone: Callable[[LocationClaim], bool],
    previous: str | None = None,
    token: str | None = None,
) -> None:
    """Make ``table`` the holder of ``location``.

    Succeeds when the location is unclaimed (no record, or a released claim), already held by ``table``
    (a retry: with a ``token``, only a claim stamped with that same token is one), held by ``previous``
    (a hand-over: the rename of ``previous`` to ``table``), or held past :data:`LEASE` by a holder
    ``is_gone`` says no longer resolves to the location. Every other holder refuses, so of two takers with
    the same ``previous`` exactly one wins: the second reads the first's claim. A rename passes a token
    because two renames of one source into the SAME destination name ``table`` alike, and without it
    the second would read the first's claim as its own retry and both would proceed.

    Raises:
        LocationHeldError: Another table holds the location.
        records.RecordChangedError: The claim kept changing under the hand-over.
    """
    key = claim_key(location)

    def _wanted() -> dict[str, object]:
        return LocationClaim(location=decoded_path(location), table=table, segments=list(segments), claimed_at=datetime.now(UTC), token=token).model_dump(
            mode="json"
        )

    def _hand_over(raw: dict[str, object]) -> dict[str, object]:
        # Re-judged on every round: `mutate_json` re-applies this to a claim another taker just wrote.
        current = LocationClaim.model_validate(raw)
        if current.released_at is not None:
            return _wanted()
        if current.table == table and (token is None or current.token == token):
            return raw
        if current.table != previous and (current.within_lease(datetime.now(UTC)) or not is_gone(current)):
            raise LocationHeldError(current)
        return _wanted()

    for _ in range(_TAKE_ATTEMPTS):
        try:
            records.create_json(store.control_root, store.storage_options, key, _wanted())
        except records.RecordExistsError:
            pass
        else:
            return
        try:
            records.mutate_json(store.control_root, store.storage_options, key, _hand_over)
        except records.RecordMissingError:
            continue  # removed between the create and the read; create again
        return
    raise records.RecordChangedError(f"the claim on {decoded_path(location)!r} kept appearing and disappearing across {_TAKE_ATTEMPTS} attempts")


class _NotHeld(Exception):
    """The claim a release read is not its table's to release."""


def release(store: ClaimStore, location: str, table: str) -> bool:
    """Release ``table``'s claim on ``location``; ``False`` when it held none.

    A claim held by another table is left alone: the release is the holder's, and a drop of an id that
    never held the location must not free it for a third. The claim is replaced by its released form
    only under the ETag it was read with, so a claim another taker wrote after that read is judged
    afresh, and left.

    Raises:
        records.RecordChangedError: The claim kept changing under the release.
    """

    def _released(raw: dict[str, object]) -> dict[str, object]:
        # Re-judged on every round, as the hand-over is: `mutate_json` re-applies this to a claim written since the read.
        current = LocationClaim.model_validate(raw)
        if current.released_at is not None or current.table != table:
            raise _NotHeld
        return current.model_copy(update={"released_at": datetime.now(UTC)}).model_dump(mode="json")

    try:
        records.mutate_json(store.control_root, store.storage_options, claim_key(location), _released)
    except (records.RecordMissingError, _NotHeld):
        return False
    return True
