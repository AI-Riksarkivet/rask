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

Who takes and releases: the catalog's create, declare, register, undrop and rename doors take a claim, a
recoverable drop keeps it for the trashed id, and a destructive drop, a deregister and the maintenance purge
release it.

A CLAIM CARRIES ITS TABLE'S UUID (ADR 0173 §6). The claim's life is a uuid's: a rename hands it to the new name,
a recoverable drop keeps it for the undrop, and a destroy or a deregister releases it, so a table registered at
the location afterwards is given a uuid of its own. The uuid lives here, outside Lance context, because
RegisterTable keeps no properties (measured on pylance 13.0.0) and the rename and both undrops re-register. A
data directory's claim ([[LH-252]]) names its holder and records no uuid.

A DOOR ACTS ON THE CLAIM IT READ (ADR 0173 §7, [[LH-521]]). A drop, a deregister and a rename change which table
``__manifest`` names at a location, by name, so each reads the claim once when its request reaches it and writes
only while that claim is unchanged, under the ETag it read (:func:`read`, :func:`fence`, :func:`hand_over`). A drop
or a deregister FENCES the claim before its backend call: while the fence stands, no take hands the location to
another name, so a rename landing between the door's read and its act cannot make the door act on another table.
The door lifts its fence once it has acted (:func:`lift_fence`); a fence its request never lifted lapses after
:data:`LEASE`.

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


class ClaimFence(BaseModel):
    """A request acting on the claim's table: while it stands, no other request takes or fences the claim."""

    #: The request that fenced it.
    token: str
    at: datetime


class LocationClaim(BaseModel):
    """One location's holder, or its last holder once released."""

    #: The decoded path the key is derived from.
    location: str
    #: The holder's canonical table id.
    table: str
    #: The holder's identifier segments, so a caller can describe it without knowing the delimiter.
    segments: list[str]
    #: The holder table's uuid, which the catalog minted when the table came into being; a data directory's claim has none.
    uuid: str | None = None
    claimed_at: datetime
    #: The request that took it, when the taker named one: only that request's retry is a retry.
    token: str | None = None
    #: When ``table`` released it. A released claim holds nothing: the location is free.
    released_at: datetime | None = None
    #: The request acting on ``table`` now, when one fenced it.
    fence: ClaimFence | None = None

    def within_lease(self, now: datetime) -> bool:
        return now - self.claimed_at < LEASE

    def fenced_against(self, token: str | None, now: datetime) -> bool:
        """Whether a request other than ``token`` is acting on the held table."""
        return self.released_at is None and self.fence is not None and self.fence.token != token and now - self.fence.at < LEASE


class ClaimRead(BaseModel):
    """A location's claim as one read found it, released or held, with the ETag a write acting on it must still match."""

    claim: LocationClaim
    etag: str


class LocationHeldError(Exception):
    """The location is held by another table that still resolves to it, or another request is acting on its holder."""

    def __init__(self, claim: LocationClaim) -> None:
        super().__init__(f"location {claim.location!r} is held by table {claim.table!r}")
        self.claim = claim


class ClaimChangedError(Exception):
    """The claim is no longer the one the request read, or another request is acting on its table."""


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


def read(store: ClaimStore, location: str) -> ClaimRead | None:
    """The record on ``location`` with its ETag, released or held, or ``None`` when no record exists.

    Raises:
        ValueError: The stored record is not a claim (pydantic's ``ValidationError`` is one).
    """
    found = records.read_json(store.control_root, store.storage_options, claim_key(location))
    return None if found is None else ClaimRead(claim=LocationClaim.model_validate(found[0]), etag=found[1])


