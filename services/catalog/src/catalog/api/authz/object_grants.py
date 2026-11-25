"""An object's grants over its life, from its create to its removal."""

import asyncio
import logging
from collections.abc import AsyncIterator, Awaitable, Callable, Coroutine, Sequence
from functools import partial
from typing import Any, Literal

import anyio.from_thread
from fastapi.concurrency import run_in_threadpool
from lance_namespace import ConcurrentModificationError, InvalidInputError, LanceNamespace, ServiceUnavailableError
from openfga_sdk import OpenFgaClient

from catalog.api.authz.context import AuthzContext
from catalog.api.authz.decisions import Authorization, authorization, wired
from catalog.api.authz.objects import GovernedObject, namespace_parent, owned_object, parent_object_of, trashed_objects
from catalog.api.authz.rungs import moves_namespace
from catalog.api.dependencies import Resolver
from catalog.api.security import Caller
from catalog.core.config import Settings
from catalog.services import object_uuids
from catalog.services.tables import creation, rename
from lakehouse.governed import fga_ids, fga_reads, fga_writes
from lakehouse.governed.door_caller import caller_subject
from lakehouse.governed.machine_identity import ServicePrincipal
from lakehouse.governed.settings import FgaSettings
from lakehouse.governed.subjects import is_person_subject
from lakehouse.problems.ns_errors import PartiallyAppliedError
from lakehouse.records import object_records


__all__ = [
    "GrantsBeforeWrite",
    "ParentChangedError",
    "end_removed_subtree",
    "name_namespace_child",
    "orphaned_children",
    "parent_edge_mover",
    "project_admin_for",
    "require_parent_as_authorized",
    "revoke_expired_object",
    "revoke_ghost_object",
    "revoke_ownership",
    "revoke_removed_object",
    "seed_before_write",
    "seed_namespace_before_write",
    "seed_ownership",
    "seed_project_admin",
    "take_back_seed",
    "take_back_unless_namespace_stands",
    "undo_a_create_whose_parent_is_unread",
    "undo_then_take_back",
    "write_registry_edges",
]


log = logging.getLogger(__name__)


def project_admin_for(token: Caller | None, named: str | None) -> str | None:
    """The subject seeded as admin of a project ``token`` creates: the person creating it, or the person
    a machine names. ``None`` only for an unauthenticated stack, where nothing is seeded.

    A MACHINE NEVER ADMINISTERS A TENANT (LH-433, the 2026-09-10 ruling ``seed_ownership`` applies to
    tables). A project has no parent to own it transitively, so a machine-created project would be left
    with no admin; the machine names the person instead (ADR 0143's provisioning operator knows the
    tenant's owner). The name must be a person: not a userset or wildcard, and not a service account's subject
    (``is_person_subject``). The caller's own type is judged by how it was proved, never by its name.
    """
    if not isinstance(token, ServicePrincipal):
        if named is not None:
            raise InvalidInputError("`admin` is named only by a machine creating a project; a person creating one is its admin")
        return token.subject if token is not None else None
    if named is None:
        raise InvalidInputError("a machine creating a project names the person who administers it in `admin`")
    if not is_person_subject(named):
        raise InvalidInputError(f"`admin` must name a person; {named!r} is not one")
    return named


async def seed_project_admin(
    client: OpenFgaClient | None,
    settings: FgaSettings,
    actor: str | None,
    *,
    project: str,
    admin: str | None,
) -> bool:
    """Grant ``admin`` (from :func:`project_admin_for`) on the new ``project:<id>`` — the tuple that makes
    a tenant self-sustaining (every later warehouse/namespace/table grant cascades from it). ``actor``,
    the caller or the declaration, is recorded as its writer.

    Written by ``POST /management/v1/projects`` in the same operation as the registry record, which is the whole
    point of Decision 1: existence and permission are minted together and cannot drift. Idempotent —
    ``write_tuples`` swallows duplicate writes on a create retry. Returns True when the tuple was
    actually written, so the caller audits a grant that really happened (never one an FGA-off stack
    silently skipped)."""
    if not (settings.fga_enabled and actor is not None and client is not None and admin is not None):
        return False
    await fga_writes.write_tuples(  # noqa: TID251
        client,
        [fga_writes.ClientTuple(user=fga_ids.user_object(admin), relation="admin", object=f"project:{project}")],
        actor=actor,
        origin="project_create",
    )
    return True


