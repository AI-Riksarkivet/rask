"""Admin control-plane endpoints: warehouse provisioning, lifecycle, protection and deletion (#3-A).

A *warehouse* = one physically separate S3 bucket owned by a project (the FGA model's catalog-root type), holding its
own directory catalog. These routes are the **control plane**: an authorized project-admin provisions a bucket at
RUNTIME (not from a bucket list fixed in the chart's values), and its id is the first segment of every name beneath it
([[LH-593]], ADR 0206), so a table named ``<warehouse>$<namespace>$<table>`` lands in that warehouse's bucket —
physically isolated from every other tenant's. Physical multi-tenancy: one project may hold many warehouses, and a
table's bytes live in the bucket its name's warehouse owns, never in a neighbour's.

Authorization is DELIBERATELY stronger than the data plane: warehouse-create gates on the project's
`can_create_warehouse` (= admin) — the model action that until now was defined but never enforced — not the
writer-tier create-on-parent that guards tables/namespaces. There is exactly ONE door and no exception:
the project must already EXIST (`require_project_exists`, a 404 naming `POST /management/v1/projects`), and that
route is where a tenant and its first admin are minted together (`open_hierarchy_lifecycle.md` Decision 1), so
creating a warehouse never grants its creator tenant admin.

DELETE is the mirror image (Decision 3): bottom-up, gated on the PROJECT's `can_administer` because
destroying a tenant's storage is a tenant-level act, refusing 409 while the warehouse holds any namespace. `cascade`,
bucket `purge_bucket` and `force` are three SEPARATE opt-ins that never share a default — see
`delete_warehouse`.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Callable, Mapping, Sequence
from contextlib import suppress
from datetime import UTC, datetime
from functools import partial
from typing import Annotated, Any, Final, Literal

from fastapi import APIRouter, Depends, Query, Request
from fastapi.concurrency import run_in_threadpool
from lance_namespace import (
    ConcurrentModificationError,
    DeregisterTableRequest,
    DescribeTableRequest,
    DescribeTableResponse,
    DropNamespaceRequest,
    InvalidInputError,
    InvalidTableStateError,
    LanceNamespace,
    LanceNamespaceError,
    NamespaceAlreadyExistsError,
    NamespaceNotEmptyError,
    NamespaceNotFoundError,
    PermissionDeniedError,
    ServiceUnavailableError,
    TableNotFoundError,
)
from openfga_sdk import OpenFgaClient
from pydantic import BaseModel, ConfigDict

from catalog.api import fga_deps
from catalog.api.announcing import AnnouncerDep
from catalog.api.declared import DECLARATION, Author, author_of, refuse_what_git_declares
from catalog.api.dependencies import (
    ControlEmitterDep,
    FgaClientDep,
    ObjectUuidsDep,
    SettingsDep,
    VendorDep,
    namespace_for_root,
)
from catalog.api.pagination import ListLimit
from catalog.api.security import Caller, CurrentToken
from catalog.api.v1.endpoints import namespaces as namespaces_api
from catalog.core.config import Settings
from catalog.core.identifiers import parse_identifier
from catalog.core.lineage_emit import DEREGISTER_TABLE
from catalog.core.store_endpoint import names_the_estate_store, require_estate_store
from catalog.core.vending import CredentialVendor, role_session_name
from catalog.schemas import (
    CreateWarehouseRequest,
    DeleteProfile,
    DeleteWarehouseResponse,
    ProtectionResponse,
    SetProtectionRequest,
    TrashListing,
    WarehouseResponse,
)
from catalog.services import clone_endings, dataplane, native, table_claims, trash_doors, warehouses
from catalog.services.announcement_intents import Announcement, ControlMessage, NameResolves, RecordGone
from catalog.services.control_records import ProtectionChange, with_protection
from catalog.services.vend_probe import CAS_CHECK, CAS_RESERVE, ENCRYPTED_WRITE, SCOPE_CHECK, ProbeCheck, ProbeReport, summarize_probe
from lakehouse import base_registry, clone_sources, data_directories, location_claims, trash
from lakehouse.control_emit import ControlEmitter, emit_control
from lakehouse.control_events import CatalogControlEvent
from lakehouse.governed import fga
from lakehouse.governed.deps import ANONYMOUS_SUBJECT
from lakehouse.naming import CATALOG_DELIMITER, CONTROL_ID_RE
from lakehouse.ns_errors import PartiallyApplied, status_for
from lakehouse.records import RecordExistsError, RecordMissingError
from lakehouse.storage import s3_client, split_s3_uri


log = logging.getLogger(__name__)

#: The probe checks whose failure is a verdict on the STORE: it ignored a credential's scope, or a conditional
#: put. Every other failing check (`issue`, `write_inside`, `read_inside`, `cleanup`) means the probe could not
#: reach the store or its STS, which says nothing about the store.
_CONFORMANCE_CHECKS: Final = frozenset({SCOPE_CHECK, CAS_RESERVE, CAS_CHECK, ENCRYPTED_WRITE})

router = APIRouter(prefix="/management/v1/warehouses", tags=["warehouse"])

# A bucket/warehouse id must be a DNS-safe S3 bucket name fragment — validated here so a malformed id
# can't produce an un-createable bucket or a path-traversing registry key. The pattern is SHARED
# (`catalog.core.identifiers`): it lived here, in projects.py and in policies.py as three copies of one
# sentence, and they had already diverged on the `$`-vs-`\Z` anchor (an id ending in a newline was
# refused by one door and accepted by another).
_ID_RE = CONTROL_ID_RE

type WarehouseStatus = Literal["active", "deactivated"]


def _validate_id(value: str, *, what: str) -> str:
    if not _ID_RE.match(value):
        raise InvalidInputError(f"invalid {what} {value!r}: must match {_ID_RE.pattern}")
    return value


def _delete_profile_fields(profile: DeleteProfile | None) -> dict[str, str]:
    """The record fields a request's delete profile writes, none when it names none. The ``estate`` mode is written
    as a mode the merge removes from an existing record and a drop reads as the estate's grace period."""
    if profile is None:
        return {}
    if profile.grace_days is None:
        return {warehouses.DELETE_MODE: profile.mode.value}
    return {warehouses.DELETE_MODE: profile.mode.value, warehouses.TRASH_GRACE_DAYS: str(profile.grace_days)}


@router.post("", response_model_exclude_none=True)
async def create_warehouse(
    request: Request,
    settings: SettingsDep,
    token: CurrentToken,
    client: FgaClientDep,
    control: ControlEmitterDep,
    vendor: VendorDep,
    body: CreateWarehouseRequest,
) -> WarehouseResponse:
    """Provision a warehouse: create its physical bucket + register it + seed FGA. Admin-gated.

    Order (fail-closed): the request's shape (400), then the project must EXIST (``require_project_exists`` — 404 naming
    ``POST /management/v1/projects`` as the fix; existence is the registry's answer, identical for every caller, so
    it runs before authz), THEN authorize ``can_create_warehouse`` on the project, then :func:`establish_warehouse`
    makes it. A warehouse git declares is not changed here ([[XC-213]]): its declaration is, and the catalog converges
    it. A re-run with the same id is idempotent (bucket + record overwrite-safe).
    """
    ids = validated_ids(settings, body)
    _warehouse_id, project, _bucket = ids
    await fga_deps.require_project_exists(settings, project)
    await fga_deps.require_can_create_warehouse(client, settings, token, project=project)
    # A declaration naming the warehouse's deletion protection asks what the protection door asks ([[LH-548]]).
    if body.protected is not None:
        await _require_project_admin(client, settings, token, project=project)
    record = await establish_warehouse(request, settings, body, ids, client=client, control=control, vendor=vendor, author=author_of(token), owner=token)
    return WarehouseResponse.model_validate(record)


#: A create's warehouse id, project and bucket, each judged a control id (:func:`validated_ids`).
type WarehouseIds = tuple[str, str, str]


