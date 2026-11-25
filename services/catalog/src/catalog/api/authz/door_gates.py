"""The gates a handler asks itself, for the doors the router leaves to their handler or decides only in part."""

import logging
from collections.abc import Mapping, Sequence
from functools import partial

from fastapi.concurrency import run_in_threadpool
from lance_namespace import InvalidInputError

from catalog.api.authz.context import AuthzContext
from catalog.api.authz.decisions import authorization, refusal_of_absent, require_any, require_rung
from catalog.api.authz.objects import addressed_object
from catalog.api.authz.rungs import ACCEPT_MOVED_TABLE_RELATION, STAGED_OPERATION_RELATION, create_parent_rung, moves_namespace
from catalog.api.dependencies import Resolver
from catalog.core import write_judge
from catalog.services import table_claims
from catalog.services.table_claims import ResolvedTable
from catalog.services.tables import commits
from lakehouse.governed import fga_ids
from lakehouse.storage.options import StorageOptions


__all__ = [
    "require_any_relation",
    "require_can_create_warehouse",
    "require_can_drop_table",
    "require_classifier_to_copy_a_label",
    "require_classifier_to_set_labels",
    "require_relation",
    "require_relation_on_a_path",
    "require_rename_destination",
    "require_run_rule_authority",
    "require_staged_operation",
]


log = logging.getLogger(__name__)


async def require_can_drop_table(
    context: AuthzContext,
    *,
    table: ResolvedTable,
) -> None:
    """Raise 403 unless the caller holds owner-tier ``can_drop`` on ``table``, the table its name resolved to.

    The gate for a destructive create ``mode=Overwrite`` against an EXISTING table: Overwrite is spec-defined
    as drop-then-recreate (spec.yaml CreateTableRequest.mode), and it replaces the tip's schema and rows, so it
    must clear the same owner-tier bar a real ``drop_table`` does — NOT the writer-tier ``can_create_table`` on
    the parent that ``authorize`` already applied for a create. Without this, a mere namespace writer could
    replace another user's table. With FGA off it allows (:func:`authorization`)."""
    if (authz := authorization(context)) is None:
        return
    await authz.require("can_drop", fga_ids.table_object(table.require_uuid()), label=addressed_object("table", table.segments))


async def require_staged_operation(
    context: AuthzContext,
    *,
    segments: list[str],
    location: str,
    operation: str,
    creates: bool,
) -> None:
    """Refuse a staged commit whose operation needs a rung the caller lacks, or that no door commits this way.

    ``creates`` is true for the table's first version, which writes the rows of a table only declared so far: it asks
    the writer rung, as every write does, since the router admits a maintainer too, and no protection record, since
    none holds a table with no version. An Overwrite of a table with a version replaces its tip, so it asks the
    owner-tier ``can_drop``; it asks nothing of the table's protection, which guards its drop (ADR 0221).
    With FGA off the rung check allows (:func:`authorization`); an operation outside the map (``Clone`` among them,
    which the branch and clone doors own) is refused whatever FGA says.

    Raises:
        InvalidInputError: ``operation`` is not one a stock commit may stage here.
    """
    if operation not in STAGED_OPERATION_RELATION:
        raise InvalidInputError(f"a {operation} cannot be committed through CreateTableVersion")
    relation = "can_write_data" if creates else STAGED_OPERATION_RELATION[operation]
    canonical = fga_ids.canonical_object_id(segments)
    authz = authorization(context)
    if authz is None:
        return
    table_uuid = await run_in_threadpool(table_claims.uuid_at, table_claims.claim_store(context.settings), location, segments)
    await authz.require(relation, fga_ids.table_object(table_uuid), label=f"table:{canonical}")


