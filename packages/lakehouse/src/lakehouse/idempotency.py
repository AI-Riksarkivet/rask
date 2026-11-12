"""Converge a REPLAYED write onto the caller's key instead of executing it twice.

WHY THIS EXISTS (§ Q17-32). A write door's failure is replayed by whoever sent it — a client's retry,
a pipeline's re-run, a request the Gateway's data plane timed out. No Dapr policy replays it, because
no service invokes the catalog through Dapr. And `python-infrastructure`'s rule
offers exactly two outs: *"Always pair retry with an idempotency key OR mark the operation
non-retryable"*. A replay the estate does not send is not one it can mark, so this is the first: a
key makes a replay — a 503, a proxy timeout, a client retry — converge rather than
re-execute.

THE KEY IS OPTIONAL AT THE DOOR, AND THAT IS A SPEC CONSTRAINT, NOT A COMPROMISE. The Lance Namespace
spec defines no `Idempotency-Key` and a stock Lance client must work with no rask SDK, so requiring it
would break conformance on 54 routed operations. A caller that sends one gets convergence; one that
does not is exactly where it was, which is why this module is a seam a door OPTS INTO rather than
middleware.

STORE-ARBITRATED, NOT READ-THEN-WRITE. A first claim is `records.create_json`, a conditional put whose winner the
STORE picks, and the takeover of an abandoned claim is a replace under the ETag of the read that judged it abandoned
([[LH-576]]): of two retries arriving after the lease, one takes the key and the other's write is refused, re-reads,
and finds the key held. Nothing here holds a lock and nothing here is in-memory, so it works across replicas.

A KEY IS ONE OPERATION ON ONE OBJECT (:class:`KeyBinding`): its door and the canonical id of the object the door acts
on. The same key on another door, or on the same door for another object, is the caller's mistake, refused rather than
answered with an operation that did not touch the object named.

A LEASE IS TIMED ON THE STORE'S CLOCK (ADR 0179): a claim is stamped with the clock of the read it follows and judged
against the clock of the read that finds it, so two pods' clocks are never compared.

A RECORD LIVES A LIFETIME PAST ITS LAST WRITE (:func:`reclaim`), judged on the age the store stamped on it. A reclaim
first replaces an expired record, under the ETag it read, with a mark no claim takes over, and only then deletes it, so
it never deletes a key a retry took over meanwhile: the store offers no conditional delete.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, JsonValue, TypeAdapter, ValidationError

from lakehouse import control_prefixes, records, stored_objects
from lakehouse.storage_options import StorageOptions


log = logging.getLogger(__name__)

#: Where the records live under the control root. A dedicated prefix rather than a suffix inside the
#: registry, because these expire and the registry records do not, and a reclaim lists this prefix alone.
PREFIX = control_prefixes.IDEMPOTENCY

#: A key is a caller-supplied token that becomes part of an object key, so it is a UUID in its canonical lower-case
#: form, which no spelling can turn into a path segment that climbs out of the prefix. Constrained HERE and not only
#: at the header: a second caller of this module must not have to remember, and the failure mode of forgetting is a
#: write outside the prefix.
_KEY_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
#: A scope is the module's caller's own token (a subject's digest), never the request's.
_SCOPE_RE = re.compile(r"^[a-z0-9]{1,64}$")
#: A listed object's ``<scope>/<key>`` beneath :data:`PREFIX`, each then held to its own rule (:func:`_record_key`).
_LISTED_RE = re.compile(rf"/{re.escape(PREFIX)}/([^/]+)/([^/]+)\.json$")

#: How long a claimed-but-unfinished attempt holds the key before another may take it. Long enough
#: that a slow door is not overtaken, short enough that a CRASHED one does not wedge a caller's key
#: forever — which would turn one process death into a permanently unusable key for that caller.
LEASE = timedelta(seconds=300)

#: Read-and-write rounds a claim or an answer spends against other attempts of one key: each round is arbitrated by
#: the store, and the bound turns a livelock into an error.
_ROUNDS = 8


class KeyBinding(BaseModel):
    """What a key is bound to: one door, acting on one object."""

    model_config = ConfigDict(frozen=True)

    #: The door's route, e.g. ``POST /v1/table/{id}/drop``.
    endpoint: str
    #: The canonical id of the object the door acts on; a door that names a second object (a rename's destination)
    #: binds both.
    target: str


class _Claimed(BaseModel):
    """A key an attempt holds (``in_flight``), or gave back unanswered (``released``)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    state: Literal["in_flight", "released"]
    binding: KeyBinding
    #: The attempt that last claimed the key: only it may give the key back.
    attempt: str
    #: When that attempt claimed it, on the store's clock.
    claimed_at: datetime