def validated_ids(settings: Settings, body: CreateWarehouseRequest) -> WarehouseIds:
    """The warehouse id, project and bucket ``body`` names, each a control id.

    Raises:
        InvalidInputError: An id is malformed, the id is the estate's own warehouse, the serving class is unknown, or
            the endpoint is not the estate's object store ([[LH-205]]): another store needs a credential the catalog
            resolves and consumes on every open, provision and vend, and no door consumes one, so admitting it would
            have the estate's own key signed toward a host the caller chose.
    """
    warehouse_id = _validate_id(body.id, what="warehouse id")
    project = _validate_id(body.project, what="project id")
    bucket = _validate_id(body.bucket or body.id, what="bucket name")
    if warehouse_id == settings.estate_warehouse:
        raise InvalidInputError(f"warehouse id {warehouse_id!r} is the estate's own warehouse; choose another")
    # Only the one serving class the estate defines: an unknown value would record a class no reader of the record knows.
    if body.serving is not None and body.serving != "gold":
        raise InvalidInputError(f"invalid serving {body.serving!r}: only 'gold' is supported")
    if not names_the_estate_store(body.endpoint, estate=settings.s3_endpoint):
        raise InvalidInputError(
            f"endpoint {body.endpoint!r} is not the estate's object store. A warehouse in another store needs a credential "
            "reference the catalog consumes on every open, provision and vend, and none is consumed yet, so the catalog "
            "would sign with the estate's own key toward that host. Omit `endpoint` to use the estate's store."
        )
    return warehouse_id, project, bucket


async def establish_warehouse(
    request: Request,
    settings: Settings,
    body: CreateWarehouseRequest,
    ids: WarehouseIds,
    *,
    client: OpenFgaClient | None,
    control: ControlEmitter,
    vendor: CredentialVendor,
    author: Author | None,
    owner: Caller | None,
) -> dict[str, str]:
    """Make the warehouse ``body`` names, or converge the one that stands onto it; the record as written.

    What the create door does once its caller is authorized, and what the catalog does for a warehouse git declares
    ([[XC-213]]): claim and provision the bucket (idempotent), probe its store, write the registry record, give the
    warehouse its row in its own catalog, seed its grants and announce it. The probe refuses a store that ignores
    credential scope or a conditional put (400) and one it cannot reach (503), so no such warehouse becomes routable
    ([[LH-481]]). ``ids`` are ``body``'s (:func:`validated_ids`), in a project its caller found in the registry.
    ``owner`` is the caller granted ``owner`` on it, none for a declared warehouse, whose project's admins own it
    through the project.

    Raises:
        NamespaceAlreadyExistsError: The id or the bucket belongs to another project.
        PermissionDeniedError: Git declares the warehouse and ``author`` is not the declaration.
        InvalidInputError: The bucket is reserved, or its store failed the probe.
        ServiceUnavailableError: The store or its STS could not be probed.
    """
    warehouse_id, project, bucket = ids
    existing = await _claim_the_bucket(settings, body, ids, author=author, owner=owner)
    root_uri = f"s3://{bucket}"
    await run_in_threadpool(warehouses.provision_bucket, bucket, settings.storage_options())
    await _require_a_conforming_store(settings, vendor, root_uri, warehouse_id=warehouse_id, author=author)
    actor = author.actor if author is not None else None
    protection = None if body.protected is None else ProtectionChange(protected=body.protected, by=actor or f"user:{ANONYMOUS_SUBJECT}")
    record = _record_for(body, existing, warehouse_id=warehouse_id, project=project, bucket=bucket, protection=protection)
    replaced, record = await _write_record(settings, body, record, existing=existing, protection=protection)
    # ITS OWN ROW IN ITS OWN CATALOG ([[LH-593]]): every name beneath a warehouse begins with its id, and the directory
    # catalog refuses a namespace whose parent has no row (measured on pylance 13.0.0). After the record, which makes
    # the root the warehouse's, and idempotent, so a re-POST converges a create that failed here.
    ns_conn = await namespace_for_root(request, settings, root_uri, endpoint=record.get("endpoint") or None)
    await run_in_threadpool(warehouses.ensure_catalog_row, ns_conn, warehouse_id)
    if owner is not None:
        await fga_deps.seed_warehouse(client, settings, owner, warehouse_id=warehouse_id, project=project)
    elif author is not None:
        await fga_deps.converge_cascade_grants(client, settings, warehouse_id=warehouse_id, project=project, actor=author.subject)
    log.info("warehouse_created", extra={"warehouse": warehouse_id, "bucket": bucket, "project": project})
    await emit_control(
        control,
        action="warehouse_created",
        object_type="warehouse",
        object_id=f"warehouse:{warehouse_id}",
        actor=actor,
        extra={"bucket": bucket, "project": project},
    )
    if replaced.get("protected") != record.get("protected"):
        await _announce_protection(control, warehouse_id, actor, protected=record.get("protected") == "true")
    return record


async def _claim_the_bucket(
    settings: Settings, body: CreateWarehouseRequest, ids: WarehouseIds, *, author: Author | None, owner: Caller | None
) -> dict[str, str] | None:
    """Refuse a create that would take another project's warehouse id or bucket, or that anyone but the declaration
    makes over a warehouse git declares, then claim the bucket for the project.

    Returns:
        The warehouse's registry record as it stood before the claim, ``None`` for a new warehouse.

    Raises:
        NamespaceAlreadyExistsError: The id or the bucket belongs to another project.
        PermissionDeniedError: Git declares the warehouse and ``author`` is not the declaration.
        InvalidInputError: The bucket is reserved platform storage, or a re-POST naming no endpoint keeps a record that
            names another store.
    """
    warehouse_id, project, bucket = ids
    so = settings.storage_options()
    records = await run_in_threadpool(warehouses.list_warehouses, settings.registry_root, so)
    # Cross-tenant takeover guard: `can_create_warehouse` gates on the caller-named `project`, so an admin of
    # ANY project could otherwise re-POST an EXISTING warehouse id under their own project — the seed ADDS
    # `warehouse:<id> project project:<theirs>` alongside the original owner's tuples, making their project's
    # admins owners of the victim's warehouse + every table under it (routing still points at the same
    # bucket → full cross-tenant disclosure). Reject a collision with a warehouse owned by another project.
    # A same-project re-create stays idempotent (the partial-failure retry path relies on it).
    existing = await run_in_threadpool(warehouses.get_warehouse, settings.registry_root, so, warehouse_id)
    if existing is not None and existing.get("project") != project:
        raise NamespaceAlreadyExistsError(f"warehouse {warehouse_id!r} is already registered to another project")
    # Only the declaration changes a warehouse git declares ([[XC-213]]): a door's create is refused whoever makes it,
    # a stack that authenticates nobody included. Judged on the read the guard above made, so the create reads its
    # record once.
    if author is not DECLARATION:
        refuse_what_git_declares(existing, obj=f"warehouse:{warehouse_id}", token=owner)
    # [[LH-205]] A re-POST that names no endpoint carries the live one forward, and `provision_bucket`
    # always reaches the ESTATE's store. For a record naming another store that would create its
    # bucket in the wrong store and keep the record pointing at the foreign one, so the re-POST is
    # refused unless it re-points the warehouse itself (an explicit endpoint, judged by `validated_ids`).
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
    # the other. `records` was listed before the claim, the provision is a network round trip, and
    # the record write comes after it — so two concurrent creates naming one bucket under
    # different projects each scan a listing taken before the other existed, both pass, and both write.
    # The claim is WRITE-ONCE AT THE STORE (`If-None-Match: *`), so the loser is refused regardless of
    # what its caller read. It is taken BEFORE `provision_bucket`: provisioning an existing bucket is a
    # silent no-op, so a rival that provisioned first and lost the claim would otherwise have already
    # touched the victim's storage.
    #
    # The scan stays because it answers a different question cheaply and with a listing already in hand;
    # this arbitrates the one case it structurally cannot see.
    await run_in_threadpool(partial(warehouses.claim_bucket, settings.registry_root, so, bucket=bucket, project=project, warehouse_id=warehouse_id))
    return existing


