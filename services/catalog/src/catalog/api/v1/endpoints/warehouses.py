"""Admin control-plane endpoints: warehouse provisioning + warehouse-scoped namespaces (#3-A).

A *warehouse* = one physically separate S3 bucket owned by a project (the FGA model's catalog-root type).
These routes are the **control plane** the catalog lacked: an authorized project-admin provisions a bucket
at RUNTIME (not the static Helm `mc mb` loop) and creates namespaces bound to it, so tables under those
namespaces land in that warehouse's bucket — physically isolated from every other tenant's. Physical
multi-tenancy: one project may hold many warehouses, and a table's bytes live in the bucket its
namespace is bound to, never in a neighbour's.

Authorization is DELIBERATELY stronger than the data plane: warehouse-create gates on the project's
`can_create_warehouse` (= admin) — the model action that until now was defined but never enforced — not the
writer-tier create-on-parent that guards tables/namespaces. The one exception is a project that does not
exist yet: it has no tuples, so that check could only ever deny and NOBODY could create a tenant's first
warehouse. There the estate admin (`can_observe_events` on the root, the `/v1/events` bar) is accepted too,
and the create seeds the caller as the new project's `admin` so the tenant is self-sustaining from then on.
"""

from __future__ import annotations

import logging
import re
from datetime import UTC, datetime

from fastapi import APIRouter, Request
from fastapi.concurrency import run_in_threadpool
from lance_namespace import (
    CreateNamespaceRequest,
    CreateNamespaceResponse,
    InvalidInputError,
    LanceNamespace,
    NamespaceAlreadyExistsError,
    NamespaceExistsRequest,
    NamespaceNotFoundError,
    PermissionDeniedError,
    TableNotFoundError,
    UnsupportedOperationError,
)
from openfga_sdk import OpenFgaClient

from catalog.api import fga_deps
from catalog.api.dependencies import (
    ControlEmitterDep,
    FgaClientDep,
    SettingsDep,
    _namespace_for_root,
)
from catalog.api.security import CurrentToken
from catalog.core.config import Settings
from catalog.core.control_emit import emit_control
from catalog.core.identifiers import parse_identifier
from catalog.schemas import (
    CreateWarehouseNamespaceRequest,
    CreateWarehouseRequest,
    WarehouseResponse,
)
from catalog.services import native, warehouses
from service_kit.governed import fga
from service_kit.governed.audit import SUCCESS, audit
from service_kit.governed.oidc import IDToken


log = logging.getLogger(__name__)

router = APIRouter(prefix="/v1/warehouses", tags=["warehouse"])

# A bucket/warehouse id must be a DNS-safe S3 bucket name fragment (lowercase alnum + hyphen, 3-63 chars) —
# validated here so a malformed id can't produce an un-createable bucket or a path-traversing registry key.
_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,61}[a-z0-9]$")


def _require_enabled(settings: Settings) -> None:
    # A DOMAIN error, not a raw HTTPException: this module was the only endpoint module bypassing the
    # RFC 9457 problem+json handler, so its errors came back shaped differently from every other route in
    # the API (audit 2026-07-14). UnsupportedOperationError maps to the spec-correct 501.
    if not settings.warehouses_enabled:
        raise UnsupportedOperationError("warehouses are disabled (set LANCE_WAREHOUSES_ENABLED)")


def _validate_id(value: str, *, what: str) -> str:
    if not _ID_RE.match(value):
        raise InvalidInputError(f"invalid {what} {value!r}: must match {_ID_RE.pattern}")
    return value


def _namespace_exists_in_default(ns: LanceNamespace, segments: list[str]) -> bool:
    """True if the top-level namespace already exists in ``ns``'s root (the default/shared root).

    Uses the native ``namespace_exists``: a clean return means it exists; ``NamespaceNotFoundError`` means it
    does not. Any OTHER error PROPAGATES — a registry/backend fault must not be read as 'absent' and let a
    hijacking bind through. Blocking IO; callers threadpool it."""
    try:
        native.call(ns, "namespace_exists", NamespaceExistsRequest(id=segments))
        return True
    except NamespaceNotFoundError:
        return False


