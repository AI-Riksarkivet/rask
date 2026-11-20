"""The catalog's side of `Idempotency-Key`: an OPTIONAL header that makes a replay converge.

WHICH DOORS TAKE IT, THE RULE (XC-219). A door takes the key when a blind retry of its write would apply it AGAIN: the
doors that mint or retire an object (a table's create, declare, register, drop, deregister, rename, restore and undrop;
a namespace's create, drop and undrop; a warehouse's or a project's delete) and the four row writes (insert,
merge_insert, update, delete), since an append lands its rows again, `x = x + 1` adds again, and a merge's or a delete's
predicate takes rows another writer committed in between. No other door takes it, because a blind retry of one is
refused or converges rather than applying twice: a column's add, alter or drop, a tag's or a branch's create or delete
and `version/create` (which names the exact version it finalizes) are refused the second time, and a schema or field
metadata update, a tag's update and a project's create set the same state again. Every keyed door has the same
semantics: the first request under a key commits and records its answer under (caller, door, object, key) beside the
digest of the request (:func:`request_digest`); a replay under the key with the same request answers that and commits
nothing; the key on another door or another object, or sent with a different request, is refused `InvalidInput` (400,
code 13); a request with no key is served as it would be without this module. The claim is :meth:`KeyedDoor.begin`, the
row writes' is `endpoints.data._keyed_table_write`, and
`tests/integration/test_a_retried_create_does_not_rewrite_the_table.py` holds the roster.

WHAT IS THE SAME REQUEST. The door, its object and what the door binds besides (an insert's mode, a row write's ref) are
the key's binding; the request itself is judged by its digest: SHA-256 over the body's bytes exactly as received (an
Arrow IPC stream, a JSON document, or nothing) and the parameters that shape the write, every query parameter as sent and
the run the spec Context names (the ``x-rask-*`` run headers). The bytes are compared, not what they decode to: a client
that encodes the same rows to other Arrow bytes, or the same JSON with other spacing or key order, sends a different
request, and its resend under the first key is refused. A retry resends what it sent. A door that declares no body has
none read, and its digest is of its parameters alone.

WHY OPTIONAL, AND WHY THAT IS NOT A COMPROMISE (§ Q17-32, `rask-lance-catalog` § D3). The Lance
Namespace spec defines no `Idempotency-Key`, and a stock Lance client must work against this catalog
with no rask SDK — so requiring one would break conformance on 54 routed operations. A caller that
sends a key gets convergence; one that does not is exactly where it was, which is why each door declares the key
rather than a middleware imposing it over every write route.

WHAT IT BUYS, precisely. A 500 raised AFTER the Lance write in `create_governed_table` — the schema
read-back, `emit_create`, `emit_control` — is replayed by whoever retries it, with nothing to converge it.
With `mode=Create` the replay answers AlreadyExists, so a successful create surfaces as a 409 nobody can
tell from a name collision; with `mode=Overwrite` each replay RE-EXECUTES the destructive path. A key
closes those replays — a 500, a 503, a proxy timeout, a client retry.

THE SCOPE IS A HASH OF THE SUBJECT, NOT THE SUBJECT. A key belongs to its caller, so two tenants using
the string `retry-1` are two operations and must not collide. The subject itself cannot be a path
segment: measured on this estate, a Dex `sub` is a base64 protobuf blob (`CiQwOGE4...EgVsb2NhbA`),
which is neither bounded nor free of characters an object key would have to escape. A short digest is
stable, bounded and opaque — and being opaque is a property rather than a cost, since the record is
not an audit trail and the audit lane already carries the principal.
"""

from __future__ import annotations

import hashlib
import json
import logging
from collections.abc import AsyncIterator, Iterable, Mapping
from datetime import datetime
from typing import Annotated, Any
from uuid import uuid4