async def _require_a_conforming_store(settings: Settings, vendor: CredentialVendor, root_uri: str, *, warehouse_id: str, author: Author | None) -> None:
    """Probe the store under ``root_uri`` before the record makes it routable ([[LH-481]], ADR 0089).

    Every Lance commit rests on the store refusing a second conditional put, and every vended credential on it
    honouring the session policy; a store that accepts either and ignores it fails silently, with a 200 on every write.
    The probe `POST …/validate` runs is run here too, so such a warehouse is refused rather than found later by a
    client. A `skip` (this deployment vends no direct credentials) proves nothing and refuses nothing. The bucket claim
    stays: it names this project and id, so the same create retried converges.

    TWO ANSWERS, because the two failures need different things from the caller. A store that broke a guarantee is
    the request's problem, 400, and retrying changes nothing. A probe that could not reach the store or its STS learned
    nothing about it, 503, and the retry is the right move; a GitOps re-POST of a live warehouse must not read a store
    blip as a verdict on its store.

    Raises:
        InvalidInputError: The store failed a conformance check.
        ServiceUnavailableError: The store or its STS could not be probed.
    """
    bucket = split_s3_uri(root_uri)[0]
    creator = role_session_name(author.subject if author is not None else ANONYMOUS_SUBJECT)
    probe = partial(_run_scope_probe, root_uri, vendor=vendor, store_endpoint=settings.s3_endpoint, session_name=creator)
    report = summarize_probe(await run_in_threadpool(probe))
    failed = [c.name for c in report.checks if c.outcome == "fail"]
    if broken := [name for name in failed if name in _CONFORMANCE_CHECKS]:
        log.warning("warehouse_store_failed_probe", extra={"warehouse": warehouse_id, "bucket": bucket, "failed": broken})
        raise InvalidInputError(
            f"warehouse {warehouse_id!r} was not registered: its object store failed the storage probe ({', '.join(broken)}). "
            "A Lance warehouse needs a store that enforces vended-credential scope, refuses a second conditional write, and "
            "takes the encryption its vends name (the catalog's role granted kms:Decrypt and kms:GenerateDataKey on the key)."
        )
    if failed:
        log.warning("warehouse_store_unprobed", extra={"warehouse": warehouse_id, "bucket": bucket, "failed": failed})
        raise ServiceUnavailableError(f"warehouse {warehouse_id!r} was not registered: its object store could not be probed ({', '.join(failed)}); retry")


def _record_for(
    body: CreateWarehouseRequest,
    existing: dict[str, str] | None,
    *,
    warehouse_id: str,
    project: str,
    bucket: str,
    protection: ProtectionChange | None,
) -> dict[str, str]:
    """The registry record ``body`` asks for, carrying forward from ``existing`` what a re-create does not own."""
    record = {
        "id": warehouse_id,
        "bucket": bucket,
        "root_uri": f"s3://{bucket}",
        "project": project,
        # Idempotent re-create must NOT resurrect a DEACTIVATED warehouse nor reset created_at (audit #1): a
        # GitOps reconcile / partial-failure retry re-POSTing an existing id would otherwise silently lift a
        # quarantine with no /activate call and no audit signal. Carry the MUTABLE lifecycle fields forward
        # from the existing record; reactivation goes ONLY through the explicit /activate endpoint.
        "status": existing.get("status", "active") if existing is not None else "active",
        "created_at": (existing.get("created_at") if existing is not None else None) or datetime.now(UTC).isoformat(),
        # [[LH-067]] Which object store this bucket is reached at, and only the estate's passes the
        # judgement in `validated_ids` ([[LH-205]]). CALLER-OWNED, so unlike the arm-never-disarm fields a re-POST
        # may correct it: a record naming another store is re-pointed at the estate with an explicit
        # empty string, which `_read_warehouse_root` reads as None. Omitted from the request means
        # "unchanged": the key is absent, so `_CALLER_OWNED`'s merge carries the live value forward.
        **({"endpoint": body.endpoint} if body.endpoint is not None else {}),
        # What a drop under this warehouse does (ADR 0146), CALLER-OWNED like the endpoint: named, it replaces the
        # live profile; omitted, the merge carries the live one forward.
        **_delete_profile_fields(body.delete_profile),
    }
    # Serving carries FORWARD on an idempotent re-create (same rationale as status above): a GitOps
    # reconcile re-POSTing the gold warehouse WITHOUT the serving field must not silently demote it to a
    # work warehouse. The field stays ABSENT (not null) on work warehouses: absent means work, and records
    # written without a serving class stay byte-identical.
    serving = body.serving or (existing.get("serving") if existing is not None else None)
    if serving:
        record["serving"] = serving
    # Deletion protection (Decision 5) is what the request declares ([[LH-548]]): `true` arms it, `false` clears it, and
    # a request naming neither leaves it as it stands. A new warehouse's record takes it here; a re-create's merge takes
    # it inside the conditional write (`upsert_warehouse`), against the record as it stands, since a re-POST that lost
    # the flag it never named would disarm the delete door's only safety catch with no error and no audit signal.
    if protection is not None:
        record = with_protection(record, protection)
    # A new warehouse's record takes the primary marker the request names, as the string `"true"` a re-create's merge
    # writes; a re-create takes it inside the merge (`upsert_warehouse`), where a request naming nothing carries the live
    # marker forward.
    if body.primary:
        record["primary"] = "true"
    return record


async def _write_record(
    settings: Settings, body: CreateWarehouseRequest, record: dict[str, str], *, existing: dict[str, str] | None, protection: ProtectionChange | None
) -> tuple[dict[str, str], dict[str, str]]:
    """Mint ``record``, or merge it into the record that stands; the record the write replaced (empty for a mint) and
    the record it wrote, so the protection this write changed is announced and no other.

    Raises:
        NamespaceAlreadyExistsError: The record moved to another project mid-request.
        ConcurrentModificationError: The write raced a concurrent delete; retry.
    """
    so = settings.storage_options()
    warehouse_id = record["id"]

    # BOTH re-create paths go through the CONDITIONAL upsert (diff2 F4), never an unconditional put of a record
    # assembled from a read taken before the guards and before `provision_bucket`, a network round trip: a deactivate
    # landing anywhere in that window would be overwritten by the status this request carried forward, lifting a
    # quarantine with no `/activate` and no audit signal. The merge happens inside the write, against the record as it
    # actually stands, so a concurrent lifecycle change survives.
    async def _converge() -> tuple[dict[str, str], dict[str, str]]:
        try:
            return await run_in_threadpool(
                partial(
                    warehouses.upsert_warehouse,
                    settings.registry_root,
                    so,
                    record,
                    serving=body.serving,
                    protection=protection,
                    primary=bool(body.primary),
                )
            )
        except warehouses.WarehouseProjectConflict:
            # The pre-flight guard (`_claim_the_bucket`) already refuses this for the ordinary case; reaching it here
            # means the record moved projects mid-request, and the answer is the same either way.
            raise NamespaceAlreadyExistsError(f"warehouse {warehouse_id!r} is already registered to another project") from None
        except RecordMissingError:
            # Raced a concurrent delete: retryable, never a blind re-create — code 14 → 409 so the
            # caller retries into a clean create, on the sequential path as on the lost mint race.
            raise ConcurrentModificationError(f"warehouse {warehouse_id!r} write raced a concurrent delete; retry") from None

    if existing is not None:
        return await _converge()
    # The id-MINT is conditional (F1): the takeover guard read the registry BEFORE deciding, and a rival create landing
    # between that read and this write must not win silently, so the store refuses the second create, whatever the
    # guard saw.
    try:
        await run_in_threadpool(warehouses.create_warehouse_record, settings.registry_root, so, record)
    except RecordExistsError:
        return await _converge()  # lost the mint race — converge on the WINNER, conditionally
    return {}, record


@router.get("", response_model_exclude_none=True)
async def list_warehouses(settings: SettingsDep, token: CurrentToken, client: FgaClientDep) -> list[WarehouseResponse]:
    """Every warehouse the caller can read. Governed like the metadata feeds: with FGA on, filtered to the
    warehouses the caller has ``can_get_metadata`` on (never discloses another tenant's bucket names)."""
    records = await run_in_threadpool(warehouses.list_warehouses, settings.registry_root, settings.storage_options())
    if (authz := fga_deps.authorization(settings, client, token)) is not None:
        # The listing's own candidates, batch-checked ([[LH-223]]): never what the caller reaches across the estate,
        # which OpenFGA stops counting at its ceiling.
        allowed = await authz.batch_check("can_get_metadata", [f"warehouse:{r['id']}" for r in records])
        records = [r for r in records if allowed.get(f"warehouse:{r['id']}")]
    return [WarehouseResponse.model_validate(r) for r in records]


