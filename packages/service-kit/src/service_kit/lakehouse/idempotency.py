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

STORE-ARBITRATED, NOT READ-THEN-WRITE. The claim is `records.create_json` — a conditional put whose
winner the STORE picks — for the same reason the registry mints ids that way: two attempts of one
retried request are precisely the concurrent case a check-then-write loses. Nothing here holds a lock
and nothing here is in-memory, so it works across replicas.
"""

from __future__ import annotations

import re
from typing import Any

from pydantic import BaseModel, ConfigDict

from service_kit.lakehouse.records import RecordChangedError, RecordExistsError, create_json, mutate_json, read_json, replace_json


#: Where the records live under the control root. A dedicated prefix rather than a suffix inside the
#: registry, because these expire and the registry records do not — a reclaim that can be scoped to one
#: prefix is a reclaim somebody will actually run.
PREFIX = "_idempotency"

#: A key is a caller-supplied token that becomes part of an object key, so it is a UUID in its canonical lower-case
#: form, which no spelling can turn into a path segment that climbs out of the prefix. Constrained HERE and not only
#: at the header: a second caller of this module must not have to remember, and the failure mode of forgetting is a
#: write outside the prefix.
_KEY_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
#: A scope is the module's caller's own token (a subject's digest), never the request's.
_SCOPE_RE = re.compile(r"^[a-z0-9]{1,64}$")

#: How long a claimed-but-unfinished attempt holds the key before another may take it. Long enough
#: that a slow door is not overtaken, short enough that a CRASHED one does not wedge a caller's key
#: forever — which would turn one process death into a permanently unusable key for that caller.
DEFAULT_LEASE_SECONDS = 300


#: A claim its own attempt gave back, because the attempt was refused before it wrote anything: the key is free.
RELEASED = "released"


class KeyReusedError(Exception):
    """The key is already bound to a DIFFERENT operation.

    Replaying here would hand the caller another endpoint's response body — a wrong answer rather than
    a slow one — so this is refused instead. The door maps it to a 400: the caller made a mistake and
    can fix it, unlike a conflict, which is about the world.
    """


class InFlightError(Exception):
    """A first attempt holds the key and has not finished.

    Neither available answer is safe: "no replay" lets the operation execute twice, and an empty replay
    answers with a result nobody produced. The door maps it to 409 with a `Retry-After`.
    """


class Replay(BaseModel):
    """What the first attempt answered, to be returned verbatim instead of executing again."""

    model_config = ConfigDict(frozen=True)

    status: int
    body: Any = None


def _record_key(scope: str, key: str) -> str:
    if not _KEY_RE.match(key):
        raise ValueError(f"invalid idempotency key {key!r}: must match {_KEY_RE.pattern}")
    if not _SCOPE_RE.match(scope):
        raise ValueError(f"invalid idempotency key scope {scope!r}: must match {_SCOPE_RE.pattern}")
    return f"{PREFIX}/{scope}/{key}.json"


def claim(
    root_uri: str,
    storage_options: dict[str, str],
    *,
    scope: str,
    key: str,
    endpoint: str,
    now: float,
    lease_seconds: int = DEFAULT_LEASE_SECONDS,
) -> Replay | None:
    """Claim `key` for `endpoint`. ``None`` means proceed; a :class:`Replay` means answer with it.

    `scope` separates callers — a key is the CALLER's, so one string from two tenants is two different
    operations and must not collide.
    """
    record_key = _record_key(scope, key)
    claimed = {"endpoint": endpoint, "state": "in_flight", "claimed_at": now}
    try:
        create_json(root_uri, storage_options, record_key, claimed)
    except RecordExistsError:
        pass
    else:
        return None

    existing = read_json(root_uri, storage_options, record_key)
    if existing is None:
        # Created and then removed between the two calls (a reclaim, or an expiry sweep). Racing it
        # again would be the same bet; taking the key is the honest answer, and the loser of the write
        # below is the one that finds a result waiting.
        return None
    record = existing[0]

    if record.get("endpoint") != endpoint:
        raise KeyReusedError(f"idempotency key {key!r} is already bound to {record.get('endpoint')!r}, not {endpoint!r}")

    if record.get("state") == "done":
        return Replay(status=int(record["status"]), body=record.get("body"))

    if record.get("state") != RELEASED and now - float(record.get("claimed_at") or 0) < lease_seconds:
        raise InFlightError(f"idempotency key {key!r} is held by an attempt that has not finished")

    # ABANDONED. Take it over, but do NOT delete-and-recreate: the original attempt may be slow rather
    # than dead, and whichever finishes writes the outcome. Re-stamping the lease is enough to stop a
    # third attempt piling in, and `record_outcome` is a merge, so the winner's result survives either
    # way.
    mutate_json(root_uri, storage_options, record_key, lambda r: {**r, "state": "in_flight", "claimed_at": now})
    return None


def release(root_uri: str, storage_options: dict[str, str], *, scope: str, key: str, claimed_at: float) -> None:
    """Give back the claim an attempt made at ``claimed_at``, for an attempt refused before it wrote anything.

    Its caller's corrected retry under the same key then runs at once instead of meeting :class:`InFlightError` for
    the rest of the lease. Only that claim is released: the record is replaced under the ETag it was read with, and
    left alone when another attempt has taken the key over or an outcome has been recorded since, so a release never
    frees a key another attempt holds. The record is kept, stamped ``released``, never deleted, as a location claim
    is released.
    """
    record_key = _record_key(scope, key)
    read = read_json(root_uri, storage_options, record_key)
    if read is None:
        return
    record, etag = read
    if record.get("state") != "in_flight" or record.get("claimed_at") != claimed_at:
        return
    try:
        replace_json(root_uri, storage_options, record_key, {**record, "state": RELEASED}, etag=etag)
    except RecordChangedError:
        return


def record_outcome(root_uri: str, storage_options: dict[str, str], *, scope: str, key: str, status: int, body: Any) -> None:  # noqa: ANN401 — the door's own response model
    """Store what this attempt answered, so a replay returns it rather than re-executing.

    A MERGE onto the claim rather than a fresh write: the record already carries the endpoint the key
    is bound to, and replacing it would drop the binding that makes :class:`KeyReusedError` possible.
    """
    mutate_json(
        root_uri,
        storage_options,
        _record_key(scope, key),
        lambda record: {**record, "state": "done", "status": int(status), "body": body},
    )


__all__ = ["DEFAULT_LEASE_SECONDS", "PREFIX", "RELEASED", "InFlightError", "KeyReusedError", "Replay", "claim", "record_outcome", "release"]
