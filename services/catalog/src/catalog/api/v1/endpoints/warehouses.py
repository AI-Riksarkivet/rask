"""Admin control-plane endpoints: warehouse provisioning + warehouse-scoped namespaces (#3-A).

A *warehouse* = one physically separate S3 bucket owned by a project (the FGA model's catalog-root type).
These routes are the **control plane** the catalog lacked: an authorized project-admin provisions a bucket
at RUNTIME (not the static Helm `mc mb` loop) and creates namespaces bound to it, so tables under those
namespaces land in that warehouse's bucket — physically isolated from every other tenant's. Physical
multi-tenancy: one project may hold many warehouses, and a table's bytes live in the bucket its
namespace is bound to, never in a neighbour's.

Authorization is DELIBERATELY stronger than the data plane: warehouse-create gates on the project's
`can_create_warehouse` (= admin) — the model action that until now was defined but never enforced — not the
writer-tier create-on-parent that guards tables/namespaces. There is exactly ONE door and no exception:
the project must already EXIST (`require_project_exists`, a 404 naming `POST /v1/projects`), and that
route is where a tenant and its first admin are minted together (`open_hierarchy_lifecycle.md` Decision 1).
An estate-admin bootstrap exception used to live here, because a not-yet-existing project had no tuples
and its first warehouse was otherwise uncreatable; it is gone, and with it the path by which creating a
warehouse could grant its creator tenant admin.

DELETE is the mirror image (Decision 3): bottom-up, gated on the PROJECT's `can_administer` because
destroying a tenant's storage is a tenant-level act, refusing 409 while any namespace is bound. `cascade`,
bucket `purge_bucket` and `force` are three SEPARATE opt-ins that never share a default — see
`delete_warehouse`.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime

from fastapi import APIRouter, Request
from fastapi.concurrency import run_in_threadpool
from lance_namespace import (
    CreateNamespaceRequest,
    CreateNamespaceResponse,
    DropNamespaceRequest,
    InvalidInputError,
    LanceNamespace,
    NamespaceAlreadyExistsError,
    NamespaceExistsRequest,
    NamespaceNotEmptyError,
    NamespaceNotFoundError,
    PermissionDeniedError,
    TableNotFoundError,
    UnsupportedOperationError,
)
from openfga_sdk import OpenFgaClient
from pydantic import BaseModel

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
from catalog.core.identifiers import CONTROL_ID_RE, parse_identifier
from catalog.schemas import (
    CreateWarehouseNamespaceRequest,
    CreateWarehouseRequest,
    WarehouseResponse,
)
from catalog.services import native, warehouses
from service_kit.governed import fga
from service_kit.governed.oidc import IDToken


log = logging.getLogger(__name__)

router = APIRouter(prefix="/v1/warehouses", tags=["warehouse"])

# A bucket/warehouse id must be a DNS-safe S3 bucket name fragment — validated here so a malformed id
# can't produce an un-createable bucket or a path-traversing registry key. The pattern is SHARED
# (`catalog.core.identifiers`): it lived here, in projects.py and in policies.py as three copies of one
# sentence, and they had already diverged on the `$`-vs-`\Z` anchor (an id ending in a newline was
# refused by one door and accepted by another).
_ID_RE = CONTROL_ID_RE


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

    Order (fail-closed): the project must EXIST (``require_project_exists`` — 404 naming
    ``POST /v1/projects`` as the fix; existence is the registry's answer, identical for every caller, so
    it runs before authz), THEN authorize ``can_create_warehouse`` on the project, THEN provision the
    bucket (idempotent), write the registry record, and grant the caller ``owner`` on ``warehouse:<id>``
    with its ``project`` edge. A re-run with the same id is idempotent (bucket + record overwrite-safe).

    The estate-admin bootstrap exception is GONE (Decision 1): tenants are minted explicitly at
    ``POST /v1/projects``, which seeds the project's admin — so by the time this runs the project has
    admins of its own and one door is enough."""
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
    # The project must exist — layer-3 invariant, from the PROJECT REGISTRY (Decision 1), checked before
    # the gate because the answer is identical for every caller and a 404 tells an authorized admin the
    # actual fix (create the tenant) where a 403 would mislead. The warehouse listing is still read here
    # for the bucket-claim guard below.
    await fga_deps.require_project_exists(settings, project)
    records = await run_in_threadpool(warehouses.list_warehouses, settings.registry_root, so)
    await fga_deps.require_can_create_warehouse(client, settings, token, project=project)

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
    # Deletion protection (Decision 5) carries forward for the SAME reason as status/serving, and it is the
    # one whose loss is silent AND irreversible: the record is rebuilt from scratch here, so a re-POST that
    # dropped the flag would disarm the delete door's only safety catch with no error and no audit signal —
    # `DELETE ?purge_bucket=true` would then take the bucket without ever asking for force=true. The field
    # stays ABSENT (not "false") on unprotected warehouses so pre-protection records remain byte-identical.
    protected = existing.get("protected") if existing is not None else None
    if protected:
        record["protected"] = protected
    await run_in_threadpool(warehouses.put_warehouse, settings.registry_root, so, record)
    await fga_deps.seed_warehouse(client, settings, token, warehouse_id=warehouse_id, project=project)
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