@router.get("/{warehouse_id}", response_model_exclude_none=True)
async def get_warehouse(warehouse_id: str, settings: SettingsDep, token: CurrentToken, client: FgaClientDep) -> WarehouseResponse:
    """One warehouse record — reader-gated on ``warehouse:<id>`` (fail-closed on an OpenFGA outage)."""
    await fga_deps.require_relation(client, settings, token, relation="can_get_metadata", obj=f"warehouse:{warehouse_id}")
    record = await run_in_threadpool(warehouses.get_warehouse, settings.registry_root, settings.storage_options(), warehouse_id)
    if record is None:
        raise TableNotFoundError(f"warehouse not found: {warehouse_id}")
    return WarehouseResponse.model_validate(record)


@router.get("/{warehouse_id}/trash", response_model_exclude_none=True)
async def list_warehouse_trash(
    warehouse_id: str,
    settings: SettingsDep,
    token: CurrentToken,
    client: FgaClientDep,
    limit: ListLimit,
    namespace: Annotated[str | None, Query(description="List only what was dropped beneath this namespace name.")] = None,
    page_token: str | None = None,
) -> TrashListing:
    """Every recoverable drop under this warehouse its caller holds a grant on: the way back to a dropped object whose
    name is lost, and what is waiting to be purged ([[LH-556]]).

    The undrop and tasks doors are addressed by name; this is the door that answers the names. The route asks
    ``can_get_metadata`` on the warehouse, as its namespace listing does, and each trashed table and namespace is judged
    on its own uuid (``table:<uuid>``, ``namespace:<uuid>``), the object its grants are held by and the undrop and tasks
    doors ask on (``fga_deps._require_trashed``): the warehouse's rung opens upward from any one child, so it says nothing
    about the siblings, and a listing judges its own candidates ([[LH-223]]). A record naming no uuid holds no grant and
    is listed to no one ([[LH-534]]).

    The candidates are the trash a delete of this warehouse would end (``warehouses.trash_owned_by``), so a sibling
    warehouse's drops on a shared bucket are never listed. They sort by name and then uuid, the key the opaque page token
    resumes after (``trash_doors.listing_key``, `lance_docs/ns_catalog/spec.yaml` PageToken).
    """
    await fga_deps.require_relation(client, settings, token, relation="can_get_metadata", obj=f"warehouse:{warehouse_id}")
    so = settings.storage_options()
    record = await run_in_threadpool(warehouses.get_warehouse, settings.registry_root, so, warehouse_id)
    if record is None:
        raise TableNotFoundError(f"warehouse not found: {warehouse_id}")
    owned = await run_in_threadpool(warehouses.trash_owned_by, settings.registry_root, so, warehouse_id)
    beneath = f"{namespace}{CATALOG_DELIMITER}" if namespace else ""
    candidates = sorted(
        ((trash_doors.listing_key(r), r) for r in owned if trash.object_uuid(r) and str(r.get("id") or "").startswith(beneath)), key=lambda c: c[0]
    )

    async def judged_on(records: Sequence[dict[str, Any]]) -> list[str | None]:
        return [fga.object_of(trash.kind_of(r), str(trash.object_uuid(r))) for r in records]

    page, next_token = await fga_deps.judged_page(
        fga_deps.authorization(settings, client, token),
        relation="can_get_metadata",
        candidates=candidates,
        objects_of=judged_on,
        page_token=page_token,
        limit=limit,
    )
    return TrashListing(objects=[trash_doors.entry(r) for r in page], page_token=next_token)


async def _set_warehouse_status(
    warehouse_id: str,
    status: WarehouseStatus,
    *,
    settings: Settings,
    token: Caller | None,
    client: OpenFgaClient | None,
    control: ControlEmitter,
) -> WarehouseResponse:
    """Shared deactivate/activate: admin-gate on the warehouse's OWN project, flip ``status``, persist.

    Lifecycle is a platform-admin op (same rung as create): a project admin may quarantine or restore a
    warehouse they own. Fail-closed: the record is read first (needed to gate on the REAL owning project, not
    a caller-supplied one). NO EXISTENCE ORACLE (audit #4): a caller who is not the warehouse's project admin
    gets the SAME 404 as a missing warehouse — the not-found and permission-denied outcomes are made
    indistinguishable so an unauthorized user cannot probe which warehouse ids exist. Status is read LIVE by
    the resolver, so no cache invalidation is needed — the very next routed request sees the new status."""
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
    refuse_what_git_declares(record, obj=f"warehouse:{warehouse_id}", token=token)
    updated = await change_warehouse_status(settings, control, warehouse_id, status=status, actor=f"user:{token.subject}" if token else None)
    return WarehouseResponse.model_validate(updated)


async def change_warehouse_status(
    settings: Settings, control: ControlEmitter, warehouse_id: str, *, status: WarehouseStatus, actor: str | None
) -> dict[str, str]:
    """Deactivate or activate warehouse ``warehouse_id`` and announce it; the record as written.

    What both status doors do once their caller is authorized, and what the catalog does for a warehouse whose status
    drifted from what git declares ([[XC-213]]).

    Raises:
        TableNotFoundError: The warehouse's record is gone.
    """
    updated = await run_in_threadpool(warehouses.set_warehouse_status, settings.registry_root, settings.storage_options(), warehouse_id, status)
    if updated is None:
        raise TableNotFoundError(f"warehouse not found: {warehouse_id}")
    log.info("warehouse_status_changed", extra={"warehouse": warehouse_id, "status": status})
    await emit_control(
        control,
        action="warehouse_deactivated" if status == "deactivated" else "warehouse_activated",
        object_type="warehouse",
        object_id=f"warehouse:{warehouse_id}",
        actor=actor,
        extra={},
    )
    return updated


@router.post("/{warehouse_id}/deactivate", response_model_exclude_none=True)
async def deactivate_warehouse(
    warehouse_id: str,
    settings: SettingsDep,
    token: CurrentToken,
    client: FgaClientDep,
    control: ControlEmitterDep,
) -> WarehouseResponse:
    """Quarantine a warehouse (#3-A lifecycle): the resolver then refuses EVERY op on a name beneath it
    (403), so no new tables are created and existing ones are suspended — the tenant-offboarding first step.
    Admin-gated on the warehouse's project. Idempotent (re-deactivating is a no-op)."""
    return await _set_warehouse_status(warehouse_id, "deactivated", settings=settings, token=token, client=client, control=control)


@router.post("/{warehouse_id}/activate", response_model_exclude_none=True)
async def activate_warehouse(
    warehouse_id: str,
    settings: SettingsDep,
    token: CurrentToken,
    client: FgaClientDep,
    control: ControlEmitterDep,
) -> WarehouseResponse:
    """Reactivate a quarantined warehouse (#3-A lifecycle) — restores routing to the names beneath it.
    Admin-gated on the warehouse's project. Idempotent."""
    return await _set_warehouse_status(warehouse_id, "active", settings=settings, token=token, client=client, control=control)


async def _require_project_admin(client: OpenFgaClient | None, settings: Settings, token: Caller | None, *, project: str) -> None:
    """Refuse a caller without ``can_administer`` on ``project``: the authority over its warehouses' destruction, and so
    over the lock on it."""
    await fga_deps.require_relation(client, settings, token, relation="can_administer", obj=f"project:{project}")


