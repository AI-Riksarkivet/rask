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
row writes' is :meth:`KeyedDoor.begin_write` (`endpoints.data._keyed_table_write`), each binding the route that matched,
and `tests/integration/test_a_retried_create_does_not_rewrite_the_table.py` holds the roster.

AN ANSWER LOST AFTER THE CHANGE (`catalog.services.idempotency_records` § what a takeover judges). The four row writes
answer a takeover from the commit their key stamped (`dataplane.committed_under_key`). Every other door declares to
:meth:`KeyedDoor.begin` how a resend takes over from an attempt lost unanswered (:data:`Takeover`), and none may leave
it out. A JUDGED door records what it intends with :meth:`Converge.intend` before it changes anything, and the takeover
is answered from that intent where the store shows its evidence, and runs where it does not or nothing was intended. A
RERUN door runs again against the state it finds: where the first run landed and nothing changed since, the second
refuses on that state (a create of a name that exists 409, a drop of a name that holds nothing 404); where another
request changed it in between (an undrop of the table a drop removed), the second applies again. A rerun is refused
instead where the name now names another object than the one the key was first claimed on, which it would act on. An UNKNOWN door's second run would apply
again, and no evidence tells whether the first landed, so its takeover, and an evidence read that fails, is refused
``ConcurrentModification`` (409, code 14), the outcome unknown, the claim kept: the caller reads the object and sends a
new key. A resend whose name holds nothing any more is authorized against the object the key was first claimed on
(:func:`captured_object`), so the replay of a drop or a rename reaches its door; after a drop that destroyed the table,
and after a deregister, its grants are gone, and that resend is refused as any request on a missing name is.

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
import re
from collections.abc import AsyncIterator, Awaitable, Callable, Iterable, Mapping, Sequence
from datetime import datetime
from functools import partial
from typing import Annotated, Literal
from uuid import uuid4

from fastapi import Depends, Header, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.routing import APIRoute
from lance_namespace import ConcurrentModificationError, InvalidInputError, LanceNamespaceError, ServiceUnavailableError
from pydantic import BaseModel, ConfigDict, JsonValue, PrivateAttr

from catalog.api.dependencies import SettingsDep, StorageOptionsDep
from catalog.api.security import Caller, CurrentToken
from catalog.core.config import Settings
from catalog.core.run_context import INPUTS_HEADER, JOB_HEADER, JOB_NAMESPACE_HEADER, RUN_ID_HEADER
from catalog.services import idempotency_records
from catalog.services.announcement_intents import Evidence
from lakehouse.problems.ns_errors import status_for
from lakehouse.storage.conditional_records import RecordChangedError
from lakehouse.storage.options import StorageOptions


#: The header: a UUID, in any case, because the key becomes part of an object key and a UUID is a segment no spelling
#: can turn into a path that leaves its prefix. Idempotency-Key is rask's own extension (the spec defines none), so its
#: grammar is the catalog's to choose. Anything else is refused 400 (code 13) on a spec route. `None` is the ordinary
#: case and means "no convergence asked for".
_KEY_PATTERN = r"^[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12}$"
IdempotencyKeyHeader = Annotated[
    str | None,
    Header(
        alias="Idempotency-Key",
        description=(
            "Optional; a UUID. The same request resent under the key (the same body bytes and parameters) is answered with "
            "the first request's response and applies nothing; the key sent with a different request, or on another door "
            "or object, is refused 400 (code 13). A resend after the first request was lost unanswered is answered with "
            "the first request's response where the catalog can prove that request landed (a recoverable drop, by its "
            "trash record, and an insert, merge_insert, update or delete, by the commit its key stamped); is "
            "refused 409 (code 14), its outcome unknown, on a door whose second run would apply again (a restore, a "
            "create with mode=Overwrite, a namespace's drop, a project's or a warehouse's delete) or where the name now "
            "names another object: read the object, then send a new key; and otherwise runs again against the state it "
            "finds, so it refuses where the first landed (409 for a name that exists, 404 for one that is gone) and "
            "applies again where another request undid it in between. A resend after a drop that destroyed "
            "the table, or after a deregister, is refused 403 like any request on a missing name, since the table's "
            "grants went with it."
        ),
        pattern=_KEY_PATTERN,
    ),
]

#: Whether the store shows one piece of a door's evidence (``catalog.api.announcing.Announcer.landed``).
type Judge = Callable[[Evidence], Awaitable[bool]]

