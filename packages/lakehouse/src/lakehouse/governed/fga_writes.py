"""Writes to the store: the grants and revokes the doors make, the hierarchy's edges as an object moves, and one audit
row per tuple from inside the write path.

Tuples persist in the OpenFGA datastore (Postgres in the deployed stack; SQLite for the auth-e2e). Each write that grants
or revokes is held to its registered call sites by a `banned-api` entry in ``pyproject.toml``.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable, Collection
from http import HTTPStatus
from typing import Any, Literal, Protocol, cast

from lance_namespace import ServiceUnavailableError
from openfga_sdk import OpenFgaClient
from openfga_sdk.client.models import (
    ClientTuple,
    ClientWriteRequest,
    ClientWriteRequestOnDuplicateWrites,
    ConflictOptions,
)
from openfga_sdk.exceptions import ApiException
from openfga_sdk.models.error_code import ErrorCode

# Re-exported deliberately, in the `X as X` form so ruff does not prune it as unused: a caller building
# a CONDITIONAL tuple needs this type, and importing it straight from the SDK would spread openfga_sdk
# imports back into the services this module exists to keep SDK-free (`ClientTuple` is re-exported for
# the same reason). Its only consumer reaches it as `fga_writes.RelationshipCondition` from another module,
# which ruff's unused-import fix cannot see.
from openfga_sdk.models.relationship_condition import RelationshipCondition as RelationshipCondition

from lakehouse.governed import fga_reads
from lakehouse.governed.audit import AuditAction, AuditOutcome, audit
from lakehouse.governed.fga_ids import user_object
from lakehouse.governed.fga_model import CHILD_EDGE_PARENT_TYPES, hierarchy_edge_tuples  # noqa: TID251 — the writes seed and move the edges
from lakehouse.governed.fga_resilience import DEFAULT_RETRY, FAIL_CLOSED, RetryPolicy, guarded, retrying


log = logging.getLogger(__name__)


# --------------------------------------------------------------------------- #
# Writes (post-create grants) + deletes (revoke-on-delete)
# --------------------------------------------------------------------------- #


#: Where a tuple write came from, recorded on every audit row so provenance can say HOW a grant
#: happened and not merely who. `create` dwarfs the rest in a real estate — it is the post-registration
#: seed — and telling it apart from a deliberate admin grant is most of the value.
#:
#: The tenant-lifecycle origins (`project_create`, `warehouse_create`, `lifecycle_delete`) name the
#: control-plane doors that mint and retire a tenant's authz: an audit row can then say a grant was born
#: WITH its project rather than added to it later, and a revoke was a deletion rather than an admin's
#: choice. `warehouse_bootstrap` is gone with the exception it named — a project's first warehouse no
#: longer mints its admin (that moved to `POST /management/v1/projects`, `open_hierarchy_lifecycle.md` Decision 1).
TupleOrigin = Literal[
    "admin_api",
    "grant_api",
    "create",
    "project_create",
    "warehouse_create",
    # Converging the warehouse grants over warehouses that already exist: re-asserting those the release declares and
    # taking back the maintenance rung from its own accounts it no longer declares. Distinct from
    # "warehouse_create" deliberately: an audit row that claims a tuple was written at CREATE time,
    # when it was actually written by a backfill months later, destroys the one property the origin
    # field exists for.
    "warehouse_grant_backfill",
    # Re-asserting a tuple the control-plane REGISTRY still justifies, after the store lost it
    # ([[LH-061]]). Its own value for `warehouse_grant_backfill`'s reason one step further: this grant was not
    # made by anyone, at create time or later — it was RECONSTRUCTED from a record, and an auditor
    # reviewing who granted a tenant's admin rung needs to see that no person did. It also bounds what
    # the row can be read to mean: the registry justifies the SEED and nothing after it, so a
    # `registry_rebuild` row is evidence of recovery, never of an authorization decision.
    "registry_rebuild",
    # REVOKING a tuple whose OBJECT no longer exists ([[LH-061]]'s repair half). Its own value for the
    # same reason `registry_rebuild` has one, in the other direction: an auditor reading a revoke needs
    # to know whether a person dropped the object or a reconciler swept authz for an object already
    # gone. `lifecycle_delete` says a door destroyed something; this says nothing was destroyed because
    # there was nothing left to destroy. It also bounds the row: a `drift_repair` revoke is only ever
    # justified by a drift finding, so it can never be evidence of an access decision.
    "drift_repair",
    # The grants the catalog writes on the namespace it keeps for provenance (ADR 0158): its parent edge and a `writer`
    # per configured provenance writer. Its own value because no person granted them; the deployment configured them.
    "provenance_namespace",
    # The parent edge a rename moves when it carries a table into another namespace ([[LH-524]]). Its own value because
    # nobody granted it: the table's grants stay on its uuid, and the rename's control event (`table_renamed`, `from`
    # and `to`) names the move.
    "rename",
    # A tuple written or revoked because git declares it, or stopped declaring it ([[XC-213]]): a project's admin, or a
    # grant to a principal that is not a person. Its own value because no caller at a door made it; the declaration did.
    "declaration",
    # Revoking a recoverable drop's grants at its deadline (ADR 0146). Its own value because no person revoked them and
    # nothing was destroyed: the grace period ended, and the bytes wait for the purge (`lifecycle_delete`).
    "trash_expired",
    # Revoking every tuple naming a principal the estate admin offboarded ([[LH-233]]). Its own value because the
    # grants were not revoked one by one by someone deciding each: one door removed a person from the estate.
    "principal_delete",
    "lifecycle_delete",
]


def _audit_tuples(action: AuditAction, tuples: list[ClientTuple], actor: str, *, origin: TupleOrigin, **extra: str) -> None:
    """One audit row PER TUPLE, emitted from inside the write path.

    This lives here rather than at the call sites because coverage by convention does not hold: of the
    eight tuple-write sites in this repo, only two ever emitted a tuple-level row, and the ones that did
    not include ``grant_on_create`` — which fires on every table and namespace registration and is
    therefore where most tuples in a real store come from. The consequence was that "who granted this?"
    had no answer for the majority of the graph, while looking like it did for the admin-API minority.

    Emitting from the library makes the coverage structural: a new write site cannot reach OpenFGA
    without passing through here, and ``actor`` is required, so it cannot compile without naming one.

    A row ASSERTS CONFIRMATION unless it says otherwise: extra fields (``reason="tuple_absent"``)
    are how a path records an attempt whose post-condition held without the server performing
    anything — a compliance query filtering them out reads only what OpenFGA confirmed.
    """
    for t in tuples:
        audit(action, AuditOutcome.SUCCESS, subject=actor, resource=t.object, grantee=t.user, relation=t.relation, origin=origin, **extra)


async def write_tuples(
    client: OpenFgaClient,
    tuples: list[ClientTuple],
    *,
    actor: str,
    origin: TupleOrigin,
    retry: RetryPolicy = DEFAULT_RETRY,
) -> None:
    """Persist relationship tuples (the single write path after a successful mutation).

    DUPLICATE IDEMPOTENCY IS THE SERVER'S JOB: the write rides ``on_duplicate_writes=IGNORE``
    (SDK >= 0.10.4), so a batch containing an already-present tuple lands its siblings in the SAME
    transaction and answers 200 — while a genuinely invalid tuple still 400s. This replaced a
    client-side recovery (classify the 400 by prose markers, then re-write the batch one tuple at a
    time) that existed for a real hazard — an OpenFGA ``Write`` is all-or-nothing, and one duplicate
    used to reject every sibling grant, which is how a warehouse creator once lost ``owner`` on
    their own warehouse — but that carried its own defects: the markers enumerated message phrasings
    (SKG-01), and the fallback turned one transactional write into N sequential round-trips. The
    server-side conflict option is the same post-condition with none of the classification.

    Transient failures are retried; on outage we fail closed with ``ServiceUnavailableError`` so the caller
    does not believe a grant succeeded when it did not. A batch in which any tuple carries the check's
    clock is refused whole with ``InvalidInputError`` (:func:`fga_reads.reject_stored_clock`), matching the
    all-or-nothing write it would have been.
    """
    fga_reads.reject_stored_clock(tuples)

    async def _do_write() -> None:
        # The cast papers over the SDK's own annotation, not our types: `write`'s `options` is
        # declared `dict[str, int | str | dict[...]]` while its body reads `options["conflict"]` as
        # a ConflictOptions via `options_to_conflict_info` (client.py:538) — the runtime contract
        # admits what the annotation forgot. Narrowing is impossible (the value IS a ConflictOptions).
        conflict = {"conflict": ConflictOptions(on_duplicate_writes=ClientWriteRequestOnDuplicateWrites.IGNORE)}
        await client.write(ClientWriteRequest(writes=tuples), options=cast("dict[str, Any]", conflict))

    await guarded(
        _do_write,
        event="openfga_write_unavailable",
        retry=retry,
    )
    # A duplicate IS the post-condition holding, so it is audited like any other success —
    # otherwise re-running a seed silently erases the provenance of tuples that are present.
    _audit_tuples(AuditAction.ACCESS_TUPLE_WRITE, tuples, actor, origin=origin)


async def grant_on_create(
    client: OpenFgaClient,
    *,
    user_sub: str,
    obj: str,
    actor: str,
    origin: TupleOrigin,
    grant_owner: bool = True,
    parent_object: str | None = None,
    retry: RetryPolicy = DEFAULT_RETRY,
) -> None:
    """Grant the creator ownership of a just-created object, in a single write.

    Always writes ``user:<principal> owner <obj>`` (:func:`user_object` of the creator) so the creator can manage what
    they made. When ``parent_object`` is given it *also* writes ``<obj> parent <parent_object>`` and its inverse, so the
    object inherits reader/writer/owner down the hierarchy (per the model). ``obj`` and ``parent_object`` are the
    objects the grants are held by: a table's or namespace's names its uuid (:func:`table_object`,
    :func:`namespace_object`), a top-level namespace's parent is its warehouse.

    Idempotent (safe to call again on retry of a create): writing existing tuples
    is swallowed. On OpenFGA outage it fails closed with ``ServiceUnavailableError``.
    """
    # `grant_owner=False` writes the hierarchy edge ALONE — for a creator who should not own what it
    # made. The estate's case is a machine: a service principal registering its own output would
    # become that table's owner, keeping drop/deregister/restore/manage_grants on every namespace of every
    # tenant it wrote to (owner ruling 2026-09-10). The object is not left unowned — ownership still descends the
    # parent chain from the project's admin — which is exactly why the edge must still be written, and
    # why this is a flag on the SAME batch rather than a second call: dropping the owner tuple and
    # dropping the edge look identical at the call site and are opposite in effect.
    tuples = [ClientTuple(user=user_object(user_sub), relation="owner", object=obj)] if grant_owner else []
    if parent_object:
        # The `parent` edge and the INVERSE edge with it, in the SAME batch, so the two directions cannot
        # drift. `hierarchy_edge_tuples` owns that pairing for every writer — see its docstring for what
        # happens to an object that gets only the forward half.
        tuples.extend(hierarchy_edge_tuples(child_object=obj, parent_object=parent_object))
    if not tuples:
        # Nothing to write: no owner grant asked for and no parent to link. A root-level object created
        # by a machine has no governance to seed, and an empty batch is a wasted round trip.
        return
    await write_tuples(
        client,
        tuples,
        actor=actor,
        origin=origin,
        retry=retry,
    )


async def grant_before_arrival(
    client: OpenFgaClient, *, user_sub: str, obj: str, actor: str, origin: TupleOrigin, grant_owner: bool, parent_object: str
) -> None:
    """What a namespace holds before the backend records it ([[LH-585]]): the creator's ``owner`` on ``obj`` (unless
    ``grant_owner`` is false, :func:`grant_on_create`) and the ``parent`` edge it inherits through, in one write.

    The inverse ``child`` edge is not written here. It is the one tuple of a link that confers something on ANOTHER
    object: ``visible_below from child`` shows the parent to whoever holds a rung on the child, so one naming an
    object that never came to stand would leave its creator seeing the parent for as long as the parent lives. An
    object is named a child only of the parent it is at rest under, once it stands there (:func:`name_child`).

    Raises:
        ServiceUnavailableError: OpenFGA gave no answer.
    """
    forward, _ = _link_by_direction(obj, parent_object)
    owner = [ClientTuple(user=user_object(user_sub), relation="owner", object=obj)] if grant_owner else []
    await write_tuples(client, [*owner, forward], actor=actor, origin=origin)


def _delete_reported_absent(exc: ApiException) -> bool:
    """True when the SERVER said the tuple was absent — keyed on the structured ``code``, never prose.

    ``.code`` derives from the parsed response body, so this classifies what OpenFGA actually
    reported rather than substring-matching its message text (SKG-01: the prose lists enumerated
    the phrasings someone had seen, and a wording change either swallowed a real failure or turned
    an idempotent revoke into a 503). Probed fact this rests on, recorded because nobody will
    re-derive it: OpenFGA does NOT validate delete tuples against the authorization model, so a
    mis-spelled relation is indistinguishable at the API from a concurrent revoke — which is why the
    honest response is an audit row that claims no removal, not a hard rejection.

    Deliberately NOT ``on_missing_deletes=IGNORE``: this 400 is the only signal the tuple was
    absent, and the audit split in :func:`delete_tuples` needs it.
    """
    return exc.status == 400 and exc.code == ErrorCode.WRITE_FAILED_DUE_TO_INVALID_INPUT


class TupleStore(Protocol):
    """A door's call that changes caller-named tuples: :func:`write_tuples` or :func:`delete_tuples`."""

    async def __call__(self, client: OpenFgaClient, tuples: list[ClientTuple], /, *, actor: str, origin: TupleOrigin) -> object: ...


