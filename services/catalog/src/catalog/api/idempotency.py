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
import time
from dataclasses import dataclass
from typing import Annotated, Any

from fastapi import Header
from fastapi.concurrency import run_in_threadpool
from lance_namespace import ConcurrentModificationError, InvalidInputError

from catalog.core.config import Settings
from service_kit.governed.oidc import IDToken
from service_kit.lakehouse import idempotency


#: The header, constrained to the same shape the seam validates. Bounded and tokenised because it
#: becomes part of an object key; `None` is the ordinary case and means "no convergence asked for".
IdempotencyKeyHeader = Annotated[
    str | None,
    Header(alias="Idempotency-Key", min_length=1, max_length=64, pattern=r"^[A-Za-z0-9._-]+$"),
]


#: The scope for a request that carries no verified principal — auth OFF, which the estate supports for
#: local bring-up. There is exactly one caller then, so one scope is the honest answer; the alternative,
#: refusing the key, would make the mechanism untestable on precisely the deployments used to try it.
_ANONYMOUS_SCOPE = "anonymous"


def _scope(token: IDToken | None) -> str:
    if token is None:
        return _ANONYMOUS_SCOPE
    return hashlib.blake2s(token.sub.encode("utf-8"), digest_size=8).hexdigest()


@dataclass(frozen=True)
class Converge:
    """A claimed key, or the no-op a keyless caller gets.

    `replay` is the first attempt's answer when there is one. `remember` stores this attempt's answer;
    it is a no-op without a key, so a door calls it unconditionally and reads the same on both paths.
    """

    settings: Settings
    storage_options: dict[str, str]
    scope: str
    key: str | None
    replay: idempotency.Replay | None

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
            self.settings.registry_root,
            self.storage_options,
            scope=self.scope,
            key=self.key,
            status=status,
            body=body,
        )


async def begin(settings: Settings, storage_options: dict[str, str], token: IDToken | None, key: str | None, *, endpoint: str) -> Converge:
    """Claim `key` for `endpoint`, or return the no-op when the caller sent none.

    The two failures are translated to the spec's own vocabulary rather than a hand-picked status, per
    `rask-lance-catalog`: a key bound to another operation is the CALLER's mistake and correctable, so
    `InvalidInput` (400); an attempt still running is a conflict about the world, so
    `ConcurrentModification` (409), which is what a retrying client should see.
    """
    if key is None:
        return Converge(settings=settings, storage_options=storage_options, scope="", key=None, replay=None)

    scope = _scope(token)
    try:
        replay = await run_in_threadpool(
            idempotency.claim,
            settings.registry_root,
            storage_options,
            scope=scope,
            key=key,
            endpoint=endpoint,
            now=time.time(),
        )
    except idempotency.KeyReusedError as exc:
        raise InvalidInputError(str(exc)) from exc
    except idempotency.InFlightError as exc:
        raise ConcurrentModificationError(str(exc)) from exc
    return Converge(settings=settings, storage_options=storage_options, scope=scope, key=key, replay=replay)


__all__ = ["Converge", "IdempotencyKeyHeader", "begin"]