# --------------------------------------------------------------------------- #
# Deletion (`open_hierarchy_lifecycle.md` Decision 3): bottom-up, and a container refuses while full.
# --------------------------------------------------------------------------- #


class DeleteWarehouseResponse(BaseModel):
    """What the delete ACTUALLY did — reported step by step, never assumed.

    A warehouse delete touches four independent stores (the native namespaces, OpenFGA, the registry, the
    bucket) and only three of them run by default. Reporting each separately is what makes a partial
    failure diagnosable instead of a lie: ``bucket_purged=false`` names bytes that are still there, and
    ``tuples_revoked`` is a count OpenFGA really returned.
    """

    id: str
    #: The bucket the warehouse owned — named even when it was NOT purged, so the operator knows what survived.
    bucket: str
    #: The namespaces a ``cascade`` actually dropped and unbound (empty on a non-cascade delete).
    namespaces_dropped: list[str]
    #: Tuples removed across the warehouse object AND every cascaded namespace. 0 when FGA is off.
    tuples_revoked: int
    bucket_purged: bool
    objects_purged: int


async def _revoke_tuples(client: OpenFgaClient | None, settings: Settings, token: IDToken | None, obj: str) -> int:
    """Delete every FGA tuple on ``obj`` and return the count (0 when FGA is off/unwired).

    Distinct from :func:`fga_deps.revoke_ownership` in exactly two ways that matter here: the audit origin
    is ``lifecycle_delete`` (a deliberate hierarchy deletion, not the drop of a single object), and the
    COUNT comes back so the response can report tuple removal it actually observed.
    """
    if not (settings.fga_enabled and client is not None):
        return 0
    # `system:catalog` is the honest actor for an auth-off stack, where there genuinely is no principal —
    # never a stand-in for one we simply did not thread through (same rule as revoke_ownership).
    actor = token.sub if token is not None else "system:catalog"
    removed = await fga.revoke_object_tuples(client, obj, actor=actor, origin="lifecycle_delete")
    if removed:
        log.info("fga_tuples_revoked", extra={"object": obj, "removed": removed})
    return removed


def _require_bucket_purgeable(settings: Settings, records: list[dict[str, str]], record: dict[str, str], warehouse_id: str) -> None:
    """Refuse a ``?purge_bucket=true`` that would destroy bytes this warehouse does not solely own.

    ``create_warehouse`` carries two bucket-claim guards, and NEITHER of them survives into the destroy
    path on its own:

    * A bucket may back TWO warehouses of the SAME project — the cross-claim guard subtracts the caller's
      own project on purpose (a work warehouse plus a ``serving="gold"`` one is exactly that shape). A purge
      deletes every object AND the bucket, so deleting one of the pair would silently destroy the other's
      data and leave its record pointing at a bucket that no longer exists.
    * The reserved platform buckets (catalog root/registry, medallion zones) are refused at create — but a
      record written before that guard existed, or by anything else with registry write access, still names
      one, and purging it would take the whole estate's registry with it.

    ``NamespaceNotEmptyError`` (409) is the refusal: the container still holds something that is not this
    warehouse's to destroy. The reserved case is ``InvalidInputError`` (400), matching create's own wording
    for the same bucket. Both run BEFORE any irreversible step, so a refusal costs nothing.
    """
    bucket = record["bucket"]
    if bucket in settings.reserved_bucket_set:
        raise InvalidInputError(
            f"bucket {bucket!r} is reserved platform storage (catalog root/registry or a medallion zone bucket) "
            f"and must not be purged — delete the warehouse without ?purge_bucket=true"
        )
    rivals = [r for r in records if r.get("bucket") == bucket and r.get("id") != warehouse_id]
    if not rivals:
        return
    # Name only SAME-project siblings. A cross-project claim is drift, and this caller cleared the bar for
    # THIS warehouse's project only — enumerating another tenant's warehouse ids is the disclosure the
    # create-side guard already declines to make.
    named = sorted(str(r["id"]) for r in rivals if r.get("project") == record["project"])
    holder = f"warehouse(s) {', '.join(named)}" if len(named) == len(rivals) else "another project's warehouse"
    raise NamespaceNotEmptyError(
        f"bucket {bucket!r} still backs {holder}, whose data the purge would destroy. "
        f"Delete this warehouse without ?purge_bucket=true, or delete the other warehouse(s) first."
    )


