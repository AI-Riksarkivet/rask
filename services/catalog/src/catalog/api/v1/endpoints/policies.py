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

A SET OR DELETE ANSWERS AT ONCE, AND NAMES WHAT IT DOES NOT PROMISE (ADR 0246, ``specs/maintenance/Reclaim.tla``
HoldWritesFirst). The door writes the record, THEN reads the condemned versions of every table the level governs and
names them in its answer (``condemned``): a reclaim batch condemns, then reads the policy, so of the door and a batch at
least one sees the other, and every version the answer does not name is kept as the policy says. A version it names may
be deleted by the batch already past its policy read, at most one batch of 16 per reclaim in flight, plus what
``version/delete`` or a dead batch condemned. No fence, no 409 for a reclaim.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from functools import partial
from typing import Any

from fastapi import APIRouter
from fastapi.concurrency import run_in_threadpool
from lance_namespace import (
    ConcurrentModificationError,
    InvalidInputError,
    LanceNamespace,
    NamespaceAlreadyExistsError,
)

from catalog.api import fga_deps
from catalog.api.dependencies import ControlEmitterDep, FgaClientDep, NamespaceDep, ResolvedTableDep, SettingsDep
from catalog.api.security import Caller, CurrentToken
from catalog.core.config import Settings
from catalog.core.identifiers import parse_identifier
from catalog.schemas import (
    CondemnedBeneath,
    CondemnedVersion,
    PolicyDeleteResponse,
    PolicyKind,
    PolicyRequest,
    PolicyResponse,
    PolicyUnset,
    ProjectPoliciesResponse,
)
from catalog.services import reclaims, table_claims, warehouses
from catalog.services.control_records import PROJECT_UUID
from lakehouse import maintenance_policies as policies
from lakehouse import records
from lakehouse.control_emit import ControlEmitter, emit_control
from lakehouse.governed import fga
from lakehouse.governed.tenancy import PROJECT, made_under, warehouses_of
from lakehouse.location_claims import ClaimStore
from lakehouse.naming import CONTROL_ID_RE


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


def _record(kind: str, canonical_id: str, body: PolicyRequest, existing: dict[str, object] | None, *, uuid: str | None) -> dict[str, object]:
    """The record to persist. A field the caller did not SET keeps whatever the stored record had.

    The record is written whole, so `model_dump()` alone made every set a full replace:
    a client that read a policy, changed one knob and sent it back wrote the model's DEFAULTS over
    every field it had not mentioned — silently clearing `scan_batch_size` (the compaction memory
    bound) and re-enabling a `cleanup_enabled=False` legal hold. The lakehouse form
    does exactly that, and its own comment states the belief this now makes true: "an omitted knob
    means inherit".

    Only EXPLICITLY set fields are applied (`exclude_unset`), which is also what the request model's
    empty-body guard already keys on — one notion of "the caller meant this", not two. ``existing`` is the record as it
    stands when the write lands (`policies.set_policy`), never one read earlier.
    """
    merged: dict[str, object] = dict(existing or {})
    merged.update(body.model_dump(exclude_unset=True))
    # Defaults still apply on a FIRST write, where there is nothing to inherit from.
    for field, value in body.model_dump().items():
        merged.setdefault(field, value)
    return {**merged, "kind": kind, "id": canonical_id, **({"uuid": uuid} if uuid is not None else {})}


async def _set(settings: Settings, kind: PolicyKind, key: str, compose: Callable[[dict[str, Any] | None], dict[str, Any]]) -> dict[str, Any]:
    """Write the record ``compose`` merges onto the one stored, conditional on that record (`policies.set_policy`), once
    for all three rungs; the record written.

    Raises:
        ConcurrentModificationError: The record kept changing under the set (409, code 14).
    """
    write = partial(policies.set_policy, settings.registry_root, settings.storage_options(), kind, key, compose)
    try:
        return await run_in_threadpool(write)
    except records.RecordChangedError as exc:
        raise ConcurrentModificationError(f"the {kind} policy kept changing while it was set; set it again") from exc