class RevokeInterruptedError(ServiceUnavailableError):
    """A revoke OpenFGA stopped answering part-way, carrying the tuples it had already removed.

    :func:`delete_tuples` deletes one tuple per write, so an outage can land between two of them, and the
    ones before it are gone whatever the caller does next. A caller reporting what it revoked counts
    ``removed`` too, or its report says less than the store lost. The message is the constant one every fail-closed
    answer carries (`fga_resilience.guarded`).
    """

    #: The tuples the server confirmed deleted before the failure.
    removed: list[ClientTuple]

    def __init__(self, *, removed: list[ClientTuple]) -> None:
        super().__init__("authorization service unavailable")
        self.removed = removed


async def delete_tuples(
    client: OpenFgaClient,
    tuples: list[ClientTuple],
    *,
    actor: str,
    origin: TupleOrigin,
    retry: RetryPolicy = DEFAULT_RETRY,
) -> list[ClientTuple]:
    """Delete relationship tuples (the revoke counterpart of :func:`write_tuples`); return those the server removed.

    Deletes each tuple in its OWN write — NOT one batch. OpenFGA's Write is transactional (all-or-nothing),
    so a single concurrently-absent tuple (the race our read-then-delete opens) in a batched delete would
    400 the WHOLE call and leave EVERY grant in place while looking like success. Per-tuple, one absent
    tuple can't block the rest, and an absent tuple is idempotent success (the post-condition — gone —
    already holds), left out of what this returns. Transient failures are retried; on outage we fail
    closed with :class:`RevokeInterruptedError` (a ``ServiceUnavailableError``) so the caller does not believe a
    revoke succeeded when it did not (stale grants would silently linger).

    A PARTIAL revoke still audits what it removed, and the error carries it. The 503 says the call did not
    finish; it does not say nothing happened, and every tuple already deleted is gone whatever the caller
    does next.
    """
    if not tuples:
        return []

    # The ONE path that does not go through `guarded`, and the reason is the `except` block below:
    # this is the only caller that must READ the failure before failing closed, because a
    # server-reported "already absent" is idempotent SUCCESS here and an outage is not. Folding it in
    # would mean teaching the shared helper a per-caller classifier, which is how the copies started.
    @retrying(retry)
    async def _delete_one(t: ClientTuple) -> None:
        await client.write(ClientWriteRequest(deletes=[t]))

    removed: list[ClientTuple] = []
    absent: list[ClientTuple] = []
    try:
        for t in tuples:
            if await _delete_unless_absent(_delete_one, t, removed=removed):
                removed.append(t)
            else:
                absent.append(t)
    finally:
        # `finally`, NOT after the loop (SKG-02). The loop deletes one tuple per write and raises from
        # INSIDE itself on an outage, so the tuples already removed were gone from OpenFGA the moment
        # the 503 was raised — and auditing only on the success path erased every one of them. The
        # estate then reads a trail saying a revoked grant is still live, which is the more dangerous
        # direction of the two: an operator re-runs nothing because nothing looks undone.
        #
        # AUDIT WHAT THE SERVER CONFIRMED (SKG-01). One blanket row per input tuple would claim a
        # revoke for tuples the server said were never there — so a mis-spelled relation would leave the REAL
        # grant standing while the trail said it was gone. The absent row keeps the attempt visible
        # (an auditor must be able to tell "never attempted" from "attempted, already gone") without
        # asserting a removal that did not happen.
        _audit_tuples(AuditAction.ACCESS_TUPLE_DELETE, removed, actor, origin=origin)
        _audit_tuples(AuditAction.ACCESS_TUPLE_DELETE, absent, actor, origin=origin, reason="tuple_absent")
    return removed