async def administered_warehouse(warehouse_id: str, settings: SettingsDep, token: CurrentToken, client: FgaClientDep) -> dict[str, str]:
    """The record of ``warehouse_id``, for a caller who administers its project (:func:`_require_project_admin`).

    The record is read first, since the gate is on the warehouse's own project and never on one the caller names. NO
    EXISTENCE ORACLE (audit #4): a caller who is not the project's admin is answered exactly the missing warehouse's 404,
    so nobody can probe which warehouse ids exist; an outage or a missing caller still answers 503 or 401.

    Args:
        warehouse_id: The warehouse the route's path names.
        settings: The catalog's settings.
        token: The verified caller.
        client: The OpenFGA client deciding.

    Returns:
        The warehouse's registry record.

    Raises:
        InvalidInputError: The id is not a warehouse id.
        TableNotFoundError: The warehouse is absent, or its caller does not administer its project.
    """
    _validate_id(warehouse_id, what="warehouse id")
    record = await run_in_threadpool(warehouses.get_warehouse, settings.registry_root, settings.storage_options(), warehouse_id)
    if record is None:
        raise TableNotFoundError(f"warehouse not found: {warehouse_id}")
    try:
        await _require_project_admin(client, settings, token, project=record["project"])
    except PermissionDeniedError as exc:
        raise TableNotFoundError(f"warehouse not found: {warehouse_id}") from exc
    return record


AdministeredWarehouseDep = Annotated[dict[str, str], Depends(administered_warehouse)]


def warehouse_its_doors_manage(warehouse_id: str, record: AdministeredWarehouseDep, token: CurrentToken) -> dict[str, str]:
    """The record of a warehouse the caller administers and git does not declare ([[XC-213]]).

    The gate of a door that changes what a declaration owns, asked after the caller's own authorization so only a
    caller who could otherwise make the change learns that git owns it.

    Raises:
        PermissionDeniedError: Git declares the warehouse.
    """
    refuse_what_git_declares(record, obj=f"warehouse:{warehouse_id}", token=token)
    return record


DoorManagedWarehouseDep = Annotated[dict[str, str], Depends(warehouse_its_doors_manage)]


@router.post("/{warehouse_id}/protection", response_model_exclude_none=True)
async def set_warehouse_protection(
    warehouse_id: str,
    body: SetProtectionRequest,
    record: DoorManagedWarehouseDep,
    settings: SettingsDep,
    token: CurrentToken,
    control: ControlEmitterDep,
) -> ProtectionResponse:
    """Arm or clear the warehouse's deletion protection ([[LH-548]]), the lock its delete turns only with ``force=true``.

    Gated on ``project#can_administer`` of the warehouse's own project, read from its record: arming or clearing the
    lock on a warehouse's destruction is the authority that destruction needs, as the delete door asks it. A caller
    without it is answered the missing warehouse's 404, as the delete and lifecycle doors answer one, so the door tells
    nobody which ids exist. The write is conditional on the record as it stands and refuses a record that moved to
    another project since the decision. Re-arming an armed warehouse keeps who armed it. Announced as
    ``warehouse_protected`` or ``warehouse_unprotected`` on every call, as the table and namespace doors announce theirs.
    \f

    Args:
        warehouse_id: The warehouse to protect or clear.
        body: The protection to set.
        record: The warehouse's record, its caller authorized on its project (:func:`administered_warehouse`).
        settings: The catalog's settings.
        token: The verified caller.
        control: Where control events go.

    Returns:
        The warehouse's id and the protection now set.

    Raises:
        TableNotFoundError: The warehouse is absent, its caller does not administer its project, or it moved to
            another project before the write.
    """
    actor = f"user:{token.subject}" if token is not None else None
    change = ProtectionChange(protected=body.protected, by=actor or f"user:{ANONYMOUS_SUBJECT}")
    await protect_warehouse(settings, control, warehouse_id, project=record["project"], change=change, actor=actor)
    return ProtectionResponse(id=warehouse_id, protected=body.protected)


async def protect_warehouse(
    settings: Settings, control: ControlEmitter, warehouse_id: str, *, project: str, change: ProtectionChange, actor: str | None
) -> None:
    """Arm or clear ``project``'s warehouse ``warehouse_id``'s deletion protection and announce it.

    What the protection door does once its caller is authorized, and what the catalog does for a warehouse whose
    protection drifted from what git declares ([[XC-213]]).

    Raises:
        TableNotFoundError: No such warehouse of ``project``.
    """
    protect = partial(warehouses.set_warehouse_protection, settings.registry_root, settings.storage_options(), warehouse_id, project=project, change=change)
    if await run_in_threadpool(protect) is None:
        raise TableNotFoundError(f"warehouse not found: {warehouse_id}")
    log.info("warehouse_protection_set", extra={"warehouse": warehouse_id, "protected": change.protected})
    await _announce_protection(control, warehouse_id, actor, protected=change.protected)


async def _announce_protection(control: ControlEmitter, warehouse_id: str, actor: str | None, *, protected: bool) -> None:
    """Announce that ``actor`` armed or cleared the warehouse's deletion protection."""
    await emit_control(
        control,
        action="warehouse_protected" if protected else "warehouse_unprotected",
        object_type="warehouse",
        object_id=f"warehouse:{warehouse_id}",
        actor=actor,
        extra={},
    )


# --------------------------------------------------------------------------- #
# Deletion (`open_hierarchy_lifecycle.md` Decision 3): bottom-up, and a container refuses while full.
# --------------------------------------------------------------------------- #


async def _namespaces_in(ns: LanceNamespace, warehouse_id: str) -> list[str]:
    """The namespaces directly under warehouse ``warehouse_id``'s own name, as sorted ids: its contents for a delete.

    A warehouse whose row is gone holds none: a half-finished earlier delete dropped it last.
    """
    try:
        _response, names = await namespaces_api._drain_namespaces(ns, [warehouse_id])  # noqa: SLF001 — one paged listing, deliberately
    except NamespaceNotFoundError:
        return []
    return [fga.canonical_object_id([warehouse_id, name]) for name in sorted(names)]


class _NamespaceSubtree(BaseModel):
    """A namespace directly in the warehouse and what the catalog lists under it, enumerated before the cascade changes anything."""

    model_config = ConfigDict(frozen=True)

    segments: list[str]
    #: The namespace's uuid, ``None`` when the backend no longer has it.
    uuid: str | None
    descendants: list[fga_deps.GovernedObject]

    @property
    def tables(self) -> list[list[str]]:
        return [item.segments for item in self.descendants if item.kind == "table"]

    @property
    def namespaces(self) -> list[list[str]]:
        """Every namespace in the subtree, deepest first and its own last, so each is empty when its own drop runs."""
        nested = sorted((item.segments for item in self.descendants if item.kind == "namespace"), key=len, reverse=True)
        return [*nested, self.segments]

    @property
    def guarded(self) -> list[fga_deps.GovernedObject]:
        """Every object in the subtree whose protection the cascade is held to: its descendants and the namespace itself."""
        own = [fga_deps.GovernedObject(kind="namespace", segments=self.segments, uuid=self.uuid)] if self.uuid is not None else []
        return [*self.descendants, *own]


async def _cascade_subtrees(ns: LanceNamespace, settings: Settings, held: list[str], *, force: bool) -> list[_NamespaceSubtree]:
    """Every namespace ``held`` directly in the warehouse with its subtree, enumerated and held to its protection before
    anything is removed.

    ALL OF THEM FIRST, so a refusal changes nothing: a protected table in the last namespace must not be found
    after the first namespace is gone. The walker and the protection pre-flight are the namespace door's, so a
    cascade through either door enumerates and refuses alike, and ``force`` turns the subtree's locks exactly as it
    turns the warehouse's.

    A namespace the backend no longer has is drift, not an error: it went between the listing and this walk (a
    concurrent drop), and its empty subtree lets the cascade still end what the catalog keeps for it. The `dir` backend lists such a namespace as empty while the root holds a ``__manifest`` and
    refuses the listing ``NamespaceNotFound`` while it holds none (measured on pylance 13.0.0), so both read as an empty
    subtree.
    """
    subtrees: list[_NamespaceSubtree] = []
    for name in held:
        segments = parse_identifier(name)
        try:
            namespace = await fga_deps.identify(ns, settings, "namespace", segments)
            listed = await run_in_threadpool(namespaces_api._collect_descendants, ns, segments)  # noqa: SLF001 — one walker, deliberately
        except NamespaceNotFoundError:
            subtrees.append(_NamespaceSubtree(segments=segments, uuid=None, descendants=[]))
            continue
        descendants = await namespaces_api._identify(ns, settings, listed)  # noqa: SLF001 — one walker, deliberately
        subtrees.append(_NamespaceSubtree(segments=segments, uuid=namespace.uuid, descendants=descendants))
    doomed = [item for subtree in subtrees for item in subtree.guarded]
    await namespaces_api._require_descendants_unprotected(settings, doomed, force=force)  # noqa: SLF001 — one protection pre-flight, deliberately
    return subtrees


