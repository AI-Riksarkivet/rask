"""Maintenance-policy endpoints (#50/#84): per-table/namespace/project overrides for the compaction sweep.

``POST …/policy/set`` and ``…/policy/delete`` are owner-tier (the router-level ``authorize`` maps them
to ``can_drop``/``can_delete`` — a retention policy authorizes destroying version history), and land on
the #41 audit trail through that same gate; ``…/policy/describe`` is a reader-tier metadata read. The
record stores both the logical id and the physical bucket-qualified path (resolved here), so the
compaction sweep enforces policies straight off the bucket with no catalog round-trip. Old-version
cleanup can never remove a tag-pinned version (Lance exempts tagged versions), so ``blessed`` and every
other promotion tag survive any policy.

The #84 PROJECT policy is the tenant-wide fallback below both: it matches by the project's warehouse
BUCKETS (resolved from the warehouse registry at set time), so it covers every dataset in the project's
buckets that no table/namespace record shadows. ``/v1/project`` is not a router-guarded resource prefix,
so — like the #17 model routes — these endpoints gate EXPLICITLY, on ``project:<id>#can_administer``
(the model's tenant-admin action; ``project`` defines no reader-tier relation, so describe gates there
too rather than checking a phantom relation, which would 400 → fail-closed 503 for everyone).
"""

from __future__ import annotations

import logging

from fastapi import APIRouter
from fastapi.concurrency import run_in_threadpool
from lance_namespace import (
    DescribeTableRequest,
    InvalidInputError,
    NamespaceAlreadyExistsError,
    NamespaceNotFoundError,
    TableNotFoundError,
)

from catalog.api import fga_deps
from catalog.api.dependencies import ControlEmitterDep, FgaClientDep, NamespaceDep, SettingsDep
from catalog.api.security import CurrentToken
from catalog.core.control_emit import emit_control
from catalog.core.identifiers import CONTROL_ID_RE, parse_identifier
from catalog.schemas import PolicyDeleteResponse, PolicyRequest, PolicyResponse
from catalog.services import native, warehouses
from service_kit.governed import fga
from service_kit.lakehouse import maintenance_policies as policies


log = logging.getLogger(__name__)

table_router = APIRouter(prefix="/v1/table", tags=["policy"])
namespace_router = APIRouter(prefix="/v1/namespace", tags=["policy"])
project_router = APIRouter(prefix="/v1/project", tags=["policy"])

# The same DNS-safe shape the whole control plane enforces for project ids — a malformed id must never
# become a path-traversing registry key or a phantom FGA object. SHARED, not re-declared: this was one of
# three copies that had drifted apart on their end anchor (`catalog.core.identifiers.CONTROL_ID_RE`).
_PROJECT_ID_RE = CONTROL_ID_RE


def _canonical(segments: list[str], delimiter: str) -> str:
    return fga.canonical_object_id(segments, delimiter=delimiter)


def _record(kind: str, canonical_id: str, path: str, body: PolicyRequest) -> dict[str, object]:
    return {"kind": kind, "id": canonical_id, "path": path, **body.model_dump()}


def _response(record: dict[str, object]) -> PolicyResponse:
    return PolicyResponse.model_validate(record)


def _sweep_path(location: str) -> str:
    """The sweep's match key for a storage location: bucket-qualified (``<bucket>/<path>``), so a
    policy can never govern a same-named path in a different tenant's bucket (the sweep spans the
    per-warehouse and multi-base buckets too)."""
    return location.removeprefix("s3://").rstrip("/")