async def _delete_unless_absent(delete_one: Callable[[ClientTuple], Awaitable[None]], t: ClientTuple, *, removed: list[ClientTuple]) -> bool:
    """Delete ``t``: whether the server removed it, False where it was already absent; ``RevokeInterruptedError`` on an outage."""
    try:
        await delete_one(t)
    except FAIL_CLOSED as exc:
        if isinstance(exc, ApiException) and _delete_reported_absent(exc):
            log.debug("openfga_delete_absent_skipped")
            return False
        log.error("openfga_delete_unavailable", exc_info=True)
        raise RevokeInterruptedError(removed=list(removed)) from exc
    return True


async def revoke_undeclared(
    client: OpenFgaClient,
    declared: Collection[ClientTuple],
    *,
    obj: str,
    relation: str,
    own: Collection[str],
    actor: str,
    origin: TupleOrigin,
) -> list[ClientTuple]:
    """Revoke each ``relation`` tuple on ``obj`` naming one of ``own`` that ``declared`` does not hold.

    The release's own accounts hold on ``obj`` exactly the ``relation`` grants the release declares, so one it stopped
    declaring is taken back; a tuple naming any other principal was granted at a door and stays ([[LH-563]]).

    Args:
        client: The OpenFGA client of the store the grants live in.
        declared: The tuples the release declares on ``obj``.
        obj: The object whose grants converge.
        relation: The one relation that converges; every other relation on ``obj`` is left as it is.
        own: The release's own accounts, as users (:func:`user_object`).
        actor: Who the audit records as revoking.
        origin: The revoking site the audit records.

    Returns:
        The tuples the server removed.
    """
    kept = {(t.user, t.relation) for t in declared}
    held = await fga_reads.read_all_tuples(client, obj=obj, relation=relation)
    stale = [found for found in held if found.user in own and (found.user, found.relation) not in kept]
    return await delete_tuples(client, stale, actor=actor, origin=origin)