def _taken(
    current: LocationClaim,
    location: str,
    table: str,
    segments: list[str],
    *,
    is_gone: Callable[[LocationClaim], bool],
    previous: str | None,
    token: str | None,
    uuid: str | None,
) -> LocationClaim:
    """The claim ``table`` holds once it takes ``location`` from ``current``; :func:`take` says when a take succeeds.

    Raises:
        LocationHeldError: Another table holds the location, or another request is acting on its holder.
    """
    now = datetime.now(UTC)

    def _claim(carried: str | None) -> LocationClaim:
        return LocationClaim(location=decoded_path(location), table=table, segments=list(segments), uuid=carried, claimed_at=now, token=token)

    if current.released_at is not None:
        return _claim(uuid)
    if current.fenced_against(token, now):
        raise LocationHeldError(current)
    if current.table == table and uuid is not None:
        return _claim(uuid)
    if current.table == table and (token is None or current.token == token):
        return current
    if current.table == previous:
        return _claim(current.uuid)
    if current.within_lease(now) or not is_gone(current):
        raise LocationHeldError(current)
    return _claim(current.uuid if uuid is None else uuid)


def take(
    store: ClaimStore,
    location: str,
    table: str,
    segments: list[str],
    *,
    is_gone: Callable[[LocationClaim], bool],
    previous: str | None = None,
    token: str | None = None,
    uuid: str | None = None,
) -> LocationClaim:
    """Make ``table`` the holder of ``location``, and answer the claim it then holds.

    Succeeds when the location is unclaimed (no record, or a released claim), already held by ``table``
    (a retry: with a ``token``, only a claim stamped with that same token is one), held by ``previous``
    (a hand-over: the rename of ``previous`` to ``table``), or held past :data:`LEASE` by a holder
    ``is_gone`` says no longer resolves to the location. Every other holder refuses, so of two takers with
    the same ``previous`` exactly one wins: the second reads the first's claim. A rename passes a token
    because two renames of one source into the SAME destination name ``table`` alike, and without it
    the second would read the first's claim as its own retry and both would proceed. A claim another request has
    fenced refuses every take but that request's own, until the fence lapses.

    ``uuid`` is the uuid of a table this take brings into being, minted by its door after its backend call found
    ``table``'s name free. A claim the take writes on an unclaimed location records it, and so does one replacing a
    gone holder's or one ``table``'s own name still holds, which an ended table of that name left unreleased (a crash
    between its detach and its release): nothing granted to an earlier table at the location reaches this one. A
    table that already exists names none and keeps the uuid its location carries: a hand-over carries it to the new
    name, a retry keeps the whole claim, and a gone holder's claim was kept for this same table under a name it no
    longer has (a rename interrupted between its hand-over and its deregister).

    Raises:
        LocationHeldError: Another table holds the location.
        records.RecordChangedError: The claim kept changing under the hand-over.
    """
    key = claim_key(location)

    def _hand_over(raw: dict[str, object]) -> dict[str, object]:
        # Re-judged on every round: `mutate_json` re-applies this to a claim another taker just wrote.
        current = LocationClaim.model_validate(raw)
        held = _taken(current, location, table, segments, is_gone=is_gone, previous=previous, token=token, uuid=uuid)
        return raw if held is current else held.model_dump(mode="json")

    for _ in range(_TAKE_ATTEMPTS):
        fresh = LocationClaim(
            location=decoded_path(location), table=table, segments=list(segments), uuid=uuid, claimed_at=datetime.now(UTC), token=token
        ).model_dump(mode="json")
        try:
            records.create_json(store.control_root, store.storage_options, key, fresh)
        except records.RecordExistsError:
            pass
        else:
            return LocationClaim.model_validate(fresh)
        try:
            held = records.mutate_json(store.control_root, store.storage_options, key, _hand_over)
        except records.RecordMissingError:
            continue  # removed between the create and the read; create again
        return LocationClaim.model_validate(held)
    raise records.RecordChangedError(f"the claim on {decoded_path(location)!r} kept appearing and disappearing across {_TAKE_ATTEMPTS} attempts")