@router.post("", response_model_exclude_none=True)
async def create_warehouse(
    settings: SettingsDep,
    token: CurrentToken,
    client: FgaClientDep,
    control: ControlEmitterDep,
    body: CreateWarehouseRequest,
) -> WarehouseResponse:
    """Provision a warehouse: create its physical bucket + register it + seed FGA. Admin-gated.

    Order (fail-closed): authorize FIRST (``can_create_warehouse`` on the project, or — for a project that
    does not exist yet — the estate admin's ``can_observe_events`` on the root), THEN provision the bucket
    (idempotent), write the registry record, and grant the caller ``owner`` on ``warehouse:<id>`` with a
    ``parent`` edge to the project. A re-run with the same id is idempotent (bucket + record both
    overwrite-safe).

    Minting a NEW tenant additionally grants the caller ``admin`` on ``project:<project>``: a brand-new
    project has no tuples, so without that seed it would stay adminless forever (see
    ``fga_deps.require_can_create_warehouse``). That bootstrap grant is audited + published like any other
    grant."""
    _require_enabled(settings)
    warehouse_id = _validate_id(body.id, what="warehouse id")
    project = _validate_id(body.project, what="project id")
    bucket = _validate_id(body.bucket or body.id, what="bucket name")
    # Serving designation (DECISIONS "Medallion tiers"): only the one class the resolver knows is
    # accepted — an unknown value would mint a record neither project_root nor project_gold_root ever
    # matches (an unroutable warehouse), so it is rejected up front like a malformed id.
    if body.serving is not None and body.serving != "gold":
        raise InvalidInputError(f"invalid serving {body.serving!r}: only 'gold' is supported")

    so = settings.storage_options()
    # Does this project exist yet? The estate stores no project records — a project EXISTS exactly when a
    # warehouse record claims it (endpoints/projects.py) — so the registry listing IS the answer, and it is
    # read BEFORE the gate because the gate's shape depends on it: a not-yet-existing project has no tuples,
    # so only the estate admin can mint it (a project-scoped check could only ever deny). The read is
    # side-effect-free and discloses nothing (no response either way), and the router-level authn floor has
    # already run; the listing is reused below for the bucket-claim guard rather than re-read.
    records = await run_in_threadpool(warehouses.list_warehouses, settings.registry_root, so)
    project_exists = any(record.get("project") == project for record in records)
    await fga_deps.require_can_create_warehouse(client, settings, token, project=project, bootstrap=not project_exists)

    # Cross-tenant takeover guard: `can_create_warehouse` gates on the caller-named `project`, so an admin of
    # ANY project could otherwise re-POST an EXISTING warehouse id under their own project — the seed ADDS
    # `warehouse:<id> project project:<theirs>` alongside the original owner's tuples, making their project's
    # members readers of the victim's warehouse + every table under it (routing still points at the same
    # bucket → full cross-tenant disclosure). Reject a collision with a warehouse owned by another project.
    # A same-project re-create stays idempotent (the partial-failure retry path below relies on it).
    existing = await run_in_threadpool(warehouses.get_warehouse, settings.registry_root, so, warehouse_id)
    if existing is not None and existing.get("project") != project:
        raise NamespaceAlreadyExistsError(f"warehouse {warehouse_id!r} is already registered to another project")

    # Reserved-bucket guard (audit 2026-07-23, the Mallory scenario's first door): the shared catalog
    # root/registry bucket and the medallion zone buckets are PLATFORM storage — a warehouse claiming one
    # would make its project the bucket's "owner" (provision_bucket is idempotent on an existing bucket, so
    # the claim silently succeeds) and a later project-policy set would govern every tenant's data in it.
    if bucket in settings.reserved_bucket_set:
        raise InvalidInputError(
            f"bucket {bucket!r} is reserved platform storage (catalog root/registry or a medallion zone bucket) and cannot back a warehouse"
        )
    # Cross-project BUCKET-claim guard — the same takeover the warehouse-ID guard above closes, through the
    # other key: `bucket` is caller-chosen and provisioning an EXISTING bucket is a silent no-op, so without
    # this scan Mallory registers `wh-evil` over the victim's `acme-wh` bucket and her project policy (via
    # set_project_policy's registry resolution) governs — and can destroy version history in — acme's data.
    rival_claims = warehouses.projects_claiming_bucket(records, bucket) - {project}
    if rival_claims:
        raise NamespaceAlreadyExistsError(f"bucket {bucket!r} is already registered to another project's warehouse")

    root_uri = f"s3://{bucket}"
    await run_in_threadpool(warehouses.provision_bucket, bucket, so)
    record = {
        "id": warehouse_id,
        "bucket": bucket,
        "root_uri": root_uri,
        "project": project,
        # Idempotent re-create must NOT resurrect a DEACTIVATED warehouse nor reset created_at (audit #1): a
        # GitOps reconcile / partial-failure retry re-POSTing an existing id would otherwise silently lift a
        # quarantine with no /activate call and no audit signal. Carry the MUTABLE lifecycle fields forward
        # from the existing record; reactivation goes ONLY through the explicit /activate endpoint.
        "status": existing.get("status", "active") if existing is not None else "active",
        "created_at": (existing.get("created_at") if existing is not None else None) or datetime.now(UTC).isoformat(),
    }
    # Serving carries FORWARD on an idempotent re-create (same rationale as status above): a GitOps
    # reconcile re-POSTing the gold warehouse WITHOUT the serving field must not silently demote it to a
    # work warehouse — the silver→gold mover would quietly fall back to the work root while the record
    # still looks fine. The field stays ABSENT (not null) on work warehouses, matching the resolver's
    # "absent = work" contract and keeping pre-serving records byte-identical.
    serving = body.serving or (existing.get("serving") if existing is not None else None)
    if serving:
        record["serving"] = serving
    await run_in_threadpool(warehouses.put_warehouse, settings.registry_root, so, record)
    bootstrapped_admin = await fga_deps.seed_warehouse(
        client,
        settings,
        token,
        warehouse_id=warehouse_id,
        project=project,
        grant_project_admin=not project_exists,
    )
    log.info("warehouse_created", extra={"warehouse": warehouse_id, "bucket": bucket, "project": project})
    actor = f"user:{token.sub}" if token else None
    await emit_control(
        control,
        action="warehouse_created",
        object_type="warehouse",
        object_id=f"warehouse:{warehouse_id}",
        actor=actor,
        extra={"bucket": bucket, "project": project},
    )
    if bootstrapped_admin:
        # The tenant-bootstrap grant is a real ACL change, so it gets the SAME treatment as one made through
        # /v1/access/grant: the `access_grant` audit action (one compliance vocabulary, queryable together)
        # carrying the reason that distinguishes it, plus the grant_added control event the console keys its
        # refresh off. Only ever emitted when seed_warehouse reports the tuple was actually written.
        audit(
            "access_grant",
            SUCCESS,
            subject=actor,
            resource=f"project:{project}",
            grantee=actor,
            relation="admin",
            reason="project_bootstrap",
        )
        await emit_control(
            control,
            action="grant_added",
            object_type="grant",
            object_id=f"project:{project}",
            actor=actor,
            extra={"relation": "admin", "subject": actor, "reason": "project_bootstrap"},
        )
    return WarehouseResponse(**record)


