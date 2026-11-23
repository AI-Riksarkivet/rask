"""The hierarchy's shape and existence guards, which ask no FGA question."""

from __future__ import annotations

import logging

from fastapi.concurrency import run_in_threadpool
from lance_namespace import (
    ConcurrentModificationError,
    InvalidInputError,
    InvalidTableStateError,
    LanceNamespace,
    NamespaceExistsRequest,
    NamespaceNotEmptyError,
    NamespaceNotFoundError,
    TableNotFoundError,
    UnsupportedOperationError,
)

from catalog.api.authz.objects import GovernedObject
from catalog.core import native
from catalog.core.config import CatalogStore
from catalog.core.identifiers import MAX_NAMESPACE_DEPTH
from catalog.services import object_uuids, protections
from catalog.services import projects as project_registry
from catalog.services.table_claims import ResolvedTable
from lakehouse.governed import fga_ids
from lakehouse.naming import CATALOG_DELIMITER


__all__ = [
    "require_namespace_as_identified",
    "require_namespace_depth",
    "require_namespace_unprotected",
    "require_not_protected",
    "require_parent",
    "require_parent_exists",
    "require_project_exists",
    "require_rename_within_its_warehouse",
    "require_standing_as_identified",
    "require_table_unprotected",
]


log = logging.getLogger(__name__)


# --------------------------------------------------------------------------- #
# Hierarchy enforcement — the estate's containment rule, checked BEFORE the write.
# --------------------------------------------------------------------------- #


def require_parent(resource: str, segments: list[str]) -> None:
    """Reject a create that would produce an object with no parent to belong to.

    THE RULE, which the authz model already states and the write path did not:
    ``project -> warehouse -> namespace (self-nesting) -> table``. `model.fga` declares
    ``table.parent: [namespace]`` and ``namespace.parent: [warehouse, namespace]``, so an object
    whose parent is absent is not "a table at the root" — it is a table the model has no way to
    describe.

    Until this existed, a single-segment table was created anyway: ``fga.parent_object`` returned
    ``None`` for it, the create proceeded, and the object got an owner grant and no containment edge.
    Nothing failed at write time and nothing was visibly wrong — the row simply sat outside every
    cascade, invisible to a warehouse-level or namespace-level grant, and impossible to reach by the
    hierarchy the UI navigates. Orphans are cheap to create and expensive to find, so the answer is a
    refusal at the door with a message that says which rung is missing.

    Raised as the spec's own ``InvalidInput`` (error code 13 → HTTP 400, RFC 9457 problem body via
    ``install_problem_handlers``), NOT a bespoke 422: the Lance Namespace error model has no 422, and
    every client SDK dispatches on the numeric ``code`` — a status outside the spec's mapping is one
    no generated client understands. The DETAIL does the teaching: it names the rule and the fix.

    Namespaces are not judged here: a namespace's parent is its warehouse or a namespace whenever it has a name to be
    created under, and a name of one segment, a warehouse's own, is refused before any door by
    :func:`_require_a_name_inside_a_warehouse`, which also refuses a table with no namespace between it and its warehouse.
    """
    if resource != "table":
        return
    if fga_ids.parent_namespace_id(segments) is not None:
        return
    ident = fga_ids.canonical_object_id(segments)
    raise InvalidInputError(
        f"table '{ident}' has no namespace to belong to. A table must live inside a namespace "
        f"(project > warehouse > namespace > table), so its identifier needs at least one "
        f"namespace segment before the table name — e.g. '<namespace>{CATALOG_DELIMITER}{ident}'. "
        "Create the namespace first if it does not exist."
    )


async def require_parent_exists(ns: LanceNamespace, resource: str, segments: list[str]) -> None:
    """`require_parent`, plus the half it never did: the parent must actually EXIST (#118).

    `require_parent` counts SEGMENTS — it proves the identifier names a parent, not that the parent
    is real. So `POST /v1/table/ghostns$t9/create` wrote a real Lance dataset into a namespace that
    does not exist, seeded an owner grant, and left no `parent` edge: the cascade-invisible orphan
    the guard's own docstring exists to prevent. Only `register_table` refused, and only because the
    `dir` backend itself checks — an accident of the backend, not a guard.

    Costs one `namespace_exists` round trip per create. That is the price of the rule the Lifecycle
    states ("Creates are top-down: parent must EXIST"), and it is paid BEFORE the native write so a
    refusal leaves nothing half-made.

    Subsumes `require_parent` rather than sitting beside it, so a door cannot acquire half the rule:
    the segment check is the cheap half and runs first, before any round trip.

    A backend that does not implement `namespace_exists` (an optional op — the spec has 54 and every
    backend implements a subset) cannot be interrogated, and refusing every create against such a
    backend would break far more than it protects. That case falls through to the segment rule alone,
    deliberately and narrowly: `UnsupportedOperationError` only, never a transport failure.
    """
    require_parent(resource, segments)
    parent = segments[:-1]
    if not parent:
        return
    try:
        await run_in_threadpool(native.call, ns, "namespace_exists", NamespaceExistsRequest(id=parent))
    except UnsupportedOperationError:
        return
    except NamespaceNotFoundError as exc:
        ident = fga_ids.canonical_object_id(segments)
        parent_id = fga_ids.canonical_object_id(parent)
        raise NamespaceNotFoundError(
            f"cannot create '{ident}': its parent namespace '{parent_id}' does not exist. "
            f"Create it first (POST /v1/namespace/{parent_id}/create, or POST /management/v1/warehouses "
            "for a warehouse) — a table whose namespace is absent is invisible to every "
            "cascade and to every parent-scoped grant."
        ) from exc


