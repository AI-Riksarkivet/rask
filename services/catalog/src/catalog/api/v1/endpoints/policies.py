"""Maintenance-policy endpoints (#50/#84): per-table/namespace/project overrides for the compaction sweep.

``POST …/policy/set`` and ``…/policy/delete`` are owner-tier (the router-level ``authorize`` maps them
to ``can_drop``/``can_delete`` — a retention policy authorizes destroying version history), and land on
the #41 audit trail through that same gate; ``…/policy/describe`` is a reader-tier metadata read, which answers an
object holding no policy ``set: false`` (:class:`PolicyUnset`) and never as a missing object. A table
or namespace record is keyed by the object's uuid (ADR 0173 §8, [[LH-523]]), which the door resolves from
the name it is addressed by, so a rename keeps the policy and a table created later under the same name
holds none of it; the maintenance sweep matches a dataset's table and ancestor namespaces by the uuids
``__manifest`` and the location claims record, with no catalog round-trip. Old-version cleanup can never
remove a tag-pinned version (Lance exempts tagged versions), so every tagged version survives any
policy.

The #84 PROJECT policy is the tenant-wide fallback below both: it matches by the project's warehouse
BUCKETS (resolved from the warehouse registry at set time), so it covers every dataset in the project's
buckets that no table/namespace record shadows. ``/management/v1/project`` is not a router-guarded resource prefix,
so — like the #17 model routes — these endpoints gate EXPLICITLY, on ``project:<id>#can_administer``
(the model's tenant-admin action; ``project`` defines no reader-tier relation, so describe gates there
too rather than checking a phantom relation, which would 400 → fail-closed 503 for everyone).

The #65 project-scoped LIST (``GET /management/v1/projects/{id}/policies``) is the tenant's VIEW of all three tiers
at once. It names a table record as its table is named now and scopes each record by that name (its first segment is
its warehouse, whose record names its project, [[LH-593]]).
"""

import logging
from collections.abc import Callable

from fastapi import APIRouter
from fastapi.concurrency import run_in_threadpool
from lance_namespace import (
    ConcurrentModificationError,
    InternalError,
    InvalidInputError,
    LanceNamespace,
    NamespaceAlreadyExistsError,
    TableNotFoundError,
)

from catalog.api.authz import door_gates, hierarchy, objects
from catalog.api.authz.context import AuthzContextDep
from catalog.api.dependencies import ControlEmitterDep, NamespaceDep, ResolvedTableDep, Resolver, ResolverDep, SettingsDep
from catalog.api.security import Caller, CurrentToken, actor_of
from catalog.core.config import CatalogStore
from catalog.core.identifiers import parse_identifier
from catalog.schemas import PolicyDeleteResponse, PolicyKind, PolicyRequest, PolicyResponse, PolicyUnset, ProjectPoliciesResponse
from catalog.services import projects as project_registry
from catalog.services import table_claims, warehouses
from lakehouse.events.control_emit import ControlEmitter, emit_control
from lakehouse.governed import fga_ids
from lakehouse.naming import CONTROL_ID_RE
from lakehouse.records import maintenance_policies as policies
from lakehouse.records.location_claims import ClaimStore
from lakehouse.records.maintenance_policies import PolicyRecord
from lakehouse.storage import conditional_records


log = logging.getLogger(__name__)

#: THE MANAGEMENT SURFACE ([[LH-021]]). rask's authorization model is not in the Lance namespace spec
#: at ANY point — grants, policies and managed access are entirely rask's own — so a spec client
#: discovering `/v1/table/{id}/access/grant` meets a verb the document never defines. These mount at
#: `/management/v1` beside the rest of the estate's governance, and inherit the same three rungs
#: from `api/v1/router.py` as the spec routers do.
table_router = APIRouter(prefix="/management/v1/table", tags=["policy"])
namespace_router = APIRouter(prefix="/management/v1/namespace", tags=["policy"])
project_router = APIRouter(prefix="/management/v1/project", tags=["policy"])
# PLURAL, and deliberately not a fourth spelling: the LIST is a collection read on the control-plane
# ``projects`` resource (``endpoints/projects.py``'s prefix), while the singular ``/management/v1/project`` keeps the
# spec's ``POST /v1/<object>/{id}/<action>`` grammar for the set/describe/delete trio.
projects_router = APIRouter(prefix="/management/v1/projects", tags=["policy"])