async def _detach_table(ns: LanceNamespace, settings: Settings, segments: list[str], *, purge: bool, table_uuid: str) -> bool:
    """Detach one table the cascade listed from the catalog, its bytes left in place; ``False`` for one it did not detach.

    DETACH, NOT DROP: this door takes bytes only on ``?purge_bucket=true``, and that holds for the tables in its
    namespaces as much as for the bucket. A detached table's bytes stay where a later ``register_table`` can attach
    them again; a purge takes them with the bucket, and then their base record goes with them ([[LH-279]]), or it
    would sanction its bases for the next table registered at the location. The location's claim goes either way,
    as at the deregister door ([[LH-204]]).

    A purge deletes the table's data directories BEFORE the deregister ([[LH-286]]): they are on approved bases, which
    the bucket purge does not reach, and the deregister is the step a re-issued call can no longer see past, so a delete
    that fails leaves the table registered for the re-issue to detach again.

    ``table_uuid`` is the uuid the cascade listed the table by. Its name is resolved again as its turn comes and its
    claim fenced, as the deregister door fences its table's (:func:`table_claims.fence_listed`, [[LH-586]]): a rename
    landing while the row goes answers 409 rather than carrying the table out of a cascade that then ends its grants,
    and a table that left since the listing, by a drop of its own or by a rename, is neither detached nor ended here.
    The fence is lifted when the detach fails with the row in place, and goes with the claim's release when the row went.

    Raises:
        ConcurrentModificationError: The table changed, or another request is acting on it; nothing of it changed here.
    """
    canonical = fga.canonical_object_id(segments)
    registry = base_registry.BaseRegistry(control_root=settings.registry_root, storage_options=settings.storage_options())
    claims = table_claims.claim_store(settings)
    resolved = await run_in_threadpool(table_claims.resolve, ns, claims, segments)
    fenced = await run_in_threadpool(partial(table_claims.fence_listed, ns, claims, resolved, listed_uuid=table_uuid, verb="deregister"))
    if fenced is None:
        return False
    try:
        # A clone's ending is filed before anything changes (ADR 0198 §1), so a filing that fails refuses with every byte
        # and the row in place, and a tag release failing below is finished by maintenance's audit, where a re-issued
        # cascade would no longer list the clone it belongs to.
        ending = await run_in_threadpool(
            partial(
                clone_endings.file_ending,
                registry,
                claims,
                fenced.location,
                table=canonical,
                forget_record=purge,
                door="warehouse_cascade",
                filing=fenced.token,
            )
        )
        if purge:
            try:
                await run_in_threadpool(
                    partial(data_directories.delete_table_directories, registry, fenced.location, uuid=table_uuid, storage_options=settings.storage_options())
                )
            except Exception:
                # This request has not touched the row, so the clone's holds are its own still.
                if ending is not None:
                    await run_in_threadpool(clone_endings.clear_ending, claims, ending)
                raise
        await run_in_threadpool(native.call, ns, "deregister_table", DeregisterTableRequest(id=segments))
    except TableNotFoundError:
        # Only the deregister answers this: the row went by a hand the fence does not bind, and this ending's finish
        # releases what the clone held. The fence is lifted, as the deregister door lifts its own.
        await run_in_threadpool(table_claims.lift_fence, claims, fenced)
        return False
    except Exception:
        # A failed native call leaves the ending: a describe cannot tell whether the row it followed is still the
        # clone's, and the finish judges it on its own reads once the claim's lease has passed. The fence is lifted,
        # for the re-issue that finishes the cascade.
        await run_in_threadpool(table_claims.lift_fence, claims, fenced)
        raise
    # What the catalog promised a clone among the bytes ends before the location's claim does, as at the deregister
    # door: a tag that cannot be deleted leaves the location held, and no clone registered there can hold its name.
    if purge:
        await run_in_threadpool(partial(base_registry.forget_base_record, registry, fenced.location, uuid=table_uuid))
    else:
        await run_in_threadpool(partial(base_registry.release_clone_sources, registry, fenced.location, uuid=table_uuid))
    # Released under the uuid the cascade listed the table by, as well as under its fence: a purge's directory delete
    # can take longer than a fence stands, and the same name registered at the location once the fence has lapsed holds
    # the claim under a uuid of its own.
    await run_in_threadpool(partial(location_claims.release, claims, fenced.location, canonical, uuid=table_uuid))
    if ending is not None:
        await run_in_threadpool(clone_endings.clear_ending, claims, ending)
    return True


async def _require_no_outside_clones(
    ns: LanceNamespace | None, settings: Settings, *, subtrees: list[_NamespaceSubtree], trashed: list[dict[str, Any]]
) -> None:
    """Refuse a ``?purge_bucket=true`` that would destroy a table a registered clone outside the warehouse reads, before
    anything changes (ADR 0198 §1): the purge empties the bucket, so a source's bytes go whatever its clones' tags say.

    Raises:
        InvalidTableStateError: A table in the warehouse, live or trashed, is read by a clone outside it (code 19, 409).
        ServiceUnavailableError: A table's tags could not be listed.
    """
    so = settings.storage_options()
    located: dict[str, str] = {}
    if ns is not None:
        for table in [table for subtree in subtrees for table in subtree.tables]:
            with suppress(TableNotFoundError):
                described: DescribeTableResponse = await run_in_threadpool(native.call, ns, "describe_table", DescribeTableRequest(id=table))
                if described.location:
                    located[fga.canonical_object_id(table)] = described.location
    for entry in trashed:
        if location := trash.location_of(entry):
            located[f"{trash.name_of(entry)}@{trash.object_uuid(entry)}"] = location
    carried = {name: await run_in_threadpool(partial(dataplane.clone_readers, location, so, table=name)) for name, location in located.items()}
    try:
        clone_sources.destroy_order({name: clone_sources.tag_of(location) for name, location in located.items()}, carried)
    except clone_sources.ReadFromOutsideError as exc:
        log.warning("warehouse_purge_refused_clone_sources", extra={"read": {name: sorted(tags) for name, tags in exc.read.items()}})
        raise InvalidTableStateError(
            f"the purge would destroy {len(exc.read)} table(s) read by registered clones outside this warehouse; drop or deregister those "
            "clones first, or delete the warehouse without ?purge_bucket=true"
        ) from exc


async def _forget_trashed(settings: Settings, entry: dict[str, Any], *, purge: bool) -> None:
    """End what one trashed object kept for an undrop besides its grants, as the trash's own ending does
    (:func:`lakehouse.trash.end`): its records, a table's claim and base record, then its record.

    A purge deletes a trashed table's data directories before its base record ([[LH-286]]): they are on approved bases,
    which the bucket purge does not reach, and nothing names them once the record is gone. Without one the bytes stay,
    as the bucket does, where a later register can attach them again. A record that keeps no uuid is filed under none,
    so nothing of it can be ended.
    """
    if (uuid := trash.object_uuid(entry)) is None:
        return
    so = settings.storage_options()
    if purge and (location := trash.location_of(entry)):
        registry = base_registry.BaseRegistry(control_root=settings.registry_root, storage_options=so)
        await run_in_threadpool(partial(data_directories.delete_table_directories, registry, location, uuid=uuid, storage_options=so))
    await run_in_threadpool(partial(trash.end, settings.registry_root, so, entry, kind=trash.kind_of(entry), uuid=uuid))


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


def _a_refusal(exc: BaseException) -> bool:
    """Whether ``exc`` is a guard's refusal, a spec error answered 4xx, as opposed to a failure of a store or of OpenFGA."""
    return isinstance(exc, LanceNamespaceError) and status_for(int(exc.code)) < 500