from fastapi import Depends, Header, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.routing import APIRoute
from lance_namespace import ConcurrentModificationError, InternalError, InvalidInputError, LanceNamespaceError
from pydantic import BaseModel, ConfigDict, PrivateAttr

from catalog.api.dependencies import SettingsDep, StorageOptionsDep
from catalog.api.security import Caller, CurrentToken
from catalog.core.run_context import INPUTS_HEADER, JOB_HEADER, JOB_NAMESPACE_HEADER, RUN_ID_HEADER
from catalog.services import idempotency_records
from lakehouse.problems.ns_errors import status_for
from lakehouse.storage.conditional_records import RecordChangedError
from lakehouse.storage.options import StorageOptions


#: The header: a UUID, in any case, because the key becomes part of an object key and a UUID is a segment no spelling
#: can turn into a path that leaves its prefix. Idempotency-Key is rask's own extension (the spec defines none), so its
#: grammar is the catalog's to choose. Anything else is refused 400 (code 13) on a spec route. `None` is the ordinary
#: case and means "no convergence asked for".
IdempotencyKeyHeader = Annotated[
    str | None,
    Header(
        alias="Idempotency-Key",
        description=(
            "Optional; a UUID. The same request resent under the key (the same body bytes and parameters) is answered with "
            "the first request's response and applies nothing; the key sent with a different request, or on another door "
            "or object, is refused 400 (code 13)."
        ),
        pattern=r"^[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12}$",
    ),
]


log = logging.getLogger(__name__)


#: The scope for a request that carries no verified principal — auth OFF, which the estate supports for
#: local bring-up. There is exactly one caller then, so one scope is the honest answer; the alternative,
#: refusing the key, would make the mechanism untestable on precisely the deployments used to try it.
_ANONYMOUS_SCOPE = "anonymous"


#: The request headers that shape a keyed write, so they belong to its digest: the run the spec Context names, which the
#: write stamps into its commit and announces. No other header does: the bearer, the key and the trace say who sent it and
#: how, not what it writes.
_DIGESTED_HEADERS: tuple[str, ...] = (RUN_ID_HEADER, JOB_NAMESPACE_HEADER, JOB_HEADER, INPUTS_HEADER)


def request_digest(body: bytes, *, query: Iterable[tuple[str, str]], headers: Mapping[str, str]) -> str:
    """The SHA-256 a key's record keeps of the request it was first sent with, as lower-case hex.

    Over a canonical prefix, then a newline, then ``body`` as received. The prefix is the compact JSON array of
    ``["query", name, value]`` for every query parameter as sent (a repeated one once per value) and
    ``["header", name, value]`` for each of :data:`_DIGESTED_HEADERS` present, sorted; a JSON array escapes every
    newline, so the first one ends it and no body can pose as parameters. The parts are fed to one hash in turn, so a body
    up to the cap is hashed where it lies and never copied.
    """
    pairs = sorted([["query", name, value] for name, value in query] + [["header", name, headers[name]] for name in _DIGESTED_HEADERS if name in headers])
    digest = hashlib.sha256(json.dumps(pairs, separators=(",", ":"), ensure_ascii=False).encode("utf-8"))
    digest.update(b"\n")
    digest.update(body)
    return digest.hexdigest()


def _scope(token: Caller | None) -> str:
    if token is None:
        return _ANONYMOUS_SCOPE
    return hashlib.blake2s(token.subject.encode("utf-8"), digest_size=8).hexdigest()