def require_namespace_depth(segments: list[str]) -> None:
    """Refuse a namespace nested deeper than the estate's ceiling — a SHAPE rule, so a 400.

    THE CEILING IS AN OPENFGA LIMIT, NOT A PREFERENCE. `warehouse#can_get_metadata` and
    `namespace#can_get_metadata` are recursive (`can_get_metadata from child`), and OpenFGA abandons a
    resolution needing too many rewrite rules. Measured against the shipped model with the real
    evaluator: at roughly 13 levels of nesting, `Check(user, can_get_metadata, warehouse:X)` stops
    answering true/false and returns `Code(2002) ... too many rewrite rules`.

    That is an ERROR, not a deny, and the fleet fails closed on an unrecognised authz error — so it
    surfaces as a 503 on the browse path. Worse, it is LATERAL: the positive check short-circuits, so
    a shallow readable table still resolves, while the NEGATIVE check for any subject on that
    warehouse errors. One pathological branch — created by a plain `writer`, or by a buggy importer —
    takes warehouse browsing down for every user of that bucket, its owners included.

    Nothing bounded creation before this. Both read walkers capped at `MAX_NAMESPACE_DEPTH` and the
    doors did not, so the estate could be driven into a state its own authz model cannot evaluate.

    The comparison is `>=`, IDENTICAL to both walkers'. It is a bound they share, and sharing the
    number while disagreeing about the operator would put the deepest legal namespace one level past
    where the walkers stop descending — its tables enumerated by nothing, so a cascade drop would
    silently leave them behind. That is the F10 item 10 failure with an off-by-one instead of a
    second literal.

    Refused at the SHAPE rung (`InvalidInputError` -> 400), before any native write, per the create
    door's order: identity -> shape -> parent exists -> authz -> conflict -> write. A depth violation
    is a malformed request, not a permission problem, and answering 403 would send the caller looking
    for a grant that would not help.
    """
    if len(segments) >= MAX_NAMESPACE_DEPTH:
        raise InvalidInputError(
            f"namespace {fga_ids.canonical_object_id(segments)!r} nests {len(segments)} levels deep; the maximum is "
            f"{MAX_NAMESPACE_DEPTH - 1}. Deeper trees exceed what the authorization model can resolve, "
            f"which takes metadata reads down for the whole warehouse rather than denying just this one."
        )


def require_not_protected(protected: bool, *, kind: str, obj_id: str, force: bool) -> None:
    """Refuse to delete an object marked ``protected`` unless the caller passed ``force=true``.

    Deletion protection: an opt-in flag on the registry
    record that makes an object refuse deletion. It is what makes ``cascade`` survivable — a
    fat-fingered cascade cannot take a protected object with it — and it is the ONE thing standing
    between a mistyped id and an irreversible purge.

    ``force`` overrides the PROTECTION ONLY. It is not an authz bypass: the FGA gate has already run
    (and runs identically with or without it), so a caller who may not delete this object cannot
    delete it by forcing. Two independent locks, and force turns exactly one.

    THE REFUSAL IS SHAPED LIKE THE OBJECT IT REFUSES, because a client acts on the code and not on the
    sentence. A protected TABLE raises ``InvalidTableStateError`` — `lance_docs/ns_catalog/spec.yaml:2431`
    defines code 19 as "Table is in an invalid state for the operation", which is exactly what protected
    is. Everything else raises ``NamespaceNotEmptyError`` (code 3), the spec's container-shaped "this
    will not be deleted right now".

    THE TABLE CASE USED TO RAISE CODE 3 TOO, and that is a live defect rather than a cosmetic one:
    ``NamespaceNotEmpty`` tells a generated client the container has contents, so the correct response
    to it is to empty the container and retry — against a table that was never full, and whose refusal
    no amount of emptying changes. The cascade path (`namespaces.py::_require_descendants_unprotected`)
    makes it worse, because there the namespace genuinely IS non-empty: the client's retry is
    well-founded, it drops every unprotected sibling, and it still cannot pass the one protected table.

    ``warehouse`` and ``project`` keep code 3 by SCOPE, not by oversight: they are rask's own hierarchy
    and appear nowhere in the lance-ns spec, so every code is an approximation there and the
    container-shaped one is the closest. The table is the rung the spec covers and the rung clients are
    generated against, so it is the one where a wrong code has a reader.
    """
    if force or not protected:
        return
    refusal = InvalidTableStateError if kind == "table" else NamespaceNotEmptyError
    raise refusal(
        f"{kind} '{obj_id}' is protected against deletion. Pass force=true to override "
        f"(protection only — the same authorization is still required), or clear the flag first."
    )