#: How a door's resend takes over from an attempt lost unanswered: a :data:`Judge` of the evidence the door records with
#: :meth:`Converge.intend` (JUDGED), ``"rerun"`` for a door whose second run refuses on the state the first left, or
#: ``"unknown"`` for one whose second run would apply again and whose first leaves no evidence. Required of every door by :meth:`KeyedDoor.begin`.
type Takeover = Judge | Literal["rerun", "unknown"]


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
    return _ANONYMOUS_SCOPE if token is None else scope_of(token.subject)


def scope_of(subject: str) -> str:
    """The scope the keys of the caller ``subject`` live under."""
    return hashlib.blake2s(subject.encode("utf-8"), digest_size=8).hexdigest()


def door_endpoint(request: Request) -> str | None:
    """The route ``request`` matched, as a key is bound to it: ``POST /v1/table/{id}/drop``; ``None`` before routing."""
    route = request.scope.get("route")
    return f"{request.method} {route.path_format}" if isinstance(route, APIRoute) else None


async def _digest_of(request: Request) -> str:
    """:func:`request_digest` of ``request``: the body its route declares, its query and its run headers."""
    route = request.scope.get("route")
    body = await request.body() if isinstance(route, APIRoute) and route.body_field is not None else b""
    context = {name: request.headers[name] for name in _DIGESTED_HEADERS if name in request.headers}
    return await run_in_threadpool(request_digest, body, query=tuple(request.query_params.multi_items()), headers=context)


def capture(name: str, obj: str | None) -> idempotency_records.Captured | None:
    """What a door retiring or moving the object ``obj`` (``table:<uuid>``) its ``name`` resolved to keeps on its claim.

    ``obj`` comes from the request's own resolution of the name, the one its authorization asked on, never from the
    request; ``None`` when the name holds nothing, which a resend's claim finds already kept.
    """
    return None if obj is None else idempotency_records.Captured(name=name, object=obj)


async def captured_object(request: Request, settings: Settings, *, subject: str, name: str) -> str | None:
    """The object ``subject``'s ``Idempotency-Key`` on ``request`` was first claimed on for ``name``, for a request whose
    name holds nothing any more; ``None`` without a key, or for a record of another door, name or request.

    Read only by the router's authorization, and only when the name holds no object: the caller is then asked the same
    rung on that object, which the catalog captured at the claim after the first attempt's authorization admitted it, so
    a caller holds no more than it held. Every failure reads as ``None``, so the refusal stays the one any missing name
    gets.
    """
    key = request.headers.get("Idempotency-Key")
    endpoint = door_endpoint(request)
    if key is None or endpoint is None or re.match(_KEY_PATTERN, key) is None:
        return None
    try:
        captured = await run_in_threadpool(
            partial(
                idempotency_records.captured_for,
                settings.registry_root,
                settings.storage_options(),
                scope=scope_of(subject),
                key=key.lower(),
                endpoint=endpoint,
                name=name,
                digest=await _digest_of(request),
            )
        )
    except Exception as exc:  # noqa: BLE001 — a lookup that fails admits nothing, and the caller meets the refusal
        log.warning("idempotency_capture_unread", extra={"error": str(exc)[:300]})
        return None
    return None if captured is None else captured.object