@router.delete("/{warehouse_id}")
async def delete_warehouse(
    warehouse_id: str,
    record: DoorManagedWarehouseDep,
    request: Request,
    settings: SettingsDep,
    token: CurrentToken,
    client: FgaClientDep,
    announcer: AnnouncerDep,
    uuids: ObjectUuidsDep,
    cascade: bool = False,
    purge_bucket: bool = False,
    force: bool = False,
) -> DeleteWarehouseResponse:
    """Delete a warehouse: its namespaces (on ``?cascade=true``), its trash, its tuples and its registry record — and
    its BUCKET only if asked.

    Gated on ``project#can_administer`` (`open_hierarchy_lifecycle.md` Decision 3), NOT on a relation of the
    warehouse itself: destroying a tenant's storage is a tenant-level act, so it clears the tenant's own
    admin bar rather than a rung someone could hold on this one warehouse.

    Order, and every step is load-bearing:

    1. The record must exist (404), read by :func:`administered_warehouse`.
    2. **Authorize first**, in that same dependency. The gate runs before the protection and emptiness checks, so an unauthorized
       caller never learns whether the warehouse is protected or what it holds — a 409 listing another
       tenant's namespace names is a disclosure a 403 must beat to.
    3. Deletion protection (Decision 5) refuses 409 unless ``force=true``. ``force`` overrides the
       PROTECTION ONLY — step 2 has already run, identically, with or without it.
    4. Emptiness: a warehouse still holding namespaces refuses 409 **naming them**; a refusal that does not
       say what blocks it just moves the search to the user. ``?cascade=true`` removes exactly those, with
       everything in them. Before anything is removed, every namespace's subtree is enumerated and held to
       its protection (:func:`_cascade_subtrees`): a protected namespace or table refuses 409 naming it unless
       ``force=true``, and that refusal changes nothing.
    5. The warehouse's own trash is ended first (``warehouses.trash_owned_by``): each trashed object's kept grants,
       policy, claim and base record, its trash record last. A record is the warehouse's when its name begins with the
       warehouse's id, so a re-issue after a failure here selects the same records. Then the cascade removes each
       namespace's subtree against the warehouse's OWN bucket-rooted connection, one namespace fully finished before
       the next: every table detached (:func:`_detach_table`; the bytes stay unless step 7 purges them) and the
       namespaces dropped deepest first, and only then what each object held ended, its records before its grants
       (``fga_deps.end_removed_subtree``), and the warehouse's own row in its catalog last.
       ``namespaces_dropped`` names every namespace removed, nested ones included. The `dir` backend drops a
       namespace Restrict whatever ``behavior`` says (measured: ``behavior="Cascade"`` is ignored, not
       refused), so emptying it first is what lets the drop land. A failure part-way leaves every object not
       yet removed with every grant it had, and every step tolerates what an earlier attempt already removed,
       so re-issuing the call finishes it.
    6. Tuples on ``warehouse:<id>``, then the bucket's purge and claim (step 7), then the registry record last: the
       record is what this door is addressed by, so a failure before it leaves the warehouse for the re-issue.
    7. **Bytes only on ``?purge_bucket=true``.** A catalog entry is recoverable, a customer's bucket is not,
       so the two never share a default; a record-less bucket is reported by the reconciler, not lost. And a
       purge that would take bytes this warehouse does not solely own — a bucket a same-project sibling
       still claims, or reserved platform storage — refuses BEFORE the cascade
       (:func:`_require_bucket_purgeable`). A purge then releases the bucket's claim
       (``warehouses.release_bucket_claim``), so another project may register the emptied bucket; a delete keeping
       the bytes keeps the claim, so no other project adopts what the bucket still holds.

    Routing reads the warehouse's record on every request, so once the record is gone no name beneath the warehouse
    reaches its bucket.
    """
    so = settings.storage_options()
    project = record["project"]
    # Everything below discloses the warehouse's CONTENTS (the 409 names its namespaces) or its protection
    # state, so it runs strictly after the gate above.
    fga_deps.require_not_protected(record, kind="warehouse", obj_id=warehouse_id, force=force)

    # A record naming another store cannot be opened ([[LH-205]]), so nothing is listed in it and only its record goes.
    in_estate_store = names_the_estate_store(record.get("endpoint"), estate=settings.s3_endpoint)
    ns_conn = await namespace_for_root(request, settings, record["root_uri"], endpoint=record.get("endpoint") or None) if in_estate_store else None
    held = await _namespaces_in(ns_conn, warehouse_id) if ns_conn is not None else []
    if held and not cascade:
        raise NamespaceNotEmptyError(
            f"warehouse '{warehouse_id}' still holds {len(held)} namespace(s): {', '.join(held)}. "
            f"Drop them first, or pass ?cascade=true to drop exactly those with the warehouse."
        )

    bucket = record["bucket"]
    if purge_bucket:
        # The registry-wide read happens ONLY on the destructive path: the guard needs every claim on this
        # bucket, and a plain (recoverable) delete has no reason to pay for it. It runs here, before the
        # cascade, so a refusal still costs nothing — the bottom-up rule applies to the bytes too. Read whole: a record
        # that cannot be read may be a sibling's claim on this bucket ([[LH-572]]).
        records = await run_in_threadpool(warehouses.every_warehouse, settings.registry_root, so)
        _require_bucket_purgeable(settings, records, record, warehouse_id)

    # [[LH-205]] The cascade opens the warehouse's store and the purge empties a bucket through the
    # ESTATE's client, so for a record naming another store the one would sign toward that host with the
    # estate's key and the other would delete the estate's same-named bucket. Asked for, either is refused here,
    # before the first step lands, so the caller gets the refusal and not a report of a cascade that listed
    # nothing. A plain delete touches no store and stays open: it is how such a record is retired.
    if cascade or purge_bucket:
        require_estate_store(record.get("endpoint"), estate=settings.s3_endpoint, subject=f"warehouse {warehouse_id!r}")

    # The warehouse's OWN endpoint ([[LH-067]]): a cascade that opened the estate's store would find none of
    # this warehouse's namespaces and report a clean delete having removed nothing.
    subtrees = await _cascade_subtrees(ns_conn, settings, held, force=force) if ns_conn is not None else []
    # The warehouse's own trash: the records whose names begin with its id.
    trashed = await run_in_threadpool(warehouses.trash_owned_by, settings.registry_root, so, warehouse_id)
    if purge_bucket:
        await _require_no_outside_clones(ns_conn, settings, subtrees=subtrees, trashed=trashed)
    # What landed, recorded as each step lands, so a failure part-way reports it rather than what was planned.
    # What the delete announces is filed before it changes anything, so a delete that fails part-way still announces what
    # it did once the store shows it ([[LH-576]]): a deregister marker per table it means to detach, landed once the
    # table's name names it no longer, and `warehouse_deleted`, landed once the warehouse's record is gone.
    detached: dict[tuple[str, ...], Announcement] = {}
    for subtree in subtrees:
        for item in subtree.descendants:
            if item.kind == "table":
                detached[tuple(item.segments)] = Announcement(
                    evidence=NameResolves(segments=item.segments, uuid=item.uuid, held=False),
                    message=announcer.marker(
                        item.segments,
                        author=token.subject if token is not None else None,
                        operation=DEREGISTER_TABLE,
                        uuids=await uuids.table(item.segments, uuid=item.uuid),
                    ),
                )
    planned = CatalogControlEvent(
        action="warehouse_deleted",
        object_type="warehouse",
        object_id=f"warehouse:{warehouse_id}",
        actor=f"user:{token.subject}" if token else None,
        extra={
            "project": project,
            "bucket": bucket,
            "namespaces_dropped": [fga.canonical_object_id(namespace) for subtree in subtrees for namespace in subtree.namespaces],
            "bucket_purged": purge_bucket,
            "objects_purged": 0,
        },
    )
    deleted = Announcement(evidence=RecordGone(key=warehouses.record_key(warehouse_id)), message=ControlMessage(event=planned))
    async with announcer.announcing(door="delete_warehouse", target=f"warehouse:{warehouse_id}", announcements=[*detached.values(), deleted]) as filing:
        namespaces_dropped: list[str] = []
        tables_removed: list[str] = []
        trash_ended: list[str] = []
        revoked = 0
        bucket_purged = False
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
                await _forget_trashed(settings, entry, purge=purge_bucket)
                trash_ended.append(f"{kind}:{canonical}")
            if ns_conn is not None:
                for subtree in subtrees:
                    # What this cascade removes of the subtree: every namespace, and each table it detaches itself. A listed
                    # table that left before its turn ([[LH-586]]) is announced by no one here and ended by no step below.
                    removed = [item for item in subtree.descendants if item.kind == "namespace"]
                    for item in subtree.descendants:
                        if item.kind != "table":
                            continue
                        # What the announcement names the table by, read while it still stands (ADR 0173 §10).
                        if await _detach_table(ns_conn, settings, item.segments, purge=purge_bucket, table_uuid=item.uuid):
                            removed.append(item)
                            tables_removed.append(fga.canonical_object_id(item.segments))
                            await filing.send(detached[tuple(item.segments)])
                    for namespace in subtree.namespaces:
                        try:
                            await run_in_threadpool(native.call, ns_conn, "drop_namespace", DropNamespaceRequest(id=namespace))
                        except NamespaceNotFoundError:
                            # Drift, or an earlier attempt's work: the namespace went before its turn, and refusing
                            # here would leave records and grants nobody can end, so the catalog's own records still go.
                            log.warning("warehouse_cascade_namespace_absent", extra={"warehouse": warehouse_id, "namespace": namespace})
                        namespaces_dropped.append(fga.canonical_object_id(namespace))
                    async for count in fga_deps.end_removed_subtree(client, settings, root_uuid=subtree.uuid, removed=removed, token=token):
                        revoked += count
                # The warehouse's own row last, once every namespace beneath it is gone.
                with suppress(NamespaceNotFoundError):
                    await run_in_threadpool(native.call, ns_conn, "drop_namespace", DropNamespaceRequest(id=[warehouse_id]))
            # A namespace an earlier attempt removed is named by nothing but this warehouse's `child` edge, so its subtree
            # is ended from there before the warehouse's own grants go.
            ended = {fga.namespace_object(subtree.uuid) for subtree in subtrees if subtree.uuid is not None}
            for orphan in await fga_deps.orphaned_children(client, settings, f"warehouse:{warehouse_id}", ended=ended):
                async for count in fga_deps.end_removed_subtree(client, settings, root_uuid=orphan, removed=[], token=token):
                    revoked += count

            revoked += await fga_deps.revoke_removed_object(client, settings, f"warehouse:{warehouse_id}", token=token)
            # The record goes LAST: it is what the door is addressed by, so while the purge or the claim's release can still
            # fail, it keeps the warehouse for the re-issue that finishes them.
            purged = 0
            if purge_bucket:
                purged = await run_in_threadpool(warehouses.purge_bucket, bucket, so)
                bucket_purged = True
                await run_in_threadpool(partial(warehouses.release_bucket_claim, settings.registry_root, so, bucket=bucket, warehouse_id=warehouse_id))
            await run_in_threadpool(warehouses.delete_warehouse_record, settings.registry_root, so, warehouse_id)
        except Exception as exc:
            # Partial-failure honesty (Decision 3), the half a raised error would otherwise erase: once the first
            # table is detached, a later step CAN still fail (an OpenFGA outage on the revoke, a registry
            # blip) — and the caller then gets a problem body that says nothing about what is already
            # gone. The error is deliberately NOT swallowed (fail-closed; every primitive is idempotent, so the
            # recovery path is to re-issue the same call), but what DID land is recorded here or it is lost.
            if isinstance(exc, fga.RevokeInterrupted):
                # The object whose revoke the outage stopped lost these before it.
                revoked += len(exc.removed)
            if _a_refusal(exc) and not (namespaces_dropped or tables_removed or trash_ended or revoked):
                # A GUARD'S REFUSAL BEFORE ANY STEP LANDED is the answer, under its own code ([[LH-572]]): a guard refuses before
                # its step writes, so nothing is partial. A failure of the store or of OpenFGA is reported as partial even with
                # nothing counted, since the step it stopped may have landed (a clone's row detached, then its tag's delete
                # failed), and a re-issue is what finishes it.
                raise
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
                    "bucket_purged": bucket_purged,
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
                    "bucket_purged": bucket_purged,
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
        # Sent with what the delete dropped and purged, under the id the planned event was filed with.
        actual = planned.model_copy(update={"extra": {**planned.extra, "namespaces_dropped": namespaces_dropped, "objects_purged": purged}})
        await filing.send(deleted.model_copy(update={"message": ControlMessage(event=actual)}))
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