async def seed_ownership(
    context: AuthzContext,
    *,
    resolver: Resolver,
    resource: str,
    segments: list[str],
    uuid: str,
) -> None:
    """Grant the creator ``owner`` and the link to its parent on the table ``table:<uuid>`` its door brings into being.

    The object is keyed by the uuid the create minted (ADR 0173 §8, [[LH-524]]), so no tuple an earlier object at the
    same name left can reach it: a table dropped and created again under that name is another uuid.

    FOR AN OBJECT THIS REQUEST BRINGS INTO BEING, and for no other: granting ``owner`` on a table the request only
    found (an ExistOk that kept it, its owner's Overwrite) would seize another caller's table. A table's door seeds it
    before the table is written (:func:`seed_before_write`, [[LH-588]]), and a door that finds the table standing writes
    no grant.

    No-op when FGA is off or the request is unauthenticated, and a 503 when FGA is on with no client wired, so a
    door calls it unconditionally with one line. Who owns what a create makes is decided here and in
    :func:`seed_namespace_before_write`, and nowhere else. The ``parent`` edge names :func:`parent_object_of`.
    """
    if context.token is None or (client := wired(context.settings, context.client)) is None:
        return
    # A MACHINE DOES NOT OWN WHAT IT REGISTERS (owner ruling 2026-09-10, zero trust). A service principal
    # registering its own output would become that table's owner, keeping `can_drop`, `can_deregister`,
    # `can_restore` and `manage_grants` on every governed table of every tenant it ever wrote to.
    #
    # THE TABLE IS NOT LEFT UNOWNED. `warehouse.owner: … or admin from project` →
    # `namespace.owner: … or owner from parent` → `table.owner: … or owner from parent`, so the
    # project's admin already owns it transitively — which is what makes this a withdrawal rather than
    # an orphaning, and is why the parent edge below is still written unconditionally. Pinned against
    # the real evaluator in `model.fga.yaml`, not argued from the model text.
    #
    # THE PRINCIPAL'S TYPE IS THE DISCRIMINATOR, never a name pattern: a verifier builds a `ServicePrincipal`
    # only for a machine it proved, a service account or a Dex client signed in for itself (ADR 0181), so the
    # question is answered by how the caller was proved, not by what its subject is called — a name test
    # drifts the moment a service is renamed.
    await fga_writes.grant_on_create(  # noqa: TID251
        client,
        owner=None if isinstance(context.token, ServicePrincipal) else context.token.subject,
        obj=owned_object(resource, uuid),
        actor=context.token.subject,
        origin="create",
        parent_object=await parent_object_of(resolver, context.settings, resource, segments=segments),
    )


async def seed_before_write(context: AuthzContext, *, resolver: Resolver, segments: list[str], uuid: str) -> None:
    """:func:`seed_ownership` for the table ``uuid`` its door minted and is about to write: its grants stand before the
    table does ([[LH-588]]), so no table stands without them.

    A process killed between the two writes leaves the grants alone, which the drift repair revokes as a ghost's once
    nothing can still record a table under their uuid (:func:`catalog.services.grant_reconcile.claim_refusal`). A seed
    that fails is taken back before its error is raised, so a grant whose answer OpenFGA lost leaves nothing either.
    """
    try:
        await seed_ownership(context, resolver=resolver, resource="table", segments=segments, uuid=uuid)
    except Exception:
        await take_back_seed(context, segments=segments, uuid=uuid, reason="grant_failed")
        raise


async def seed_namespace_before_write(context: AuthzContext, *, resolver: Resolver, segments: list[str], uuid: str) -> None:
    """Grant the creator ``owner`` of the namespace ``uuid`` its door minted and is about to write, and the ``parent``
    edge it inherits through, so no namespace stands without them ([[LH-585]]).

    Every ``namespace`` relation resolves through a direct tuple or ``from parent``, so one standing with no tuple is
    invisible to its creator and its project's admin alike, while its name refuses every retry. A machine owns no
    namespace it creates (:func:`seed_ownership`). The inverse ``child`` edge waits until the namespace stands
    (:func:`name_namespace_child`): it is the one tuple that confers something on the parent's side, so what a door
    killed between its two writes leaves behind, on a uuid no object holds, grants nothing to anyone. A seed that fails
    is taken back before its error is raised.
    """
    if context.token is None or (client := wired(context.settings, context.client)) is None:
        return
    try:
        await fga_writes.grant_before_arrival(  # noqa: TID251
            client,
            owner=None if isinstance(context.token, ServicePrincipal) else context.token.subject,
            obj=fga_ids.namespace_object(uuid),
            actor=context.token.subject,
            origin="create",
            parent_object=await namespace_parent(resolver, context.settings, segments),
        )
    except Exception:
        await take_back_seed(
            AuthzContext(client=client, settings=context.settings, token=context.token),
            segments=segments,
            uuid=uuid,
            reason="grant_failed",
            resource="namespace",
        )
        raise