async def commit_unless_changed(
    client: OpenFgaClient,
    *,
    deletes: list[ClientTuple],
    writes: list[ClientTuple],
    actor: str,
    origin: TupleOrigin,
) -> bool:
    """Apply ``deletes`` and ``writes`` as ONE Write that commits only on the state they were read from; whether it did.

    OpenFGA applies a Write all-or-nothing and, without ``on_duplicate_writes=IGNORE``, refuses it whole when a delete is
    absent or a write already present, answering 400 ``write_failed_due_to_invalid_input`` for either (measured on the
    pinned server's memory datastore 2026-10-25: neither half of a refused Write is applied). On a SQL datastore two
    Writes that overlap in time can instead meet in the database, and the loser's transaction is rolled back and
    answered 409 (gRPC Aborted, "transactional write failed due to conflict" in the pinned v1.18.3). Either way nothing
    committed, so of two writers that read one state and each delete or write the same tuple, one commits and the
    other is told ``False`` and should read again.

    NOT RETRIED. A Write whose answer was lost may have committed, and its retry would then be refused as if another
    writer had won; an outage fails closed with ``ServiceUnavailableError`` instead, an unknown outcome to the caller.
    Every tuple a committed Write changed is audited; a refused one changed nothing.
    """
    fga_reads.reject_stored_clock(writes)
    try:
        await client.write(ClientWriteRequest(writes=writes or None, deletes=deletes or None))
    except FAIL_CLOSED as exc:
        if isinstance(exc, ApiException) and (_delete_reported_absent(exc) or exc.status == HTTPStatus.CONFLICT):
            return False
        log.error("openfga_write_unavailable", exc_info=True)
        raise ServiceUnavailableError("authorization service unavailable") from exc
    _audit_tuples(AuditAction.ACCESS_TUPLE_DELETE, deletes, actor, origin=origin)
    _audit_tuples(AuditAction.ACCESS_TUPLE_WRITE, writes, actor, origin=origin)
    return True