@router.get("", response_model_exclude_none=True)
async def list_warehouses(settings: SettingsDep, token: CurrentToken, client: FgaClientDep) -> list[WarehouseResponse]:
    """Every warehouse the caller can read. Governed like the metadata feeds: with FGA on, filtered to the
    warehouses the caller has ``can_get_metadata`` on (never discloses another tenant's bucket names)."""
    _require_enabled(settings)
    records = await run_in_threadpool(warehouses.list_warehouses, settings.registry_root, settings.storage_options())
    if settings.fga_enabled and client is not None and token is not None:
        allowed = set(await fga.list_objects(client, user=token.sub, relation="can_get_metadata", object_type="warehouse"))
        records = [r for r in records if f"warehouse:{r['id']}" in allowed]
    return [WarehouseResponse(**r) for r in records]


@router.get("/{warehouse_id}", response_model_exclude_none=True)
async def get_warehouse(warehouse_id: str, settings: SettingsDep, token: CurrentToken, client: FgaClientDep) -> WarehouseResponse:
    """One warehouse record — reader-gated on ``warehouse:<id>`` (fail-closed on an OpenFGA outage)."""
    _require_enabled(settings)
    await fga_deps.require_relation(client, settings, token, relation="can_get_metadata", obj=f"warehouse:{warehouse_id}")
    record = await run_in_threadpool(warehouses.get_warehouse, settings.registry_root, settings.storage_options(), warehouse_id)
    if record is None:
        raise TableNotFoundError(f"warehouse not found: {warehouse_id}")
    return WarehouseResponse(**record)