def _encryption_put_args(options: Mapping[str, str]) -> dict[str, Any]:
    """The ``put_object`` arguments that encrypt as a writer holding vended ``options`` does (``vending.encryption``)."""
    args: dict[str, Any] = {}
    if algorithm := options.get("aws_server_side_encryption"):
        args["ServerSideEncryption"] = algorithm
    if key := options.get("aws_sse_kms_key_id"):
        args["SSEKMSKeyId"] = key
    if (bucket_key := options.get("aws_sse_bucket_key_enabled")) is not None:
        args["BucketKeyEnabled"] = bucket_key == "true"
    return args


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
    sealed = f"{probe_prefix}/encrypted"
    # The PARENT, never a sibling: a credential scoped to `<probe>/` must not reach the warehouse root,
    # and the root is where a policy that was ignored would let it write.
    outside = f"{prefix.rstrip('/') + '/' if prefix else ''}_validate_scope_probe_should_fail"

    checks: list[ProbeCheck] = []
    try:
        vended = vendor.vend(table_location=f"s3://{bucket}/{probe_prefix}", tier="probe", session_name=session_name)
        # The probe signs as the client a door hands the vend to, so it holds what that door would answer ([[LH-072]]).
        options = None if vended is None else vended.as_storage_options()
    except Exception as exc:  # noqa: BLE001 — any vend failure is one reportable outcome, not a 500
        checks.append(ProbeCheck(name="issue", outcome="fail", detail=f"{type(exc).__name__}: {exc}"))
        options = None
    else:
        checks.append(
            ProbeCheck(name="issue", outcome="pass")
            if options is not None
            else ProbeCheck(name="issue", outcome="skip", detail="server_mediated: this warehouse vends no direct credentials")
        )

    if options is None:
        checks += [
            ProbeCheck(name=n, outcome="skip", detail="no credential") for n in ("write_inside", "read_inside", SCOPE_CHECK, CAS_RESERVE, CAS_CHECK, "cleanup")
        ]
        return checks

    access_key, secret_key, session_token = options.credential_in_clear()
    s3 = s3_client(store_endpoint or None, access_key=access_key, secret_key=secret_key, session_token=session_token, region=options.region)

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
        # As the credential tells its writer to encrypt, after a plain write the store took: a store whose role holds no grant
        # on the vended KMS key fails here, a verdict on the store, not the first write to the warehouse's first table
        # ([[LH-262]]). The read back asks the key's decrypt as a reader would.
        if encrypted := _encryption_put_args(options.options):

            def _round_trip() -> None:
                s3.put_object(Bucket=bucket, Key=sealed, Body=b"scope-probe", **encrypted)
                s3.get_object(Bucket=bucket, Key=sealed)

            _step(ENCRYPTED_WRITE, _round_trip)
        # `_step` returns "the step got the outcome we wanted", so a False here means the out-of-scope
        # write LANDED and left an object at the warehouse root. Cleaned below — but only in that case:
        # a correctly scoped credential is refused the delete too, and reporting that refusal as a
        # cleanup failure would raise a false alarm about the store that just passed.
        scope_refused = _step(SCOPE_CHECK, lambda: s3.put_object(Bucket=bucket, Key=outside, Body=b"scope-probe"), expect_refusal=True)
        # PUT-IF-NOT-EXISTS, the primitive Lance's commit protocol rests on
        # (`lance_docs/lance/format/table/transaction.md` § "Commit Protocol" -> "Storage Primitives"). Two conditional
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
            s3.delete_object(Bucket=bucket, Key=sealed)
            if not scope_refused:
                s3.delete_object(Bucket=bucket, Key=outside)

        _step("cleanup", _clean)
    else:
        checks += [ProbeCheck(name=n, outcome="skip", detail="nothing was written") for n in ("read_inside", SCOPE_CHECK, CAS_RESERVE, CAS_CHECK, "cleanup")]
    return checks