async def require_rename_destination(
    context: AuthzContext,
    *,
    resolver: Resolver,
    source: list[str],
    destination: list[str],
) -> None:
    """Refuse a rename of table ``source`` to ``destination`` unless the caller may put a table there.

    A rename mints a name in the destination's namespace. Within the source's own namespace it asks what a create asks
    there (``can_create_table``, :func:`create_parent_rung`), so a source owner cannot plant a table where they may
    create none. Into another namespace the table arrives with every grant its uuid holds, so it asks
    :data:`ACCEPT_MOVED_TABLE_RELATION` in that rung's place: under managed access only a caller holding grant authority
    on the destination brings grants into it ([[LH-568]]). Asked before anything moves. With FGA off it allows
    (:func:`authorization`).
    """
    if (authz := authorization(context)) is None:
        return
    if (parent := create_parent_rung("table", destination)) is None:
        return
    if moves_namespace(source, destination):
        parent = write_judge.Rung(relation=ACCEPT_MOVED_TABLE_RELATION, on=parent.on)
    await require_rung(authz.client, resolver, user=authz.subject, rung=parent)


async def require_relation(
    context: AuthzContext,
    *,
    relation: str,
    obj: str,
    label: str | None = None,
) -> None:
    """Gate on one ``relation`` over an explicit FGA object id, through the audited ``require_audited`` core.

    For handlers whose object is not table/namespace-shaped (e.g. the #3-A ``warehouse:<id>`` control
    plane) — funneling here instead of a bare ``fga_reads.check`` keeps every deny/allow/outage on the #41 audit
    trail. ``label`` names the object in a refusal: a table's or namespace's name, never the uuid it is held by. A no-op
    with FGA off. With it on, no client is 503 and no caller 401 (:func:`authorization`): a door the router leaves to its
    handler is decided here alone, so this gate refuses on its own whatever ran before it ([[LH-237]])."""
    if (authz := authorization(context)) is None:
        return
    await authz.require(relation, obj, label=label)


async def require_any_relation(context: AuthzContext, *, doors: list[tuple[str, str]]) -> None:
    """Gate on ANY of ``doors``, each a ``(relation, object)`` pair, through the audited :func:`require_any` core.

    For a handler that two rungs open, one on its object and one above it (a tenant's record: a member's, or the estate
    observer's). The trail holds one ALLOW naming the door that decided, or a DENY for every door the caller lacked, so
    its reader sees which object the request was decided on. A no-op with FGA off; with it on, no client is 503 and no
    caller 401 (:func:`authorization`).

    Raises:
        PermissionDeniedError: The caller holds none of ``doors``.
        ServiceUnavailableError: FGA is on and unwired, or OpenFGA could not decide.
        UnauthenticatedError: FGA is on and nobody is signed in.
    """
    if (authz := authorization(context)) is None:
        return
    await require_any(authz.client, user=authz.subject, doors=doors)


async def require_relation_on_a_path(
    context: AuthzContext,
    *,
    relation: str,
    obj: str | None,
    path: str,
) -> None:
    """Gate on ``relation`` over the object found at ``path``, a place the caller named only through data it sent (a shallow
    clone's source, which its own manifest names), refusing alike whether an object is there or not.

    The refusal names ``path`` and never the object, so it tells no caller whether, or as what, a governed table lives
    there ([[LH-436]]); with no object there (``obj`` ``None``) it refuses whatever authorization is on, since no tuple can
    grant a relation on nothing. Who is asking is settled first either way (:func:`authorization`), so a caller with no
    token, or an unwired client, is answered alike for a path that holds an object and one that holds none.

    Raises:
        PermissionDeniedError: The caller holds no ``relation`` on the object, or no object is at ``path`` (403).
        ServiceUnavailableError: FGA is on and unwired.
        UnauthenticatedError: FGA is on and nobody is signed in.
    """
    authz = authorization(context)
    if obj is None:
        raise refusal_of_absent(user=context.token.subject if context.token is not None else "", relation=relation, label=path)
    if authz is not None:
        await authz.require(relation, obj, label=path)