# The same DNS-safe shape the whole control plane enforces for project ids — a malformed id must never
# become a path-traversing registry key or a phantom FGA object. SHARED, not re-declared: this was one of
# three copies that had drifted apart on their end anchor (`catalog.core.identifiers.CONTROL_ID_RE`).
_PROJECT_ID_RE = CONTROL_ID_RE


def _record(
    kind: PolicyKind,
    canonical_id: str,
    body: PolicyRequest,
    *,
    existing: PolicyRecord | None,
    uuid: str | None,
    location: str | None = None,
    buckets: list[str] | None = None,
) -> PolicyRecord:
    """The record to persist. A field the caller did not SET keeps whatever the stored record had.

    The record is written whole, so `model_dump()` alone made every set a full replace:
    a client that read a policy, changed one knob and sent it back wrote the model's DEFAULTS over
    every field it had not mentioned — silently clearing `scan_batch_size` (the compaction memory
    bound) and re-enabling a `cleanup_enabled=False` legal hold. The lakehouse form
    does exactly that, and its own comment states the belief this now makes true: "an omitted knob
    means inherit".

    Only EXPLICITLY set fields are applied (`exclude_unset`), which is also what the request model's
    empty-body guard already keys on — one notion of "the caller meant this", not two. ``existing`` is the record as it
    stands when the write lands (`policies.set_policy`), never one read earlier. The identity is this door's: its kind,
    the name it was addressed by, and what its kind is matched by (a ``uuid``, a table's ``location``, a project's
    ``buckets``).
    """
    merged: dict[str, object] = existing.stored() if existing is not None else {}
    merged.update(body.model_dump(exclude_unset=True))
    # Defaults still apply on a FIRST write, where there is nothing to inherit from.
    for field, value in body.model_dump().items():
        merged.setdefault(field, value)
    return PolicyRecord.model_validate({**merged, "kind": kind, "id": canonical_id, "uuid": uuid, "location": location, "buckets": buckets or []})


async def _set(settings: CatalogStore, kind: PolicyKind, key: str, *, compose: Callable[[PolicyRecord | None], PolicyRecord]) -> PolicyRecord:
    """Write the record ``compose`` merges onto the one stored, conditional on that record (`policies.set_policy`), once
    for all three rungs; the record written.

    Raises:
        ConcurrentModificationError: The record kept changing under the set (409, code 14).
    """
    try:
        return await run_in_threadpool(policies.set_policy, settings.registry_root, settings.storage_options(), kind, key=key, compose=compose)
    except conditional_records.RecordChangedError as exc:
        raise ConcurrentModificationError(f"the {kind} policy kept changing while it was set; set it again") from exc
    except policies.MalformedPolicyError as exc:
        raise InternalError(f"{exc}; delete the policy, then set it") from exc


def _response(record: PolicyRecord) -> PolicyResponse:
    return PolicyResponse.model_validate({**record.stored(), "set": True})


async def _describe(settings: CatalogStore, *, kind: PolicyKind, key: str, canonical: str, uuid: str | None) -> PolicyResponse | PolicyUnset:
    """Read one policy, once for all three rungs: its record, or :class:`PolicyUnset` where the object holds none.

    Each route establishes its object before this read, a table's or namespace's by resolving its name and a
    project's by reading its registry record, so an object that does not stand, or that its caller may not see,
    never reaches it ([[LH-589]]).
    """
    try:
        record = await run_in_threadpool(policies.get_policy, settings.registry_root, settings.storage_options(), kind, key=key)
    except policies.MalformedPolicyError as exc:
        raise InternalError(f"{exc}; delete the policy, then set it") from exc
    if record is None:
        return PolicyUnset(kind=kind, id=canonical, uuid=uuid, set=False)
    # Named as the door was addressed: the name the record kept is the one its object had when it was set.
    return _response(record.model_copy(update={"id": canonical}))


async def _delete(settings: CatalogStore, control: ControlEmitter, token: Caller | None, *, kind: PolicyKind, key: str, canonical: str) -> PolicyDeleteResponse:
    """Remove one policy (idempotent), log it and announce it — once for all three rungs."""
    await run_in_threadpool(policies.delete_policy, settings.registry_root, settings.storage_options(), kind, key=key)
    log.info("maintenance_policy_deleted", extra={"kind": kind, "id": canonical})
    await emit_control(
        control,
        action="policy_deleted",
        object_type="policy",
        # A table's or namespace's key is its uuid, the object its grants are held by; a project's is its id.
        object_id=f"{kind}:{key}",
        object_name=canonical if kind in ("table", "namespace") else None,
        actor=actor_of(token),
        extra={"kind": kind},
    )
    return PolicyDeleteResponse(status="deleted", kind=kind, id=canonical)