class _Answered(BaseModel):
    """A key whose operation answered: a replay returns the answer."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    state: Literal["done"]
    binding: KeyBinding
    status: int
    body: JsonValue = None


class _Reclaiming(BaseModel):
    """The mark a reclaim writes over an expired record before deleting it: no claim takes a key over while it stands."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    state: Literal["reclaiming"]
    at: datetime


_STORED: TypeAdapter[_Claimed | _Answered | _Reclaiming] = TypeAdapter(Annotated[_Claimed | _Answered | _Reclaiming, Field(discriminator="state")])


class KeyReusedError(Exception):
    """The key is already bound to a DIFFERENT operation, or to the same door acting on another object.

    Replaying here would hand the caller another operation's response body — a wrong answer rather than
    a slow one — so this is refused instead. The door maps it to a 400: the caller made a mistake and
    can fix it, unlike a conflict, which is about the world.
    """


class InFlightError(Exception):
    """A first attempt holds the key and has not finished, or a reclaim is removing the key.

    Neither available answer is safe: "no replay" lets the operation execute twice, and an empty replay
    answers with a result nobody produced. The door maps it to 409 with a `Retry-After`.
    """


class KeyRecordUnreadableError(Exception):
    """The key's record is there and cannot be read as one, so neither a replay nor a new attempt can be judged."""


class Replay(BaseModel):
    """What the first attempt answered, to be returned verbatim instead of executing again."""

    model_config = ConfigDict(frozen=True)

    status: int
    body: JsonValue = None


class Reclaimed(BaseModel):
    """What one reclaim did."""

    #: Records deleted.
    reclaimed: int = 0
    #: Expired records written again between the listing and their reclaim, kept for another lifetime.
    kept: int = 0
    #: Expired records past this reclaim's limit, left for the next.
    left: int = 0
    #: Objects under the prefix that are no record this module writes, left alone.
    unreadable: int = 0


def _record_key(scope: str, key: str) -> str:
    if not _KEY_RE.match(key):
        raise ValueError(f"invalid idempotency key {key!r}: must match {_KEY_RE.pattern}")
    if not _SCOPE_RE.match(scope):
        raise ValueError(f"invalid idempotency key scope {scope!r}: must match {_SCOPE_RE.pattern}")
    return f"{PREFIX}/{scope}/{key}.json"


def _stored(found: records.Stored, key: str) -> _Claimed | _Answered | _Reclaiming:
    try:
        return _STORED.validate_python(found.record)
    except ValidationError as exc:
        raise KeyRecordUnreadableError(f"the record of idempotency key {key!r} cannot be read as one") from exc


