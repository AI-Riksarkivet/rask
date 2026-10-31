"""The catalog's side of `Idempotency-Key`: an OPTIONAL header that makes a replay converge.

WHY OPTIONAL, AND WHY THAT IS NOT A COMPROMISE (§ Q17-32, `rask-lance-catalog` § D3). The Lance
Namespace spec defines no `Idempotency-Key`, and a stock Lance client must work against this catalog
with no rask SDK — so requiring one would break conformance on 54 routed operations. A caller that
sends a key gets convergence; one that does not is exactly where it was, which is the whole reason the
mechanism is a seam a door opts into rather than middleware over 91 write routes.

WHAT IT BUYS, precisely. `dapr-resiliency.yaml` retries a write door's failure, and a 500 raised AFTER
the Lance write in `create_governed_table` — the schema read-back, `emit_create`, `emit_control` — is
replayed with nothing to converge it. With `mode=Create` the replay answers AlreadyExists, so a
successful create surfaces as a 409 nobody can tell from a name collision; with `mode=Overwrite` each
replay RE-EXECUTES the destructive path. The catalog's app-id already left the bare-500 matcher (the
rule's other branch); this closes the replays that remain — a 503, a proxy timeout, a client retry.

THE SCOPE IS A HASH OF THE SUBJECT, NOT THE SUBJECT. A key belongs to its caller, so two tenants using
the string `retry-1` are two operations and must not collide. The subject itself cannot be a path
segment: measured on this estate, a Dex `sub` is a base64 protobuf blob (`CiQwOGE4...EgVsb2NhbA`),
which is neither bounded nor free of characters an object key would have to escape. A short digest is
stable, bounded and opaque — and being opaque is a property rather than a cost, since the record is
not an audit trail and the audit lane already carries the principal.
"""

from __future__ import annotations

import hashlib
import logging
import time
from collections.abc import AsyncIterator
from typing import Annotated, Any

from fastapi import Depends, Header
from fastapi.concurrency import run_in_threadpool
from lance_namespace import ConcurrentModificationError, InvalidInputError, LanceNamespaceError
from pydantic import BaseModel, ConfigDict

from catalog.api.dependencies import SettingsDep, StorageOptionsDep
from catalog.api.security import Caller, CurrentToken
from service_kit.lakehouse import idempotency
from service_kit.lakehouse.ns_errors import status_for
from service_kit.lakehouse.objectfs import StorageOptions


#: The header: a UUID, in any case, because the key becomes part of an object key and a UUID is a segment no spelling
#: can turn into a path that leaves its prefix. Idempotency-Key is rask's own extension (the spec defines none), so its
#: grammar is the catalog's to choose. Anything else is refused 400 (code 13) on a spec route. `None` is the ordinary
#: case and means "no convergence asked for".
IdempotencyKeyHeader = Annotated[
    str | None,
    Header(alias="Idempotency-Key", pattern=r"^[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12}$"),
]


log = logging.getLogger(__name__)


#: The scope for a request that carries no verified principal — auth OFF, which the estate supports for
#: local bring-up. There is exactly one caller then, so one scope is the honest answer; the alternative,
#: refusing the key, would make the mechanism untestable on precisely the deployments used to try it.
_ANONYMOUS_SCOPE = "anonymous"


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
    claimed_at: float = 0.0
    replay: idempotency.Replay | None = None

    async def release_if_refused(self, exc: BaseException) -> None:
        """Give the claim back when ``exc`` is a refusal (a 4xx of the spec's taxonomy), which the keyed doors raise
        before any write: the caller's corrected retry under the key then runs rather than meeting 409 for the
        lease ([[LH-487]]). A fault (5xx), or anything else, keeps the lease, since the write may have landed.
        """
        if self.key is None or not isinstance(exc, LanceNamespaceError) or status_for(int(exc.code)) >= 500:
            return
        await run_in_threadpool(
            idempotency.release,
            self.registry_root,
            self.storage_options,
            scope=self.scope,
            key=self.key,
            claimed_at=self.claimed_at,
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
            idempotency.record_outcome,
            self.registry_root,
            self.storage_options,
            scope=self.scope,
            key=self.key,
            status=status,
            body=body,
        )


async def begin(registry_root: str, storage_options: StorageOptions, scope: str, key: str | None, *, endpoint: str) -> Converge:
    """Claim `key` for `endpoint`, or return the no-op when the caller sent none.

    The two failures are translated to the spec's own vocabulary rather than a hand-picked status, per
    `rask-lance-catalog`: a key bound to another operation is the CALLER's mistake and correctable, so
    `InvalidInput` (400); an attempt still running is a conflict about the world, so
    `ConcurrentModification` (409), which is what a retrying client should see.
    """
    if key is None:
        return Converge(registry_root=registry_root, storage_options=storage_options, scope=scope, key=None)

    # One key, however the caller cases its hex digits.
    key = key.lower()
    claimed_at = time.time()
    try:
        replay = await run_in_threadpool(
            idempotency.claim,
            registry_root,
            storage_options,
            scope=scope,
            key=key,
            endpoint=endpoint,
            now=claimed_at,
        )
    except idempotency.KeyReusedError as exc:
        raise InvalidInputError(str(exc)) from exc
    except idempotency.InFlightError as exc:
        raise ConcurrentModificationError(str(exc)) from exc
    return Converge(registry_root=registry_root, storage_options=storage_options, scope=scope, key=key, claimed_at=claimed_at, replay=replay)


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
    converge: Converge | None = None

    async def begin(self, *, endpoint: str) -> Converge:
        """Claim the request's key for ``endpoint`` (:func:`begin`), after the door's shape refusals."""
        self.converge = await begin(self.registry_root, self.storage_options, self.scope, self.key, endpoint=endpoint)
        return self.converge


async def keyed_door(
    settings: SettingsDep, so: StorageOptionsDep, token: CurrentToken, idempotency_key: IdempotencyKeyHeader = None
) -> AsyncIterator[KeyedDoor]:
    """The door's :class:`KeyedDoor`; a refusal the door raises after its claim gives the claim back ([[LH-487]]).

    A dependency with ``yield`` is handed the endpoint's exception at the ``yield`` (FastAPI 0.140.13), before the
    problem handlers answer it, so every keyed door releases through this one place.
    """
    door = KeyedDoor(registry_root=settings.registry_root, storage_options=so, scope=_scope(token), key=idempotency_key)
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


__all__ = ["Converge", "IdempotencyKeyHeader", "KeyedDoor", "KeyedDoorDep", "begin"]