async def name_namespace_child(context: AuthzContext, *, resolver: Resolver, segments: list[str], uuid: str) -> None:
    """Name the namespace ``uuid`` a child of its parent, now that it stands there: the end of its create.

    The namespace exists by now, owned and inheriting, so an outage here is logged at ERROR and raised over nothing:
    it is then named a child of no parent, which costs whoever holds a rung only beneath it the sight of the namespaces
    above, and which its parent's listing covers for a removal.
    """
    if context.token is None or (client := wired(context.settings, context.client)) is None:
        return
    obj = fga_ids.namespace_object(uuid)
    try:
        await fga_writes.name_child(  # noqa: TID251
            client, obj=obj, parent=await namespace_parent(resolver, context.settings, segments), actor=context.token.subject, origin="create"
        )
    except ServiceUnavailableError as exc:
        log.error("namespace_arrival_not_settled", extra={"object": obj, "name": fga_ids.canonical_object_id(segments), "error": str(exc)})


async def take_back_seed(
    context: AuthzContext,
    *,
    segments: list[str],
    uuid: str,
    reason: str,
    resource: Literal["table", "namespace"] = "table",
) -> None:
    """Revoke what :func:`seed_before_write` wrote for the ``resource`` ``uuid``, whose door wrote no object under it.

    Best effort, and never raised: the caller is answering the failure that brought it here. A table's grants left
    behind are a ghost's, which the drift repair revokes; a namespace's name a uuid no object holds and no ``child``
    edge, so they grant nothing.
    """
    try:
        await revoke_ownership(context, obj=owned_object(resource, uuid), origin="create")
    except Exception as exc:  # noqa: BLE001 — the door's own failure is what the caller is told
        log.warning("seed_not_taken_back", extra={"object": fga_ids.canonical_object_id(segments), "uuid": uuid, "reason": reason, "error": str(exc)[:300]})


async def take_back_unless_namespace_stands(ns: LanceNamespace, context: AuthzContext, *, segments: list[str], uuid: str, reason: str) -> None:
    """Take back the grants written for the namespace ``uuid``, whose create door is failing or kept another namespace,
    unless the namespace stands under ``segments``: a create whose answer the store lost may have landed, and a namespace
    never stands without its grants ([[LH-585]]). An answer that cannot be read is read on the side that keeps them.
    """
    try:
        standing = await run_in_threadpool(object_uuids.resolve_namespace_uuid, ns, segments)
    except Exception as exc:  # noqa: BLE001 — an unreadable answer may be the namespace standing
        log.warning("namespace_standing_unread", extra={"namespace": fga_ids.canonical_object_id(segments), "uuid": uuid, "error": str(exc)[:300]})
        return
    if standing != uuid:
        await take_back_seed(context, segments=segments, uuid=uuid, reason=reason, resource="namespace")


class ParentChangedError(ConcurrentModificationError):
    """A create's parent namespace was dropped or replaced between the request's authorization and its write.

    A conflict of its own kind, so a door that reads a conflict on its table as a race it lost to another create of the
    same name (an ExistOk) never reads this one so: the table such a door would keep is in a namespace its caller was
    not judged on.
    """


async def undo_a_create_whose_parent_is_unread(
    context: AuthzContext,
    *,
    resource: Literal["table", "namespace"],
    segments: list[str],
    uuid: str,
    undo: Callable[[], Awaitable[None]],
    remedy: str,
) -> None:
    """Take back the ``resource`` a door recorded and could not confirm in the namespace it was authorized on
    (:func:`require_parent_as_authorized` refused it, or could not read the parent), for the door to raise what stopped it.

    Raises:
        PartiallyAppliedError: The undo failed, so the object stands, with its grants, where nobody confirmed it belongs: a
            retry would meet it as already there, so the answer names it and its remedy.
    """
    if await undo_then_take_back(context, segments=segments, uuid=uuid, undo=undo, reason="parent_unconfirmed", resource=resource):
        return
    name = fga_ids.canonical_object_id(segments)
    raise PartiallyAppliedError(
        f"the {resource} {name} was recorded, its parent namespace could not be confirmed as the one authorized, and it could not be removed",
        extensions={resource: name, "recorded": True, "remedy": remedy},
    )