def claim(
    root_uri: str,
    storage_options: StorageOptions,
    *,
    scope: str,
    key: str,
    binding: KeyBinding,
    attempt: str,
    lease: timedelta = LEASE,
) -> Replay | None:
    """Claim ``key`` for ``binding``'s operation as ``attempt``. ``None`` means proceed; a :class:`Replay` means answer with it.

    `scope` separates callers — a key is the CALLER's, so one string from two tenants is two different
    operations and must not collide. An attempt that held the key past ``lease`` without answering is taken over, and
    so is a key an attempt gave back; whichever attempt finishes records the answer.

    Args:
        root_uri: The control root the records live under.
        storage_options: How to reach it.
        scope: The caller the key belongs to.
        key: The caller's key, a canonical lower-case UUID.
        binding: The door and the object the operation acts on.
        attempt: This attempt's token, which only it gives back (:func:`release`).
        lease: How long an unanswered claim holds the key, on the store's clock.

    Returns:
        ``None`` when this attempt holds the key, or the answer an earlier attempt recorded.

    Raises:
        ValueError: The key or the scope is not one this module writes.
        KeyReusedError: The key is bound to another door or another object.
        InFlightError: Another attempt holds the key, or a reclaim is removing it.
        KeyRecordUnreadableError: The key's record cannot be read as one.
        records.RecordChangedError: The record kept changing across every round.
    """
    record_key = _record_key(scope, key)
    for _ in range(_ROUNDS):
        found = records.read_json_at(root_uri, storage_options, record_key)
        stored = found.stored
        claimed = _Claimed(state="in_flight", binding=binding, attempt=attempt, claimed_at=found.at).model_dump(mode="json")
        if stored is None:
            try:
                records.create_json(root_uri, storage_options, record_key, claimed)
            except records.RecordExistsError:
                continue
            return None
        current = _stored(stored, key)
        if isinstance(current, _Reclaiming):
            raise InFlightError(f"idempotency key {key!r} outlived its lifetime and is being reclaimed: retry, or send a new key")
        if current.binding != binding:
            bound = current.binding
            raise KeyReusedError(f"idempotency key {key!r} is bound to {bound.endpoint} on {bound.target}, not {binding.endpoint} on {binding.target}")
        if isinstance(current, _Answered):
            return Replay(status=current.status, body=current.body)
        if current.state == "in_flight" and found.at - current.claimed_at < lease:
            raise InFlightError(f"idempotency key {key!r} is held by an attempt that has not finished")
        try:
            records.replace_json(root_uri, storage_options, record_key, claimed, etag=stored.etag)
        except records.RecordChangedError:
            continue
        return None
    raise records.RecordChangedError(f"the record of idempotency key {key!r} kept changing across {_ROUNDS} rounds")


def release(root_uri: str, storage_options: StorageOptions, *, scope: str, key: str, attempt: str) -> None:
    """Give back the claim ``attempt`` holds, for an attempt refused before it wrote anything.

    Its caller's corrected retry under the same key then runs at once instead of meeting :class:`InFlightError` for
    the rest of the lease. Only that claim is released: the record is replaced under the ETag it was read with, and
    left alone when another attempt has taken the key over or an outcome has been recorded since, so a release never
    frees a key another attempt holds. The record is kept, stamped ``released``, never deleted, as a location claim
    is released.
    """
    record_key = _record_key(scope, key)
    stored = records.read_json_at(root_uri, storage_options, record_key).stored
    if stored is None:
        return
    current = _stored(stored, key)
    if not isinstance(current, _Claimed) or current.state != "in_flight" or current.attempt != attempt:
        return
    try:
        records.replace_json(root_uri, storage_options, record_key, current.model_copy(update={"state": "released"}).model_dump(mode="json"), etag=stored.etag)
    except records.RecordChangedError:
        return


