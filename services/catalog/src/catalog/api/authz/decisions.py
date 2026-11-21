"""One decision, asked, audited and refused: a rung on the object its name resolves to, any of several doors, a trashed
object, and a refused read answered as not found.

Fail-closed twice over: an unwired client raises 503 here, and ``fga_reads.check``/``fga_reads.batch_check`` raise 503
on an OpenFGA outage rather than allowing.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from typing import NamedTuple

from lance_namespace import (
    NamespaceNotFoundError,
    PermissionDeniedError,
    ServiceUnavailableError,
    TableNotFoundError,
    UnauthenticatedError,
    UnsupportedOperationError,
)
from openfga_sdk import OpenFgaClient

from catalog.api import idempotency
from catalog.api.authz.objects import resolve_rung, rung_label, standing_ancestors, trashed_at_or_above
from catalog.api.authz.rungs import READ_RELATIONS, UUID_KEYED
from catalog.api.dependencies import Resolver
from catalog.api.security import Caller
from catalog.core import write_judge
from lakehouse.governed import fga_ids, fga_reads
from lakehouse.governed.audit import AuditAction, AuditOutcome, audit
from lakehouse.governed.settings import FgaSettings
from lakehouse.records.trash import TrashRecord


__all__ = [
    "Authorization",
    "answered",
    "authorization",
    "record_undeclared",
    "refuse_absent",
    "refused_read_as_absent",
    "require_any",
    "require_audited",
    "require_fga",
    "require_rung",
    "require_trashed",
    "require_wired",
    "wired",
]


log = logging.getLogger(__name__)


def record_undeclared(path: str, fga_type: str, suffix: str, *, user: str, obj: str) -> None:
    """Log and audit an undeclared-door refusal. The ERROR line names the path and suffix because the
    500 the caller receives is redacted, so it is where an operator finds the route that shipped bare."""
    log.error("undeclared_door", extra={"path": path, "fga_type": fga_type, "suffix": suffix})
    audit(AuditAction.AUTHZ, AuditOutcome.FAILURE, subject=user, resource=obj, reason="undeclared_door", suffix=suffix)


async def answered(client: OpenFgaClient, *, user: str, relation: str, obj: str, **recorded: str) -> bool:
    """Whether ``user`` holds ``relation`` on ``obj``, with an outage recorded before it fails the request closed.

    Every check a gate makes goes through here, so OpenFGA not answering is one ``failure`` record naming the relation
    whichever check met it, never a silence and never a denial against the caller. ``recorded`` overrides what that
    record names (its ``resource``, and any further field). The verdict's own record is the caller's to write: a gate
    of one rung, a gate of several doors and a probe each record a verdict differently.

    Raises:
        ServiceUnavailableError: OpenFGA did not answer.
    """
    try:
        return await fga_reads.check(client, user=user, relation=relation, obj=obj)
    except ServiceUnavailableError:
        audit(AuditAction.AUTHZ, AuditOutcome.FAILURE, relation=relation, subject=user, **{"resource": obj, **recorded, "reason": "authz_unavailable"})
        raise


async def require_audited(client: OpenFgaClient, *, user: str, relation: str, obj: str, label: str | None = None) -> None:
    """Check one ``relation`` on ``obj``, audit the decision, and raise 403 on denial.

    ``label`` is what the refusal names the object as, its name for a table or namespace, so a refusal reads the same
    whether the name holds an object or not (:func:`require_rung`).
    """
    allowed = await answered(client, user=user, relation=relation, obj=obj)
    audit(
        AuditAction.AUTHZ, AuditOutcome.ALLOW if allowed else AuditOutcome.DENY, relation=relation, subject=user, resource=obj
    )  # #41 audit every authz decision
    if not allowed:
        log.info("access_denied", extra={"relation": relation, "object": obj})
        raise PermissionDeniedError(f"{relation} required on {label or obj}")


def refuse_absent(*, user: str, relation: str, label: str) -> PermissionDeniedError:
    """The refusal of ``relation`` on a name that holds no object, audited by the name: no tuple can grant it."""
    audit(AuditAction.AUTHZ, AuditOutcome.DENY, relation=relation, subject=user, resource=label, reason="no_such_object")
    log.info("access_denied", extra={"relation": relation, "object": label})
    return PermissionDeniedError(f"{relation} required on {label}")


async def require_rung(client: OpenFgaClient, resolver: Resolver, *, user: str, rung: write_judge.Rung) -> str:
    """Ask ``rung`` on the object its name resolves to, refuse it as :func:`require_audited` does, and return that object.

    A name that holds no object is refused alike: no tuple can name it, and the refusal names the name, as it does for
    an object that is there. One exception, a keyed resend: a name its caller's ``Idempotency-Key`` was first claimed on
    for this door and this request is asked the same rung on the object the claim captured
    (``idempotency.captured_object``), so the replay of a drop, a deregister or a rename reaches its door, and a caller
    the rung refuses there meets the same refusal.
    """
    label = rung_label(rung)
    if not isinstance(rung.on, str) and rung.on.in_trash:
        segments = list(rung.on.segments)
        trashed = await resolver.trashed(rung.on.kind, segments)
        if trashed:
            return await require_trashed(client, resolver, user=user, relation=rung.relation, named=rung.on, label=label, trashed=trashed)
        resolver.admit_trashed(rung.on.kind, segments, [])
    obj = await resolve_rung(resolver, rung)
    if obj is None and not isinstance(rung.on, str):
        obj = await idempotency.captured_object(resolver.request, resolver.settings, subject=user, name=fga_ids.canonical_object_id(list(rung.on.segments)))
    if obj is None:
        raise refuse_absent(user=user, relation=rung.relation, label=label)
    await require_audited(client, user=user, relation=rung.relation, obj=obj, label=label)
    return obj


async def require_trashed(
    client: OpenFgaClient,
    resolver: Resolver,
    *,
    user: str,
    relation: str,
    named: write_judge.Named,
    label: str,
    trashed: list[TrashRecord],
) -> str:
    """Ask ``relation`` on every trashed object ``named`` leads to, admit those the caller may act on, and refuse if none.

    A trash door is addressed by a name several dropped objects may have held (ADR 0173 §9, [[LH-525]]), and each is
    its own object with its own grants. The door then acts only on what was admitted, so its answer, the list of
    uuids it offers when several remain, names nothing its caller holds no grant on. Each decision is audited.
    """
    objects = {fga_ids.object_of(named.kind, uuid): record for record in trashed if (uuid := record.uuid) is not None}
    allowed = await fga_reads.batch_check(client, user=user, relation=relation, objects=sorted(objects))
    for obj in sorted(objects):
        audit(AuditAction.AUTHZ, AuditOutcome.ALLOW if allowed.get(obj, False) else AuditOutcome.DENY, relation=relation, subject=user, resource=obj)
    admitted = {obj: record for obj, record in objects.items() if allowed.get(obj, False)}
    resolver.admit_trashed(named.kind, list(named.segments), list(admitted.values()))
    if not admitted:
        log.info("access_denied", extra={"relation": relation, "object": label})
        raise PermissionDeniedError(f"{relation} required on {label}")
    return next(iter(admitted))


async def require_any(client: OpenFgaClient, *, user: str, doors: list[tuple[str, str]], label: str | None = None) -> None:
    """Allow if ANY ``(relation, object)`` door checks out; audit the decision, and raise 403 otherwise.

    :func:`require_audited` expresses a single rung; a two-door gate (the project's own admin OR the estate admin)
    cannot be built by chaining it, because the first call would raise on the door the caller did not come
    through. Chaining with a swallowed 403 is no better: it writes a DENY into the #41 compliance trail for
    a legitimate admin's allowed request. Here exactly ONE ALLOW record is written, naming the door that
    decided; a full denial writes one DENY per door probed, so the trail still says precisely which rungs
    the caller lacked. Fail-closed identically to :func:`require_audited`: an OpenFGA outage on ANY probe audits a
    FAILURE and propagates (503) rather than falling through to the next door.
    """
    for relation, obj in doors:
        if await answered(client, user=user, relation=relation, obj=obj):
            audit(AuditAction.AUTHZ, AuditOutcome.ALLOW, relation=relation, subject=user, resource=obj)
            return
    for relation, obj in doors:  # reaching here means every door was probed and every one denied
        audit(AuditAction.AUTHZ, AuditOutcome.DENY, relation=relation, subject=user, resource=obj)
    wanted = " or ".join(f"{relation} on {label or obj}" for relation, obj in doors)
    log.info("access_denied", extra={"relation": "any_of", "object": wanted})
    raise PermissionDeniedError(f"one of [{wanted}] required")


def require_wired(client: OpenFgaClient | None) -> OpenFgaClient:
    """Fail CLOSED on an authz layer that is enabled but unwired — the 503 half of the estate gate.

    One body for one message (catalog-api-09). Eleven handlers carried their own copy of
    ``if client is None: raise ServiceUnavailableError(...)``, which is how two of them ended up
    disagreeing about whether an absent client is 503 or 409.
    """
    if client is None:
        raise ServiceUnavailableError("authorization service is not available")
    return client


def wired(settings: FgaSettings, client: OpenFgaClient | None) -> OpenFgaClient | None:
    """The client to write and read grants with, ``None`` only where authorization is off.

    Off is what the setting says and nothing else ([[LH-237]], [[LH-585]]): a seed, a revoke or a listing that read a
    missing client as "off" would, on a deployment whose client failed to build, leave an object with no owner, keep a
    dropped object's grants, or report an estate with none, each without a word.

    Raises:
        ServiceUnavailableError: Authorization is on and no client is wired.
    """
    return require_wired(client) if settings.fga_enabled else None


def require_fga(settings: FgaSettings, client: OpenFgaClient | None, *, feature: str) -> OpenFgaClient:
    """The estate gate every FGA-BACKED door opens with, and the client it needs, in one call.

    Two refusals, and the distinction is deliberate: with FGA OFF the deployment genuinely does not
    implement the feature (406 ``UnsupportedOperation`` — there are no tuples to review, simulate or
    grant), while ENABLED-but-unwired is a fault the caller should retry against (503, fail-closed —
    never a silent allow, never an empty grant list that reads as "nobody has access").

    ``feature`` names what is unavailable, so the 406 still says which door refused.

    Doors that answer an FGA-off stack with DATA rather than a refusal (``_read_managed_access``
    reports ``managed_access=False``; ``list_members`` reports an empty list) do not use this — for
    them "off" is an answer, not a refusal.
    """
    if not settings.fga_enabled:
        raise UnsupportedOperationError(f"{feature} requires OpenFGA (this stack runs auth-off)")
    return require_wired(client)


class Authorization(NamedTuple):
    """The authorization a request is decided under while FGA is on: the wired client, and the subject it decides for.

    An internal value bundle, never boundary input, so a ``NamedTuple``: immutable and typed, with nothing to validate.
    """

    client: OpenFgaClient
    subject: str

    async def require(self, relation: str, obj: str, *, label: str | None = None) -> None:
        """Refuse unless the subject holds ``relation`` on ``obj``, the decision audited as :func:`require_audited` audits it."""
        await require_audited(self.client, user=self.subject, relation=relation, obj=obj, label=label)

    async def batch_check(self, relation: str, objects: Sequence[str]) -> dict[str, bool]:
        """Whether the subject holds ``relation`` on each of ``objects``, in one round trip: a listing judging its items."""
        return await fga_reads.batch_check(self.client, user=self.subject, relation=relation, objects=list(objects))


def authorization(settings: FgaSettings, client: OpenFgaClient | None, token: Caller | None) -> Authorization | None:
    """The client and the subject a decision is asked with, ``None`` only with FGA off, when every decision allows.

    Every gate and every listing that judges its items asks this, so none reads an authorization layer that is on as
    one that is off ([[LH-237]]): an unwired client is the 503 :func:`require_wired` answers and no caller is a 401,
    whatever ran before the gate. Off is one explicit answer, never a missing client.

    Raises:
        ServiceUnavailableError: FGA is on and unwired.
        UnauthenticatedError: FGA is on and nobody is signed in.
    """
    if not settings.fga_enabled:
        return None
    wired = require_wired(client)
    if token is None:
        raise UnauthenticatedError("authentication required")
    return Authorization(client=wired, subject=token.subject)


async def refused_read_as_absent(
    client: OpenFgaClient,
    resolver: Resolver,
    *,
    token: Caller,
    fga_type: str,
    denied: str,
    segments: list[str],
) -> None:
    """Raise the spec's NOT-FOUND for a refused READ, whether its name holds no object or one the caller cannot see, to a
    caller who sees above the name.

    Returns — never raises — in every other case, so the caller's `raise` re-throws the 403 unchanged.

    THE SPEC ASKS FOR 404 AND THIS DOOR ANSWERED 403 FOR EVERYTHING. `authorize` runs before any
    endpoint, so an object with no tuples is refused identically whether it is forbidden or absent.
    Measured on the deployed catalog 2026-09-11: an absent table under a namespace alice holds
    `can_get_metadata`, `can_create_table` AND `can_delete` on answered 403 code 15, so a generated
    client cannot tell "ask for access" from "you typed the wrong name".

    THREE CONDITIONS, and each one is load-bearing:

    * THE DENIED RUNG MUST BE A READ RUNG, and it is keyed on the relation `action_relation` actually
      resolved — never re-derived from the suffix. Deriving it again from the trailing path segment
      classified `access/list` (owner-tier: it lists who holds what) as a read, because the segment is
      the word "list"; `tests/integration/test_authz.py` caught it. The rung the door resolved to is the
      only thing that knows what the door DOES. The estate's no-existence-oracle rule is deliberate on
      the destructive doors — `delete_warehouse`, `delete_project` and `_set_warehouse_status` collapse
      PermissionDenied into NotFound precisely so a delete cannot enumerate ids — and keying on the rung
      keeps every one of them out of this path by construction.
    * THE CALLER MUST SEE ABOVE THE NAME: ``can_get_metadata`` on a namespace standing above it, at any depth, or on an
      object a recoverable drop trashed at the name or an ancestor's (below). Without that the answer is today's 403, so
      this cannot be used to walk the id space from outside the hierarchy.
    * WHETHER THE NAME HOLDS AN OBJECT IS NEVER ASKED ([[LH-236]]). On a table and a namespace every read rung is the
      caller's sight of the object (a table's ``can_read_data`` is ``reader``, as its ``can_get_metadata`` is, and a
      namespace's read doors all ask ``can_get_metadata``), so a refused read names an object the caller cannot see,
      and what it is told must not depend on whether that object exists. Seeing a namespace is not seeing its children:
      the model grants ``can_get_metadata`` on a namespace upward from any one child, and a listing shows each child
      only to a caller who holds it. An absence converted alone would answer 404 for a ghost and 403 for a hidden
      sibling, and so name every object the listing hides.

    EVERY ANCESTOR THAT STANDS IS ASKED, not only the nearest. A hidden namespace between the name and a namespace the
    caller sees must not end the walk, or a name probed beneath it would answer 403 where one beneath an absent
    namespace answers 404. A cascade leaves a child whose parent went with it ([[LH-536]]); its standing ancestors are
    read the same way. A name's first segment is its warehouse ([[LH-593]]), an ancestor of every name and asked as any
    other: upward visibility (``can_get_metadata from child``) makes a reader of one namespace a reader of its warehouse,
    and such a reader is answered the absence for every name in the warehouse alike, hidden or absent, so the answer
    tells nothing about which of them exist. What a RECOVERABLE drop leaves is answered by
    its trash: the drop keeps its objects' grants for the undrop, so a caller holding ``can_get_metadata`` on an object
    trashed at the name or an ancestor's, whatever stands there now, already sees it through the trash doors and is
    answered the absence rather than a refusal that hides it (:func:`trashed_at_or_above`). A trash it cannot read leaves
    the 403.

    The refusal is audited as the denial it is before this runs; only the answer is an absent name's. The request never
    reaches the endpoint either way: this raises, and its caller re-raises. Nothing is read, and no grant is implied by
    the 404.
    """
    if denied not in READ_RELATIONS or fga_type not in UUID_KEYED:
        return
    try:
        # The trash is listed only when no standing ancestor admits the caller: it is one read of every record.
        if not await _reads_any(client, user=token.subject, objects=await standing_ancestors(resolver, segments)) and not await _reads_any(
            client, user=token.subject, objects=await trashed_at_or_above(resolver, fga_type, segments)
        ):
            return
    except ServiceUnavailableError:
        return  # the authz layer is down; the 403 the caller already has is the fail-closed answer
    except (OSError, ValueError):
        log.warning("refused_read_trash_unreadable", extra={"object": fga_ids.canonical_object_id(segments)}, exc_info=True)
        return
    ident = fga_ids.canonical_object_id(segments)
    absent = TableNotFoundError if fga_type == "table" else NamespaceNotFoundError
    raise absent(f"{fga_type} {ident!r} not found")


async def _reads_any(client: OpenFgaClient, *, user: str, objects: list[str]) -> bool:
    """Whether ``user`` holds ``can_get_metadata`` on any of ``objects``."""
    if not objects:
        return False
    allowed = await fga_reads.batch_check(client, user=user, relation="can_get_metadata", objects=objects)
    return any(allowed.get(obj, False) for obj in objects)