def _link_by_direction(obj: str, parent: str) -> tuple[ClientTuple, list[ClientTuple]]:
    """``obj``'s link to ``parent`` by direction: its ``parent`` edge, and the inverse ``child`` edge where ``parent``'s type takes one."""
    forward, *inverse = hierarchy_edge_tuples(child_object=obj, parent_object=parent)
    return forward, inverse


async def move_parent(client: OpenFgaClient, *, obj: str, source: str, destination: str, actor: str, origin: TupleOrigin) -> bool:
    """Move ``obj``'s ``parent`` edge from ``source`` to ``destination`` in ONE Write; whether it committed.

    The ``parent`` edge is the direction every check inherits through, so it changes whole or not at all: the Write
    deletes the old edge and writes the new, and ``obj`` at no moment inherits from both parents or from neither. It
    commits only while the old edge stands and the new one is absent (:func:`commit_unless_changed`): ``False`` says
    the edge is not as the caller read it, and that nothing changed.

    The inverse ``child`` edges are not moved here. A removal of a parent ends every object its ``child`` edges name,
    so an object is named a child only of the parent it is at rest under: its mover takes the old edge away once this
    move has committed and writes the new one after the object has arrived (:func:`unname_child`,
    :func:`name_child`).

    Raises:
        ServiceUnavailableError: OpenFGA gave no answer, so whether the Write committed is unknown to the caller.
    """
    left, _ = _link_by_direction(obj, source)
    entered, _ = _link_by_direction(obj, destination)
    return await commit_unless_changed(client, deletes=[left], writes=[entered], actor=actor, origin=origin)