def record_outcome(root_uri: str, storage_options: StorageOptions, *, scope: str, key: str, status: int, body: JsonValue) -> None:
    """Store what this attempt answered, so a replay returns it rather than re-executing.

    Written onto the key's binding, under the ETag of the read it follows. A record no longer there, or one a reclaim
    is removing, is logged and left: the attempt's write landed, and failing its answer now would only send the caller
    back to run it again.

    Raises:
        KeyRecordUnreadableError: The key's record cannot be read as one.
        records.RecordChangedError: The record kept changing across every round.
    """
    record_key = _record_key(scope, key)
    for _ in range(_ROUNDS):
        stored = records.read_json_at(root_uri, storage_options, record_key).stored
        if stored is None:
            log.error("idempotency_outcome_unrecorded", extra={"key": key, "status": status, "reason": "the key's record is gone"})
            return
        current = _stored(stored, key)
        if isinstance(current, _Reclaiming):
            log.error("idempotency_outcome_unrecorded", extra={"key": key, "status": status, "reason": "the key's record is being reclaimed"})
            return
        answered = _Answered(state="done", binding=current.binding, status=status, body=body).model_dump(mode="json")
        try:
            records.replace_json(root_uri, storage_options, record_key, answered, etag=stored.etag)
        except records.RecordChangedError:
            continue
        return
    raise records.RecordChangedError(f"the record of idempotency key {key!r} kept changing across {_ROUNDS} rounds")


def reclaim(root_uri: str, storage_options: StorageOptions, *, lifetime: timedelta, limit: int) -> Reclaimed:
    """Delete the records nothing has written for ``lifetime``, at most ``limit`` of them, oldest first.

    Each record's age is the store's clock less the time the store stamped on its last write, read with its ETag in one
    answer. An expired record is replaced, under that ETag, with a mark no claim takes over and only then deleted, so a
    retry that took its key over after the read keeps it: its write moved the ETag, and the replace is refused.

    Args:
        root_uri: The control root the records live under.
        storage_options: How to reach it.
        lifetime: How long a record outlives its last write.
        limit: The most records one reclaim removes.

    Returns:
        What it reclaimed, kept and left.
    """
    listing = stored_objects.list_under_at(f"{root_uri.rstrip('/')}/{PREFIX}", storage_options)
    expired = sorted((listed for listed in listing.objects if listing.at - listed.modified >= lifetime), key=lambda listed: listed.modified)
    done = Reclaimed(left=max(len(expired) - limit, 0))
    for listed in expired[:limit]:
        match _reclaim_listed(root_uri, storage_options, listed.path, lifetime=lifetime):
            case "reclaimed":
                done.reclaimed += 1
            case "kept":
                done.kept += 1
            case "unreadable":
                done.unreadable += 1
    return done


def _listed_record_key(path: str) -> str | None:
    """The record key of the object a listing found at ``path``; ``None`` for a name no key and scope spell."""
    named = _LISTED_RE.search(path)
    if named is None or not _SCOPE_RE.match(named.group(1)) or not _KEY_RE.match(named.group(2)):
        return None
    return f"{PREFIX}/{named.group(1)}/{named.group(2)}.json"


def _reclaim_listed(root_uri: str, storage_options: StorageOptions, path: str, *, lifetime: timedelta) -> Literal["reclaimed", "kept", "unreadable"]:
    """Reclaim the record a listing found at ``path`` if it is still expired when read: its mark replaces it under the
    ETag of that read, then it goes."""
    record_key = _listed_record_key(path)
    if record_key is None:
        log.warning("idempotency_record_unreadable", extra={"path": path, "reason": "its name is no key's"})
        return "unreadable"
    try:
        found = records.read_json_at(root_uri, storage_options, record_key)
    except ValueError:
        log.warning("idempotency_record_unreadable", extra={"path": path, "reason": "its bytes are no JSON"})
        return "unreadable"
    stored = found.stored
    if stored is None or found.at - stored.modified < lifetime:
        return "kept"
    mark = _Reclaiming(state="reclaiming", at=found.at).model_dump(mode="json")
    try:
        records.replace_json(root_uri, storage_options, record_key, mark, etag=stored.etag)
    except records.RecordChangedError:
        return "kept"
    stored_objects.delete_object(root_uri, storage_options, record_key)
    return "reclaimed"


__all__ = [
    "LEASE",
    "PREFIX",
    "InFlightError",
    "KeyBinding",
    "KeyRecordUnreadableError",
    "KeyReusedError",
    "Reclaimed",
    "Replay",
    "claim",
    "reclaim",
    "record_outcome",
    "release",
]
