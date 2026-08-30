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

The #65 project-scoped LIST (``GET /v1/projects/{id}/policies``) is the tenant's VIEW of all three tiers
at once. It scopes table/namespace records BINDING-DERIVED (logical id → top-level segment → namespace
binding → warehouse → project) rather than by the record's stored bucket, and that is not a stylistic
choice: ``set_namespace_policy`` below builds its ``path`` from ``settings.root`` — the DEFAULT root —
because the handler takes no ``NamespaceDep`` and so never resolves the namespace's warehouse root the
way ``set_table_policy`` does. Every namespace record for a warehouse-bound namespace therefore carries
the WRONG bucket, and a bucket-only filter would both miss the owning project's records and attribute
them to whoever owns the default bucket. That defect is deliberately NOT fixed here (it changes stored-
record semantics for the sweep, which survives it only because ``resolve_policy`` also matches namespace
records by logical parent chain) — the listing has to be correct despite it, and stays correct if it is
ever fixed, because the binding half is the PRIMARY match and the bucket half only adds records whose
physical path already lives in one of the project's own warehouse buckets.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter
from fastapi.concurrency import run_in_threadpool
from lance_namespace import (
    DescribeTableRequest,
    InvalidInputError,
    LanceNamespaceError,
    NamespaceAlreadyExistsError,
    NamespaceNotFoundError,
    TableNotFoundError,
)

from catalog.api import fga_deps
from catalog.api.dependencies import ControlEmitterDep, FgaClientDep, NamespaceDep, SettingsDep
from catalog.api.security import CurrentToken
from catalog.core.config import Settings
from catalog.core.identifiers import CONTROL_ID_RE, parse_identifier
from catalog.schemas import PolicyDeleteResponse, PolicyRequest, PolicyResponse, ProjectPoliciesResponse
from catalog.services import native, warehouses
from service_kit.control_emit import ControlEmitter, emit_control
from service_kit.governed import fga
from service_kit.governed.oidc import IDToken
from service_kit.lakehouse import maintenance_policies as policies


log = logging.getLogger(__name__)

table_router = APIRouter(prefix="/v1/table", tags=["policy"])
namespace_router = APIRouter(prefix="/v1/namespace", tags=["policy"])
project_router = APIRouter(prefix="/v1/project", tags=["policy"])
# PLURAL, and deliberately not a fourth spelling: the LIST is a collection read on the control-plane
# ``projects`` resource (``endpoints/projects.py``'s prefix), while the singular ``/v1/project`` keeps the
# spec's ``POST /v1/<object>/{id}/<action>`` grammar for the set/describe/delete trio.
projects_router = APIRouter(prefix="/v1/projects", tags=["policy"])

# The same DNS-safe shape the whole control plane enforces for project ids — a malformed id must never
# become a path-traversing registry key or a phantom FGA object. SHARED, not re-declared: this was one of
# three copies that had drifted apart on their end anchor (`catalog.core.identifiers.CONTROL_ID_RE`).
_PROJECT_ID_RE = CONTROL_ID_RE


def _canonical(segments: list[str], delimiter: str) -> str:
    return fga.canonical_object_id(segments, delimiter=delimiter)


def _record(kind: str, canonical_id: str, path: str, body: PolicyRequest, existing: dict[str, object] | None = None) -> dict[str, object]:
    """The record to persist. A field the caller did not SET keeps whatever the stored record had.

    `put_policy` overwrites the whole object, so `model_dump()` alone made every set a full replace:
    a client that read a policy, changed one knob and sent it back wrote the model's DEFAULTS over
    every field it had not mentioned — silently clearing `scan_batch_size` (the compaction memory
    bound) and `auto_cleanup_interval_commits` (which owns version reclamation). The lakehouse form
    does exactly that, and its own comment states the belief this now makes true: "an omitted knob
    means inherit".

    Only EXPLICITLY set fields are applied (`exclude_unset`), which is also what the request model's
    empty-body guard already keys on — one notion of "the caller meant this", not two.
    """
    merged: dict[str, object] = dict(existing or {})
    merged.update(body.model_dump(exclude_unset=True))
    # Defaults still apply on a FIRST write, where there is nothing to inherit from.
    for field, value in body.model_dump().items():
        merged.setdefault(field, value)
    return {**merged, "kind": kind, "id": canonical_id, "path": path}