def _write_over(store: ClaimStore, location: str, read_as: ClaimRead | None, claim: LocationClaim) -> None:
    """Write ``claim`` on ``location`` only while its record is still what ``read_as`` found: absent, or that ETag.

    Raises:
        ClaimChangedError: The record changed since that read.
    """
    key, body = claim_key(location), claim.model_dump(mode="json")
    try:
        if read_as is None:
            records.create_json(store.control_root, store.storage_options, key, body)
        else:
            records.replace_json(store.control_root, store.storage_options, key, body, etag=read_as.etag)
    except (records.RecordExistsError, records.RecordChangedError):
        raise ClaimChangedError(f"the claim on {decoded_path(location)!r} changed since it was read") from None


def hand_over(
    store: ClaimStore,
    location: str,
    read_as: ClaimRead | None,
    *,
    table: str,
    segments: list[str],
    previous: str,
    token: str,
    is_gone: Callable[[LocationClaim], bool],
) -> LocationClaim:
    """Hand ``location`` from ``previous`` to ``table`` as :func:`take` would, judged on the claim ``read_as`` found.

    The rename's take: one conditional write over the record its request read, never a re-read, so of a rename and
    any other door acting on the same claim, the one that writes second answers that the claim changed.

    Raises:
        LocationHeldError: Another table holds the location, or another request is acting on its holder.
        ClaimChangedError: The claim changed since ``read_as``.
    """
    if read_as is None:
        held = LocationClaim(location=decoded_path(location), table=table, segments=list(segments), claimed_at=datetime.now(UTC), token=token)
    else:
        held = _taken(read_as.claim, location, table, segments, is_gone=is_gone, previous=previous, token=token, uuid=None)
    _write_over(store, location, read_as, held)
    return held


def fence(
    store: ClaimStore, location: str, read_as: ClaimRead | None, *, table: str, segments: list[str], token: str, is_gone: Callable[[LocationClaim], bool]
) -> None:
    """Fence ``table``'s claim on ``location`` for request ``token``, judged on the claim ``read_as`` found.

    ``table`` keeps the claim and its uuid, and the fence marks that request ``token`` is acting on it: a drop or a
    deregister fences before its backend call, so no rename hands the location to another name until the door has
    acted and lifts its fence. A claim the location does not yet carry (no record, or a released one) is taken for
    ``table``, and so is one whose holder is gone, as :func:`take` would.

    Raises:
        LocationHeldError: Another table holds the location.
        ClaimChangedError: The claim changed since ``read_as``, or another request is acting on ``table``.
    """
    now = datetime.now(UTC)
    mark = ClaimFence(token=token, at=now)
    current = read_as.claim if read_as is not None else None
    if current is not None and current.fenced_against(token, now):
        raise ClaimChangedError(f"another request is acting on table {current.table!r}")
    if current is None or current.released_at is not None:
        carried = current.uuid if current is not None and current.table == table else None
        fenced = LocationClaim(location=decoded_path(location), table=table, segments=list(segments), uuid=carried, claimed_at=now, fence=mark)
    elif current.table == table:
        fenced = current.model_copy(update={"fence": mark})
    elif current.within_lease(now) or not is_gone(current):
        raise LocationHeldError(current)
    else:
        fenced = LocationClaim(location=decoded_path(location), table=table, segments=list(segments), uuid=current.uuid, claimed_at=now, fence=mark)
    _write_over(store, location, read_as, fenced)


def lift_fence(store: ClaimStore, location: str, token: str) -> None:
    """Lift request ``token``'s fence from ``location``'s claim; a claim it no longer fences is left as it is.

    Raises:
        records.RecordChangedError: The claim kept changing under the lift.
    """

    def _lifted(raw: dict[str, object]) -> dict[str, object]:
        current = LocationClaim.model_validate(raw)
        if current.fence is None or current.fence.token != token:
            raise _NotHeld
        return current.model_copy(update={"fence": None}).model_dump(mode="json")

    try:
        records.mutate_json(store.control_root, store.storage_options, claim_key(location), _lifted)
    except (records.RecordMissingError, _NotHeld):
        return


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