def _condemned_under(settings: Settings, *, table_uuid: str | None = None, within: Callable[[list[str]], bool] | None = None) -> CondemnedBeneath:
    """The versions already condemned of the table ``table_uuid``, or of every table whose name now ``within`` takes,
    read AFTER the door's write (HoldWritesFirst, ADR 0246): what its answer does not promise, the first
    :data:`reclaims.CONDEMNED_SHOWN` named and the rest counted. A record that cannot be read is named in the answer, never
    a refusal: the policy stands as written."""
    found = reclaims.condemned_versions_of(
        settings.registry_root, settings.storage_options(), table_claims.claim_store(settings), table_uuid=table_uuid, within=within
    )
    return CondemnedBeneath(
        condemned=[
            CondemnedVersion(table=name, branch=branch, branch_identifier=identifier, version=version)
            for name, branch, identifier, version in found.named[: reclaims.CONDEMNED_SHOWN]
        ],
        condemned_beyond=max(0, len(found.named) - reclaims.CONDEMNED_SHOWN),
        condemned_unread=found.unread,
    )


def _response(record: dict[str, object], *, condemned: CondemnedBeneath | None = None) -> PolicyResponse:
    return PolicyResponse.model_validate({**record, "set": True, **(condemned.model_dump() if condemned is not None else {})})


async def _describe(
    settings: Settings, *, kind: PolicyKind, key: str, canonical: str, uuid: str | None, project_uuid: str | None = None
) -> PolicyResponse | PolicyUnset:
    """Read one policy, once for all three rungs: its record, or :class:`PolicyUnset` where the object holds none. A
    project's record is its only when it names ``project_uuid``, its live creation (:func:`_of_creation`).

    Each route establishes its object before this read, a table's or namespace's by resolving its name and a
    project's by reading its registry record, so an object that does not stand, or that its caller may not see,
    never reaches it ([[LH-589]]).
    """
    record = await run_in_threadpool(policies.get_policy, settings.registry_root, settings.storage_options(), kind, key)
    if kind == "project":
        record = _of_creation(record, project_uuid)
    if record is None:
        return PolicyUnset(kind=kind, id=canonical, uuid=uuid, set=False)
    # Named as the door was addressed: the name the record kept is the one its object had when it was set.
    return _response({**record, "id": canonical})


async def _delete(
    settings: Settings, control: ControlEmitter, token: Caller | None, kind: str, *, key: str, canonical: str, condemned: Callable[[], CondemnedBeneath]
) -> PolicyDeleteResponse:
    """Remove one policy (idempotent), log it and announce it — once for all three rungs; the answer names the versions
    already condemned beneath it (``condemned``, read after the delete): removing a record hands what it governed to the
    next level. A hold that governs what it governed keeps every version the answer does not name; a retention the next
    level keeps more under binds the batches that condemn after the delete, and a batch already past its policy read
    finishes under the earlier one."""
    await run_in_threadpool(partial(policies.delete_policy, settings.registry_root, settings.storage_options(), kind, key))
    named = await run_in_threadpool(condemned)
    log.info("maintenance_policy_deleted", extra={"kind": kind, "id": canonical})
    await emit_control(
        control,
        action="policy_deleted",
        object_type="policy",
        # A table's or namespace's key is its uuid, the object its grants are held by; a project's is its id.
        object_id=f"{kind}:{key}",
        object_name=canonical if kind in ("table", "namespace") else None,
        actor=f"user:{token.subject}" if token else None,
        extra={"kind": kind},
    )
    return PolicyDeleteResponse(status="deleted", kind=kind, id=canonical, **named.model_dump())


@table_router.post("/{id}/policy/set", response_model_exclude_none=True)
async def set_table_policy(
    id: str,
    body: PolicyRequest,
    table: ResolvedTableDep,
    settings: SettingsDep,
    token: CurrentToken,
    control: ControlEmitterDep,
) -> PolicyResponse:
    """Set (or replace) the table's maintenance policy — owner-gated by the router (``can_drop``).

    Its settings govern the table over any namespace's or project's, but for a hold: ``cleanup_enabled`` false at any level
    that can govern the table keeps its whole history, and this record cannot lift a farther level's. Answers at once,
    naming the table's versions already condemned (``condemned``), which it does not promise."""
    canonical = fga.canonical_object_id(table.segments)
    table_uuid = table.require_uuid()
    # The location beside the uuid, so a listing names the table as it is named now (`table_claims.names_now`).
    record = await _set(
        settings, "table", table_uuid, lambda stored: {**_record("table", canonical, body, stored, uuid=table_uuid), "location": str(table.location)}
    )
    condemned = await run_in_threadpool(partial(_condemned_under, settings, table_uuid=table_uuid))
    log.info("maintenance_policy_set", extra={"kind": "table", "id": canonical})
    await emit_control(
        control,
        action="policy_set",
        object_type="policy",
        object_id=fga.table_object(table_uuid),
        object_name=canonical,
        actor=f"user:{token.subject}" if token else None,
        extra={"kind": "table"},
    )
    return _response(record, condemned=condemned)