async def require_parent_as_authorized(
    ns: LanceNamespace, settings: FgaSettings, client: OpenFgaClient | None, *, resolver: Resolver, resource: str, segments: list[str]
) -> None:
    """Refuse a create whose parent namespace is not the one the request was authorized on, asked once the backend
    has recorded the new object ([[LH-585]]).

    The router authorizes a create on its parent's uuid and the backend records the object by name, so a namespace
    dropped and created again between the two would receive an object its caller was never judged on, hanging off a
    ``parent`` edge to a namespace that is gone. The reading is made after the write because a namespace holding the
    new object is not dropped without it, so no drop can follow the reading. A parent that is a warehouse is its
    id, which no re-create changes. With FGA off nothing was authorized, and nothing is asked.

    Raises:
        ParentChangedError: The parent's name holds another namespace, or none; the caller's door undoes what it
            recorded, as it does when the parent cannot be read at all.
    """
    parent = segments[:-1]
    if wired(settings, client) is None or len(parent) < 2:
        return
    authorized = await resolver.namespace_uuid(parent)
    standing = await run_in_threadpool(object_uuids.resolve_namespace_uuid, ns, parent)
    if standing != authorized:
        log.warning(
            "create_refused_parent_changed",
            extra={"object": fga_ids.canonical_object_id(segments), "resource": resource, "authorized": authorized, "standing": standing},
        )
        raise ParentChangedError(
            f"namespace {fga_ids.canonical_object_id(parent)} was dropped or replaced while {resource} {fga_ids.canonical_object_id(segments)} was being created; retry"
        )


async def undo_then_take_back(
    context: AuthzContext,
    *,
    segments: list[str],
    uuid: str,
    undo: Callable[[], Awaitable[None]],
    reason: str,
    resource: Literal["table", "namespace"] = "table",
) -> bool:
    """Undo the ``resource`` ``uuid`` its door brought into being and is about to fail for, then take its grants back;
    whether the undo went through.

    The object first, its grants once it is gone ([[LH-588]]): an undo that fails leaves the object standing, and it
    keeps its grants, so it never stands without them; the failure is logged for the operator and answered ``False``.
    Never raised: the caller is answering the failure that brought it here.
    """
    name = fga_ids.canonical_object_id(segments)
    try:
        await undo()
    except Exception as undo_exc:  # noqa: BLE001 — the door's own failure is what the caller is told
        log.error("create_compensation_failed", extra={"object": name, "resource": resource, "reason": reason, "error": str(undo_exc)[:300]})
        return False
    log.warning("create_compensated", extra={"object": name, "resource": resource, "reason": reason})
    await take_back_seed(context, segments=segments, uuid=uuid, reason=reason, resource=resource)
    return True


class GrantsBeforeWrite:
    """The ``before_register`` a create or declare door hands :func:`tables.creation.declare_table`: called on the backend
    write's worker thread with the uuid the declaration minted and claimed and the location it claimed, it writes the
    table's grants on the request's event loop (:func:`seed_before_write`) and remembers whose they are and where that
    table lives. :meth:`parent_as_authorized` is the door's ``after_register``.
    """

    def __init__(self, context: AuthzContext, *, resolver: Resolver, segments: list[str]) -> None:
        self._context = context
        self._resolver = resolver
        self._segments = segments
        #: The table the grants were written for, and its location, once they were.
        self.uuid: str | None = None
        self._location: str | None = None

    def __call__(self, uuid: str, location: str) -> None:
        anyio.from_thread.run(
            partial(
                seed_before_write,
                self._context,
                resolver=self._resolver,
                segments=self._segments,
                uuid=uuid,
            )
        )
        self.uuid, self._location = uuid, location

    def parent_as_authorized(self, ns: LanceNamespace) -> None:
        """:func:`require_parent_as_authorized` for the table, from the backend write's worker thread."""
        anyio.from_thread.run(
            partial(
                require_parent_as_authorized,
                ns,
                self._context.settings,
                self._context.client,
                resolver=self._resolver,
                resource="table",
                segments=self._segments,
            )
        )

    async def take_back(self, *, reason: str) -> None:
        """Take back the grants written, the door having undone the table they were written for."""
        if self.uuid is not None:
            await take_back_seed(self._context, segments=self._segments, uuid=self.uuid, reason=reason)

    async def take_back_unless_kept(self, kept: str | None) -> None:
        """Take back the grants written for a table the door did not keep: ``kept`` is the uuid of the table the door
        answers for, another table's when it found one standing under the name."""
        if self.uuid != kept:
            await self.take_back(reason="not_kept")

    async def take_back_unless_standing(self, ns: LanceNamespace) -> None:
        """Take back the grants written for a table that does not stand, its door having failed. One that stands, its
        door having failed after the backend recorded it and its undo having failed too, keeps them, so it never stands
        without them."""
        if self._location is None or await run_in_threadpool(creation.stands_at, ns, self._segments, self._location):
            return
        await self.take_back(reason="not_written")