class Converge(BaseModel):
    """A claimed key, or the no-op a keyless caller gets.

    `replay` is the first attempt's answer when there is one. `remember` stores this attempt's answer, and
    `release_if_refused` gives the claim back for a refusal; both are no-ops without a key, so a door calls them
    unconditionally and reads the same on both paths.
    """

    model_config = ConfigDict(frozen=True)

    registry_root: str
    storage_options: StorageOptions
    scope: str
    key: str | None
    #: This request's attempt at the key: the claim it gives back is only its own.
    attempt: str = ""
    replay: idempotency_records.Replay | None = None
    #: Set when this attempt took the key over from one that lapsed unanswered: when that attempt claimed it, on the
    #: store's clock. A door that must not run twice looks for that attempt's write first.
    taken_over_since: datetime | None = None

    @property
    def mark(self) -> str | None:
        """What a door stamps on its commit under this key, so a takeover can find the commit (``None`` without a key)."""
        return None if self.key is None else f"{self.scope}/{self.key}"

    async def release_if_refused(self, exc: BaseException) -> None:
        """Give the claim back when ``exc`` is a refusal (a 4xx of the spec's taxonomy), which the keyed doors raise
        before any write: the caller's corrected retry under the key then runs rather than meeting 409 for the
        lease ([[LH-487]]). A fault (5xx), or anything else, keeps the lease, since the write may have landed.
        """
        if self.key is None or not isinstance(exc, LanceNamespaceError) or status_for(int(exc.code)) >= 500:
            return
        await run_in_threadpool(
            idempotency_records.release,
            self.registry_root,
            self.storage_options,
            scope=self.scope,
            key=self.key,
            attempt=self.attempt,
        )

    async def remember(self, status: int, response: Any) -> None:  # noqa: ANN401 — the door's own response model
        """Store what this attempt answered. A no-op without a key, so a door calls it unconditionally.

        SERIALISES INSIDE THE GUARD, and that is not only a saved cycle: the keyless path is every
        existing caller and every unit test that drives a handler with a stand-in response object, so
        a `model_dump` outside the guard would make the seam's presence change behaviour for callers
        who never asked for it — which is exactly what a door-level opt-in must not do.
        """
        if self.key is None:
            return
        body = response.model_dump(mode="json", exclude_none=True) if hasattr(response, "model_dump") else response
        await run_in_threadpool(
            idempotency_records.record_outcome,
            self.registry_root,
            self.storage_options,
            scope=self.scope,
            key=self.key,
            status=status,
            body=body,
        )


async def begin(registry_root: str, storage_options: StorageOptions, scope: str, key: str, *, binding: idempotency_records.KeyBinding, digest: str) -> Converge:
    """Claim `key` for the operation `binding` names and the request `digest` names.

    The failures are translated to the spec's own vocabulary rather than a hand-picked status, per
    `rask-lance-catalog`: a key bound to another door or another object, or sent with another request, is the CALLER's
    mistake and correctable, so `InvalidInput` (400); an attempt still running is a conflict about the world, so `ConcurrentModification` (409),
    which is what a retrying client should see; a record that is there and cannot be read as one is `Internal` (500,
    code 18), which only writing it again clears.
    """
    # One key, however the caller cases its hex digits.
    key = key.lower()
    attempt = uuid4().hex
    try:
        claimed = await run_in_threadpool(
            idempotency_records.claim,
            registry_root,
            storage_options,
            scope=scope,
            key=key,
            binding=binding,
            digest=digest,
            attempt=attempt,
        )
    except idempotency_records.KeyReusedError as exc:
        raise InvalidInputError(str(exc)) from exc
    except (idempotency_records.InFlightError, RecordChangedError) as exc:
        raise ConcurrentModificationError(str(exc)) from exc
    except idempotency_records.KeyRecordUnreadableError as exc:
        raise InternalError(str(exc)) from exc
    replay = claimed if isinstance(claimed, idempotency_records.Replay) else None
    taken_over_since = claimed.since if isinstance(claimed, idempotency_records.TakenOver) else None
    return Converge(
        registry_root=registry_root, storage_options=storage_options, scope=scope, key=key, attempt=attempt, replay=replay, taken_over_since=taken_over_since
    )