@table_router.post("/{id}/policy/describe", response_model_exclude_none=True)
async def describe_table_policy(id: str, table: ResolvedTableDep, settings: SettingsDep) -> PolicyResponse | PolicyUnset:
    """The table's maintenance policy (reader-gated), or ``set: false`` when none is set."""
    uuid = table.require_uuid()
    return await _describe(settings, kind="table", key=uuid, canonical=fga.canonical_object_id(table.segments), uuid=uuid)


@table_router.post("/{id}/policy/delete", response_model_exclude_none=True)
async def delete_table_policy(id: str, table: ResolvedTableDep, settings: SettingsDep, token: CurrentToken, control: ControlEmitterDep) -> PolicyDeleteResponse:
    """Remove the table's maintenance policy (idempotent) — owner-gated by the router. The table is then governed by its
    nearest namespace's, else its project's, policy; the answer names its versions already condemned."""
    uuid = table.require_uuid()
    condemned = partial(_condemned_under, settings, table_uuid=uuid)
    return await _delete(settings, control, token, "table", key=uuid, canonical=fga.canonical_object_id(table.segments), condemned=condemned)


@namespace_router.post("/{id}/policy/set", response_model_exclude_none=True)
async def set_namespace_policy(
    id: str, body: PolicyRequest, ns: NamespaceDep, settings: SettingsDep, token: CurrentToken, control: ControlEmitterDep
) -> PolicyResponse:
    """Set (or replace) a namespace-level policy — applies to every table under the namespace, at any depth: its
    retention and cadence unless a table policy or a nearer namespace's sets them, and a hold (``cleanup_enabled`` false)
    over every table beneath it whatever their own policies say. Answers at once, naming the versions already condemned
    of every table beneath it (``condemned``), which it does not promise. Owner-gated by the router (``can_delete``)."""
    namespace = await _namespace(id, ns, settings)
    canonical = fga.canonical_object_id(namespace.segments)
    record = await _set(settings, "namespace", namespace.uuid, lambda stored: _record("namespace", canonical, body, stored, uuid=namespace.uuid))
    condemned = await run_in_threadpool(partial(_condemned_under, settings, within=_beneath(namespace.segments)))
    log.info("maintenance_policy_set", extra={"kind": "namespace", "id": canonical})
    await emit_control(
        control,
        action="policy_set",
        object_type="policy",
        object_id=fga.namespace_object(namespace.uuid),
        object_name=canonical,
        actor=f"user:{token.subject}" if token else None,
        extra={"kind": "namespace"},
    )
    return _response(record, condemned=condemned)


@namespace_router.post("/{id}/policy/describe", response_model_exclude_none=True)
async def describe_namespace_policy(id: str, ns: NamespaceDep, settings: SettingsDep) -> PolicyResponse | PolicyUnset:
    """The namespace's maintenance policy (reader-gated), or ``set: false`` when none is set."""
    namespace = await _namespace(id, ns, settings)
    return await _describe(settings, kind="namespace", key=namespace.uuid, canonical=fga.canonical_object_id(namespace.segments), uuid=namespace.uuid)


@namespace_router.post("/{id}/policy/delete", response_model_exclude_none=True)
async def delete_namespace_policy(id: str, ns: NamespaceDep, settings: SettingsDep, token: CurrentToken, control: ControlEmitterDep) -> PolicyDeleteResponse:
    """Remove the namespace's maintenance policy (idempotent) — owner-gated by the router. The answer names the versions
    already condemned beneath it."""
    namespace = await _namespace(id, ns, settings)
    condemned = partial(_condemned_under, settings, within=_beneath(namespace.segments))
    return await _delete(settings, control, token, "namespace", key=namespace.uuid, canonical=fga.canonical_object_id(namespace.segments), condemned=condemned)


def _beneath(segments: list[str]) -> Callable[[list[str]], bool]:
    """Whether a table's name lies beneath the namespace ``segments`` names."""
    return lambda name: name[: len(segments)] == segments and len(name) > len(segments)