async def _parents_of(authz: Authorization, obj: str) -> list[str]:
    """The objects ``obj``'s ``parent`` edges name, sorted: one for an object any door left."""
    return sorted(held.user for held in await fga_reads.read_object_tuples(authz.client, obj) if held.relation == "parent")


async def _leave_source(authz: Authorization, *, obj: str, source: str, destination: str) -> None:
    """Move ``obj``'s ``parent`` edge from namespace ``source`` to ``destination``, before its name moves ([[LH-568]]).

    Every grant stays on ``obj``, the table's uuid ([[LH-524]]). Only where the table inherits from moves, as it would
    for a table created in ``destination``, and that edge moves in one Write (``fga_writes.move_parent``): the table at no
    moment inherits from both namespaces or from neither. Its ``child`` edge on ``source`` is then taken away, and none
    is written on ``destination`` until the table is named there (:func:`_arrive_at_destination`): a namespace's
    removal ends every object its ``child`` edges name (:func:`end_removed_subtree`), and a table between its two names
    is in neither namespace's listing, so one named a child of either could be ended while it lives.

    A move that does not commit found the edge elsewhere than ``source`` alone, and is settled on what stands
    (:func:`_settle_an_unmoved_edge`): an earlier move into ``destination`` that went no further is finished here, and
    anything else refuses the rename before it moves the table. When OpenFGA gives no answer at any step, whether a
    Write committed is unknown, so both edges are returned to ``source`` before the outage is answered, as far as
    OpenFGA then answers (:func:`_return_to_source`).

    Raises:
        ConcurrentModificationError: ``obj``'s parent edge did not move and no earlier move into ``destination`` explains it.
        ServiceUnavailableError: OpenFGA gave no answer.
    """
    try:
        if not await fga_writes.move_parent(authz.client, obj=obj, source=source, destination=destination, actor=authz.subject, origin="rename"):  # noqa: TID251
            await _settle_an_unmoved_edge(authz, obj=obj, source=source, destination=destination)
        await fga_writes.unname_child(authz.client, obj=obj, parent=source, actor=authz.subject, origin="rename")  # noqa: TID251
    except ServiceUnavailableError:
        await _return_to_source(authz, obj=obj, source=source, destination=destination)
        raise


async def _settle_an_unmoved_edge(authz: Authorization, *, obj: str, source: str, destination: str) -> None:
    """Settle where ``obj`` inherits from when its move from ``source`` to ``destination`` did not commit, by what stands.

    - Its ``parent`` edge names ``destination`` alone: an earlier rename moved it and went no further. It stands.
    - It names ``source`` and ``destination`` both ([[LH-586]]): the table inherits from the namespace it is leaving
      beside the one it is entering, which no door leaves. Its mover holds what a move asks on both ends, and taking
      the edge to ``source`` away admits nobody the move would not, so the move is finished rather than left for an
      operator's hands on the store.
    - Anything else is an edge where no move into ``destination`` left it, or another Write that met this one in the
      store: nothing is written, and the rename is refused.

    Raises:
        ConcurrentModificationError: ``obj``'s parent edge is not one an earlier move into ``destination`` left.
        ServiceUnavailableError: OpenFGA gave no answer.
    """
    parents = await _parents_of(authz, obj)
    if parents == [destination]:
        return
    inherits_from_both = parents == sorted([source, destination])
    if inherits_from_both and await fga_writes.leave_parent(authz.client, obj=obj, parent=source, actor=authz.subject, origin="rename"):  # noqa: TID251
        return
    log.error("rename_parent_edge_not_moved", extra={"object": obj, "source": source, "destination": destination, "parents": parents})
    raise ConcurrentModificationError(
        "cannot rename the table into another namespace: where it inherits its grants from did not move, so nothing else did. "
        "It is not the namespace its name is in, or it changed as it was moved. A rename of it that went no further is finished "
        "by renaming it into that rename's namespace"
    )