@table_router.post("/{id}/policy/set", response_model_exclude_none=True)
async def set_table_policy(
    id: str,
    body: PolicyRequest,
    ns: NamespaceDep,
    settings: SettingsDep,
    token: CurrentToken,
    control: ControlEmitterDep,
) -> PolicyResponse:
    """Set (or replace) the table's maintenance policy — owner-gated by the router (``can_drop``)."""
    segments = parse_identifier(id, settings.delimiter)
    described = await run_in_threadpool(native.call, ns, "describe_table", DescribeTableRequest(id=segments))
    if described.location is None:
        raise TableNotFoundError(f"table {id!r} has no storage location to police")
    canonical = _canonical(segments, settings.delimiter)
    record = _record("table", canonical, _sweep_path(described.location), body)
    await run_in_threadpool(policies.put_policy, settings.registry_root, settings.storage_options(), record)
    log.info("maintenance_policy_set", extra={"kind": "table", "id": canonical})
    await emit_control(
        control,
        action="policy_set",
        object_type="policy",
        object_id=f"table:{canonical}",
        actor=f"user:{token.sub}" if token else None,
        extra={"kind": "table", "path": record["path"]},
    )
    return _response(record)


@table_router.post("/{id}/policy/describe", response_model_exclude_none=True)
async def describe_table_policy(id: str, settings: SettingsDep) -> PolicyResponse:
    """The table's maintenance policy (reader-gated); 404 when none is set."""
    canonical = _canonical(parse_identifier(id, settings.delimiter), settings.delimiter)
    record = await run_in_threadpool(policies.get_policy, settings.registry_root, settings.storage_options(), "table", canonical)
    if record is None:
        raise TableNotFoundError(f"no maintenance policy set for table {id!r}")
    return _response(record)


@table_router.post("/{id}/policy/delete", response_model_exclude_none=True)
async def delete_table_policy(id: str, settings: SettingsDep, token: CurrentToken, control: ControlEmitterDep) -> PolicyDeleteResponse:
    """Remove the table's maintenance policy (idempotent) — owner-gated by the router."""
    canonical = _canonical(parse_identifier(id, settings.delimiter), settings.delimiter)
    await run_in_threadpool(policies.delete_policy, settings.registry_root, settings.storage_options(), "table", canonical)
    log.info("maintenance_policy_deleted", extra={"kind": "table", "id": canonical})
    await emit_control(
        control,
        action="policy_deleted",
        object_type="policy",
        object_id=f"table:{canonical}",
        actor=f"user:{token.sub}" if token else None,
        extra={"kind": "table"},
    )
    return PolicyDeleteResponse(status="deleted", kind="table", id=canonical)


@namespace_router.post("/{id}/policy/set", response_model_exclude_none=True)
async def set_namespace_policy(id: str, body: PolicyRequest, settings: SettingsDep, token: CurrentToken, control: ControlEmitterDep) -> PolicyResponse:
    """Set (or replace) a namespace-level policy — applies to every dataset under the namespace's
    directory prefix unless a table policy overrides it. Owner-gated by the router (``can_delete``)."""
    segments = parse_identifier(id, settings.delimiter)
    if not segments:
        raise InvalidInputError("a policy needs a concrete namespace, not the root")
    canonical = _canonical(segments, settings.delimiter)
    # The dir backend maps a namespace to its directory prefix under the root bucket.
    prefix = _sweep_path(f"{settings.root.rstrip('/')}/{'/'.join(segments)}")
    record = _record("namespace", canonical, prefix, body)
    await run_in_threadpool(policies.put_policy, settings.registry_root, settings.storage_options(), record)
    log.info("maintenance_policy_set", extra={"kind": "namespace", "id": canonical})
    await emit_control(
        control,
        action="policy_set",
        object_type="policy",
        object_id=f"namespace:{canonical}",
        actor=f"user:{token.sub}" if token else None,
        extra={"kind": "namespace", "path": record["path"]},
    )
    return _response(record)


@namespace_router.post("/{id}/policy/describe", response_model_exclude_none=True)
async def describe_namespace_policy(id: str, settings: SettingsDep) -> PolicyResponse:
    """The namespace's maintenance policy (reader-gated); 404 when none is set."""
    canonical = _canonical(parse_identifier(id, settings.delimiter), settings.delimiter)
    record = await run_in_threadpool(policies.get_policy, settings.registry_root, settings.storage_options(), "namespace", canonical)
    if record is None:
        raise NamespaceNotFoundError(f"no maintenance policy set for namespace {id!r}")
    return _response(record)