def _in_warehouses(warehouse_ids: set[str]) -> Callable[[list[str]], bool]:
    """Whether a table's name lies in one of ``warehouse_ids`` (its first segment, [[LH-593]])."""
    return lambda name: bool(name) and name[0] in warehouse_ids


async def _namespace(id: str, ns: LanceNamespace, settings: Settings) -> fga_deps.GovernedObject:
    """The namespace ``id`` names, with the uuid its policy is kept under; the root holds no policy."""
    segments = parse_identifier(id)
    if not segments:
        raise InvalidInputError("a policy needs a concrete namespace, not the root")
    return await fga_deps.identify(ns, settings, "namespace", segments)


def _of_creation(record: dict[str, Any] | None, project_uuid: str | None) -> dict[str, Any] | None:
    """``record``, the policy stored under a project's id, where it was set by the creation ``project_uuid`` of the
    project, the live one; ``None`` for one an earlier creation left (ADR 0241 §9 P2, ADR 0245), which is no policy of the
    tenant holding the id now: it is neither read nor merged onto, and the converge tick reports it."""
    if record is None or not made_under(record, project_uuid):
        return None
    return record


def _validated_project(id: str) -> str:
    if not _PROJECT_ID_RE.match(id):
        raise InvalidInputError(f"invalid project id {id!r}: must match {_PROJECT_ID_RE.pattern}")
    return id


@project_router.post("/{id}/policy/set", response_model_exclude_none=True)
async def set_project_policy(
    id: str,
    body: PolicyRequest,
    settings: SettingsDep,
    token: CurrentToken,
    client: FgaClientDep,
    control: ControlEmitterDep,
) -> PolicyResponse:
    """Set (or replace) the project-level maintenance policy (#84) — the tenant-wide default the sweep
    falls back to when no table or namespace policy matches, and, when it holds (``cleanup_enabled`` false), a hold over
    every table in the project's buckets whatever their own policies say. Answers at once, naming the versions already
    condemned of every table in the project's warehouses (``condemned``), which it does not promise. Admin-gated
    (``can_administer`` on ``project:<id>``, checked explicitly — see the module docstring).

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
    tenant = await fga_deps.require_on_tenant(client, settings, token, relation="can_administer", kind=PROJECT, object_id=project)
    so = settings.storage_options()
    standing = tenant.uuid
    # Read whole ([[LH-572]]): a record that cannot be read is a bucket of this project the policy would leave on the
    # defaults, or another project's claim on one it would govern. A record a dead creation's create left is no warehouse
    # of the live one (ADR 0241 §9 P2, ADR 0245).
    registered = await run_in_threadpool(warehouses.every_warehouse, settings.registry_root, so)
    mine = warehouses_of(registered, standing)
    buckets = sorted({str(r["bucket"]) for r in mine if r.get("bucket") and (r.get("status") or "active") == "active"})
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
    # The record names the creation that set it, and merges onto none an earlier creation of the id left.
    record = await _set(
        settings,
        "project",
        project,
        lambda stored: {
            **_record("project", project, body, _of_creation(stored, standing), uuid=None),
            "buckets": buckets,
            PROJECT_UUID: standing,
        },
    )
    condemned = await run_in_threadpool(partial(_condemned_under, settings, within=_in_warehouses({str(r["id"]) for r in mine if r.get("id")})))
    log.info("maintenance_policy_set", extra={"kind": "project", "id": project})
    await emit_control(
        control,
        action="policy_set",
        object_type="policy",
        object_id=f"project:{project}",
        actor=f"user:{token.subject}" if token else None,
        extra={"kind": "project", "buckets": buckets},
    )
    return _response(record, condemned=condemned)


@project_router.post("/{id}/policy/describe", response_model_exclude_none=True)
async def describe_project_policy(id: str, settings: SettingsDep, token: CurrentToken, client: FgaClientDep) -> PolicyResponse | PolicyUnset:
    """The project's maintenance policy — admin-gated like set/delete (``project`` defines no
    reader-tier relation) — or ``set: false`` when none is set; a project the registry does not know is not found."""
    project = _validated_project(id)
    # Gated before the existence is told, so a caller who may not administer the project is refused alike whether it
    # exists or not (``fga_deps.require_on_tenant``).
    tenant = await fga_deps.require_on_tenant(client, settings, token, relation="can_administer", kind=PROJECT, object_id=project)
    return await _describe(settings, kind="project", key=project, canonical=project, uuid=None, project_uuid=tenant.uuid)


@project_router.post("/{id}/policy/delete", response_model_exclude_none=True)
async def delete_project_policy(id: str, settings: SettingsDep, token: CurrentToken, client: FgaClientDep, control: ControlEmitterDep) -> PolicyDeleteResponse:
    """Remove the project's maintenance policy (idempotent) — admin-gated. The answer names the versions already condemned
    of every table in the project's warehouses."""
    project = _validated_project(id)
    tenant = await fga_deps.require_on_tenant(client, settings, token, relation="can_administer", kind=PROJECT, object_id=project)
    so = settings.storage_options()
    mine = warehouses_of(await run_in_threadpool(warehouses.every_warehouse, settings.registry_root, so), tenant.uuid)
    condemned = partial(_condemned_under, settings, within=_in_warehouses({str(r["id"]) for r in mine if r.get("id")}))
    return await _delete(settings, control, token, "project", key=project, canonical=project, condemned=condemned)