async def _arrive_at_destination(authz: Authorization, *, obj: str, destination: str) -> None:
    """Name ``obj`` a child of namespace ``destination`` once its name is registered there: the end of its move.

    The rename still holds the table's claim fenced ([[LH-586]]), so between the registration and this step no other
    rename moves the table on, no drop ends it and no cascade removes it: its ``parent`` edge is where
    :func:`_leave_source` put it, and the ``child`` edge written here names a table that lives in ``destination``.
    A removal of the namespace the table left ends only what it removed itself, so it takes no tuple of this table.

    The rename has happened by now, so an outage here is logged at ERROR and raised over nothing: the table is then
    named a child of no namespace, which only a removal's recovery walk reads and the namespace's listing covers.
    """
    try:
        await fga_writes.name_child(authz.client, obj=obj, parent=destination, actor=authz.subject, origin="rename")  # noqa: TID251
    except ServiceUnavailableError as exc:
        log.error("rename_arrival_not_settled", extra={"object": obj, "parent": destination, "error": str(exc)})


async def _return_to_source(authz: Authorization, *, obj: str, source: str, destination: str) -> None:
    """Return ``obj``'s ``parent`` edge to namespace ``source`` and name it a child there again: the undo of
    :func:`_leave_source`, for a rename whose name stayed in ``source``.

    It moves the edge back when the move committed and leaves it when it never did, so it is also what follows a move
    whose answer was lost. What it cannot put back, it cannot answer to anyone either, since the rename's own refusal
    is the caller's answer: an edge that names neither namespace, or an OpenFGA that gives no answer, is logged at
    ERROR. The table then inherits from ``destination`` under a name in ``source``, which shuts ``source``'s readers
    out, admits nobody the move did not, and is finished by renaming the table into ``destination`` again.
    """
    try:
        if not await fga_writes.move_parent(authz.client, obj=obj, source=destination, destination=source, actor=authz.subject, origin="rename"):  # noqa: TID251
            parents = await _parents_of(authz, obj)
            if parents != [source]:
                log.error("rename_parent_edge_not_restored", extra={"object": obj, "source": source, "destination": destination, "parents": parents})
                return
        await fga_writes.name_child(authz.client, obj=obj, parent=source, actor=authz.subject, origin="rename")  # noqa: TID251
    except ServiceUnavailableError as exc:
        log.error("rename_parent_edge_not_restored", extra={"object": obj, "source": source, "destination": destination, "error": str(exc)})


async def parent_edge_mover(context: AuthzContext, *, resolver: Resolver, obj: str, source: list[str], destination: list[str]) -> rename.Reparent | None:
    """How a rename of table ``source`` to ``destination`` takes ``obj``'s parent edge along, or ``None`` when it has
    none to take: the rename keeps its namespace, or FGA is off (:func:`authorization`).

    A rename into another namespace moves its table while it holds the location's claim, in a thread
    (``tables.rename.rename_table``), and its parent edge moves there too, so the name and where the table inherits from
    change under one arbiter: until the destination is registered no other rename moves this table's edge, and an
    undone rename has returned the edge before the claim is free ([[LH-568]]). The arrival runs once the name is
    registered, while the rename still holds the claim fenced (:func:`_arrive_at_destination`). Each step runs on the
    event loop this was built on, the request's, and the thread waits for it.

    Raises:
        NamespaceNotFoundError: A namespace the rename names is gone.
        ServiceUnavailableError: FGA is on and unwired.
        UnauthenticatedError: FGA is on and nobody is signed in.
    """
    if not moves_namespace(source, destination) or (authz := authorization(context)) is None:
        return None
    left = await parent_object_of(resolver, context.settings, "table", segments=source)
    entered = await parent_object_of(resolver, context.settings, "table", segments=destination)
    if left is None or entered is None:
        return None
    loop = asyncio.get_running_loop()

    def waited(step: Callable[[], Coroutine[Any, Any, None]]) -> Callable[[], None]:
        return lambda: asyncio.run_coroutine_threadsafe(step(), loop).result()

    return rename.Reparent(
        leave=waited(partial(_leave_source, authz, obj=obj, source=left, destination=entered)),
        arrive=waited(partial(_arrive_at_destination, authz, obj=obj, destination=entered)),
        put_back=waited(partial(_return_to_source, authz, obj=obj, source=left, destination=entered)),
    )