async def require_classifier_to_copy_a_label(
    context: AuthzContext,
    *,
    table: table_claims.DoorTable,
    so: StorageOptions,
    branch: str | None,
    expressions: Sequence[str],
) -> None:
    """Refuse an ``add_columns`` or ``update`` expression that reads a classified column unless the caller holds
    ``can_classify`` on the table ([[LH-207]], owner 2026-10-26).

    Such an expression copies the values into a column with no label (measured on pylance 13.0.0:
    ``add_columns({"leak": "secret"})`` copies them unlabelled), which a writer could then vend raw. This narrows the
    copy rather than closing it: a writer reads the column too, so a copy made outside the catalog is LH-288's.
    With FGA off it allows (:func:`authorization`).
    """
    if (authz := authorization(context)) is None:
        return
    dataset = await run_in_threadpool(partial(table.open, so, branch=branch))
    try:
        read = await run_in_threadpool(commits.expressions_reading_a_label, dataset, list(expressions))
    except OSError:
        # Labels that cannot be read are not "no labels": the copy is judged as reading one.
        log.warning("copy_labels_unreadable", exc_info=True)
        read = ["<labels unreadable>"]
    if read:
        log.info("copy_of_a_label_asks_can_classify", extra={"expressions": read})
        await authz.require("can_classify", fga_ids.table_object(table.resolved.require_uuid()), label=fga_ids.canonical_object_id(table.segments))


async def require_classifier_to_set_labels(
    context: AuthzContext,
    *,
    obj: str,
    label: str,
    labels: Mapping[str, Mapping[str, str]],
) -> None:
    """Refuse fields arriving with ``rask.*`` metadata unless the caller holds ``can_classify`` on ``obj`` and ``can_apply``
    on each value, as a label set later through ``update_field_metadata`` is asked ([[LH-207]]).

    A label decides how a table's bytes leave the store, so a create may not set one its caller could not set after.
    ``obj`` is the table, or for a table not made yet its namespace, whose ``classifier`` its tables inherit. With no
    labels, or with FGA off, it allows (:func:`authorization`).
    """
    if not labels:
        return
    if (authz := authorization(context)) is None:
        return
    await authz.require("can_classify", obj, label=label)
    for value in sorted({value for keys in labels.values() for value in keys.values()}):
        await authz.require("can_apply", f"classification:{value}")


async def require_run_rule_authority(context: AuthzContext, *, project: str | None) -> None:
    """Raise 403 unless the caller may declare or drop a namespace's ``rask.requires_run`` (ADR 0211).

    That is the namespace's project admin (``can_administer`` on ``project:<project>``), or the estate admin
    (``can_declare_unowned_run_rule`` on the root object) where no project owns the namespace. With FGA off it allows
    (:func:`authorization`).
    """
    if (authz := authorization(context)) is None:
        return
    if project is not None:
        await authz.require("can_administer", f"project:{project}")
    else:
        await authz.require("can_declare_unowned_run_rule", context.settings.fga_root_object)


async def require_can_create_warehouse(
    context: AuthzContext,
    *,
    project: str,
) -> None:
    """Gate the #3-A admin control-plane warehouse-create on ``project#can_create_warehouse``.

    The model's highest-privilege action (``project.can_create_warehouse = admin``,
    ``lakehouse/governed/auth/model.fga``): provisioning a physical bucket is a project-admin
    operation, not the writer-tier create-on-parent that guards tables/namespaces. Fail-closed like
    every other gate: 403 on deny, 503 on an OpenFGA outage. With FGA off it allows (:func:`authorization`).

    There is deliberately NO bootstrap exception any more. It existed because a not-yet-existing
    project had no tuples, so its first warehouse was uncreatable — the estate admin got a side door
    on the root. Decision 1 replaced the implicit mint: the estate admin
    creates the PROJECT explicitly (``POST /management/v1/projects``, which seeds its admin), and
    ``require_project_exists`` refuses a warehouse for an unknown tenant BEFORE this gate runs — so
    by the time we are here, the project exists and has admins, and one door is enough."""
    if (authz := authorization(context)) is None:
        return
    await authz.require("can_create_warehouse", f"project:{project}")