async def _set_warehouse_status(
    warehouse_id: str,
    status: str,
    settings: Settings,
    token: IDToken | None,
    client: OpenFgaClient | None,
) -> WarehouseResponse:
    """Shared deactivate/activate: admin-gate on the warehouse's OWN project, flip ``status``, persist.

    Lifecycle is a platform-admin op (same rung as create): a project admin may quarantine or restore a
    warehouse they own. Fail-closed: the record is read first (needed to gate on the REAL owning project, not
    a caller-supplied one). NO EXISTENCE ORACLE (audit #4): a caller who is not the warehouse's project admin
    gets the SAME 404 as a missing warehouse — the not-found and permission-denied outcomes are made
    indistinguishable so an unauthorized user cannot probe which warehouse ids exist. Status is read LIVE by
    the resolver, so no cache invalidation is needed — the very next routed request sees the new status."""
    _require_enabled(settings)
    so = settings.storage_options()
    record = await run_in_threadpool(warehouses.get_warehouse, settings.registry_root, so, warehouse_id)
    if record is None:
        raise TableNotFoundError(f"warehouse not found: {warehouse_id}")
    try:
        await fga_deps.require_can_create_warehouse(client, settings, token, project=record["project"])
    except PermissionDeniedError as exc:
        # Collapse denied → not-found so existence is not disclosed to a non-admin (audit #4). A legitimate
        # admin of the warehouse's own project still passes; anyone else sees exactly a missing-warehouse 404.
        raise TableNotFoundError(f"warehouse not found: {warehouse_id}") from exc
    updated = await run_in_threadpool(warehouses.set_warehouse_status, settings.registry_root, so, warehouse_id, status)
    if updated is None:  # raced away between the read and the write — treat as gone
        raise TableNotFoundError(f"warehouse not found: {warehouse_id}")
    log.info("warehouse_status_changed", extra={"warehouse": warehouse_id, "status": status})
    return WarehouseResponse(**updated)


@router.post("/{warehouse_id}/deactivate", response_model_exclude_none=True)
async def deactivate_warehouse(
    warehouse_id: str,
    settings: SettingsDep,
    token: CurrentToken,
    client: FgaClientDep,
    control: ControlEmitterDep,
) -> WarehouseResponse:
    """Quarantine a warehouse (#3-A lifecycle): the resolver then refuses EVERY op on its bound namespaces
    (403), so no new tables are created and existing ones are suspended — the tenant-offboarding first step.
    Admin-gated on the warehouse's project. Idempotent (re-deactivating is a no-op)."""
    result = await _set_warehouse_status(warehouse_id, "deactivated", settings, token, client)
    await emit_control(
        control,
        action="warehouse_deactivated",
        object_type="warehouse",
        object_id=f"warehouse:{warehouse_id}",
        actor=f"user:{token.sub}" if token else None,
        extra={},
    )
    return result


@router.post("/{warehouse_id}/activate", response_model_exclude_none=True)
async def activate_warehouse(
    warehouse_id: str,
    settings: SettingsDep,
    token: CurrentToken,
    client: FgaClientDep,
    control: ControlEmitterDep,
) -> WarehouseResponse:
    """Reactivate a quarantined warehouse (#3-A lifecycle) — restores routing to its bound namespaces.
    Admin-gated on the warehouse's project. Idempotent."""
    result = await _set_warehouse_status(warehouse_id, "active", settings, token, client)
    await emit_control(
        control,
        action="warehouse_activated",
        object_type="warehouse",
        object_id=f"warehouse:{warehouse_id}",
        actor=f"user:{token.sub}" if token else None,
        extra={},
    )
    return result