@table_router.post("/{id}/policy/set", response_model_exclude_none=True)
async def set_table_policy(
    *,
    id: str,
    body: PolicyRequest,
    table: ResolvedTableDep,
    settings: SettingsDep,
    token: CurrentToken,
    control: ControlEmitterDep,
) -> PolicyResponse:
    """Set (or replace) the table's maintenance policy — owner-gated by the router (``can_drop``)."""
    canonical = fga_ids.canonical_object_id(table.segments)
    table_uuid = table.require_uuid()
    # The location beside the uuid, so a listing names the table as it is named now (`table_claims.names_now`).
    record = await _set(
        settings, "table", table_uuid, compose=lambda stored: _record("table", canonical, body, existing=stored, uuid=table_uuid, location=str(table.location))
    )
    log.info("maintenance_policy_set", extra={"kind": "table", "id": canonical})
    await emit_control(
        control,
        action="policy_set",
        object_type="policy",
        object_id=fga_ids.table_object(table_uuid),
        object_name=canonical,
        actor=actor_of(token),
        extra={"kind": "table"},
    )
    return _response(record)


@table_router.post("/{id}/policy/describe", response_model_exclude_none=True)
async def describe_table_policy(id: str, table: ResolvedTableDep, settings: SettingsDep) -> PolicyResponse | PolicyUnset:
    """The table's maintenance policy (reader-gated), or ``set: false`` when none is set."""
    uuid = table.require_uuid()
    return await _describe(settings, kind="table", key=uuid, canonical=fga_ids.canonical_object_id(table.segments), uuid=uuid)


@table_router.post("/{id}/policy/delete", response_model_exclude_none=True)
async def delete_table_policy(
    *, id: str, table: ResolvedTableDep, settings: SettingsDep, token: CurrentToken, control: ControlEmitterDep
) -> PolicyDeleteResponse:
    """Remove the table's maintenance policy (idempotent) — owner-gated by the router."""
    return await _delete(settings, control, token, kind="table", key=table.require_uuid(), canonical=fga_ids.canonical_object_id(table.segments))


@namespace_router.post("/{id}/policy/set", response_model_exclude_none=True)
async def set_namespace_policy(
    *, id: str, body: PolicyRequest, ns: NamespaceDep, settings: SettingsDep, token: CurrentToken, control: ControlEmitterDep, resolver: ResolverDep
) -> PolicyResponse:
    """Set (or replace) a namespace-level policy — applies to every table under the namespace, at any depth, unless
    a table policy or a nearer namespace's overrides it. Owner-gated by the router (``can_delete``)."""
    namespace = await _namespace(id, resolver, ns)
    await hierarchy.require_namespace_as_identified(ns, namespace)
    canonical = fga_ids.canonical_object_id(namespace.segments)
    record = await _set(
        settings, "namespace", namespace.uuid, compose=lambda stored: _record("namespace", canonical, body, existing=stored, uuid=namespace.uuid)
    )
    log.info("maintenance_policy_set", extra={"kind": "namespace", "id": canonical})
    await emit_control(
        control,
        action="policy_set",
        object_type="policy",
        object_id=fga_ids.namespace_object(namespace.uuid),
        object_name=canonical,
        actor=actor_of(token),
        extra={"kind": "namespace"},
    )
    return _response(record)


@namespace_router.post("/{id}/policy/describe", response_model_exclude_none=True)
async def describe_namespace_policy(*, id: str, ns: NamespaceDep, settings: SettingsDep, resolver: ResolverDep) -> PolicyResponse | PolicyUnset:
    """The namespace's maintenance policy (reader-gated), or ``set: false`` when none is set."""
    namespace = await _namespace(id, resolver, ns)
    return await _describe(settings, kind="namespace", key=namespace.uuid, canonical=fga_ids.canonical_object_id(namespace.segments), uuid=namespace.uuid)