async def revoke_ownership(context: AuthzContext, *, obj: str, origin: fga_writes.TupleOrigin) -> None:
    """Delete every FGA tuple on ``obj`` (the revoke counterpart of :func:`seed_ownership`), recorded as ``origin``:
    ``lifecycle_delete`` for an object a door dropped or detached, ``create`` for a create taking back the grants it
    wrote. The trail is the only record of why a grant went, so the two are never one label ([[LH-585]]).

    No-op when FGA is off, and a 503 when it is on with no client wired, so the grants are never silently kept.
    Removes the owner grant, the ``parent`` edge, AND any later reader or writer grants. Needs no token: the
    mutation was already authorized by :func:`authorize`, and the cleanup deletes every subject's tuple regardless of
    who.

    Revoke runs AFTER the (irreversible) native mutation, so on an OpenFGA outage it fails closed (503)
    with the tuples left stale until reconciled — the mutation itself already committed. The object is a uuid no
    other object will hold, so a stale tuple grants nothing on anything created later.
    """
    if (client := wired(context.settings, context.client)) is None:
        return
    # The revoked tuples' writer is the caller who dropped the object.
    actor = caller_subject(context.token)
    removed = await fga_writes.revoke_object_tuples(client, obj, actor=actor, origin=origin)  # noqa: TID251
    if removed:
        log.info("fga_tuples_revoked", extra={"object": obj, "removed": len(removed)})


async def revoke_removed_object(
    context: AuthzContext,
    obj: str,
) -> int:
    """Delete every tuple on ``obj``, an object a lifecycle delete removed, and return how many the server removed.

    Distinct from :func:`revoke_ownership` in two ways that matter to a cascade: the audit origin is
    ``lifecycle_delete`` (a deletion of the hierarchy, not one object's drop), and the count comes back for the door to
    report. 0 when FGA is off; a 503 when it is on with no client wired. An outage part-way raises :class:`fga_writes.RevokeInterruptedError` naming what went.
    """
    if (client := wired(context.settings, context.client)) is None:
        return 0
    actor = caller_subject(context.token)
    removed = await fga_writes.revoke_object_tuples(client, obj, actor=actor, origin="lifecycle_delete")  # noqa: TID251
    if removed:
        log.info("fga_tuples_revoked", extra={"object": obj, "removed": len(removed)})
    return len(removed)


async def revoke_ghost_object(
    context: AuthzContext,
    obj: str,
) -> int:
    """Delete every tuple on ``obj``, an object no registry names ([[LH-061]]'s repair half), and return how many went.

    The origin is ``drift_repair``: nothing was destroyed, the object was already gone, and the revoke is justified only
    by the drift finding the catalog judged again on its own reads. 0 when FGA is off; a 503 when it is on with no
    client wired.
    """
    if (client := wired(context.settings, context.client)) is None:
        return 0
    actor = caller_subject(context.token)
    return len(await fga_writes.revoke_object_tuples(client, obj, actor=actor, origin="drift_repair"))  # noqa: TID251


async def write_registry_edges(
    context: AuthzContext,
    edges: list[fga_writes.ClientTuple],
) -> None:
    """Write ``edges``, structural tuples a registry record justifies, under the ``registry_rebuild`` origin ([[LH-061]]):
    no person granted them, they were reconstructed from the record. A no-op when FGA is off; a 503 when it is on
    with no client wired."""
    if (client := wired(context.settings, context.client)) is None or not edges:
        return
    actor = caller_subject(context.token)
    await fga_writes.write_tuples(client, edges, actor=actor, origin="registry_rebuild")  # noqa: TID251


async def revoke_expired_object(
    context: AuthzContext,
    obj: str,
) -> list[fga_writes.ClientTuple]:
    """Delete every tuple on ``obj``, a trashed object whose deadline has passed, and return what the server removed.

    The origin is ``trash_expired`` (ADR 0146): nobody revoked them and nothing was destroyed, the grace period ended
    and the bytes wait for the purge. The tuples come back so the door can tell each person who lost one. Empty when
    FGA is off; a 503 when it is on with no client wired. An outage part-way raises :class:`fga_writes.RevokeInterruptedError` naming what went.
    """
    if (client := wired(context.settings, context.client)) is None:
        return []
    actor = caller_subject(context.token)
    removed = await fga_writes.revoke_object_tuples(client, obj, actor=actor, origin="trash_expired")  # noqa: TID251
    if removed:
        log.info("fga_tuples_revoked", extra={"object": obj, "removed": len(removed)})
    return removed