def _response(record: dict[str, object]) -> PolicyResponse:
    return PolicyResponse.model_validate(record)


def _sweep_path(location: str) -> str:
    """The sweep's match key for a storage location: bucket-qualified (``<bucket>/<path>``), so a
    policy can never govern a same-named path in a different tenant's bucket (the sweep spans the
    per-warehouse and multi-base buckets too)."""
    return location.removeprefix("s3://").rstrip("/")


async def _describe(settings: Settings, kind: str, canonical: str, missing: type[LanceNamespaceError]) -> PolicyResponse:
    """Read one policy or 404, once (catalog-api-14).

    The three describe handlers each carried their own copy of read-then-refuse, and the copies had
    already drifted: table and PROJECT both raised ``TableNotFoundError`` for a missing policy while
    namespace raised ``NamespaceNotFoundError`` — three rungs, two answers, for one condition. The
    exception class is a parameter now, so the choice is stated at the route instead of re-derived.
    """
    record = await run_in_threadpool(policies.get_policy, settings.registry_root, settings.storage_options(), kind, canonical)
    if record is None:
        raise missing(f"no maintenance policy set for {kind} {canonical!r}")
    return _response(record)


async def _delete(settings: Settings, control: ControlEmitter, token: IDToken | None, kind: str, canonical: str) -> PolicyDeleteResponse:
    """Remove one policy (idempotent), log it and announce it — once for all three rungs."""
    await run_in_threadpool(policies.delete_policy, settings.registry_root, settings.storage_options(), kind, canonical)
    log.info("maintenance_policy_deleted", extra={"kind": kind, "id": canonical})
    await emit_control(
        control,
        action="policy_deleted",
        object_type="policy",
        object_id=f"{kind}:{canonical}",
        actor=f"user:{token.sub}" if token else None,
        extra={"kind": kind},
    )
    return PolicyDeleteResponse(status="deleted", kind=kind, id=canonical)


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
    prior = await run_in_threadpool(policies.get_policy, settings.registry_root, settings.storage_options(), "table", canonical)
    record = _record("table", canonical, _sweep_path(described.location), body, prior)
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
    return await _describe(settings, "table", _canonical(parse_identifier(id, settings.delimiter), settings.delimiter), TableNotFoundError)


@table_router.post("/{id}/policy/delete", response_model_exclude_none=True)
async def delete_table_policy(id: str, settings: SettingsDep, token: CurrentToken, control: ControlEmitterDep) -> PolicyDeleteResponse:
    """Remove the table's maintenance policy (idempotent) — owner-gated by the router."""
    return await _delete(settings, control, token, "table", _canonical(parse_identifier(id, settings.delimiter), settings.delimiter))


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
    prior = await run_in_threadpool(policies.get_policy, settings.registry_root, settings.storage_options(), "namespace", canonical)
    record = _record("namespace", canonical, prefix, body, prior)
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
    return await _describe(settings, "namespace", _canonical(parse_identifier(id, settings.delimiter), settings.delimiter), NamespaceNotFoundError)


@namespace_router.post("/{id}/policy/delete", response_model_exclude_none=True)
async def delete_namespace_policy(id: str, settings: SettingsDep, token: CurrentToken, control: ControlEmitterDep) -> PolicyDeleteResponse:
    """Remove the namespace's maintenance policy (idempotent) — owner-gated by the router."""
    return await _delete(settings, control, token, "namespace", _canonical(parse_identifier(id, settings.delimiter), settings.delimiter))


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
    prior = await run_in_threadpool(policies.get_policy, settings.registry_root, so, "project", project)
    record = {**_record("project", project, "", body, prior), "buckets": buckets}
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
    # `TableNotFoundError` on the PROJECT rung is what the copied body already raised — the spec code
    # is what maps to the 404, and changing it here would be a contract change, not a refactor.
    return await _describe(settings, "project", project, TableNotFoundError)


