"""Admin control-plane endpoints: warehouse provisioning + warehouse-scoped namespaces (#3-A).

A *warehouse* = one physically separate S3 bucket owned by a project (the FGA model's catalog-root type).
These routes are the **control plane** the catalog lacked: an authorized project-admin provisions a bucket
at RUNTIME (not from a bucket list fixed in the chart's values) and creates namespaces bound to it, so tables under those
namespaces land in that warehouse's bucket — physically isolated from every other tenant's. Physical
multi-tenancy: one project may hold many warehouses, and a table's bytes live in the bucket its
namespace is bound to, never in a neighbour's.

Authorization is DELIBERATELY stronger than the data plane: warehouse-create gates on the project's
`can_create_warehouse` (= admin) — the model action that until now was defined but never enforced — not the
writer-tier create-on-parent that guards tables/namespaces. There is exactly ONE door and no exception:
the project must already EXIST (`require_project_exists`, a 404 naming `POST /management/v1/projects`), and that
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
import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from functools import partial
from typing import Annotated, Any, Final

from fastapi import APIRouter, Header, Request
from fastapi.concurrency import run_in_threadpool
from lance_namespace import (
    ConcurrentModificationError,
    CreateNamespaceRequest,
    CreateNamespaceResponse,
    DeregisterTableRequest,
    DeregisterTableResponse,
    DropNamespaceRequest,
    InvalidInputError,
    LanceNamespace,
    ListTablesRequest,
    NamespaceAlreadyExistsError,
    NamespaceExistsRequest,
    NamespaceNotEmptyError,
    NamespaceNotFoundError,
    PermissionDeniedError,
    ServiceUnavailableError,
    TableNotFoundError,
    UnsupportedOperationError,
)
from openfga_sdk import OpenFgaClient
from pydantic import BaseModel, ConfigDict

from catalog.api import fga_deps
from catalog.api.dependencies import (
    ControlEmitterDep,
    FgaClientDep,
    LineageEmitterDep,
    ObjectUuidsDep,
    ResolverDep,
    SettingsDep,
    VendorDep,
    estate_namespace,
    namespace_for_root,
)
from catalog.api.security import Caller, CurrentToken
from catalog.api.v1.endpoints import namespaces as namespaces_api
from catalog.core import tier_policy
from catalog.core.config import Settings
from catalog.core.formats import reject_unsupported_properties
from catalog.core.identifiers import parse_identifier, with_minted_uuid
from catalog.core.lineage_emit import DEREGISTER_TABLE
from catalog.core.store_endpoint import names_the_estate_store, require_estate_store
from catalog.core.vending import CredentialVendor, role_session_name
from catalog.schemas import (
    CreateWarehouseNamespaceRequest,
    CreateWarehouseRequest,
    DeleteWarehouseResponse,
    EstateBindingsResponse,
    UnbindWarehouseNamespaceResponse,
    WarehouseNamespacesResponse,
    WarehouseResponse,
)
from catalog.services import native, object_uuids, table_claims, warehouses
from catalog.services.vend_probe import CAS_CHECK, CAS_RESERVE, SCOPE_CHECK, ProbeCheck, ProbeReport, summarize_probe
from service_kit.control_emit import emit_control
from service_kit.governed import fga
from service_kit.governed.deps import ANONYMOUS_SUBJECT
from service_kit.lakehouse import base_registry, location_claims, object_records, trash
from service_kit.lakehouse.naming import CONTROL_ID_RE
from service_kit.lakehouse.ns_errors import PartiallyApplied
from service_kit.lakehouse.records import RecordExistsError, RecordMissingError
from storage import s3_client, split_s3_uri


log = logging.getLogger(__name__)

#: The probe checks whose failure is a verdict on the STORE: it ignored a credential's scope, or a conditional
#: put. Every other failing check (`issue`, `write_inside`, `read_inside`, `cleanup`) means the probe could not
#: reach the store or its STS, which says nothing about the store.
_CONFORMANCE_CHECKS: Final = frozenset({SCOPE_CHECK, CAS_RESERVE, CAS_CHECK})

router = APIRouter(prefix="/management/v1/warehouses", tags=["warehouse"])

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
    vendor: VendorDep,
    body: CreateWarehouseRequest,
) -> WarehouseResponse:
    """Provision a warehouse: create its physical bucket + register it + seed FGA. Admin-gated.

    Order (fail-closed): the project must EXIST (``require_project_exists`` — 404 naming
    ``POST /management/v1/projects`` as the fix; existence is the registry's answer, identical for every caller, so
    it runs before authz), THEN authorize ``can_create_warehouse`` on the project, THEN provision the
    bucket (idempotent), probe its store, write the registry record, and grant the caller ``owner`` on
    ``warehouse:<id>`` with its ``project`` edge. The probe refuses a store that ignores credential scope or a
    conditional put (400) and one it cannot reach (503), so no such warehouse becomes routable ([[LH-481]]). A re-run with the same id is idempotent (bucket + record overwrite-safe).

    The estate-admin bootstrap exception is GONE (Decision 1): tenants are minted explicitly at
    ``POST /management/v1/projects``, which seeds the project's admin — so by the time this runs the project has
    admins of its own and one door is enough."""
    _require_enabled(settings)
    warehouse_id = _validate_id(body.id, what="warehouse id")
    project = _validate_id(body.project, what="project id")
    bucket = _validate_id(body.bucket or body.id, what="bucket name")
    # Serving designation (docs/adr/0034-medallion-tiers-hybrid-physical-layout-2026-07-24.md "Medallion tiers"): only the one class the resolver knows is
    # accepted — an unknown value would mint a record neither project_root nor project_gold_root ever
    # matches (an unroutable warehouse), so it is rejected up front like a malformed id.
    if body.serving is not None and body.serving != "gold":
        raise InvalidInputError(f"invalid serving {body.serving!r}: only 'gold' is supported")
    # [[LH-205]] Another store needs a credential the catalog resolves and consumes on every open,
    # provision and vend, and no door consumes one: the request has no `credential_ref` and nothing
    # reads the record's. Admitting the endpoint would have the estate's own key signed toward a host
    # this caller chose, so it is refused here, before the bucket is claimed or provisioned.
    if not names_the_estate_store(body.endpoint, estate=settings.s3_endpoint):
        raise InvalidInputError(
            f"endpoint {body.endpoint!r} is not the estate's object store. A warehouse in another store needs a credential "
            "reference the catalog consumes on every open, provision and vend, and none is consumed yet, so the catalog "
            "would sign with the estate's own key toward that host. Omit `endpoint` to use the estate's store."
        )

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
    # [[LH-205]] A re-POST that names no endpoint carries the live one forward, and `provision_bucket`
    # below always reaches the ESTATE's store. For a record naming another store that would create its
    # bucket in the wrong store and keep the record pointing at the foreign one, so the re-POST is
    # refused unless it re-points the warehouse itself (an explicit endpoint, judged above).
    if existing is not None and body.endpoint is None:
        require_estate_store(existing.get("endpoint"), estate=settings.s3_endpoint, subject=f"warehouse {warehouse_id!r}")

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

    # [[LH-053]] AND THE SCAN ABOVE IS NOT ENOUGH, which is why both are here rather than one replacing
    # the other. `records` was listed before the gate, the provision below is a network round trip, and
    # the record write is further down still — so two concurrent creates naming one bucket under
    # different projects each scan a listing taken before the other existed, both pass, and both write.
    # The claim is WRITE-ONCE AT THE STORE (`If-None-Match: *`), so the loser is refused regardless of
    # what its caller read. It is taken BEFORE `provision_bucket`: provisioning an existing bucket is a
    # silent no-op, so a rival that provisioned first and lost the claim would otherwise have already
    # touched the victim's storage.
    #
    # The scan stays because it answers a different question cheaply and with a listing already in hand;
    # this arbitrates the one case it structurally cannot see.
    await run_in_threadpool(partial(warehouses.claim_bucket, settings.registry_root, so, bucket=bucket, project=project, warehouse_id=warehouse_id))

    root_uri = f"s3://{bucket}"
    await run_in_threadpool(warehouses.provision_bucket, bucket, so)
    # [[LH-481]] THE STORE IS PROVED BEFORE THE RECORD MAKES IT ROUTABLE (ADR 0089). Every Lance commit rests on
    # the store refusing a second conditional put, and every vended credential on it honouring the session
    # policy; a store that accepts either and ignores it fails silently, with a 200 on every write. The probe
    # `POST …/validate` runs is run here too, so such a warehouse is refused rather than found later by a
    # client. A `skip` (this deployment vends no direct credentials) proves nothing and refuses nothing. The
    # bucket claim above stays: it names this project and id, so the same create retried converges.
    #
    # TWO ANSWERS, because the two failures need different things from the caller. A store that broke a
    # guarantee is the request's problem, 400, and retrying changes nothing. A probe that could not reach the
    # store or its STS learned nothing about it, 503, and the retry is the right move; a GitOps re-POST of a
    # live warehouse must not read a store blip as a verdict on its store.
    creator = role_session_name(token.subject if token is not None else ANONYMOUS_SUBJECT)
    probe = partial(_run_scope_probe, root_uri, vendor=vendor, store_endpoint=settings.s3_endpoint, session_name=creator)
    report = summarize_probe(await run_in_threadpool(probe))
    failed = [c.name for c in report.checks if c.outcome == "fail"]
    if broken := [name for name in failed if name in _CONFORMANCE_CHECKS]:
        log.warning("warehouse_store_failed_probe", extra={"warehouse": warehouse_id, "bucket": bucket, "failed": broken})
        raise InvalidInputError(
            f"warehouse {warehouse_id!r} was not registered: its object store failed the storage probe ({', '.join(broken)}). "
            "A Lance warehouse needs a store that enforces vended-credential scope and refuses a second conditional write."
        )
    if failed:
        log.warning("warehouse_store_unprobed", extra={"warehouse": warehouse_id, "bucket": bucket, "failed": failed})
        raise ServiceUnavailableError(f"warehouse {warehouse_id!r} was not registered: its object store could not be probed ({', '.join(failed)}); retry")
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
        # [[LH-067]] Which object store this bucket is reached at, and only the estate's passes the
        # judgement above ([[LH-205]]). CALLER-OWNED, so unlike the arm-never-disarm fields a re-POST
        # may correct it: a record naming another store is re-pointed at the estate with an explicit
        # empty string, which `_resolve_warehouse_root` reads as None. Omitted from the request means
        # "unchanged": the key is absent, so `_CALLER_OWNED`'s merge carries the live value forward.
        **({"endpoint": body.endpoint} if body.endpoint is not None else {}),
    }
    # Serving carries FORWARD on an idempotent re-create (same rationale as status above): a GitOps
    # reconcile re-POSTing the gold warehouse WITHOUT the serving field must not silently demote it to a
    # work warehouse — a gold writer resolving the tenant's root would quietly fall back to the work root while the record
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
    if body.protected:
        # #123: the flag is finally settable at the door that writes the record. String "true", the
        # shape `require_not_protected` and the hand-armed records already use.
        protected = "true"
    if protected:
        record["protected"] = protected
    # `primary` carries forward for the same reason as serving/protected, and its loss has the sharpest
    # consequence of the three: dropping it does not disable a safety catch, it makes the project
    # UNROUTABLE — `_resolve_root` refuses an ambiguous set outright, so a re-POST that silently
    # demoted the marked record would stop the tenant's cascade rather than misroute it. Stored as the
    # boolean the resolver reads (`record.get("primary") is True`), and ABSENT on unmarked records so
    # every pre-2026-09-06 record stays byte-identical.

    # BOTH re-create paths go through the CONDITIONAL upsert (diff2 F4). They used to end in an
    # unconditional `put_warehouse` of a record assembled from a read taken at the top of this handler —
    # before the guards above and before `provision_bucket`, a network round trip. A deactivate landing
    # anywhere in that window was overwritten by the status this request carried forward, lifting a
    # quarantine with no `/activate` and no audit signal. The merge now happens inside the write,
    # against the record as it actually stands, so a concurrent lifecycle change survives.
    async def _converge() -> dict[str, str]:
        try:
            return await run_in_threadpool(
                warehouses.upsert_warehouse,
                settings.registry_root,
                so,
                record,
                serving=body.serving,
                protect=bool(body.protected),
                primary=bool(body.primary),
            )
        except warehouses.WarehouseProjectConflict:
            # The pre-flight guard above already refuses this for the ordinary case; reaching it here
            # means the record moved projects mid-request, and the answer is the same either way.
            raise NamespaceAlreadyExistsError(f"warehouse {warehouse_id!r} is already registered to another project") from None
        except RecordMissingError:
            # Raced a concurrent delete: retryable, never a blind re-create — code 14 → 409 so the
            # caller retries into a clean create. (Previously only the lost-mint-race branch noticed
            # this; the sequential path would have re-created the record the operator just deleted.)
            raise ConcurrentModificationError(f"warehouse {warehouse_id!r} write raced a concurrent delete; retry") from None

    if existing is None:
        # The id-MINT is conditional (F1): the takeover guard above read the registry BEFORE deciding,
        # and a rival create landing between that read and this write used to win silently — the store
        # now refuses the second create, whatever the guard saw.
        try:
            await run_in_threadpool(warehouses.create_warehouse_record, settings.registry_root, so, record)
        except RecordExistsError:
            record = await _converge()  # lost the mint race — converge on the WINNER, conditionally
    else:
        record = await _converge()
    await fga_deps.seed_warehouse(client, settings, token, warehouse_id=warehouse_id, project=project)
    log.info("warehouse_created", extra={"warehouse": warehouse_id, "bucket": bucket, "project": project})
    actor = f"user:{token.subject}" if token else None
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
        # The listing's own candidates, batch-checked ([[LH-223]]): never what the caller reaches across the estate,
        # which OpenFGA stops counting at its ceiling.
        allowed = await fga.batch_check(client, user=token.subject, relation="can_get_metadata", objects=[f"warehouse:{r['id']}" for r in records])
        records = [r for r in records if allowed.get(f"warehouse:{r['id']}")]
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


@router.get("/-/bindings", response_model_exclude_none=True)
async def list_estate_bindings(
    settings: SettingsDep,
    token: CurrentToken,
    client: FgaClientDep,
) -> EstateBindingsResponse:
    """Every binding the caller may see, from ONE ``list_bindings`` pass (#86).

    ``/-/`` rather than a bare ``/bindings``: the sibling routes are ``/{warehouse_id}/…`` and
    ``bindings`` is a VALID warehouse id, so a literal segment there would shadow a real tenant's
    warehouse. ``-`` can never be an id (``CONTROL_ID_RE`` requires ≥3 chars with alphanumeric ends),
    which makes the collision impossible rather than merely unlikely. This is the FIRST ``/-/`` route
    in the estate and it is our own convention, borrowed from GitLab — the Lance spec's own root
    placeholder is the delimiter, which is not available here.

    Governed exactly like the warehouse listing it complements: with FGA on, filtered to the
    warehouses the caller can ``can_get_metadata`` — a binding names a namespace AND the tenant
    bucket it lives in, so leaking one leaks another tenant's shape.
    """
    _require_enabled(settings)
    # `read_bindings`, NOT the tolerant `list_bindings`: the per-warehouse sibling 503s on any binding
    # it could not parse, and answering the estate question with the opposite tolerance would put an
    # invisible bound namespace back — a 200 with one silently missing, which the frontend's
    # read-failed channel cannot see because nothing failed. One corrupt object degrades LOUDLY here,
    # exactly as it does one route over.
    records, skipped = await run_in_threadpool(warehouses.read_bindings, settings.registry_root, settings.storage_options())
    if skipped:
        raise ServiceUnavailableError(
            f"{len(skipped)} namespace binding(s) could not be read (e.g. {skipped[0]}) — refusing to "
            f"report a partial estate, because a binding you cannot read is a namespace you cannot see."
        )
    if settings.fga_enabled and client is not None and token is not None:
        # The listing's own candidates, batch-checked ([[LH-223]]).
        objects = sorted({f"warehouse:{r.get('warehouse_id')}" for r in records})
        allowed = await fga.batch_check(client, user=token.subject, relation="can_get_metadata", objects=objects)
        records = [r for r in records if allowed.get(f"warehouse:{r.get('warehouse_id')}")]
    return EstateBindingsResponse(bindings={str(r["top_ns"]): str(r["warehouse_id"]) for r in records})


@router.get("/{warehouse_id}/namespaces", response_model_exclude_none=True)
async def list_warehouse_namespaces(
    warehouse_id: str,
    settings: SettingsDep,
    token: CurrentToken,
    client: FgaClientDep,
    resolver: ResolverDep,
) -> WarehouseNamespacesResponse:
    """The namespaces bound to this warehouse, read from the REGISTRY — the same source the delete
    door consults. This read exists so no surface has to INFER the answer: the lakehouse warehouse
    page used to derive its namespace list by grouping the table registry, which made an EMPTY
    namespace structurally invisible — the page said "no namespaces" while the delete door refused
    409 naming one (observed live, #66). Reader-gated like the record itself (``can_get_metadata``);
    a missing warehouse is the same 404 as everywhere else."""
    _require_enabled(settings)
    await fga_deps.require_relation(client, settings, token, relation="can_get_metadata", obj=f"warehouse:{warehouse_id}")
    so = settings.storage_options()
    record = await run_in_threadpool(warehouses.get_warehouse, settings.registry_root, so, warehouse_id)
    if record is None:
        raise TableNotFoundError(f"warehouse not found: {warehouse_id}")
    bound = await run_in_threadpool(warehouses.namespaces_bound_to, settings.registry_root, so, warehouse_id)
    # PER-ITEM, because the route's gate does not imply its contents. `can_get_metadata` on a container
    # is `reader or can_get_metadata from child`, so holding `reader` on ONE namespace under this
    # warehouse opens this route — and the unfiltered answer then named every SIBLING namespace bound
    # to it. The same split `list_tables` and `list_namespaces` already carry, and the same one
    # `estate_bindings` a few lines above applies to its own records: the ROUTE may open on the
    # container, each ITEM is checked on itself.
    #
    # A namespace name discloses how a tenant organises data, which is why the answer is FILTERED
    # rather than refused — refusing would break the breadcrumb the widened relation exists to serve.
    if settings.fga_enabled and token is not None and client is not None:
        bound, _ = await fga_deps.authorized_page(
            client,
            resolver,
            user=token.subject,
            relation="can_get_metadata",
            kind="namespace",
            candidates=[(name, [name]) for name in sorted(bound)],
            page_token=None,
            limit=None,
        )
    return WarehouseNamespacesResponse(namespaces=bound)


async def _set_warehouse_status(
    warehouse_id: str,
    status: str,
    settings: Settings,
    token: Caller | None,
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
        actor=f"user:{token.subject}" if token else None,
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
        actor=f"user:{token.subject}" if token else None,
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
    resolver: ResolverDep,
    body: CreateWarehouseNamespaceRequest,
) -> CreateNamespaceResponse:
    """Create a top-level namespace INSIDE this warehouse's bucket and bind it, so all its tables route
    there (#3-A physical isolation). Gated on ``can_create_namespace`` (writer) on ``warehouse:<id>``.

    The namespace is created via the warehouse's bucket-rooted connection (not the default), the binding is
    persisted + cached (so subsequent table ops resolve without a registry read), and FGA is seeded with the
    namespace's ``parent`` edge pointing at the warehouse — so the owner's grant cascades into the tables."""
    _require_enabled(settings)
    ns_name = _validate_id(body.namespace, what="namespace name")
    reject_unsupported_properties(body.properties)
    declared = tier_policy.declared_tier(body.properties)
    if body.properties and body.adopt_existing:
        raise InvalidInputError("adopt_existing keeps the namespace's own properties, so properties cannot be set on the same call")
    await fga_deps.require_relation(client, settings, token, relation="can_create_namespace", obj=f"warehouse:{warehouse_id}")
    record = await run_in_threadpool(warehouses.get_warehouse, settings.registry_root, settings.storage_options(), warehouse_id)
    if record is None:
        raise TableNotFoundError(f"warehouse not found: {warehouse_id}")
    if declared is not None:
        await fga_deps.require_tier_authority(client, settings, token, project=record["project"])
    # Deactivation gate (audit #2/#6): this handler resolves the bucket connection DIRECTLY from
    # record["root_uri"] via namespace_for_root — it never routes through get_namespace, so the resolver's
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

    # One segment: the control-id rule admits no delimiter.
    segments = [ns_name]
    # Collision guard (#3-A): a top-level namespace NAME that already exists UNBOUND in the DEFAULT root must
    # not be bound to a warehouse. Binding routes every future <name>$* op to this warehouse's bucket, so the
    # default-root namespace's tables become unreachable via the API (orphaned) — and the positive routing
    # cache makes it permanent. The write-once guard above only catches names bound to ANOTHER warehouse;
    # this is the other half of the same hazard. The operator must pick a fresh name or migrate first.
    default_ns = await estate_namespace(request, settings)
    if await run_in_threadpool(_namespace_exists_in_default, default_ns, segments):
        raise NamespaceAlreadyExistsError(
            f"namespace {ns_name!r} already exists in the default root; binding it to a warehouse would "
            "orphan its tables — choose a fresh name or migrate the tables first"
        )

    # AT THE WAREHOUSE'S OWN ENDPOINT ([[LH-067]]). This door never routes through `get_namespace`,
    # so the resolver that threads the record's endpoint does not run here — creating the namespace
    # at the estate's store instead would put it in a different object store from every table
    # later written to it.
    ns_conn = await namespace_for_root(request, settings, root_uri, endpoint=record.get("endpoint") or None)
    # [[LH-519]] A uuid for the namespace this request creates (ADR 0173 §2, §6); an adopted namespace keeps its own.
    req = CreateNamespaceRequest(id=segments, properties=with_minted_uuid(body.properties))
    adopted = False
    try:
        response: CreateNamespaceResponse = await run_in_threadpool(native.call, ns_conn, "create_namespace", req)
    except NamespaceAlreadyExistsError:
        # The namespace already exists AT THIS WAREHOUSE'S ROOT. With `adopt_existing` that is the
        # bytes-first migration converging (the #54 flow: copy the datasets into the bucket, THEN
        # bind) — the binding + FGA trailer below is exactly what it still needs. Without the flag
        # it stays a refusal: silently adopting would let a typo'd create inherit a stranger's data.
        # Both hazard guards above ran either way; this branch never relaxes them.
        if not body.adopt_existing:
            raise
        log.info("adopting existing namespace %s at warehouse %s (bytes-first migration)", ns_name, warehouse_id)
        response = CreateNamespaceResponse()
        adopted = True
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

    async def _undo_create() -> None:
        # Drop the namespace this request made, and drop the binding with it — a binding pointing at
        # a namespace that no longer exists is drift the reconciler would later report as a mystery.
        # The cache entry goes too, or this replica keeps routing a name it just deleted.
        await run_in_threadpool(native.call, ns_conn, "drop_namespace", DropNamespaceRequest(id=segments))
        await run_in_threadpool(warehouses.unbind_namespace, settings.registry_root, settings.storage_options(), ns_name)
        request.app.state.warehouse_binding_cache.pop(ns_name, None)

    # Seed FGA: owner on the namespace + parent edge to the WAREHOUSE (not the shared root), so the
    # concentric cascade project → warehouse → namespace → table reaches the tables created here.
    #
    # This is F3's third partial-failure state (diff2), and the LAST of the three to be closed. A
    # failed grant left the namespace natively created, bound, and owned by nobody; the plain retry
    # 409'd at native `NamespaceAlreadyExists`, so the only converging path was passing
    # `adopt_existing=true` — a bytes-first MIGRATION flag doing undocumented double duty as a repair
    # for a fault the caller did not cause and could not diagnose. Now the failure cleans up after
    # itself and an ordinary retry works.
    #
    # `undo=None` WHEN ADOPTED, and this is the same distinction the Arrow create door draws with
    # `_compensation_allowed`: an adopted namespace holds datasets that existed before this request
    # and were copied in by a migration. Dropping those to repair a missing tuple would turn a
    # transient FGA blip into the loss of the very data the migration was moving.
    await fga_deps.seed_ownership_or_compensate(
        client,
        settings,
        token,
        resolver,
        resource="namespace",
        segments=segments,
        uuid=await run_in_threadpool(object_uuids.require_namespace_uuid, ns_conn, segments),
        parent_object=f"warehouse:{warehouse_id}",
        undo=None if adopted else _undo_create,
    )
    log.info("warehouse_namespace_created", extra={"warehouse": warehouse_id, "namespace": ns_name})
    await emit_control(
        control,
        action="warehouse_bound",
        object_type="warehouse",
        object_id=f"warehouse:{warehouse_id}",
        actor=f"user:{token.subject}" if token else None,
        extra={"namespace": ns_name},
    )
    return response


@router.delete("/{warehouse_id}/namespaces/{top_ns}", response_model_exclude_none=True)
async def unbind_warehouse_namespace(
    warehouse_id: str,
    top_ns: str,
    request: Request,
    settings: SettingsDep,
    token: CurrentToken,
    client: FgaClientDep,
    control: ControlEmitterDep,
) -> UnbindWarehouseNamespaceResponse:
    """Detach a top-level namespace from its warehouse WITHOUT dropping either.

    THE REPAIR A BINDING NEEDS WHEN IT OUTLIVES WHAT IT POINTED AT. A binding is a routing record —
    `top_ns -> warehouse_id -> root_uri` — and until this door existed the only way to remove one was
    `POST /v1/namespace/{id}/drop`, which destroys the namespace and its tables to reach a JSON file.
    Three such records were found stranded on this estate (§ Q15-1), naming warehouses that hold no
    bytes, and the repair was blocked on having no non-destructive door.

    THE ORDER IS `delete_warehouse`'s, scaled to one binding, and every step carries its reason:

    1. the warehouse record must exist (404);
    2. **authorize first** on ``project#can_administer`` — detaching a namespace's storage routing is a
       tenant-level act, the same reasoning that put the warehouse delete on the tenant's admin bar
       rather than a rung someone holds on this one warehouse. A denial collapses to the same 404 the
       missing warehouse gets (the NO EXISTENCE ORACLE class rule, audit #4), so nobody probes ids here
       that the louder doors protect;
    3. the binding must name THIS warehouse (404 otherwise) — a tenant may not unbind a namespace
       through a warehouse id that does not hold it;
    4. **emptiness**: a namespace still holding tables refuses 409 NAMING them. Unbinding a live
       namespace does not delete a byte and is still the worst outcome available — every table in it
       becomes unresolvable, because routing no longer knows which bucket holds it. Silent
       unreachability beats a loud refusal for nobody;
    5. remove the record, then broadcast ``warehouse_unbound`` so every replica evicts. The binding
       cache is positive-and-forever, so without the broadcast the registry says unbound while running
       pods keep routing — the repair would report success and change nothing.
    """
    _require_enabled(settings)
    so = settings.storage_options()
    record = await run_in_threadpool(warehouses.get_warehouse, settings.registry_root, so, warehouse_id)
    if record is None:
        raise TableNotFoundError(f"warehouse not found: {warehouse_id}")
    try:
        await fga_deps.require_relation(client, settings, token, relation="can_administer", obj=f"project:{record['project']}")
    except PermissionDeniedError as exc:
        raise TableNotFoundError(f"warehouse not found: {warehouse_id}") from exc

    # DEACTIVATION GATE, mirroring `create_warehouse_namespace`: this handler resolves the bucket
    # connection directly from the binding's `root_uri` and never routes through `get_namespace`, so the
    # resolver's quarantine does not cover it (audit #2/#6). A quarantined warehouse is a
    # tenant mid-offboarding: unbinding it there would let a principal who still holds the rung dismantle
    # the routing the quarantine froze, and the offboarding path is the warehouse delete's cascade.
    if (record.get("status") or "active") != "active":
        raise PermissionDeniedError(f"warehouse {warehouse_id!r} is deactivated (quarantined); unbind through the warehouse delete instead")

    binding = await run_in_threadpool(warehouses.binding_for_namespace, settings.registry_root, so, top_ns)
    if binding is None or binding.get("warehouse_id") != warehouse_id:
        raise TableNotFoundError(f"namespace {top_ns!r} is not bound to warehouse {warehouse_id!r}")

    # The record's endpoint, so the emptiness probe reads the store the warehouse names and a record
    # naming another store is refused at the builder ([[LH-205]]) rather than probed at the estate's.
    ns = await namespace_for_root(request, settings, str(binding["root_uri"]), endpoint=record.get("endpoint") or None)
    tables = await run_in_threadpool(_tables_in_namespace, ns, top_ns)
    if tables:
        raise NamespaceNotEmptyError(
            f"namespace '{top_ns}' still holds {len(tables)} table(s): {', '.join(tables)}. "
            f"Unbinding it would leave them unresolvable — drop or move them first."
        )

    await run_in_threadpool(warehouses.unbind_namespace, settings.registry_root, so, top_ns)
    log.info("warehouse_namespace_unbound", extra={"warehouse": warehouse_id, "namespace": top_ns})
    await emit_control(
        control,
        action="warehouse_unbound",
        object_type="warehouse",
        object_id=f"warehouse:{warehouse_id}",
        actor=f"user:{token.subject}" if token else None,
        extra={"namespace": top_ns},
    )
    return UnbindWarehouseNamespaceResponse(warehouse_id=warehouse_id, namespace=top_ns, unbound=True)


#: Page ceiling for the unbind's emptiness probe. Matches the drop path's own cap for the same reason —
#: a listing is bounded work, and an unbounded loop on a pathological namespace turns a safety check
#: into the outage it was protecting against.
_MAX_LIST_PAGES = 100


def _tables_in_namespace(ns: LanceNamespace, top_ns: str) -> list[str]:
    """Every table directly under ``top_ns``, paged to the same ceiling the drop path uses.

    A TRUNCATED enumeration must not read as empty: the unbind's whole safety argument is "nothing is
    left to strand", and a page cap that silently drops the tail turns that into a guess. So a listing
    that runs out of pages reports what it saw plus a marker, which the caller refuses on.
    """
    found: list[str] = []
    page: str | None = None
    for _ in range(_MAX_LIST_PAGES):
        listed = native.call(ns, "list_tables", ListTablesRequest(id=[top_ns], include_declared=True, page_token=page))
        found.extend(listed.tables or [])
        page = listed.page_token or None
        if not page:
            return found
    log.warning("unbind_table_listing_truncated", extra={"namespace": top_ns})
    found.append("… (listing truncated; refusing rather than guessing)")
    return found


# --------------------------------------------------------------------------- #
# Deletion (`open_hierarchy_lifecycle.md` Decision 3): bottom-up, and a container refuses while full.
# --------------------------------------------------------------------------- #


class _BoundSubtree(BaseModel):
    """A namespace bound to the warehouse and what the catalog lists under it, enumerated before the cascade changes anything."""

    model_config = ConfigDict(frozen=True)

    segments: list[str]
    #: The bound namespace's uuid, ``None`` when the backend no longer has it.
    uuid: str | None
    descendants: list[fga_deps.GovernedObject]

    @property
    def tables(self) -> list[list[str]]:
        return [item.segments for item in self.descendants if item.kind == "table"]

    @property
    def namespaces(self) -> list[list[str]]:
        """Every namespace in the subtree, deepest first and the bound one last, so each is empty when its own drop runs."""
        nested = sorted((item.segments for item in self.descendants if item.kind == "namespace"), key=len, reverse=True)
        return [*nested, self.segments]

    @property
    def guarded(self) -> list[fga_deps.GovernedObject]:
        """Every object in the subtree whose protection the cascade is held to: its descendants and the bound namespace."""
        own = [fga_deps.GovernedObject(kind="namespace", segments=self.segments, uuid=self.uuid)] if self.uuid is not None else []
        return [*self.descendants, *own]


async def _cascade_subtrees(ns: LanceNamespace, settings: Settings, bound: list[str], *, force: bool) -> list[_BoundSubtree]:
    """Every bound namespace with its subtree, enumerated and held to its protection before anything is removed.

    ALL OF THEM FIRST, so a refusal changes nothing: a protected table in the last namespace must not be found
    after the first namespace is gone. The walker and the protection pre-flight are the namespace door's, so a
    cascade through either door enumerates and refuses alike, and ``force`` turns the subtree's locks exactly as it
    turns the warehouse's.

    A bound namespace the backend does not have is drift, not an error: the binding outlived it (a half-finished
    earlier delete), and its empty subtree lets the cascade remove the binding the catalog still owns. The `dir`
    backend lists such a namespace as empty while the root holds a ``__manifest`` and refuses the listing
    ``NamespaceNotFound`` while it holds none (measured on pylance 13.0.0), so both read as an empty subtree.
    """
    subtrees: list[_BoundSubtree] = []
    for top_ns in bound:
        segments = parse_identifier(top_ns)
        try:
            namespace = await fga_deps.identify(ns, settings, "namespace", segments)
            listed = await run_in_threadpool(namespaces_api._collect_descendants, ns, segments)  # noqa: SLF001 — one walker, deliberately
        except NamespaceNotFoundError:
            subtrees.append(_BoundSubtree(segments=segments, uuid=None, descendants=[]))
            continue
        descendants = await namespaces_api._identify(ns, settings, listed)  # noqa: SLF001 — one walker, deliberately
        subtrees.append(_BoundSubtree(segments=segments, uuid=namespace.uuid, descendants=descendants))
    doomed = [item for subtree in subtrees for item in subtree.guarded]
    await namespaces_api._require_descendants_unprotected(settings, doomed, force=force)  # noqa: SLF001 — one protection pre-flight, deliberately
    return subtrees


async def _detach_table(ns: LanceNamespace, settings: Settings, segments: list[str], *, purge: bool) -> bool:
    """Detach one table from the catalog, its bytes left in place; ``False`` when it was already gone.

    DETACH, NOT DROP: this door takes bytes only on ``?purge_bucket=true``, and that holds for the tables in its
    namespaces as much as for the bucket. A detached table's bytes stay where a later ``register_table`` can attach
    them again; a purge takes them with the bucket, and then their base record goes with them ([[LH-279]]), or it
    would sanction its bases for the next table registered at the location. The location's claim goes either way,
    as at the deregister door ([[LH-204]]).
    """
    try:
        detached: DeregisterTableResponse = await run_in_threadpool(native.call, ns, "deregister_table", DeregisterTableRequest(id=segments))
    except TableNotFoundError:
        return False
    if detached.location:
        canonical = fga.canonical_object_id(segments)
        await run_in_threadpool(location_claims.release, table_claims.claim_store(settings), detached.location, canonical)
        if purge:
            registry = base_registry.BaseRegistry(control_root=settings.registry_root, storage_options=settings.storage_options())
            await run_in_threadpool(partial(base_registry.forget_base_record, registry, detached.location, table=canonical))
    return True


async def _forget_trashed(settings: Settings, entry: dict[str, Any]) -> None:
    """End what one trashed object kept for an undrop besides its grants: its policy, claim and base record, then its record.

    The record goes last, as the trash purge orders it: it is all that still names what the object holds, so a
    failure before it is cleared leaves it for the re-issue to end. Its records are ended, and it is cleared, under the
    uuid it keeps.
    """
    so = settings.storage_options()
    kind, canonical, location = str(entry.get("kind") or "table"), str(entry.get("id") or ""), str(entry.get("location") or "")
    claims = table_claims.claim_store(settings)
    uuid = trash.object_uuid(entry)
    if uuid is not None:
        await run_in_threadpool(object_records.end_object_records, settings.registry_root, so, kind, uuid)
    if location:
        await run_in_threadpool(location_claims.release, claims, location, canonical)
        registry = base_registry.BaseRegistry(control_root=settings.registry_root, storage_options=so)
        await run_in_threadpool(partial(base_registry.forget_base_record, registry, location, table=canonical))
    if uuid is not None:
        await run_in_threadpool(trash.clear, settings.registry_root, so, uuid, kind=kind)


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
    emitter: LineageEmitterDep,
    uuids: ObjectUuidsDep,
    cascade: bool = False,
    purge_bucket: bool = False,
    force: bool = False,
    authorization: Annotated[str | None, Header()] = None,
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
       say what blocks it just moves the search to the user. ``?cascade=true`` removes exactly those, with
       everything in them. Before anything is removed, every bound namespace's subtree is enumerated and held to
       its protection (:func:`_cascade_subtrees`): a protected namespace or table refuses 409 naming it unless
       ``force=true``, and that refusal changes nothing.
    5. The warehouse's own trash is ended first (``warehouses.trash_owned_by``): each trashed object's kept grants,
       policy, claim and base record, its trash record last. The bindings select it, so it goes while they still
       stand, and a re-issue after a failure here selects the same records. Then the cascade removes each bound
       namespace's subtree against the warehouse's OWN bucket-rooted connection, one namespace fully finished before
       the next: every table detached (:func:`_detach_table`; the bytes stay unless step 7 purges them) and the
       namespaces dropped deepest first, and only then what each object held ended, its records before its grants
       (``fga_deps.end_removed_subtree``), the binding last.
       ``namespaces_dropped`` names every namespace removed, nested ones included. The `dir` backend drops a
       namespace Restrict whatever ``behavior`` says (measured: ``behavior="Cascade"`` is ignored, not
       refused), so emptying it first is what lets the drop land. A failure part-way leaves every object not
       yet removed with every grant it had, and every step tolerates what an earlier attempt already removed,
       so re-issuing the call finishes it.
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

    # [[LH-205]] The cascade opens the warehouse's store and the purge empties a bucket through the
    # ESTATE's client, so for a record naming another store the one would sign toward that host with the
    # estate's key and the other would delete the estate's same-named bucket. Refused here, before the
    # first step lands, so the caller gets the refusal and not a partial-delete report. A plain delete
    # touches no store and stays open: it is how such a record is retired.
    if bound or purge_bucket:
        require_estate_store(record.get("endpoint"), estate=settings.s3_endpoint, subject=f"warehouse {warehouse_id!r}")

    # The warehouse's OWN endpoint ([[LH-067]]): a cascade that opened the estate's store would find none of
    # this warehouse's namespaces and report a clean delete having removed nothing.
    ns_conn = await namespace_for_root(request, settings, record["root_uri"], endpoint=record.get("endpoint") or None) if bound else None
    subtrees = await _cascade_subtrees(ns_conn, settings, bound, force=force) if ns_conn is not None else []
    # The warehouse's own trash, which the bindings select: it is ended before the cascade below removes them.
    trashed = await run_in_threadpool(partial(warehouses.trash_owned_by, settings.registry_root, so, warehouse_id, bucket=bucket))
    # What landed, recorded as each step lands, so a failure part-way reports it rather than what was planned.
    namespaces_dropped: list[str] = []
    tables_removed: list[str] = []
    trash_ended: list[str] = []
    revoked = 0
    try:
        for entry in trashed:
            kind, canonical = str(entry.get("kind") or "table"), str(entry.get("id") or "")
            # A trashed object's grants are held by the uuid its record keeps ([[LH-524]]). Counted as the revoke lands,
            # so a failure in the records after it still reports these tuples.
            trashed_uuid = trash.object_uuid(entry)
            if trashed_uuid is not None:
                obj = fga.object_of(kind, trashed_uuid)
                revoked += await fga_deps.revoke_removed_object(client, settings, obj, token=token)
            else:
                log.warning("warehouse_cascade_trash_uuid_unread", extra={"warehouse": warehouse_id, "kind": kind, "id": canonical})
            await _forget_trashed(settings, entry)
            trash_ended.append(f"{kind}:{canonical}")
        if ns_conn is not None:
            for subtree in subtrees:
                for table in subtree.tables:
                    # What the announcement names the table by, read while it still stands (ADR 0173 §10).
                    known = await uuids.table(table)
                    if await _detach_table(ns_conn, settings, table, purge=purge_bucket):
                        tables_removed.append(fga.canonical_object_id(table))
                        await namespaces_api._announce_removed(  # noqa: SLF001 — one announcement, deliberately
                            emitter,
                            table,
                            operation=DEREGISTER_TABLE,
                            token=token,
                            authorization=authorization,
                            uuids=uuids,
                            known=known,
                        )
                for namespace in subtree.namespaces:
                    try:
                        await run_in_threadpool(native.call, ns_conn, "drop_namespace", DropNamespaceRequest(id=namespace))
                    except NamespaceNotFoundError:
                        # Drift, or an earlier attempt's work: the binding outlived what it pointed at, and refusing
                        # here would leave a binding nobody can delete, so the catalog's own records still go.
                        log.warning("warehouse_cascade_namespace_absent", extra={"warehouse": warehouse_id, "namespace": namespace})
                    namespaces_dropped.append(fga.canonical_object_id(namespace))
                async for count in fga_deps.end_removed_subtree(client, settings, root_uuid=subtree.uuid, removed=subtree.descendants, token=token):
                    revoked += count
                top_ns = subtree.segments[0]
                await run_in_threadpool(warehouses.unbind_namespace, settings.registry_root, so, top_ns)
                request.app.state.warehouse_binding_cache.pop(top_ns, None)
        # A namespace an earlier attempt removed is named by nothing but this warehouse's `child` edge, so its subtree
        # is ended from there before the warehouse's own grants go.
        ended = {fga.namespace_object(subtree.uuid) for subtree in subtrees if subtree.uuid is not None}
        for orphan in await fga_deps.orphaned_children(client, settings, f"warehouse:{warehouse_id}", ended=ended):
            async for count in fga_deps.end_removed_subtree(client, settings, root_uuid=orphan, removed=[], token=token):
                revoked += count

        revoked += await fga_deps.revoke_removed_object(client, settings, f"warehouse:{warehouse_id}", token=token)
        await run_in_threadpool(warehouses.delete_warehouse_record, settings.registry_root, so, warehouse_id)
        purged = await run_in_threadpool(warehouses.purge_bucket, bucket, so) if purge_bucket else 0
    except Exception as exc:
        # Partial-failure honesty (Decision 3), the half a raised error would otherwise erase: once the first
        # table is detached, a later step CAN still fail (an OpenFGA outage on the revoke, a registry
        # blip) — and the caller then gets a problem body that says nothing about what is already
        # gone. The error is deliberately NOT swallowed (fail-closed; every primitive is idempotent, so the
        # recovery path is to re-issue the same call), but what DID land is recorded here or it is lost.
        if isinstance(exc, fga.RevokeInterrupted):
            # The object whose revoke the outage stopped lost these before it.
            revoked += len(exc.removed)
        log.error(
            "warehouse_delete_partial",
            extra={
                "warehouse": warehouse_id,
                "project": project,
                "bucket": bucket,
                "namespaces_dropped": namespaces_dropped,
                "tables_removed": tables_removed,
                "trash_ended": trash_ended,
                "tuples_revoked": revoked,
                "bucket_purged": False,
            },
            exc_info=True,
        )
        # AND TELL THE CALLER, not only the log. The log records what landed for an operator; the
        # caller got a problem body that said nothing, so it could not tell a delete that did nothing
        # from one that destroyed three namespaces and then failed — and those need different next
        # actions. The outcome rides as RFC 9457 extension members, which is what lets a 5xx say
        # something true about the caller's own objects while `detail` stays redacted.
        #
        # ServiceUnavailable rather than the original error, and that is the honest code: every
        # primitive here is idempotent, so the recovery is to re-issue the SAME call — which is
        # precisely what a 503 asks a client to do. The cause is chained, so the real failure is still
        # in the traceback and the log.
        raise PartiallyApplied(
            f"warehouse {warehouse_id!r} was PARTIALLY deleted: {len(namespaces_dropped)} namespace(s) dropped, "
            f"{len(tables_removed)} table(s) removed, {len(trash_ended)} trashed object(s) ended and {revoked} tuple(s) "
            "revoked before the failure. Every step is "
            "idempotent — re-issue this call to finish it.",
            problem_extra={
                "warehouse": warehouse_id,
                "namespaces_dropped": namespaces_dropped,
                "tables_removed": tables_removed,
                "trash_ended": trash_ended,
                "tuples_revoked": revoked,
                "bucket_purged": False,
                "partial": True,
            },
        ) from exc

    log.info(
        "warehouse_deleted",
        extra={
            "warehouse": warehouse_id,
            "project": project,
            "bucket": bucket,
            "namespaces_dropped": namespaces_dropped,
            "tables_removed": tables_removed,
            "trash_ended": trash_ended,
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
        actor=f"user:{token.subject}" if token else None,
        extra={
            "project": project,
            "bucket": bucket,
            "namespaces_dropped": namespaces_dropped,
            "bucket_purged": purge_bucket,
            "objects_purged": purged,
        },
    )
    return DeleteWarehouseResponse(
        id=warehouse_id,
        bucket=bucket,
        namespaces_dropped=namespaces_dropped,
        tuples_revoked=revoked,
        bucket_purged=purge_bucket,
        objects_purged=purged,
    )


@router.post("/{warehouse_id}/validate", response_model_exclude_none=True)
async def validate_warehouse_credentials(
    warehouse_id: str,
    settings: SettingsDep,
    token: CurrentToken,
    client: FgaClientDep,
    vendor: VendorDep,
) -> ProbeReport:
    """Prove this warehouse's vended credentials are actually SCOPED, by using one.

    THE CONTROL THIS ASSERTS IS THE WHOLE STORAGE POSTURE. `core/vending.py::build_session_policy`
    restricts every credential to one bucket + prefix, and the goal names STS as the answer for
    storage — so if the object store ACCEPTS the inline session policy and IGNORES it, every credential
    the estate has ever vended reached the whole bucket, every log line reads normal, and no audit
    record could reconstruct which of them over-reached. Nothing asserted it: every vending defect in
    this estate's history was found by a client failing later, never by the catalog.

    IT WRITES, and that is the point — a policy document can be inspected without learning anything
    about the store that receives it. The probe vends a real credential for `_validate/<uuid4>` under
    the warehouse's own root, writes and reads inside it, then attempts a write to the PARENT prefix
    which MUST be refused, and removes what it made. The failure of that last write is the finding.

    Admin-gated on the warehouse's own project and collapsing denied → 404, the same
    no-existence-oracle rule the lifecycle doors follow (audit #4): this door writes to a tenant's
    bucket, so its gate is the create/lifecycle rung and not a read one.

    Reports every check even after one fails (see :func:`summarize_probe`) — an over-permissive store
    and an unreachable one need different answers, and a first-failure exit cannot tell them apart.
    """
    _require_enabled(settings)
    so = settings.storage_options()
    record = await run_in_threadpool(warehouses.get_warehouse, settings.registry_root, so, warehouse_id)
    if record is None:
        raise TableNotFoundError(f"warehouse not found: {warehouse_id}")
    try:
        await fga_deps.require_can_create_warehouse(client, settings, token, project=record["project"])
    except PermissionDeniedError as exc:
        raise TableNotFoundError(f"warehouse not found: {warehouse_id}") from exc
    # [[LH-205]] The probe vends through the estate's STS and writes through the estate's client, so for
    # a record naming another store it would vend and write against the wrong store entirely.
    require_estate_store(record.get("endpoint"), estate=settings.s3_endpoint, subject=f"warehouse {warehouse_id!r}")

    subject = token.subject if token is not None else ANONYMOUS_SUBJECT
    probe = partial(_run_scope_probe, str(record["root_uri"]), vendor=vendor, store_endpoint=settings.s3_endpoint, session_name=role_session_name(subject))
    checks = await run_in_threadpool(probe)
    return summarize_probe(checks)


def _run_scope_probe(root_uri: str, *, vendor: CredentialVendor, store_endpoint: str, session_name: str) -> list[ProbeCheck]:
    """The IO half: vend, write in, read in, write OUT (must fail), clean up.

    The probe signs with the vended credential and reaches the store at ``store_endpoint``, the catalog's own
    address: the vended endpoint is the one a client reaches from where it runs ([[LH-489]]), which the catalog's
    pod need not resolve. The credential is named ``session_name`` at the store, for the admin who asked.

    Every step records its own outcome and the ones it makes impossible are SKIPPED rather than
    reported as failures — a probe that could not write has learned nothing about scope, and saying so
    is the difference between "the store is over-permissive" and "the store was unreachable".
    """
    bucket, prefix = split_s3_uri(root_uri.rstrip("/"))
    probe_prefix = f"{prefix.rstrip('/') + '/' if prefix else ''}_validate/{uuid.uuid4().hex}"
    inside = f"{probe_prefix}/probe"
    # Its own key, inside the same vended prefix: the conditional put must land on a key nothing else
    # wrote, or a leftover from a previous run would make the FIRST put look like a racing writer.
    cas = f"{probe_prefix}/cas"
    # The PARENT, never a sibling: a credential scoped to `<probe>/` must not reach the warehouse root,
    # and the root is where a policy that was ignored would let it write.
    outside = f"{prefix.rstrip('/') + '/' if prefix else ''}_validate_scope_probe_should_fail"

    checks: list[ProbeCheck] = []
    try:
        vended = vendor.vend(table_location=f"s3://{bucket}/{probe_prefix}", tier="maintain", session_name=session_name)
    except Exception as exc:  # noqa: BLE001 — any vend failure is one reportable outcome, not a 500
        checks.append(ProbeCheck(name="issue", outcome="fail", detail=f"{type(exc).__name__}: {exc}"))
        vended = None
    else:
        checks.append(
            ProbeCheck(name="issue", outcome="pass")
            if vended is not None
            else ProbeCheck(name="issue", outcome="skip", detail="server_mediated: this warehouse vends no direct credentials")
        )

    if vended is None:
        checks += [
            ProbeCheck(name=n, outcome="skip", detail="no credential") for n in ("write_inside", "read_inside", SCOPE_CHECK, CAS_RESERVE, CAS_CHECK, "cleanup")
        ]
        return checks

    options = vended.storage_options
    s3 = s3_client(
        store_endpoint or None,
        access_key=options.get("aws_access_key_id") or options.get("access_key_id"),
        secret_key=options.get("aws_secret_access_key") or options.get("secret_access_key"),
        session_token=options.get("aws_session_token") or options.get("session_token"),
        region=options.get("region") or options.get("aws_region"),
    )

    def _step(name: str, fn: Callable[[], object], *, expect_refusal: bool = False) -> bool:
        try:
            fn()
        except Exception as exc:  # noqa: BLE001 — the store's refusal is the RESULT here, not an error
            detail = f"{type(exc).__name__}: {str(exc)[:120]}"
            checks.append(ProbeCheck(name=name, outcome="pass" if expect_refusal else "fail", detail="" if expect_refusal else detail))
            return expect_refusal
        checks.append(
            ProbeCheck(name=name, outcome="fail", detail="the store accepted a write OUTSIDE the credential's prefix")
            if expect_refusal
            else ProbeCheck(name=name, outcome="pass")
        )
        return not expect_refusal

    wrote = _step("write_inside", lambda: s3.put_object(Bucket=bucket, Key=inside, Body=b"scope-probe"))
    if wrote:
        _step("read_inside", lambda: s3.get_object(Bucket=bucket, Key=inside))
        # `_step` returns "the step got the outcome we wanted", so a False here means the out-of-scope
        # write LANDED and left an object at the warehouse root. Cleaned below — but only in that case:
        # a correctly scoped credential is refused the delete too, and reporting that refusal as a
        # cleanup failure would raise a false alarm about the store that just passed.
        scope_refused = _step(SCOPE_CHECK, lambda: s3.put_object(Bucket=bucket, Key=outside, Body=b"scope-probe"), expect_refusal=True)
        # PUT-IF-NOT-EXISTS, the primitive Lance's commit protocol rests on
        # (`lance_docs/file_format.md` § "Commit Protocol" -> "Storage Primitives"). Two conditional
        # puts at one key: the first must be ACCEPTED and the second REFUSED. A store that accepts
        # both has no "exactly one writer succeeds" guarantee, so two concurrent Lance writers would
        # each believe they committed the same manifest version — with a 200 on both and no error
        # anywhere. That is the failure this step exists to name, and it is why the SECOND put is the
        # check rather than the first.
        reserved = _step(CAS_RESERVE, lambda: s3.put_object(Bucket=bucket, Key=cas, Body=b"first", IfNoneMatch="*"))
        if reserved:
            _step(CAS_CHECK, lambda: s3.put_object(Bucket=bucket, Key=cas, Body=b"second", IfNoneMatch="*"), expect_refusal=True)
        else:
            # The store refused the HEADER, not a racing writer. That is a real failure — recorded by
            # the step above — but it measures nothing about a second writer, so the claim stays
            # UNKNOWN rather than becoming a verdict the probe never reached.
            checks.append(ProbeCheck(name=CAS_CHECK, outcome="skip", detail="the store refused the conditional header itself"))

        def _clean() -> None:
            s3.delete_object(Bucket=bucket, Key=inside)
            s3.delete_object(Bucket=bucket, Key=cas)
            if not scope_refused:
                s3.delete_object(Bucket=bucket, Key=outside)

        _step("cleanup", _clean)
    else:
        checks += [ProbeCheck(name=n, outcome="skip", detail="nothing was written") for n in ("read_inside", SCOPE_CHECK, CAS_RESERVE, CAS_CHECK, "cleanup")]
    return checks