async def end_removed_subtree(
    context: AuthzContext,
    *,
    root_uuid: str | None,
    removed: Sequence[GovernedObject],
) -> AsyncIterator[int]:
    """End what the namespace ``root_uuid`` and ``removed``, which a lifecycle delete removed, still hold, children first.

    Each object's protection and maintenance-policy records go, keyed by its uuid, then its grants. Yields each
    object's revoked count as its revoke lands, so a door reporting what it revoked counts every object revoked before a
    failure. Run ONLY ONCE EVERY OBJECT IN THE SUBTREE IS GONE ([[LH-495]]): a grant revoked before its object is
    removed strands the object, alive and with nobody left who may read, drop or recover it through a door.

    THE GRANTS GO LAST, BECAUSE THEY ARE WHAT FINDS AN OBJECT AGAIN. Every object created under a namespace leaves a
    ``child`` edge on it (``fga_model.hierarchy_edge_tuples``), and only that object's own revoke removes the edge. A
    re-issue lists only what still stands, so an edge still standing is what names an object a failure stopped before
    its end, and this walk ends it then. The edge names the object by its uuid ([[LH-524]]), which is the key its
    records are kept under, so an object found only by its edge loses its records as well as its grants. Each edge is
    followed before the object holding it is revoked. ``removed`` (the catalog's own listing) covers an object created
    before the edges existed; with FGA off nothing names an object once it is gone, and only the listed ones are ended.
    Every clear is idempotent, so a protection record is cleared without asking whether one exists: an object a forced
    attempt left unfinished may be re-issued without ``force``. A root whose uuid nothing records any more
    (``root_uuid`` is ``None``) has its children ended from ``removed`` alone.

    AN OBJECT IN THE TRASH KEEPS ITS RECORDS AND GRANTS. A recoverable drop keeps them for its undrop (#75), and a
    trashed table is undropped into its namespace once that namespace is created again, so they go only when its
    trash record does: at the trash purge, or with its warehouse (``catalog.services.warehouses.trash_owned_by``).
    """
    so = context.settings.storage_options()
    seen: set[str] = set()
    roots = [fga_ids.namespace_object(root_uuid)] if root_uuid is not None else []
    in_trash: set[str] | None = None

    async def trashed(obj: str) -> bool:
        # Read once per walk, and only when an edge names an object the listing did not: a `child` edge names its
        # object by uuid, and the trash record names its object by the uuid its claim or its properties keep.
        nonlocal in_trash
        if in_trash is None:
            in_trash = await run_in_threadpool(trashed_objects, context.settings)
        return obj in in_trash

    async def end_children(obj: str) -> AsyncIterator[int]:
        # The objects a namespace's `child` edges name, each ended before the namespace holding the edge.
        if (reading := wired(context.settings, context.client)) is None or not obj.startswith("namespace:"):
            return
        for held in await fga_reads.read_object_tuples(reading, obj):
            if held.relation == "child" and held.user and held.user not in seen and not await trashed(held.user):
                async for count in end(held.user):
                    yield count

    async def end(obj: str) -> AsyncIterator[int]:
        seen.add(obj)
        async for count in end_children(obj):
            yield count
        kind, _, uuid = obj.partition(":")
        await run_in_threadpool(object_records.end_object_records, context.settings.registry_root, so, kind, uuid=uuid)
        yield await revoke_removed_object(context, obj)

    children_first = sorted(removed, key=lambda item: (item.kind == "namespace", -len(item.segments)))
    for obj in [*(item.fga_object() for item in children_first), *roots]:
        if obj not in seen:
            async for count in end(obj):
                yield count


async def orphaned_children(client: OpenFgaClient | None, settings: Settings, container: str, *, ended: set[str]) -> list[str]:
    """The uuids of the namespaces ``container``'s ``child`` edges name that ``ended`` does not, outside the trash.

    A lifecycle delete re-issued after a failure finds a namespace an earlier attempt removed by nothing but its
    container's edge: no listing names it and its uuid is recorded nowhere else ([[LH-524]]).
    """
    if (client := wired(settings, client)) is None:
        return []
    in_trash = await run_in_threadpool(trashed_objects, settings)
    return sorted(
        held.user.removeprefix("namespace:")
        for held in await fga_reads.read_object_tuples(client, container)
        if held.relation == "child" and held.user and held.user.startswith("namespace:") and held.user not in ended and held.user not in in_trash
    )