async def require_namespace_as_identified(ns: LanceNamespace, namespace: GovernedObject) -> None:
    """Refuse a door about to act on ``namespace`` by name once that name holds another namespace, or none.

    The backend addresses a namespace by name alone (``DropNamespaceRequest`` names no uuid, `spec.yaml`), so a drop and
    a create landing after the request was authorized would have the door act on a namespace its caller was never
    judged on. The same refusal as a create's parent that changed (:class:`ParentChangedError`): a retry is judged
    afresh, on the namespace the name holds then.

    Raises:
        ConcurrentModificationError: The name holds another namespace, or none.
    """
    standing = await run_in_threadpool(object_uuids.resolve_namespace_uuid, ns, namespace.segments)
    require_standing_as_identified(namespace.segments, identified=namespace.uuid, standing=standing)


def require_standing_as_identified(segments: list[str], *, identified: str, standing: str | None) -> None:
    """:func:`require_namespace_as_identified` on a uuid its caller has just read for ``segments`` (``standing``), for a
    walk that describes each namespace before it drops it.

    Raises:
        ConcurrentModificationError: ``standing`` is not ``identified``.
    """
    if standing != identified:
        name = fga_ids.canonical_object_id(segments)
        log.warning("door_refused_namespace_changed", extra={"object": name, "identified": identified, "standing": standing})
        raise ConcurrentModificationError(f"namespace {name} was dropped or replaced while this request acted on it; retry")


async def require_table_unprotected(settings: CatalogStore, table: ResolvedTable, *, force: bool) -> None:
    """Refuse a door that destroys some of ``table`` while its protection record stands, unless ``force``.

    The record is read under the uuid the door's name resolved to (ADR 0173 §8): a table that gave up the name, or
    one that took it since, holds a record under another key.

    Raises:
        TableNotFoundError: The name named no table.
        InvalidTableStateError: The table is protected.
    """
    if force:
        return
    guard = await run_in_threadpool(protections.read, settings, "table", table.require_uuid())
    require_not_protected(guard is not None and guard.protected, kind="table", obj_id=fga_ids.canonical_object_id(table.segments), force=False)


async def require_namespace_unprotected(settings: CatalogStore, namespace: GovernedObject, *, force: bool) -> None:
    """Refuse a door that removes ``namespace`` while its protection record stands, unless ``force``.

    Raises:
        NamespaceNotEmptyError: The namespace is protected.
    """
    if force:
        return
    guard = await run_in_threadpool(protections.read, settings, "namespace", namespace.uuid)
    require_not_protected(guard is not None and guard.protected, kind="namespace", obj_id=fga_ids.canonical_object_id(namespace.segments), force=False)


async def require_project_exists(settings: CatalogStore, project: str) -> None:
    """Refuse an operation naming a project the registry does not know.

    Existence is a LAYER-3 invariant: it comes from the project
    registry, not from FGA tuples and not from warehouse records implying a tenant. Checked before
    authz on purpose — the answer is the same for every caller, discloses nothing, and a 404 here
    tells an authorized admin the actual fix (create the project) instead of a misleading 403.

    Raises the same ``TableNotFoundError`` (spec code 4 → HTTP 404 problem body) the project detail
    route already answers for an unknown tenant, so one client error path covers both.
    """
    record = await run_in_threadpool(project_registry.get_project, settings.registry_root, settings.storage_options(), project)
    if record is None:
        raise TableNotFoundError(
            f"project not found: {project}. A warehouse must belong to an existing project "
            f"(project > warehouse > namespace > table) — create it first: POST /management/v1/projects "
            f'with {{"id": "{project}"}}.'
        )


def require_rename_within_its_warehouse(source: list[str], destination: list[str]) -> None:
    """Refuse a rename whose destination is not a table name in the source's own warehouse ([[LH-593]]).

    A rename moves a pointer inside one directory catalog and never bytes, and a table's bytes lie in its warehouse's
    root, so no rename carries a table to another warehouse. It follows that a rename never crosses tenants.

    Raises:
        InvalidInputError: ``destination`` names another warehouse, or no namespace of one.
    """
    if len(destination) < 3 or destination[0] != source[0]:
        raise InvalidInputError(
            f"table '{fga_ids.canonical_object_id(source)}' cannot be renamed to '{fga_ids.canonical_object_id(destination)}': a rename keeps a "
            f"table in its warehouse, so the new name is {source[0]}{CATALOG_DELIMITER}<namespace>{CATALOG_DELIMITER}<table>"
        )