@router.delete("/{warehouse_id}")
async def delete_warehouse(
    warehouse_id: str,
    request: Request,
    settings: SettingsDep,
    token: CurrentToken,
    client: FgaClientDep,
    control: ControlEmitterDep,
    cascade: bool = False,
    purge_bucket: bool = False,
    force: bool = False,
) -> DeleteWarehouseResponse:
    """Delete a warehouse: its bindings, its tuples and its registry record — and its BUCKET only if asked.

    Gated on ``project#can_administer`` (`open_hierarchy_lifecycle.md` Decision 3), NOT on a relation of the
    warehouse itself: destroying a tenant's storage is a tenant-level act, so it clears the tenant's own
    admin bar rather than a rung someone could hold on this one warehouse.

    Order, and every step is load-bearing:

    1. The record must exist (404).
    2. **Authorize first.** The gate runs before the protection and emptiness checks, so an unauthorized
       caller never learns whether the warehouse is protected or what it holds — a 409 listing another
       tenant's namespace names is a disclosure a 403 must beat to.
    3. Deletion protection (Decision 5) refuses 409 unless ``force=true``. ``force`` overrides the
       PROTECTION ONLY — step 2 has already run, identically, with or without it.
    4. Emptiness: a warehouse still holding namespaces refuses 409 **naming them**; a refusal that does not
       say what blocks it just moves the search to the user. ``?cascade=true`` drops exactly those.
    5. The cascade drops each bound namespace through the REAL native path, against the warehouse's OWN
       bucket-rooted connection (so it drops in the right bucket), then revokes its tuples and removes its
       binding — one namespace fully finished before the next, so a mid-cascade failure leaves a consistent
       prefix and a retry converges (every primitive is idempotent). A namespace that still holds TABLES
       refuses on its own rung: the request omits ``behavior``, so the native drop is Restrict and the level
       below is emptied first. Do NOT reach for ``behavior="Cascade"`` here — measured against the directory
       backend, it does not reject the value, it IGNORES it and drops Restrict-style anyway, so the field
       would read as a working cascade while doing nothing.
    6. Tuples on ``warehouse:<id>``, then the registry record.
    7. **Bytes only on ``?purge_bucket=true``.** A catalog entry is recoverable, a customer's bucket is not,
       so the two never share a default; a record-less bucket is reported by the reconciler, not lost. And a
       purge that would take bytes this warehouse does not solely own — a bucket a same-project sibling
       still claims, or reserved platform storage — refuses BEFORE the cascade
       (:func:`_require_bucket_purgeable`).

    The routing cache is evicted for every dropped namespace: ``_resolve_warehouse_root`` caches bindings
    POSITIVELY and forever (they are immutable while they exist), so a binding deleted here but left in the
    cache would keep routing that namespace into a warehouse that no longer exists.
    """
    _require_enabled(settings)
    _validate_id(warehouse_id, what="warehouse id")
    so = settings.storage_options()
    record = await run_in_threadpool(warehouses.get_warehouse, settings.registry_root, so, warehouse_id)
    if record is None:
        raise TableNotFoundError(f"warehouse not found: {warehouse_id}")

    project = record["project"]
    try:
        await fga_deps.require_relation(client, settings, token, relation="can_administer", obj=f"project:{project}")
    except PermissionDeniedError as exc:
        # NO EXISTENCE ORACLE (audit #4), matching `_set_warehouse_status` in this same module: a caller who
        # is not the warehouse's project admin gets exactly the missing-warehouse 404, so the not-found and
        # permission-denied outcomes are indistinguishable and nobody can probe which warehouse ids exist.
        # Delete is strictly MORE sensitive than the deactivate that established this rule — leaving the two
        # doors disagreeing would mean the quieter one leaks what the louder one protects.
        raise TableNotFoundError(f"warehouse not found: {warehouse_id}") from exc
    # Everything below discloses the warehouse's CONTENTS (the 409 names its namespaces) or its protection
    # state, so it runs strictly after the gate above.
    fga_deps.require_not_protected(record, kind="warehouse", obj_id=warehouse_id, force=force)

    bound = await run_in_threadpool(warehouses.namespaces_bound_to, settings.registry_root, so, warehouse_id)
    if bound and not cascade:
        raise NamespaceNotEmptyError(
            f"warehouse '{warehouse_id}' still holds {len(bound)} namespace(s): {', '.join(bound)}. "
            f"Drop them first, or pass ?cascade=true to drop exactly those with the warehouse."
        )

    bucket = record["bucket"]
    if purge_bucket:
        # The registry-wide read happens ONLY on the destructive path: the guard needs every claim on this
        # bucket, and a plain (recoverable) delete has no reason to pay for it. It runs here, before the
        # cascade, so a refusal still costs nothing — the bottom-up rule applies to the bytes too.
        records = await run_in_threadpool(warehouses.list_warehouses, settings.registry_root, so)
        _require_bucket_purgeable(settings, records, record, warehouse_id)

    root_uri = record["root_uri"]
    dropped: list[str] = []
    revoked = 0
    try:
        if bound:
            ns_conn = _namespace_for_root(request, settings, root_uri)
            for top_ns in bound:
                segments = parse_identifier(top_ns, settings.delimiter)
                try:
                    await run_in_threadpool(native.call, ns_conn, "drop_namespace", DropNamespaceRequest(id=segments))
                except NamespaceNotFoundError:
                    # Drift, not an error: the binding outlived the namespace (a half-finished earlier delete,
                    # or a reconciler case). Refusing here would make the binding undeletable by anyone;
                    # continue so the record the catalog still owns is cleaned up.
                    log.warning("warehouse_cascade_namespace_absent", extra={"warehouse": warehouse_id, "namespace": top_ns})
                revoked += await _revoke_tuples(client, settings, token, f"namespace:{fga.canonical_object_id(segments, delimiter=settings.delimiter)}")
                await run_in_threadpool(warehouses.unbind_namespace, settings.registry_root, so, top_ns)
                request.app.state.warehouse_binding_cache.pop(top_ns, None)
                dropped.append(top_ns)

        revoked += await _revoke_tuples(client, settings, token, f"warehouse:{warehouse_id}")
        await run_in_threadpool(warehouses.delete_warehouse_record, settings.registry_root, so, warehouse_id)
        purged = await run_in_threadpool(warehouses.purge_bucket, bucket, so) if purge_bucket else 0
    except Exception:
        # Partial-failure honesty (Decision 3), the half a raised error would otherwise erase: once the first
        # namespace drop lands, a later step CAN still fail (an OpenFGA outage on the revoke, a registry
        # blip) — and the caller then gets a problem body that says nothing about the namespaces already
        # gone. The error is deliberately NOT swallowed (fail-closed; every primitive is idempotent, so the
        # recovery path is to re-issue the same call), but what DID land is recorded here or it is lost.
        log.error(
            "warehouse_delete_partial",
            extra={
                "warehouse": warehouse_id,
                "project": project,
                "bucket": bucket,
                "namespaces_dropped": dropped,
                "tuples_revoked": revoked,
                "bucket_purged": False,
            },
            exc_info=True,
        )
        raise

    log.info(
        "warehouse_deleted",
        extra={
            "warehouse": warehouse_id,
            "project": project,
            "bucket": bucket,
            "namespaces_dropped": dropped,
            "tuples_revoked": revoked,
            "bucket_purged": purge_bucket,
            "objects_purged": purged,
            "forced": force,
        },
    )
    await emit_control(
        control,
        action="warehouse_deleted",
        object_type="warehouse",
        object_id=f"warehouse:{warehouse_id}",
        actor=f"user:{token.sub}" if token else None,
        extra={
            "project": project,
            "bucket": bucket,
            "namespaces_dropped": dropped,
            "bucket_purged": purge_bucket,
            "objects_purged": purged,
        },
    )
    return DeleteWarehouseResponse(
        id=warehouse_id,
        bucket=bucket,
        namespaces_dropped=dropped,
        tuples_revoked=revoked,
        bucket_purged=purge_bucket,
        objects_purged=purged,
    )