def _named_now(settings: Settings, records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Each record, a table's named as its table is named now, which a rename since the policy was set changed; a
    table's whose table is gone governs nothing and is left out (``table_claims.names_now``)."""
    store = table_claims.claim_store(settings)
    named = (_with_current_name(store, record) for record in records)
    return [record for record in named if record is not None]


def _with_current_name(store: ClaimStore, record: dict[str, Any]) -> dict[str, Any] | None:
    """A table record with ``id`` its table's name now (:func:`table_claims.current_name`), ``None`` when that table is
    gone; any other record as kept. A namespace is never renamed, and a project's id is its own."""
    if record.get("kind") != "table":
        return record
    location, uuid = record.get("location"), record.get("uuid")
    name = table_claims.current_name(store, str(location), str(uuid)) if location and uuid else None
    return None if name is None else {**record, "id": name}


def _scoped_to_project(record: dict[str, Any], *, warehouse_ids: frozenset[str], project: str, project_uuid: str | None) -> bool:
    """Whether one stored policy record governs data inside ``project``'s live creation ``project_uuid``.

    A ``project`` record matches by ID and creation and by nothing else — matching it on buckets would surface a rival
    tenant's project record the moment two registry entries contest a bucket (the state
    ``set_project_policy`` refuses at write time, and a read must not resurrect it), and one an earlier creation of the
    id left is the dead tenant's (:func:`_of_creation`).

    A table/namespace record matches when its object's name begins with one of the project's warehouses ([[LH-593]]).
    """
    kind = record.get("kind")
    if kind == "project":
        return record.get("id") == project and _of_creation(record, project_uuid) is not None
    if kind not in ("table", "namespace"):
        return False
    segments = parse_identifier(str(record.get("id") or ""))
    return bool(segments) and segments[0] in warehouse_ids


@projects_router.get("/{id}/policies", response_model_exclude_none=True)
async def list_project_policies(id: str, settings: SettingsDep, token: CurrentToken, client: FgaClientDep) -> ProjectPoliciesResponse:
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
    tenant = await fga_deps.require_on_tenant(client, settings, token, relation="can_administer", kind=PROJECT, object_id=project)
    so = settings.storage_options()
    standing = tenant.uuid
    registered = await run_in_threadpool(warehouses.list_warehouses, settings.registry_root, so)
    mine = warehouses_of(registered, standing)
    buckets = sorted({str(r["bucket"]) for r in mine if r.get("bucket")})
    warehouse_ids = sorted({str(r["id"]) for r in mine if r.get("id")})
    records = await run_in_threadpool(policies.list_policies, settings.registry_root, so)
    named = await run_in_threadpool(_named_now, settings, records)
    scoped = [r for r in named if _scoped_to_project(r, warehouse_ids=frozenset(warehouse_ids), project=project, project_uuid=standing)]
    scoped.sort(key=lambda r: (str(r.get("kind")), str(r.get("id"))))
    log.info("maintenance_policies_listed", extra={"project": project, "policies": len(scoped), "buckets": len(buckets), "warehouses": len(warehouse_ids)})
    return ProjectPoliciesResponse(project=project, buckets=buckets, warehouses=warehouse_ids, policies=[_response(r) for r in scoped])


# The v1 aggregator includes one ``router`` per module — the four policy routers are stitched here.
router = APIRouter()
router.include_router(table_router)
router.include_router(namespace_router)
router.include_router(project_router)
router.include_router(projects_router)