@namespace_router.post("/{id}/policy/delete", response_model_exclude_none=True)
async def delete_namespace_policy(id: str, settings: SettingsDep, token: CurrentToken, control: ControlEmitterDep) -> PolicyDeleteResponse:
    """Remove the namespace's maintenance policy (idempotent) — owner-gated by the router."""
    canonical = _canonical(parse_identifier(id, settings.delimiter), settings.delimiter)
    await run_in_threadpool(policies.delete_policy, settings.registry_root, settings.storage_options(), "namespace", canonical)
    log.info("maintenance_policy_deleted", extra={"kind": "namespace", "id": canonical})
    await emit_control(
        control,
        action="policy_deleted",
        object_type="policy",
        object_id=f"namespace:{canonical}",
        actor=f"user:{token.sub}" if token else None,
        extra={"kind": "namespace"},
    )
    return PolicyDeleteResponse(status="deleted", kind="namespace", id=canonical)


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
    await fga_deps.require_relation(client, settings, token, relation="can_administer", obj=f"project:{project}")
    so = settings.storage_options()
    registered = await run_in_threadpool(warehouses.list_warehouses, settings.registry_root, so)
    buckets = sorted({str(r["bucket"]) for r in registered if r.get("bucket") and r.get("project") == project and (r.get("status") or "active") == "active"})
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
    record = {**_record("project", project, "", body), "buckets": buckets}
    await run_in_threadpool(policies.put_policy, settings.registry_root, so, record)
    log.info("maintenance_policy_set", extra={"kind": "project", "id": project})
    await emit_control(
        control,
        action="policy_set",
        object_type="policy",
        object_id=f"project:{project}",
        actor=f"user:{token.sub}" if token else None,
        extra={"kind": "project", "buckets": buckets},
    )
    return _response(record)


@project_router.post("/{id}/policy/describe", response_model_exclude_none=True)
async def describe_project_policy(id: str, settings: SettingsDep, token: CurrentToken, client: FgaClientDep) -> PolicyResponse:
    """The project's maintenance policy — admin-gated like set/delete (``project`` defines no
    reader-tier relation); 404 when none is set."""
    project = _validated_project(id)
    await fga_deps.require_relation(client, settings, token, relation="can_administer", obj=f"project:{project}")
    record = await run_in_threadpool(policies.get_policy, settings.registry_root, settings.storage_options(), "project", project)
    if record is None:
        raise TableNotFoundError(f"no maintenance policy set for project {id!r}")
    return _response(record)


@project_router.post("/{id}/policy/delete", response_model_exclude_none=True)
async def delete_project_policy(id: str, settings: SettingsDep, token: CurrentToken, client: FgaClientDep, control: ControlEmitterDep) -> PolicyDeleteResponse:
    """Remove the project's maintenance policy (idempotent) — admin-gated."""
    project = _validated_project(id)
    await fga_deps.require_relation(client, settings, token, relation="can_administer", obj=f"project:{project}")
    await run_in_threadpool(policies.delete_policy, settings.registry_root, settings.storage_options(), "project", project)
    log.info("maintenance_policy_deleted", extra={"kind": "project", "id": project})
    await emit_control(
        control,
        action="policy_deleted",
        object_type="policy",
        object_id=f"project:{project}",
        actor=f"user:{token.sub}" if token else None,
        extra={"kind": "project"},
    )
    return PolicyDeleteResponse(status="deleted", kind="project", id=project)


# The v1 aggregator includes one ``router`` per module — the three policy routers are stitched here.
router = APIRouter()
router.include_router(table_router)
router.include_router(namespace_router)
router.include_router(project_router)