@namespace_router.post("/{id}/policy/delete", response_model_exclude_none=True)
async def delete_namespace_policy(
    *, id: str, ns: NamespaceDep, settings: SettingsDep, token: CurrentToken, control: ControlEmitterDep, resolver: ResolverDep
) -> PolicyDeleteResponse:
    """Remove the namespace's maintenance policy (idempotent) — owner-gated by the router."""
    namespace = await _namespace(id, resolver, ns)
    await hierarchy.require_namespace_as_identified(ns, namespace)
    return await _delete(settings, control, token, kind="namespace", key=namespace.uuid, canonical=fga_ids.canonical_object_id(namespace.segments))


async def _namespace(id: str, resolver: Resolver, ns: LanceNamespace) -> objects.GovernedObject:
    """The namespace ``id`` names as the request resolved it, with the uuid its policy is kept under; the root holds no policy."""
    segments = parse_identifier(id)
    if not segments:
        raise InvalidInputError("a policy needs a concrete namespace, not the root")
    return await objects.identify_namespace(resolver, ns, segments)


def _validated_project(id: str) -> str:
    if not _PROJECT_ID_RE.match(id):
        raise InvalidInputError(f"invalid project id {id!r}: must match {_PROJECT_ID_RE.pattern}")
    return id


@project_router.post("/{id}/policy/set", response_model_exclude_none=True)
async def set_project_policy(
    *,
    id: str,
    body: PolicyRequest,
    context: AuthzContextDep,
    control: ControlEmitterDep,
) -> PolicyResponse:
    """Set (or replace) the project-level maintenance policy (#84) — the tenant-wide default the sweep
    falls back to when no table or namespace policy matches. Admin-gated (``can_administer`` on
    ``project:<id>``, checked explicitly — see the module docstring).

    The record's ``buckets`` are the project's ACTIVE warehouse buckets, resolved from the warehouse
    registry NOW: a warehouse provisioned after this set is not covered until the policy is re-set
    (the same set-time-resolution stance as the table policy's physical path). Staleness runs the OTHER
    way too: a warehouse deactivated AFTER this set stays on the stored record — the policy keeps
    governing that bucket's datasets until the policy is re-set (or deleted); a deactivation does not
    rewrite existing policy records. A project with no active warehouse is refused — a policy that
    could never match anything should fail loudly at set time, not lie dormant.

    Defense in depth (audit 2026-07-23): a resolved bucket that ANOTHER project's warehouse also claims
    is refused with 409 — even if a rival claim somehow got past ``create_warehouse``'s guards, it must
    not become a policy governing (and destroying version history in) the other tenant's data."""
    project = _validated_project(id)
    await door_gates.require_relation(context, relation="can_administer", obj=fga_ids.project_object(project))
    so = context.settings.storage_options()
    # Read whole ([[LH-572]]): a record that cannot be read is a bucket of this project the policy would leave on the
    # defaults, or another project's claim on one it would govern.
    registered = await run_in_threadpool(warehouses.every_warehouse, context.settings.registry_root, so)
    buckets = sorted({r.bucket for r in registered if r.project == project and not r.is_deactivated})
    if not buckets:
        raise InvalidInputError(
            f"project {project!r} has no active warehouse — provision one first "
            "(a project policy scopes to the project's warehouse buckets, resolved at set time)"
        )
    for bucket in buckets:
        rivals = warehouses.projects_claiming_bucket(registered, bucket) - {project}
        if rivals:
            raise NamespaceAlreadyExistsError(
                f"bucket {bucket!r} is also claimed by another project's warehouse — the registry is "
                "contested; refusing to set a policy over another tenant's data (fix the warehouse "
                "records first)"
            )
    record = await _set(
        context.settings, "project", project, compose=lambda stored: _record("project", project, body, existing=stored, uuid=None, buckets=buckets)
    )
    log.info("maintenance_policy_set", extra={"kind": "project", "id": project})
    await emit_control(
        control,
        action="policy_set",
        object_type="policy",
        object_id=fga_ids.project_object(project),
        actor=actor_of(context.token),
        extra={"kind": "project", "buckets": buckets},
    )
    return _response(record)


@project_router.post("/{id}/policy/describe", response_model_exclude_none=True)
async def describe_project_policy(*, id: str, context: AuthzContextDep) -> PolicyResponse | PolicyUnset:
    """The project's maintenance policy — admin-gated like set/delete (``project`` defines no
    reader-tier relation) — or ``set: false`` when none is set; a project the registry does not know is not found."""
    project = _validated_project(id)
    await door_gates.require_relation(context, relation="can_administer", obj=fga_ids.project_object(project))
    # Read after the gate, so a caller who may not administer the project is refused alike whether it exists or not.
    if await run_in_threadpool(project_registry.get_project, context.settings.registry_root, context.settings.storage_options(), project) is None:
        raise TableNotFoundError(f"project not found: {project}")
    return await _describe(context.settings, kind="project", key=project, canonical=project, uuid=None)