class Converge(BaseModel):
    """A claimed key, or the no-op a keyless caller gets.

    `replay` is the first attempt's answer when there is one. `intend` records what this attempt is about to change,
    `remember` stores this attempt's answer, and `release_if_refused` gives the claim back for a refusal; each is a no-op
    without a key, so a door calls them unconditionally and reads the same on both paths.
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
    #: What that attempt recorded it was about to change, if it recorded anything.
    taken_over_intent: idempotency_records.Intent | None = None
    #: The object the key was first claimed on, if its door captured one.
    taken_over_captured: idempotency_records.Captured | None = None

    @property
    def mark(self) -> str | None:
        """What a door stamps on its commit under this key, so a takeover can find the commit (``None`` without a key)."""
        return None if self.key is None else f"{self.scope}/{self.key}"

    async def intend(self, evidence: Sequence[Evidence], answer: BaseModel) -> None:
        """Record, before a JUDGED door changes anything, the evidence its change leaves and the answer it then gives.

        One conditional write; a no-op without a key.

        Raises:
            ConcurrentModificationError: Another attempt took the key over since; this one changed nothing.
        """
        if self.key is None:
            return
        replay = idempotency_records.Replay(status=200, body=answer.model_dump(mode="json", exclude_none=True))
        intent = idempotency_records.Intent(evidence=list(evidence), answer=replay)
        try:
            await run_in_threadpool(
                partial(
                    idempotency_records.intend,
                    self.registry_root,
                    self.storage_options,
                    scope=self.scope,
                    key=self.key,
                    attempt=self.attempt,
                    intent=intent,
                )
            )
        except (idempotency_records.InFlightError, RecordChangedError) as exc:
            raise ConcurrentModificationError(str(exc)) from exc

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

    async def remember(self, status: int, response: BaseModel | JsonValue) -> None:
        """Store what this attempt answered. A no-op without a key, so a door calls it unconditionally.

        SERIALISES INSIDE THE GUARD, and that is not only a saved cycle: the keyless path is every
        existing caller and every unit test that drives a handler with a stand-in response object, so
        a `model_dump` outside the guard would make the seam's presence change behaviour for callers
        who never asked for it — which is exactly what a door-level opt-in must not do.
        """
        if self.key is None:
            return
        body = response.model_dump(mode="json", exclude_none=True) if isinstance(response, BaseModel) else response
        await run_in_threadpool(
            idempotency_records.record_outcome,
            self.registry_root,
            self.storage_options,
            scope=self.scope,
            key=self.key,
            status=status,
            body=body,
        )


def _outcome_unknown(key: str) -> ConcurrentModificationError:
    return ConcurrentModificationError(
        f"the attempt under idempotency key {key!r} was lost before it answered, and whether its change landed cannot be told: "
        "read the object, then send a new key"
    )


async def _claim(
    registry_root: str,
    storage_options: StorageOptions,
    scope: str,
    key: str,
    *,
    binding: idempotency_records.KeyBinding,
    digest: str,
    captured: idempotency_records.Captured | None,
) -> Converge:
    """Claim `key` for the operation `binding` names and the request `digest` names.

    The failures are translated to the spec's own vocabulary rather than a hand-picked status, per
    `rask-lance-catalog`: a key bound to another door or another object, or sent with another request, is the CALLER's
    mistake and correctable, so `InvalidInput` (400); an attempt still running is a conflict about the world, so
    `ConcurrentModification` (409), which is what a retrying client should see; a record that is there and cannot be read
    as one is that conflict too, the outcome of whatever wrote it unknown, so the request changes nothing.
    """
    attempt = uuid4().hex
    try:
        claimed = await run_in_threadpool(
            partial(
                idempotency_records.claim,
                registry_root,
                storage_options,
                scope=scope,
                key=key,
                binding=binding,
                digest=digest,
                attempt=attempt,
                captured=captured,
            )
        )
    except idempotency_records.KeyReusedError as exc:
        raise InvalidInputError(str(exc)) from exc
    except (idempotency_records.InFlightError, RecordChangedError) as exc:
        raise ConcurrentModificationError(str(exc)) from exc
    except idempotency_records.KeyRecordUnreadableError as exc:
        raise _outcome_unknown(key) from exc
    taken = claimed if isinstance(claimed, idempotency_records.TakenOver) else None
    return Converge(
        registry_root=registry_root,
        storage_options=storage_options,
        scope=scope,
        key=key,
        attempt=attempt,
        replay=claimed if isinstance(claimed, idempotency_records.Replay) else None,
        taken_over_since=None if taken is None else taken.since,
        taken_over_intent=None if taken is None else taken.intent,
        taken_over_captured=None if taken is None else taken.captured,
    )


class KeyedDoor(BaseModel):
    """A keyed door's request: the key it was sent, and the claim :meth:`begin` makes for it.

    Built per request by :func:`keyed_door`, which gives the claim back when the door is refused (:meth:`Converge.release_if_refused`).
    """

    model_config = ConfigDict(arbitrary_types_allowed=True)

    #: Where the claims are kept: the control root.
    registry_root: str
    storage_options: StorageOptions
    #: The caller the key belongs to (:func:`_scope`).
    scope: str
    key: str | None
    #: The route the request matched (:func:`door_endpoint`), which the key is bound to.
    endpoint: str | None = None
    converge: Converge | None = None
    #: The request, whose body, query and run headers :meth:`begin` digests.
    _request: Request | None = PrivateAttr(default=None)

    def hold(self, request: Request) -> None:
        """Keep the request :meth:`begin` digests."""
        self._request = request

    async def begin(self, *, target: str, takeover: Takeover, captured: idempotency_records.Captured | None = None) -> Converge:
        """Claim the request's key for the door acting on ``target`` (:func:`_claim`), after the door's shape refusals.

        ``target`` is the canonical id of the object the door acts on (``fga_ids.canonical_object_id``), so the same key on
        another object is refused rather than answered with this one's response ([[LH-576]]); the same key sent with
        another request is refused too. ``captured`` is the object the door's name resolved to (:func:`capture`), kept on
        the claim, by which a resend whose name holds nothing any more is authorized (:func:`captured_object`).

        ``takeover`` is how a resend takes over from an attempt lost unanswered (:data:`Takeover`). A JUDGED door's is
        answered from what that attempt intended (:meth:`Converge.intend`) where the judge shows every piece of its
        evidence, and runs where any piece is missing or nothing was intended. A RERUN door runs again. Either is refused,
        its outcome unknown, where the name now names another object than the key was first claimed on, and so is an
        UNKNOWN door's, and a judge whose read fails. The refusal keeps the claim, so a later resend is judged the same.
        """
        converge = await self._begin(target=target, qualifier="", captured=captured)
        if converge.taken_over_since is None or converge.key is None:
            self.converge = converge
            return converge
        if takeover == "unknown":
            raise _outcome_unknown(converge.key)
        intent = converge.taken_over_intent
        if takeover != "rerun" and intent is not None:
            try:
                landed = all([await takeover(evidence) for evidence in intent.evidence])
            except ServiceUnavailableError:
                raise
            except Exception as exc:  # noqa: BLE001 — evidence that cannot be read shows neither outcome
                raise _outcome_unknown(converge.key) from exc
            if landed:
                await converge.remember(intent.answer.status, intent.answer.body)
                self.converge = converge.model_copy(update={"replay": intent.answer})
                return self.converge
        # Run again, unless the name now names another object than the key was first claimed on: the run would act on it.
        first = converge.taken_over_captured
        if first is not None and captured is not None and captured.object != first.object:
            raise _outcome_unknown(converge.key)
        self.converge = converge
        return converge

    async def begin_write(self, *, target: str, qualifier: str) -> Converge:
        """:meth:`begin` for a row write, whose key also binds ``qualifier`` (its mode and ref) and whose takeover finds
        the lost attempt's commit by the key it stamps (`endpoints.data._keyed_table_write`)."""
        self.converge = await self._begin(target=target, qualifier=qualifier, captured=None)
        return self.converge

    async def _begin(self, *, target: str, qualifier: str, captured: idempotency_records.Captured | None) -> Converge:
        if self.key is None or self.endpoint is None or self._request is None:
            return Converge(registry_root=self.registry_root, storage_options=self.storage_options, scope=self.scope, key=None)
        # One key, however the caller cases its hex digits.
        key = self.key.lower()
        binding = idempotency_records.KeyBinding(endpoint=f"{self.endpoint}{qualifier}", target=target)
        return await _claim(
            self.registry_root, self.storage_options, self.scope, key, binding=binding, digest=await _digest_of(self._request), captured=captured
        )


async def keyed_door(
    request: Request, settings: SettingsDep, so: StorageOptionsDep, token: CurrentToken, idempotency_key: IdempotencyKeyHeader = None
) -> AsyncIterator[KeyedDoor]:
    """The door's :class:`KeyedDoor`; a refusal the door raises after its claim gives the claim back ([[LH-487]]).

    A dependency with ``yield`` is handed the endpoint's exception at the ``yield`` (FastAPI 0.140.13), before the
    problem handlers answer it, so every keyed door releases through this one place. FastAPI reads the body a door
    declares before it solves the door's dependencies, and ``Request.body`` keeps what it read, so :meth:`KeyedDoor.begin`
    digests the door's own bytes without reading the stream again; a door that declares no body has none read.
    """
    door = KeyedDoor(registry_root=settings.registry_root, storage_options=so, scope=_scope(token), key=idempotency_key, endpoint=door_endpoint(request))
    door.hold(request)
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


__all__ = [
    "Converge",
    "IdempotencyKeyHeader",
    "Judge",
    "KeyedDoor",
    "KeyedDoorDep",
    "capture",
    "captured_object",
    "door_endpoint",
    "request_digest",
    "scope_of",
]