async def leave_parent(client: OpenFgaClient, *, obj: str, parent: str, actor: str, origin: TupleOrigin) -> bool:
    """Delete ``obj``'s ``parent`` edge to ``parent`` in ONE Write; whether it committed.

    For an object found inheriting from two parents, the one it is leaving and the one it is entering: the edge to
    ``parent`` goes and the other stands, so ``obj`` inherits from the parent it enters and at no moment from neither.
    It commits only while the edge stands (:func:`commit_unless_changed`): ``False`` says the edge is not as the caller
    read it, and that nothing changed. The inverse ``child`` edge is its mover's to take away (:func:`unname_child`).

    Raises:
        ServiceUnavailableError: OpenFGA gave no answer, so whether the Write committed is unknown to the caller.
    """
    left, _ = _link_by_direction(obj, parent)
    return await commit_unless_changed(client, deletes=[left], writes=[], actor=actor, origin=origin)


async def name_child(client: OpenFgaClient, *, obj: str, parent: str, actor: str, origin: TupleOrigin) -> None:
    """Write the inverse edge that names ``obj`` a child of ``parent``, where ``parent``'s type takes one; an edge
    already there is left as it is (:func:`write_tuples`).

    Raises:
        ServiceUnavailableError: OpenFGA gave no answer.
    """
    _, inverse = _link_by_direction(obj, parent)
    if inverse:
        await write_tuples(client, inverse, actor=actor, origin=origin)