@project_router.post("/{id}/policy/delete", response_model_exclude_none=True)
async def delete_project_policy(*, id: str, context: AuthzContextDep, control: ControlEmitterDep) -> PolicyDeleteResponse:
    """Remove the project's maintenance policy (idempotent) — admin-gated."""
    project = _validated_project(id)
    await door_gates.require_relation(context, relation="can_administer", obj=fga_ids.project_object(project))
    return await _delete(context.settings, control, context.token, kind="project", key=project, canonical=project)


def _named_now(settings: CatalogStore, records: list[PolicyRecord]) -> list[PolicyRecord]:
    """Each record, a table's named as its table is named now, which a rename since the policy was set changed; a
    table's whose table is gone governs nothing and is left out (``table_claims.names_now``)."""
    store = table_claims.claim_store(settings)
    named = (_with_current_name(store, record) for record in records)
    return [record for record in named if record is not None]


def _with_current_name(store: ClaimStore, record: PolicyRecord) -> PolicyRecord | None:
    """A table record with ``id`` its table's name now (:func:`table_claims.current_name`), ``None`` when that table is
    gone; any other record as kept. A namespace is never renamed, and a project's id is its own."""
    if record.kind != "table":
        return record
    name = table_claims.current_name(store, record.location, str(record.uuid)) if record.location else None
    return None if name is None else record.model_copy(update={"id": name})


def _scoped_to_project(record: PolicyRecord, *, warehouse_ids: frozenset[str], project: str) -> bool:
    """Whether one stored policy record governs data inside ``project``.

    A ``project`` record matches by ID and by nothing else — matching it on buckets would surface a rival
    tenant's project record the moment two registry entries contest a bucket (the state
    ``set_project_policy`` refuses at write time, and a read must not resurrect it).

    A table/namespace record matches when its object's name begins with one of the project's warehouses ([[LH-593]]).
    """
    if record.kind == "project":
        return record.id == project
    segments = parse_identifier(record.id)
    return bool(segments) and segments[0] in warehouse_ids


@projects_router.get("/{id}/policies", response_model_exclude_none=True)
async def list_project_policies(*, id: str, context: AuthzContextDep) -> ProjectPoliciesResponse:
    """Every maintenance policy governing data inside this project (#65) — the tenant-scoped VIEW.

    Admin-gated on ``project:<id>#can_administer``, exactly like ``describe_project_policy``: the trio
    already set that bar, and a second (estate-observer) door for the same information would split the
    tier — a tenant admin could then be shown one of their records and denied the list of all of them.

    ALL warehouse statuses count, not just active ones. ``set_project_policy`` filters to active because
    it is WRITING coverage (a policy over a bucket that no live warehouse serves could never match); this
    READS coverage, and a deactivated warehouse's data — plus any policy still governing it — remains the
    tenant's. Narrowing here would hide exactly the retention record an operator is looking for while
    offboarding.

    Tolerant like every other listing: an unreadable warehouse or policy record is skipped with a warning
    by the registry primitives themselves.
    """
    project = _validated_project(id)
    await door_gates.require_relation(context, relation="can_administer", obj=fga_ids.project_object(project))
    so = context.settings.storage_options()
    registered = await run_in_threadpool(warehouses.list_warehouses, context.settings.registry_root, so)
    mine = [r for r in registered if r.project == project]
    buckets = sorted({r.bucket for r in mine})
    warehouse_ids = sorted({r.id for r in mine})
    records = await run_in_threadpool(policies.list_policies, context.settings.registry_root, so)
    named = await run_in_threadpool(_named_now, context.settings, records)
    scoped = [r for r in named if _scoped_to_project(r, warehouse_ids=frozenset(warehouse_ids), project=project)]
    scoped.sort(key=lambda r: (r.kind, r.id))
    log.info("maintenance_policies_listed", extra={"project": project, "policies": len(scoped), "buckets": len(buckets), "warehouses": len(warehouse_ids)})
    return ProjectPoliciesResponse(project=project, buckets=buckets, warehouses=warehouse_ids, policies=[_response(r) for r in scoped])


# The v1 aggregator includes one ``router`` per module — the four policy routers are stitched here.