@router.post("/{warehouse_id}/namespaces", response_model_exclude_none=True)
async def create_warehouse_namespace(
    warehouse_id: str,
    request: Request,
    settings: SettingsDep,
    token: CurrentToken,
    client: FgaClientDep,
    control: ControlEmitterDep,
    body: CreateWarehouseNamespaceRequest,
) -> CreateNamespaceResponse:
    """Create a top-level namespace INSIDE this warehouse's bucket and bind it, so all its tables route
    there (#3-A physical isolation). Gated on ``can_create_namespace`` (writer) on ``warehouse:<id>``.

    The namespace is created via the warehouse's bucket-rooted connection (not the default), the binding is
    persisted + cached (so subsequent table ops resolve without a registry read), and FGA is seeded with the
    namespace's ``parent`` edge pointing at the warehouse — so the owner's grant cascades into the tables."""
    _require_enabled(settings)
    ns_name = _validate_id(body.namespace, what="namespace name")
    await fga_deps.require_relation(client, settings, token, relation="can_create_namespace", obj=f"warehouse:{warehouse_id}")
    record = await run_in_threadpool(warehouses.get_warehouse, settings.registry_root, settings.storage_options(), warehouse_id)
    if record is None:
        raise TableNotFoundError(f"warehouse not found: {warehouse_id}")
    # Deactivation gate (audit #2/#6): this handler resolves the bucket connection DIRECTLY from
    # record["root_uri"] via _namespace_for_root — it never routes through get_namespace, so the resolver's
    # deactivation quarantine does NOT cover it. Without this check a principal still holding
    # can_create_namespace could provision a namespace + seed fresh FGA grants inside a QUARANTINED bucket (a
    # persistence foothold that survives a naive offboarding). Mirror the resolver's gate here.
    if (record.get("status") or "active") != "active":
        raise PermissionDeniedError(f"warehouse {warehouse_id!r} is deactivated (quarantined); cannot create namespaces in it")
    root_uri = record["root_uri"]

    # Binding is WRITE-ONCE: reject re-binding a top-level namespace already bound to a DIFFERENT warehouse.
    # Without this, tenant B could bind tenant A's namespace name → the binding object is overwritten, A's
    # existing tables become unreachable (routing sends the id to B's bucket where they don't exist) and A's
    # new writes physically land in B's bucket; positive-cached-forever routing makes replicas disagree.
    existing_binding = await run_in_threadpool(warehouses.warehouse_for_namespace, settings.registry_root, settings.storage_options(), ns_name)
    if existing_binding is not None and existing_binding != root_uri:
        raise NamespaceAlreadyExistsError(f"namespace {ns_name!r} is already bound to another warehouse")

    segments = parse_identifier(ns_name, settings.delimiter)
    # Collision guard (#3-A): a top-level namespace NAME that already exists UNBOUND in the DEFAULT root must
    # not be bound to a warehouse. Binding routes every future <name>$* op to this warehouse's bucket, so the
    # default-root namespace's tables become unreachable via the API (orphaned) — and the positive routing
    # cache makes it permanent. The write-once guard above only catches names bound to ANOTHER warehouse;
    # this is the other half of the same hazard. The operator must pick a fresh name or migrate first.
    default_ns: LanceNamespace = request.app.state.namespace
    if await run_in_threadpool(_namespace_exists_in_default, default_ns, segments):
        raise NamespaceAlreadyExistsError(
            f"namespace {ns_name!r} already exists in the default root; binding it to a warehouse would "
            "orphan its tables — choose a fresh name or migrate the tables first"
        )

    ns_conn = _namespace_for_root(request, settings, root_uri)
    req = CreateNamespaceRequest(id=segments)
    response: CreateNamespaceResponse = await run_in_threadpool(native.call, ns_conn, "create_namespace", req)
    # Persist + cache the binding BEFORE returning, so the very next table-create routes to this bucket.
    await run_in_threadpool(
        warehouses.bind_namespace,
        settings.registry_root,
        settings.storage_options(),
        ns_name,
        warehouse_id,
        root_uri,
    )
    request.app.state.warehouse_binding_cache[ns_name] = {"warehouse_id": warehouse_id, "root_uri": root_uri}
    # Seed FGA: owner on the namespace + parent edge to the WAREHOUSE (not the shared root), so the
    # concentric cascade project → warehouse → namespace → table reaches the tables created here.
    if settings.fga_enabled and token is not None and client is not None:
        await fga.grant_on_create(
            client,
            user_sub=token.sub,
            resource="namespace",
            obj_id=fga.canonical_object_id(segments, delimiter=settings.delimiter),
            actor=token.sub,
            origin="create",
            parent_object=f"warehouse:{warehouse_id}",
        )
    log.info("warehouse_namespace_created", extra={"warehouse": warehouse_id, "namespace": ns_name})
    await emit_control(
        control,
        action="warehouse_bound",
        object_type="warehouse",
        object_id=f"warehouse:{warehouse_id}",
        actor=f"user:{token.sub}" if token else None,
        extra={"namespace": ns_name},
    )
    return response