async def unname_child(client: OpenFgaClient, *, obj: str, parent: str, actor: str, origin: TupleOrigin) -> None:
    """Delete the inverse edge that names ``obj`` a child of ``parent``; an edge already absent is left absent
    (:func:`delete_tuples`).

    Raises:
        ServiceUnavailableError: OpenFGA gave no answer.
    """
    _, inverse = _link_by_direction(obj, parent)
    await delete_tuples(client, inverse, actor=actor, origin=origin)


async def revoke_object_tuples(
    client: OpenFgaClient,
    obj: str,
    *,
    actor: str,
    origin: TupleOrigin,
    retry: RetryPolicy = DEFAULT_RETRY,
) -> list[ClientTuple]:
    """Delete EVERY tuple on ``obj`` — the revoke-on-delete cleanup. Returns WHAT the server removed.

    **The tuples, not a count, and the difference is who gets told.** Every destructive door in the
    estate ends here — a project delete, a warehouse delete, a cascading namespace drop, an overwrite
    create — and each revokes every grant on the object. Returning an integer meant the estate knew
    that fourteen grants vanished and not whose, so the subjects who could no longer see their own
    data learned it by hitting a 403 mid-task: the failure `grant_revoked` exists to prevent. The
    tuples were already read in order to be deleted, so handing them back costs nothing and is what
    lets a caller name the affected principals. Callers wanting the old number take ``len(...)``.

    A dropped/renamed object's grants (owner, the parent edge, AND any later reader or writer
    grants) must not linger: if the id is later reused, those stale tuples would
    silently re-grant the old subjects on the NEW object (stale-grant privilege bleed). Reads the
    full tuple set, then deletes it. No-op (0) when the object has none.

    Scope: revokes the object's OWN tuples and its inverse ``child`` edge. A cascade that removes a subtree
    revokes each object in it through the catalog's ``object_grants.end_removed_subtree``, which finds them by
    those ``child`` edges. An outage part-way raises :class:`RevokeInterruptedError` naming the tuples removed.
    """
    tuples = await fga_reads.read_object_tuples(client, obj, retry=retry)
    if not tuples:
        return []
    # The inverse `child` edge is the ONE tuple about this object that a read by object cannot see:
    # `grant_on_create` stores it as `<obj> child <parent>`, so its OBJECT is the parent and it
    # survives a revoke that only reads `object == obj`. Left behind it is a phantom child — the
    # access graph renders an edge to an object that no longer exists, and the drop looks incomplete.
    # The parent is recoverable from what was just read: the `parent` edge is `<parent> parent <obj>`,
    # so its USER is the parent object. Deleting an absent tuple is swallowed by `delete_tuples`, so
    # this is safe for objects created before the edge existed and for types that never take one.
    doomed = list(tuples)
    parent = next((t.user for t in tuples if t.relation == "parent" and t.user), None)
    if parent and parent.split(":", 1)[0] in CHILD_EDGE_PARENT_TYPES:
        doomed.append(ClientTuple(user=obj, relation="child", object=parent))
    return await delete_tuples(
        client,
        doomed,
        actor=actor,
        origin=origin,
        retry=retry,
    )


__all__ = [
    "ClientTuple",
    "RelationshipCondition",
    "RevokeInterruptedError",
    "TupleOrigin",
    "TupleStore",
    "commit_unless_changed",
    "delete_tuples",
    "grant_before_arrival",
    "grant_on_create",
    "leave_parent",
    "move_parent",
    "name_child",
    "revoke_object_tuples",
    "revoke_undeclared",
    "unname_child",
    "write_tuples",
]