class KeyedDoor(BaseModel):
    """A keyed door's request: the key it was sent, and the claim :meth:`begin` makes for it.

    Built per request by :func:`keyed_door`, which gives the claim back when the door is refused (:meth:`Converge.release_if_refused`).
    """

    #: Where the claims are kept: the control root.
    registry_root: str
    storage_options: StorageOptions
    #: The caller the key belongs to (:func:`_scope`).
    scope: str
    key: str | None
    #: The request's query parameters as sent, and its :data:`_DIGESTED_HEADERS`: with the body, what
    #: :func:`request_digest` judges.
    query: tuple[tuple[str, str], ...] = ()
    context: dict[str, str] = {}
    converge: Converge | None = None
    #: The body the door declares, as FastAPI read it; empty for a door that declares none. Private, so the model holds
    #: the bytes it is handed rather than a validated copy.
    _body: bytes = PrivateAttr(default=b"")

    def hold(self, body: bytes, *, query: tuple[tuple[str, str], ...], context: dict[str, str]) -> None:
        """Keep what :meth:`begin` digests: the door's body, its query parameters and its run headers."""
        self._body = body
        self.query = query
        self.context = context

    async def begin(self, *, endpoint: str, target: str) -> Converge:
        """Claim the request's key for ``endpoint`` acting on ``target`` (:func:`begin`), after the door's shape refusals.

        ``target`` is the canonical id of the object the door acts on (``fga_ids.canonical_object_id``), so the same key on
        another object is refused rather than answered with this one's response ([[LH-576]]); the same key sent with
        another request is refused too. The request is digested here, so only a caller every door's authorization and
        shape refusal admitted has its body hashed, off the event loop, as the claim's own store round trips are.
        """
        if self.key is None:
            self.converge = Converge(registry_root=self.registry_root, storage_options=self.storage_options, scope=self.scope, key=None)
            return self.converge
        digest = await run_in_threadpool(request_digest, self._body, query=self.query, headers=self.context)
        binding = idempotency_records.KeyBinding(endpoint=endpoint, target=target)
        self.converge = await begin(self.registry_root, self.storage_options, self.scope, self.key, binding=binding, digest=digest)
        return self.converge


async def keyed_door(
    request: Request, settings: SettingsDep, so: StorageOptionsDep, token: CurrentToken, idempotency_key: IdempotencyKeyHeader = None
) -> AsyncIterator[KeyedDoor]:
    """The door's :class:`KeyedDoor`; a refusal the door raises after its claim gives the claim back ([[LH-487]]).

    A dependency with ``yield`` is handed the endpoint's exception at the ``yield`` (FastAPI 0.140.13), before the
    problem handlers answer it, so every keyed door releases through this one place. What the digest judges is gathered
    here and hashed at :meth:`KeyedDoor.begin`. FastAPI reads the body a door declares before it solves the door's
    dependencies, and ``Request.body`` keeps what it read, so the door's own bytes are taken without reading the stream
    again; a door that declares no body has none read, so its digest is of its parameters alone.
    """
    door = KeyedDoor(registry_root=settings.registry_root, storage_options=so, scope=_scope(token), key=idempotency_key)
    if idempotency_key is not None:
        route = request.scope.get("route")
        door.hold(
            await request.body() if isinstance(route, APIRoute) and route.body_field is not None else b"",
            query=tuple(request.query_params.multi_items()),
            context={name: request.headers[name] for name in _DIGESTED_HEADERS if name in request.headers},
        )
    try:
        yield door
    except Exception as exc:
        if door.converge is not None:
            # The door's own refusal is what the caller is told; a release the store fails leaves the claim to its lease.
            try:
                await door.converge.release_if_refused(exc)
            except Exception as release_exc:  # noqa: BLE001 — never answered over the door's refusal
                log.warning("idempotency_release_failed", extra={"key": door.key, "error": str(release_exc)[:300]})
        raise


KeyedDoorDep = Annotated[KeyedDoor, Depends(keyed_door)]


__all__ = ["Converge", "IdempotencyKeyHeader", "KeyedDoor", "KeyedDoorDep", "begin", "request_digest"]