@project_router.post("/{id}/policy/delete", response_model_exclude_none=True)
async def delete_project_policy(id: str, settings: SettingsDep, token: CurrentToken, client: FgaClientDep, control: ControlEmitterDep) -> PolicyDeleteResponse:
    """Remove the project's maintenance policy (idempotent) — admin-gated."""
    project = _validated_project(id)
    await fga_deps.require_relation(client, settings, token, relation="can_administer", obj=f"project:{project}")
    return await _delete(settings, control, token, "project", project)


def _scoped_to_project(record: dict[str, Any], *, delimiter: str, namespaces: frozenset[str], buckets: frozenset[str], project: str) -> bool:
    """Whether one stored policy record governs data inside ``project``.

    A ``project`` record matches by ID and by nothing else — matching it on buckets would surface a rival
    tenant's project record the moment two registry entries contest a bucket (the state
    ``set_project_policy`` refuses at write time, and a read must not resurrect it).

    A table/namespace record matches when its logical id's TOP-LEVEL segment is a namespace bound to one
    of the project's warehouses (the primary — see the module docstring on the default-root defect), or,
    failing that, when the bucket of its physical ``path`` is one of the project's own warehouse buckets
    (which is what reaches the medallion-nested datasets whose ids carry no binding).
    """
    kind = record.get("kind")
    if kind == "project":
        return record.get("id") == project
    if kind not in ("table", "namespace"):
        return False
    segments = parse_identifier(str(record.get("id") or ""), delimiter)
    if segments and segments[0] in namespaces:
        return True
    return str(record.get("path") or "").split("/", 1)[0] in buckets


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
    by the registry primitives themselves. An unreadable BINDING is different and is reported —
    ``read_bindings`` (not ``list_bindings``) so the skip list crosses the return boundary into
    ``incomplete``/``skipped_bindings``. A binding you cannot read is a namespace you cannot see, and a
    quietly short list would read as checked-and-clean. It still answers 200: this is a listing, not the
    destructive door ``namespaces_bound_to`` guards, where one corrupt object correctly refuses the whole
    operation.
    """
    project = _validated_project(id)
    await fga_deps.require_relation(client, settings, token, relation="can_administer", obj=f"project:{project}")
    so = settings.storage_options()
    registered = await run_in_threadpool(warehouses.list_warehouses, settings.registry_root, so)
    mine = [r for r in registered if r.get("project") == project]
    buckets = sorted({str(r["bucket"]) for r in mine if r.get("bucket")})
    warehouse_ids = {str(r["id"]) for r in mine if r.get("id")}
    bindings, skipped = await run_in_threadpool(warehouses.read_bindings, settings.registry_root, so)
    namespaces = sorted({str(b["top_ns"]) for b in bindings if b.get("top_ns") and b.get("warehouse_id") in warehouse_ids})
    records = await run_in_threadpool(policies.list_policies, settings.registry_root, so)
    # Defense in depth (the `set_project_policy` cross-claim precedent): the platform's own buckets — the
    # catalog root, the control/registry root, the model registry, the medallion zones — are refused at
    # warehouse-create, so a project record claiming one means the registry is contested. Honour the
    # reservation on the MATCH set rather than trusting it, or one bad record would hand this tenant every
    # policy in the shared root; `buckets` still reports what the registry actually says.
    matchable = frozenset(buckets) - settings.reserved_bucket_set
    scoped = [r for r in records if _scoped_to_project(r, delimiter=settings.delimiter, namespaces=frozenset(namespaces), buckets=matchable, project=project)]
    scoped.sort(key=lambda r: (str(r.get("kind")), str(r.get("id"))))
    log.info(
        "maintenance_policies_listed",
        extra={"project": project, "policies": len(scoped), "buckets": len(buckets), "namespaces": len(namespaces), "skipped_bindings": len(skipped)},
    )
    return ProjectPoliciesResponse(
        project=project,
        buckets=buckets,
        namespaces=namespaces,
        policies=[_response(r) for r in scoped],
        incomplete=bool(skipped),
        skipped_bindings=sorted(skipped),
    )


# The v1 aggregator includes one ``router`` per module — the four policy routers are stitched here.
router = APIRouter()
router.include_router(table_router)
router.include_router(namespace_router)
router.include_router(project_router)
router.include_router(projects_router)
