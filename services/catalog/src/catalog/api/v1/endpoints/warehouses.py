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
route is where a tenant and its first admin are minted together, so
creating a warehouse never grants its creator tenant admin.

DELETE is the mirror image (Decision 3): bottom-up, gated on the PROJECT's `can_administer` because
destroying a tenant's storage is a tenant-level act, refusing 409 while the warehouse holds any namespace. `cascade`,
bucket `purge_bucket` and `force` are three SEPARATE opt-ins that never share a default — see
`delete_warehouse`.
"""

import logging
from collections.abc import AsyncGenerator, Sequence
from contextlib import aclosing, suppress
from datetime import UTC, datetime
from functools import partial
from typing import Annotated, Final, Literal

from fastapi import APIRouter, Depends, Query, Request
from fastapi.concurrency import run_in_threadpool
from lance_namespace import (
    ConcurrentModificationError,
    DropNamespaceRequest,
    InvalidInputError,
    LanceNamespace,
    LanceNamespaceError,
    NamespaceAlreadyExistsError,
    NamespaceNotFoundError,
    PermissionDeniedError,
    ServiceUnavailableError,
    TableNotFoundError,
)
from pydantic import BaseModel, ConfigDict, SkipValidation

from catalog.api import idempotency as idem
from catalog.api.announcing import Announcer, AnnouncerDep, Filing
from catalog.api.authz import decisions, door_gates, hierarchy, object_grants, objects, visible_pages, warehouse_grants
from catalog.api.authz.context import AuthzContext, AuthzContextDep
from catalog.api.declared import DECLARATION, Author, author_of, refuse_what_git_declares
from catalog.api.dependencies import (
    ControlEmitterDep,
    ObjectUuids,
    ObjectUuidsDep,
    SettingsDep,
    VendorDep,
    namespace_for_root,
)
from catalog.api.pagination import ListLimit
from catalog.api.security import ANONYMOUS_SUBJECT, Caller, CurrentToken, actor_of, record_actor_of
from catalog.core import native
from catalog.core.config import CatalogStorageSettings, CatalogStore, Settings
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
from catalog.services import namespace_subtrees, storage_probe, trash_doors, warehouse_removal, warehouses
from catalog.services.announcement_intents import Announcement, ControlMessage, RecordGone, Unregistered
from catalog.services.control_records import ProtectionChange, with_protection
from catalog.services.vend_probe import (
    CAS_CHECK,
    CAS_RESERVE,
    ENCRYPTED_WRITE,
    FINAL_MANIFEST_CHECK,
    REF_CHECK,
    SCOPE_CHECK,
    ProbeReport,
    summarize_probe,
)
from lakehouse.events.control_emit import ControlEmitter, emit_control
from lakehouse.events.control_events import CatalogControlEvent
from lakehouse.governed import fga_ids, fga_writes
from lakehouse.naming import ANONYMOUS_ACTOR, CATALOG_DELIMITER, CONTROL_ID_RE
from lakehouse.problems.ns_errors import PartiallyAppliedError, status_for
from lakehouse.records.trash import TrashRecord
from lakehouse.records.warehouse_records import WarehouseRecord
from lakehouse.storage.conditional_records import RecordExistsError, RecordMissingError
from lakehouse.storage.options import StorageOptions
from lakehouse.storage.uri import split_s3_uri


log = logging.getLogger(__name__)

#: The probe checks whose failure is a verdict on the STORE: it ignored a credential's scope, a conditional put, or
#: the write tier's Deny. Every other failing check (`issue`, `write_inside`, `read_inside`, `issue_write_tier`,
#: `staged_manifest`, `cleanup`, `store_answered`) means the probe could not reach the store or its STS, which says
#: nothing about the store.
_CONFORMANCE_CHECKS: Final = frozenset({SCOPE_CHECK, CAS_RESERVE, CAS_CHECK, ENCRYPTED_WRITE, FINAL_MANIFEST_CHECK, REF_CHECK})

management_router = APIRouter(prefix="/management/v1/warehouses", tags=["warehouse"])

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


@management_router.post("", response_model_exclude_none=True)
async def create_warehouse(
    *,
    request: Request,
    context: AuthzContextDep,
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
    ids = validated_ids(context.settings, body)
    _warehouse_id, project, _bucket = ids
    await hierarchy.require_project_exists(context.settings, project)
    await door_gates.require_can_create_warehouse(context, project=project)
    # A declaration naming the warehouse's deletion protection asks what the protection door asks ([[LH-548]]).
    if body.protected is not None:
        await _require_project_admin(context, project=project)
    record = await establish_warehouse(request, context, body, ids=ids, control=control, vendor=vendor, author=author_of(context.token))
    return WarehouseResponse.model_validate(record.stored())


#: A create's warehouse id, project and bucket, each judged a control id (:func:`validated_ids`).
type WarehouseIds = tuple[str, str, str]


def validated_ids(settings: Settings, body: CreateWarehouseRequest) -> WarehouseIds:
    """The warehouse id, project and bucket ``body`` names, each a control id.

    Raises:
        InvalidInputError: An id is malformed, the id is the estate's own warehouse, or the endpoint is not the estate's object store ([[LH-205]]): another store needs a credential the catalog
            resolves and consumes on every open, provision and vend, and no door consumes one, so admitting it would
            have the estate's own key signed toward a host the caller chose.
    """
    warehouse_id = _validate_id(body.id, what="warehouse id")
    project = _validate_id(body.project, what="project id")
    bucket = _validate_id(body.bucket or body.id, what="bucket name")
    if warehouse_id == settings.estate_warehouse:
        raise InvalidInputError(f"warehouse id {warehouse_id!r} is the estate's own warehouse; choose another")
    if not names_the_estate_store(body.endpoint, estate=settings.s3_endpoint):
        raise InvalidInputError(
            f"endpoint {body.endpoint!r} is not the estate's object store. A warehouse in another store needs a credential "
            "reference the catalog consumes on every open, provision and vend, and none is consumed yet, so the catalog "
            "would sign with the estate's own key toward that host. Omit `endpoint` to use the estate's store."
        )
    return warehouse_id, project, bucket


async def establish_warehouse(
    request: Request,
    context: AuthzContext,
    body: CreateWarehouseRequest,
    *,
    ids: WarehouseIds,
    control: ControlEmitter,
    vendor: CredentialVendor,
    author: Author | None,
) -> WarehouseRecord:
    """Make the warehouse ``body`` names, or converge the one that stands onto it; the record as written.

    What the create door does once its caller is authorized, and what the catalog does for a warehouse git declares
    ([[XC-213]]): claim and provision the bucket (idempotent), probe its store, write the registry record, give the
    warehouse its row in its own catalog, seed its grants and announce it. The probe refuses a store that ignores
    credential scope, a conditional put or the write tier's policy (400) and one it cannot reach (503), so no such
    warehouse becomes routable ([[LH-481]], [[LH-511]]). ``ids`` are ``body``'s (:func:`validated_ids`), in a project its caller found in the registry.
    ``context.token`` is the caller granted ``owner`` on it, none for a declared warehouse, whose project's admins own it
    through the project.

    Raises:
        NamespaceAlreadyExistsError: The id or the bucket belongs to another project.
        PermissionDeniedError: Git declares the warehouse and ``author`` is not the declaration.
        InvalidInputError: The bucket is reserved, or its store failed the probe.
        ServiceUnavailableError: The store or its STS could not be probed.
    """
    warehouse_id, project, bucket = ids
    existing = await _claim_the_bucket(context.settings, body, ids, author=author, owner=context.token)
    root_uri = f"s3://{bucket}"
    await run_in_threadpool(warehouses.provision_bucket, bucket, context.settings.storage_options())
    await _require_a_conforming_store(context.settings, vendor, root_uri, warehouse_id=warehouse_id, author=author)
    actor = author.actor if author is not None else None
    protection = None if body.protected is None else ProtectionChange(protected=body.protected, by=actor or ANONYMOUS_ACTOR)
    record = _record_for(body, existing, warehouse_id=warehouse_id, project=project, bucket=bucket, protection=protection)
    replaced, record = await _write_record(context.settings, body, record, existing=existing, protection=protection)
    # ITS OWN ROW IN ITS OWN CATALOG ([[LH-593]]): every name beneath a warehouse begins with its id, and the directory
    # catalog refuses a namespace whose parent has no row (measured on pylance 13.0.0). After the record, which makes
    # the root the warehouse's, and idempotent, so a re-POST converges a create that failed here.
    ns_conn = await namespace_for_root(request, context.settings, root_uri, endpoint=record.endpoint or None)
    await run_in_threadpool(warehouses.ensure_catalog_row, ns_conn, warehouse_id)
    if context.token is not None:
        await warehouse_grants.seed_warehouse(context, warehouse_id=warehouse_id, project=project)
    elif author is not None:
        await warehouse_grants.converge_warehouse_grants(context.client, context.settings, warehouse_id=warehouse_id, project=project, actor=author.subject)
    log.info("warehouse_created", extra={"warehouse": warehouse_id, "bucket": bucket, "project": project})
    await emit_control(
        control,
        action="warehouse_created",
        object_type="warehouse",
        object_id=f"warehouse:{warehouse_id}",
        actor=actor,
        extra={"bucket": bucket, "project": project},
    )
    if (replaced.is_protected if replaced is not None else False) != record.is_protected:
        await _announce_protection(control, warehouse_id, actor, protected=record.is_protected)
    return record


async def _claim_the_bucket(
    settings: Settings, body: CreateWarehouseRequest, ids: WarehouseIds, *, author: Author | None, owner: Caller | None
) -> WarehouseRecord | None:
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
    if existing is not None and existing.project != project:
        raise NamespaceAlreadyExistsError(f"warehouse {warehouse_id!r} is already registered to another project")
    # Only the declaration changes a warehouse git declares ([[XC-213]]): a door's create is refused whoever makes it,
    # a stack that authenticates nobody included. Judged on the read the guard above made, so the create reads its
    # record once.
    if author is not DECLARATION:
        refuse_what_git_declares(existing.managed_by if existing is not None else None, obj=f"warehouse:{warehouse_id}", token=owner)
    # [[LH-205]] A re-POST that names no endpoint carries the live one forward, and `provision_bucket`
    # always reaches the ESTATE's store. For a record naming another store that would create its
    # bucket in the wrong store and keep the record pointing at the foreign one, so the re-POST is
    # refused unless it re-points the warehouse itself (an explicit endpoint, judged by `validated_ids`).
    if existing is not None and body.endpoint is None:
        require_estate_store(existing.endpoint, estate=settings.s3_endpoint, subject=f"warehouse {warehouse_id!r}")

    # Reserved-bucket guard (audit 2026-07-23, the Mallory scenario's first door): the shared catalog
    # root/registry bucket and the platform's own buckets are PLATFORM storage — a warehouse claiming one
    # would make its project the bucket's "owner" (provision_bucket is idempotent on an existing bucket, so
    # the claim silently succeeds) and a later project-policy set would govern every tenant's data in it.
    if bucket in settings.reserved_bucket_set:
        raise InvalidInputError(
            f"bucket {bucket!r} is reserved platform storage (the catalog root or registry, or a platform bucket) and cannot back a warehouse"
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


async def _require_a_conforming_store(
    settings: CatalogStorageSettings, vendor: CredentialVendor, root_uri: str, *, warehouse_id: str, author: Author | None
) -> None:
    """Probe the store under ``root_uri`` before the record makes it routable ([[LH-481]], ADR 0089).

    Every Lance commit rests on the store refusing a second conditional put, every vended credential on it honouring
    the session policy, and managed versioning on it refusing a writer the final manifest and every ref ([[LH-511]]); a
    store that accepts any of them and ignores it fails silently, with a 200 on every write.
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
    probe = partial(storage_probe.run_scope_probe, root_uri, vendor=vendor, store_endpoint=settings.s3_endpoint, session_name=creator)
    report = summarize_probe(await run_in_threadpool(probe))
    failed = [c.name for c in report.checks if c.outcome == "fail"]
    if broken := [name for name in failed if name in _CONFORMANCE_CHECKS]:
        log.warning("warehouse_store_failed_probe", extra={"warehouse": warehouse_id, "bucket": bucket, "failed": broken})
        raise InvalidInputError(
            f"warehouse {warehouse_id!r} was not registered: its object store failed the storage probe ({', '.join(broken)}). "
            "A Lance warehouse needs a store that enforces vended-credential scope, refuses a second conditional write, "
            "refuses a writer's credential the final manifest and every put under _refs/, and takes the encryption its "
            "vends name (the catalog's role granted kms:Decrypt and kms:GenerateDataKey on the key)."
        )
    if failed:
        log.warning("warehouse_store_unprobed", extra={"warehouse": warehouse_id, "bucket": bucket, "failed": failed})
        raise ServiceUnavailableError(f"warehouse {warehouse_id!r} was not registered: its object store could not be probed ({', '.join(failed)}); retry")


def _record_for(
    body: CreateWarehouseRequest,
    existing: WarehouseRecord | None,
    *,
    warehouse_id: str,
    project: str,
    bucket: str,
    protection: ProtectionChange | None,
) -> WarehouseRecord:
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
        "status": str(existing.status or "active") if existing is not None else "active",
        "created_at": (existing.stored().get("created_at") if existing is not None else None) or datetime.now(UTC).isoformat(),
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
    return WarehouseRecord.model_validate(record)


async def _write_record(
    settings: CatalogStore, body: CreateWarehouseRequest, record: WarehouseRecord, *, existing: WarehouseRecord | None, protection: ProtectionChange | None
) -> tuple[WarehouseRecord | None, WarehouseRecord]:
    """Mint ``record``, or merge it into the record that stands; the record the write replaced (``None`` for a mint) and
    the record it wrote, so the protection this write changed is announced and no other.

    Raises:
        NamespaceAlreadyExistsError: The record moved to another project mid-request.
        ConcurrentModificationError: The write raced a concurrent delete; retry.
    """
    so = settings.storage_options()
    warehouse_id = record.id

    # BOTH re-create paths go through the CONDITIONAL upsert (diff2 F4), never an unconditional put of a record
    # assembled from a read taken before the guards and before `provision_bucket`, a network round trip: a deactivate
    # landing anywhere in that window would be overwritten by the status this request carried forward, lifting a
    # quarantine with no `/activate` and no audit signal. The merge happens inside the write, against the record as it
    # actually stands, so a concurrent lifecycle change survives.
    async def _converge() -> tuple[WarehouseRecord, WarehouseRecord]:
        try:
            return await run_in_threadpool(
                partial(
                    warehouses.upsert_warehouse,
                    settings.registry_root,
                    so,
                    record,
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
    return None, record


@management_router.get("", response_model_exclude_none=True)
async def list_warehouses(context: AuthzContextDep) -> list[WarehouseResponse]:
    """Every warehouse the caller can read. Governed like the metadata feeds: with FGA on, filtered to the
    warehouses the caller has ``can_get_metadata`` on (never discloses another tenant's bucket names)."""
    records = await run_in_threadpool(warehouses.list_warehouses, context.settings.registry_root, context.settings.storage_options())
    if (authz := decisions.authorization(context)) is not None:
        # The listing's own candidates, batch-checked ([[LH-223]]): never what the caller reaches across the estate,
        # which OpenFGA stops counting at its ceiling.
        allowed = await authz.batch_check("can_get_metadata", [f"warehouse:{r.id}" for r in records])
        records = [r for r in records if allowed.get(f"warehouse:{r.id}")]
    return [WarehouseResponse.model_validate(r.stored()) for r in records]


@management_router.get("/{warehouse_id}", response_model_exclude_none=True)
async def get_warehouse(*, warehouse_id: str, context: AuthzContextDep) -> WarehouseResponse:
    """One warehouse record — reader-gated on ``warehouse:<id>`` (fail-closed on an OpenFGA outage)."""
    await door_gates.require_relation(context, relation="can_get_metadata", obj=f"warehouse:{warehouse_id}")
    record = await run_in_threadpool(warehouses.get_warehouse, context.settings.registry_root, context.settings.storage_options(), warehouse_id)
    if record is None:
        raise TableNotFoundError(f"warehouse not found: {warehouse_id}")
    return WarehouseResponse.model_validate(record.stored())


@management_router.get("/{warehouse_id}/trash", response_model_exclude_none=True)
async def list_warehouse_trash(
    *,
    warehouse_id: str,
    context: AuthzContextDep,
    limit: ListLimit,
    namespace: Annotated[str | None, Query(description="List only what was dropped beneath this namespace name.")] = None,
    page_token: str | None = None,
) -> TrashListing:
    """Every recoverable drop under this warehouse its caller holds a grant on: the way back to a dropped object whose
    name is lost, and what is waiting to be purged ([[LH-556]]).

    The undrop and tasks doors are addressed by name; this is the door that answers the names. The route asks
    ``can_get_metadata`` on the warehouse, as its namespace listing does, and each trashed table and namespace is judged
    on its own uuid (``table:<uuid>``, ``namespace:<uuid>``), the object its grants are held by and the undrop and tasks
    doors ask on (``decisions.require_trashed``): the warehouse's rung opens upward from any one child, so it says nothing
    about the siblings, and a listing judges its own candidates ([[LH-223]]). A record naming no uuid holds no grant and
    is listed to no one ([[LH-534]]).

    The candidates are the trash a delete of this warehouse would end (``warehouses.trash_owned_by``), so a sibling
    warehouse's drops on a shared bucket are never listed. They sort by name and then uuid, the key the opaque page token
    resumes after (``trash_doors.listing_key``, `lance_docs/ns_catalog/spec.yaml` PageToken).
    """
    await door_gates.require_relation(context, relation="can_get_metadata", obj=f"warehouse:{warehouse_id}")
    so = context.settings.storage_options()
    record = await run_in_threadpool(warehouses.get_warehouse, context.settings.registry_root, so, warehouse_id)
    if record is None:
        raise TableNotFoundError(f"warehouse not found: {warehouse_id}")
    owned = await run_in_threadpool(warehouses.trash_owned_by, context.settings.registry_root, so, warehouse_id)
    beneath = f"{namespace}{CATALOG_DELIMITER}" if namespace else ""
    candidates = sorted(((trash_doors.listing_key(r), r) for r in owned if r.uuid and r.id.startswith(beneath)), key=lambda c: c[0])

    async def judged_on(records: Sequence[TrashRecord]) -> list[str | None]:
        return [fga_ids.object_of(r.kind, str(r.uuid)) for r in records]

    page, next_token = await visible_pages.judged_page(
        decisions.authorization(context),
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
    context: AuthzContext,
    control: ControlEmitter,
) -> WarehouseResponse:
    """Shared deactivate/activate: admin-gate on the warehouse's OWN project, flip ``status``, persist.

    Lifecycle is a platform-admin op (same rung as create): a project admin may quarantine or restore a
    warehouse they own. Fail-closed: the record is read first (needed to gate on the REAL owning project, not
    a caller-supplied one). NO EXISTENCE ORACLE (audit #4): a caller who is not the warehouse's project admin
    gets the SAME 404 as a missing warehouse — the not-found and permission-denied outcomes are made
    indistinguishable so an unauthorized user cannot probe which warehouse ids exist. Status is read LIVE by
    the resolver, so no cache invalidation is needed — the very next routed request sees the new status."""
    so = context.settings.storage_options()
    record = await run_in_threadpool(warehouses.get_warehouse, context.settings.registry_root, so, warehouse_id)
    if record is None:
        raise TableNotFoundError(f"warehouse not found: {warehouse_id}")
    try:
        await door_gates.require_can_create_warehouse(context, project=record.project)
    except PermissionDeniedError as exc:
        # Collapse denied → not-found so existence is not disclosed to a non-admin (audit #4). A legitimate
        # admin of the warehouse's own project still passes; anyone else sees exactly a missing-warehouse 404.
        raise TableNotFoundError(f"warehouse not found: {warehouse_id}") from exc
    refuse_what_git_declares(record.managed_by, obj=f"warehouse:{warehouse_id}", token=context.token)
    updated = await change_warehouse_status(context.settings, control, warehouse_id, status=status, actor=actor_of(context.token))
    return WarehouseResponse.model_validate(updated.stored())


async def change_warehouse_status(
    settings: CatalogStore, control: ControlEmitter, warehouse_id: str, *, status: WarehouseStatus, actor: str | None
) -> WarehouseRecord:
    """Deactivate or activate warehouse ``warehouse_id`` and announce it; the record as written.

    What both status doors do once their caller is authorized, and what the catalog does for a warehouse whose status
    drifted from what git declares ([[XC-213]]).

    Raises:
        TableNotFoundError: The warehouse's record is gone.
    """
    updated = await run_in_threadpool(warehouses.set_warehouse_status, settings.registry_root, settings.storage_options(), warehouse_id, status=status)
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


@management_router.post("/{warehouse_id}/deactivate", response_model_exclude_none=True)
async def deactivate_warehouse(
    *,
    warehouse_id: str,
    context: AuthzContextDep,
    control: ControlEmitterDep,
) -> WarehouseResponse:
    """Quarantine a warehouse (#3-A lifecycle): the resolver then refuses EVERY op on a name beneath it
    (403), so no new tables are created and existing ones are suspended — the tenant-offboarding first step.
    Admin-gated on the warehouse's project. Idempotent (re-deactivating is a no-op)."""
    return await _set_warehouse_status(warehouse_id, "deactivated", context=context, control=control)


@management_router.post("/{warehouse_id}/activate", response_model_exclude_none=True)
async def activate_warehouse(
    *,
    warehouse_id: str,
    context: AuthzContextDep,
    control: ControlEmitterDep,
) -> WarehouseResponse:
    """Reactivate a quarantined warehouse (#3-A lifecycle) — restores routing to the names beneath it.
    Admin-gated on the warehouse's project. Idempotent."""
    return await _set_warehouse_status(warehouse_id, "active", context=context, control=control)


async def _require_project_admin(context: AuthzContext, *, project: str) -> None:
    """Refuse a caller without ``can_administer`` on ``project``: the authority over its warehouses' destruction, and so
    over the lock on it."""
    await door_gates.require_relation(context, relation="can_administer", obj=f"project:{project}")


async def administered_warehouse(warehouse_id: str, context: AuthzContextDep) -> WarehouseRecord:
    """The record of ``warehouse_id``, for a caller who administers its project (:func:`_require_project_admin`).

    The record is read first, since the gate is on the warehouse's own project and never on one the caller names. NO
    EXISTENCE ORACLE (audit #4): a caller who is not the project's admin is answered exactly the missing warehouse's 404,
    so nobody can probe which warehouse ids exist; an outage or a missing caller still answers 503 or 401.

    Args:
        warehouse_id: The warehouse the route's path names.
        context: The request's authorization context: the OpenFGA client, the settings and the caller.

    Returns:
        The warehouse's registry record.

    Raises:
        InvalidInputError: The id is not a warehouse id.
        TableNotFoundError: The warehouse is absent, or its caller does not administer its project.
    """
    _validate_id(warehouse_id, what="warehouse id")
    record = await run_in_threadpool(warehouses.get_warehouse, context.settings.registry_root, context.settings.storage_options(), warehouse_id)
    if record is None:
        raise TableNotFoundError(f"warehouse not found: {warehouse_id}")
    try:
        await _require_project_admin(context, project=record.project)
    except PermissionDeniedError as exc:
        raise TableNotFoundError(f"warehouse not found: {warehouse_id}") from exc
    return record


AdministeredWarehouseDep = Annotated[WarehouseRecord, Depends(administered_warehouse)]


def warehouse_its_doors_manage(warehouse_id: str, record: AdministeredWarehouseDep, token: CurrentToken) -> WarehouseRecord:
    """The record of a warehouse the caller administers and git does not declare ([[XC-213]]).

    The gate of a door that changes what a declaration owns, asked after the caller's own authorization so only a
    caller who could otherwise make the change learns that git owns it.

    Raises:
        PermissionDeniedError: Git declares the warehouse.
    """
    refuse_what_git_declares(record.managed_by, obj=f"warehouse:{warehouse_id}", token=token)
    return record


DoorManagedWarehouseDep = Annotated[WarehouseRecord, Depends(warehouse_its_doors_manage)]


@management_router.post("/{warehouse_id}/protection", response_model_exclude_none=True)
async def set_warehouse_protection(
    *,
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
    actor = actor_of(token)
    change = ProtectionChange(protected=body.protected, by=record_actor_of(token))
    await protect_warehouse(settings, control, warehouse_id, project=record.project, change=change, actor=actor)
    return ProtectionResponse(id=warehouse_id, protected=body.protected)


async def protect_warehouse(
    settings: CatalogStore, control: ControlEmitter, warehouse_id: str, *, project: str, change: ProtectionChange, actor: str | None
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
# Deletion: bottom-up, and a container refuses while full.
# --------------------------------------------------------------------------- #


async def _cascade_subtrees(ns: LanceNamespace, settings: CatalogStore, held: list[str], *, force: bool) -> list[warehouse_removal.NamespaceSubtree]:
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
    subtrees: list[warehouse_removal.NamespaceSubtree] = []
    for name in held:
        segments = parse_identifier(name)
        try:
            namespace = await objects.standing(ns, settings, "namespace", segments=segments)
            listed = await run_in_threadpool(namespace_subtrees.collect_descendants, ns, segments)
        except NamespaceNotFoundError:
            subtrees.append(warehouse_removal.NamespaceSubtree(segments=segments, uuid=None, descendants=[]))
            continue
        descendants = await objects.identify_descendants(ns, settings, listed)
        subtrees.append(warehouse_removal.NamespaceSubtree(segments=segments, uuid=namespace.uuid, descendants=descendants))
    doomed = [item for subtree in subtrees for item in subtree.guarded]
    await hierarchy.require_descendants_unprotected(settings, doomed, force=force)
    return subtrees


def _a_refusal(exc: BaseException) -> bool:
    """Whether ``exc`` is a guard's refusal, a spec error answered 4xx, as opposed to a failure of a store or of OpenFGA."""
    return isinstance(exc, LanceNamespaceError) and status_for(int(exc.code)) < 500


@management_router.delete("/{warehouse_id}")
async def delete_warehouse(
    *,
    warehouse_id: str,
    keyed: idem.KeyedDoorDep,
    request: Request,
    context: AuthzContextDep,
    announcer: AnnouncerDep,
    uuids: ObjectUuidsDep,
    cascade: bool = False,
    purge_bucket: bool = False,
    force: bool = False,
) -> DeleteWarehouseResponse:
    """Delete a warehouse: its namespaces (on ``?cascade=true``), its trash, its tuples and its registry record — and
    its BUCKET only if asked.

    Gated on ``project#can_administer``, NOT on a relation of the
    warehouse itself: destroying a tenant's storage is a tenant-level act, so it clears the tenant's own
    admin bar rather than a rung someone could hold on this one warehouse.

    Order, and every step is load-bearing:

    1. The record must exist (404), read by :func:`administered_warehouse`.
    2. **Authorize first**, in that same read. The gate runs before the protection and emptiness checks, so an unauthorized
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
       the next: every table detached (:func:`warehouse_removal.detach_table`; the bytes stay unless step 7 purges them) and the
       namespaces dropped deepest first, and only then what each object held ended, its records before its grants
       (``object_grants.end_removed_subtree``), and the warehouse's own row in its catalog last.
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
       (:func:`warehouse_removal.require_bucket_purgeable`). A purge then releases the bucket's claim
       (``warehouses.release_bucket_claim``), so another project may register the emptied bucket; a delete keeping
       the bytes keeps the claim, so no other project adopts what the bucket still holds.

    Routing reads the warehouse's record on every request, so once the record is gone no name beneath the warehouse
    reaches its bucket.
    """
    # Asked before the record is read: a replay under the caller's key answers the first delete and reads nothing, so a
    # warehouse registered under the id since is never the one a resent delete removes.
    converge = await keyed.begin(target=f"warehouse:{warehouse_id}", takeover="unknown")
    if converge.replay is not None:
        return DeleteWarehouseResponse.model_validate(converge.replay.body)
    record = warehouse_its_doors_manage(warehouse_id, await administered_warehouse(warehouse_id, context), context.token)
    so = context.settings.storage_options()
    asked = _DeleteAsked(warehouse_id=warehouse_id, cascade=cascade, purge_bucket=purge_bucket, force=force)
    plan = await _plan_the_delete(record, asked, request=request, settings=context.settings, so=so)
    ns_conn = plan.ns_conn
    delete = _WarehouseDelete(
        warehouse_id=warehouse_id, project=record.project, bucket=record.bucket, purge_bucket=purge_bucket, so=so, subtrees=plan.subtrees, trashed=plan.trashed
    )
    announced = await _what_the_delete_announces(delete, announcer=announcer, token=context.token, uuids=uuids)
    async with announcer.announcing(
        door="delete_warehouse", target=f"warehouse:{warehouse_id}", announcements=[*announced.detached.values(), announced.deleted]
    ) as filing:
        steps = _cascade_the_delete(delete, ns_conn, context=context)
        landed = await _run_the_cascade(steps, delete, announced=announced, filing=filing)

        log.info(
            "warehouse_deleted",
            extra={
                "warehouse": warehouse_id,
                "project": delete.project,
                "bucket": delete.bucket,
                "namespaces_dropped": landed.namespaces_dropped,
                "tables_removed": landed.tables_removed,
                "trash_ended": landed.trash_ended,
                "tuples_revoked": landed.revoked,
                "bucket_purged": purge_bucket,
                "objects_purged": landed.purged,
                "forced": force,
            },
        )
        # Sent with what the delete dropped and purged, under the id the planned event was filed with.
        planned = announced.planned
        actual = planned.model_copy(update={"extra": {**planned.extra, "namespaces_dropped": landed.namespaces_dropped, "objects_purged": landed.purged}})
        await filing.send(announced.deleted.model_copy(update={"message": ControlMessage(event=actual)}))
    answer = DeleteWarehouseResponse(
        id=warehouse_id,
        bucket=delete.bucket,
        namespaces_dropped=landed.namespaces_dropped,
        tuples_revoked=landed.revoked,
        bucket_purged=purge_bucket,
        objects_purged=landed.purged,
    )
    await converge.remember(200, answer)
    return answer


async def _run_the_cascade(steps: AsyncGenerator[_Step], delete: _WarehouseDelete, *, announced: _DeleteAnnouncements, filing: Filing) -> _Landed:
    """Run a warehouse delete's ``steps``, announcing each detached table: what landed, or the partial refusal."""
    landed = _Landed()
    try:
        # Each step is counted as it lands, so a failure part-way reports what landed before it.
        async with aclosing(steps):
            async for step in steps:
                landed = landed.added(step.landed)
                await _announce_a_detached_table(step, announced=announced, filing=filing)
    except Exception as exc:
        # Partial-failure honesty (Decision 3), the half a raised error would otherwise erase: once the first
        # table is detached, a later step CAN still fail (an OpenFGA outage on the revoke, a registry
        # blip) — and the caller then gets a problem body that says nothing about what is already
        # gone. The error is deliberately NOT swallowed (fail-closed; every primitive is idempotent, so the
        # recovery path is to re-issue the same call), but what DID land is recorded here or it is lost.
        if isinstance(exc, fga_writes.RevokeInterruptedError):
            # The object whose revoke the outage stopped lost these before it.
            landed = landed.added(_Landed(revoked=len(exc.removed)))
        if _a_refusal(exc) and not (landed.namespaces_dropped or landed.tables_removed or landed.trash_ended or landed.revoked):
            # A GUARD'S REFUSAL BEFORE ANY STEP LANDED is the answer, under its own code ([[LH-572]]): a guard refuses before
            # its step writes, so nothing is partial. A failure of the store or of OpenFGA is reported as partial even with
            # nothing counted, since the step it stopped may have landed (a clone's row detached, then its tag's delete
            # failed), and a re-issue is what finishes it.
            raise
        raise _partially_deleted(landed, delete) from exc
    return landed


async def _announce_a_detached_table(step: _Step, *, announced: _DeleteAnnouncements, filing: Filing) -> None:
    """Send the deregister marker of the table ``step`` detached, if it detached one."""
    if step.detached is not None:
        await filing.send(announced.detached[step.detached])


class _DeleteAsked(BaseModel):
    """What a warehouse delete request asks for."""

    model_config = ConfigDict(frozen=True)

    warehouse_id: str
    cascade: bool
    purge_bucket: bool
    force: bool


class _WarehouseDelete(BaseModel):
    """A warehouse delete that passed every refusal, and what it removes."""

    model_config = ConfigDict(frozen=True)

    warehouse_id: str
    project: str
    bucket: str
    purge_bucket: bool
    so: StorageOptions
    subtrees: list[warehouse_removal.NamespaceSubtree]
    trashed: list[TrashRecord]


class _DeleteAnnouncements(BaseModel):
    """What a warehouse delete files before it changes anything."""

    model_config = ConfigDict(frozen=True)

    #: A deregister marker per table the delete means to detach, by the table's segments.
    detached: dict[tuple[str, ...], Announcement]
    planned: CatalogControlEvent
    deleted: Announcement


class _Landed(BaseModel):
    """What a warehouse delete has landed, so a failure part-way reports it rather than what was planned."""

    model_config = ConfigDict(frozen=True)

    namespaces_dropped: list[str] = []
    tables_removed: list[str] = []
    trash_ended: list[str] = []
    revoked: int = 0
    bucket_purged: bool = False
    purged: int = 0

    def added(self, step: _Landed) -> _Landed:
        """What landed with ``step`` landed too."""
        return _Landed(
            namespaces_dropped=[*self.namespaces_dropped, *step.namespaces_dropped],
            tables_removed=[*self.tables_removed, *step.tables_removed],
            trash_ended=[*self.trash_ended, *step.trash_ended],
            revoked=self.revoked + step.revoked,
            bucket_purged=self.bucket_purged or step.bucket_purged,
            purged=self.purged + step.purged,
        )


class _Step(BaseModel):
    """One step a warehouse delete landed, and the segments of the table it detached, which its caller announces."""

    model_config = ConfigDict(frozen=True)

    landed: _Landed
    detached: tuple[str, ...] | None = None


class _DeletePlan(BaseModel):
    """What a delete that passed every refusal removes: through the warehouse's store, its subtrees and its trash."""

    model_config = ConfigDict(frozen=True, arbitrary_types_allowed=True)

    # The connection is an opaque collaborator, not a value pydantic can judge.
    ns_conn: SkipValidation[LanceNamespace | None]
    subtrees: list[warehouse_removal.NamespaceSubtree]
    trashed: list[TrashRecord]


async def _plan_the_delete(record: WarehouseRecord, asked: _DeleteAsked, *, request: Request, settings: Settings, so: StorageOptions) -> _DeletePlan:
    """Every refusal a delete makes before it changes anything; the store's connection, the subtrees and the trash it removes."""
    warehouse_id = asked.warehouse_id
    # Everything below discloses the warehouse's CONTENTS (the 409 names its namespaces) or its protection
    # state, so it runs only once the caller has read the record through `administered_warehouse`, which authorizes it.
    hierarchy.require_not_protected(record.is_protected, kind="warehouse", obj_id=warehouse_id, force=asked.force)

    # A record naming another store cannot be opened ([[LH-205]]), so nothing is listed in it and only its record goes.
    in_estate_store = names_the_estate_store(record.endpoint, estate=settings.s3_endpoint)
    ns_conn = await namespace_for_root(request, settings, record.root_uri, endpoint=record.endpoint or None) if in_estate_store else None
    held = await warehouse_removal.namespaces_in(ns_conn, warehouse_id) if ns_conn is not None else []
    if held and not asked.cascade:
        raise hierarchy.ContainerHoldsError(
            f"warehouse '{warehouse_id}' still holds {len(held)} namespace(s): {', '.join(held)}. "
            f"Drop them first, or pass ?cascade=true to drop exactly those with the warehouse.",
            holds=list(held),
        )

    if asked.purge_bucket:
        # The registry-wide read happens ONLY on the destructive path: the guard needs every claim on this
        # bucket, and a plain (recoverable) delete has no reason to pay for it. It runs here, before the
        # cascade, so a refusal still costs nothing — the bottom-up rule applies to the bytes too. Read whole: a record
        # that cannot be read may be a sibling's claim on this bucket ([[LH-572]]).
        records = await run_in_threadpool(warehouses.every_warehouse, settings.registry_root, so)
        warehouse_removal.require_bucket_purgeable(settings, records, record, warehouse_id=warehouse_id)

    # [[LH-205]] The cascade opens the warehouse's store and the purge empties a bucket through the
    # ESTATE's client, so for a record naming another store the one would sign toward that host with the
    # estate's key and the other would delete the estate's same-named bucket. Asked for, either is refused here,
    # before the first step lands, so the caller gets the refusal and not a report of a cascade that listed
    # nothing. A plain delete touches no store and stays open: it is how such a record is retired.
    if asked.cascade or asked.purge_bucket:
        require_estate_store(record.endpoint, estate=settings.s3_endpoint, subject=f"warehouse {warehouse_id!r}")

    # The warehouse's OWN endpoint ([[LH-067]]): a cascade that opened the estate's store would find none of
    # this warehouse's namespaces and report a clean delete having removed nothing.
    subtrees = await _cascade_subtrees(ns_conn, settings, held, force=asked.force) if ns_conn is not None else []
    # The warehouse's own trash: the records whose names begin with its id.
    trashed = await run_in_threadpool(warehouses.trash_owned_by, settings.registry_root, so, warehouse_id)
    if asked.purge_bucket:
        await warehouse_removal.require_no_outside_clones(ns_conn, settings, subtrees=subtrees, trashed=trashed)
    return _DeletePlan(ns_conn=ns_conn, subtrees=subtrees, trashed=trashed)


async def _what_the_delete_announces(delete: _WarehouseDelete, *, announcer: Announcer, token: Caller | None, uuids: ObjectUuids) -> _DeleteAnnouncements:
    """What a warehouse delete files before it changes anything, read while what it names still stands."""
    # A delete that fails part-way still announces what it did once the store shows it ([[LH-576]]): a marker is landed
    # once that table is registered under no name and kept by no trash record (`Unregistered`), and `warehouse_deleted`
    # once the warehouse's record is gone. Its limit: the store cannot tell which actor retired a table, so another's
    # deregister or destroying drop after this door died satisfies it too, and this door's event goes out as well, under
    # its own ids.
    detached: dict[tuple[str, ...], Announcement] = {}
    for subtree in delete.subtrees:
        for item in subtree.descendants:
            if item.kind == "table":
                detached[tuple(item.segments)] = Announcement(
                    evidence=Unregistered(segments=item.segments, uuid=item.uuid, location=item.location, trashed=False),
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
        object_id=f"warehouse:{delete.warehouse_id}",
        actor=actor_of(token),
        extra={
            "project": delete.project,
            "bucket": delete.bucket,
            "namespaces_dropped": [fga_ids.canonical_object_id(namespace) for subtree in delete.subtrees for namespace in subtree.namespaces],
            "bucket_purged": delete.purge_bucket,
            "objects_purged": 0,
        },
    )
    deleted = Announcement(evidence=RecordGone(key=warehouses.record_key(delete.warehouse_id)), message=ControlMessage(event=planned))
    return _DeleteAnnouncements(detached=detached, planned=planned, deleted=deleted)


async def _cascade_the_delete(delete: _WarehouseDelete, ns_conn: LanceNamespace | None, *, context: AuthzContext) -> AsyncGenerator[_Step]:
    """Remove what the warehouse holds in the order :func:`delete_warehouse` states, its record last, yielding each step as it lands."""
    for entry in delete.trashed:
        async with aclosing(_end_trashed(entry, delete, context=context)) as ending:
            async for step in ending:
                yield step
    if ns_conn is not None:
        async with aclosing(_remove_every_subtree(delete, ns_conn, context=context)) as removing:
            async for step in removing:
                yield step
        # The warehouse's own row last, once every namespace beneath it is gone.
        with suppress(NamespaceNotFoundError):
            await run_in_threadpool(native.call, ns_conn, "drop_namespace", DropNamespaceRequest(id=[delete.warehouse_id]))
    # A namespace an earlier attempt removed is named by nothing but this warehouse's `child` edge, so its subtree
    # is ended from there before the warehouse's own grants go.
    ended = {fga_ids.namespace_object(subtree.uuid) for subtree in delete.subtrees if subtree.uuid is not None}
    for orphan in await object_grants.orphaned_children(context.client, context.settings, f"warehouse:{delete.warehouse_id}", ended=ended):
        async for count in object_grants.end_removed_subtree(context, root_uuid=orphan, removed=[]):
            yield _Step(landed=_Landed(revoked=count))

    yield _Step(landed=_Landed(revoked=await object_grants.revoke_removed_object(context, f"warehouse:{delete.warehouse_id}")))
    # The record goes LAST: it is what the door is addressed by, so while the purge or the claim's release can still
    # fail, it keeps the warehouse for the re-issue that finishes them.
    if delete.purge_bucket:
        purged = await run_in_threadpool(warehouses.purge_bucket, delete.bucket, delete.so)
        yield _Step(landed=_Landed(purged=purged, bucket_purged=True))
        await run_in_threadpool(
            partial(warehouses.release_bucket_claim, context.settings.registry_root, delete.so, bucket=delete.bucket, warehouse_id=delete.warehouse_id)
        )
    await run_in_threadpool(warehouses.delete_warehouse_record, context.settings.registry_root, delete.so, delete.warehouse_id)


async def _remove_every_subtree(delete: _WarehouseDelete, ns_conn: LanceNamespace, *, context: AuthzContext) -> AsyncGenerator[_Step]:
    """Remove each namespace of the warehouse with everything in it, one fully before the next, yielding each step."""
    for subtree in delete.subtrees:
        async with aclosing(_remove_subtree(subtree, delete, ns_conn, context=context)) as removing:
            async for step in removing:
                yield step


async def _end_trashed(entry: TrashRecord, delete: _WarehouseDelete, *, context: AuthzContext) -> AsyncGenerator[_Step]:
    """End one trashed object of the warehouse, its kept grants first, yielding each step as it lands."""
    kind, canonical = str(entry.kind or "table"), entry.id
    # A trashed object's grants are held by the uuid its record keeps ([[LH-524]]). Counted as the revoke lands,
    # so a failure in the records after it still reports these tuples.
    trashed_uuid = entry.uuid
    if trashed_uuid is not None:
        obj = fga_ids.object_of(kind, trashed_uuid)
        yield _Step(landed=_Landed(revoked=await object_grants.revoke_removed_object(context, obj)))
    else:
        log.warning("warehouse_cascade_trash_uuid_unread", extra={"warehouse": delete.warehouse_id, "kind": kind, "id": canonical})
    await warehouse_removal.forget_trashed(context.settings, entry, purge=delete.purge_bucket)
    yield _Step(landed=_Landed(trash_ended=[f"{kind}:{canonical}"]))


async def _remove_subtree(
    subtree: warehouse_removal.NamespaceSubtree, delete: _WarehouseDelete, ns_conn: LanceNamespace, *, context: AuthzContext
) -> AsyncGenerator[_Step]:
    """Remove one namespace of the warehouse with everything in it, yielding each step as it lands."""
    # What this cascade removes of the subtree: every namespace, and each table it detaches itself. A listed
    # table that left before its turn ([[LH-586]]) is announced by no one here and ended by no step below.
    removed = [item for item in subtree.descendants if item.kind == "namespace"]
    for item in subtree.descendants:
        if item.kind != "table":
            continue
        # What the announcement names the table by, read while it still stands (ADR 0173 §10).
        if await warehouse_removal.detach_table(ns_conn, context.settings, item.segments, purge=delete.purge_bucket, table_uuid=item.uuid):
            removed.append(item)
            yield _Step(landed=_Landed(tables_removed=[fga_ids.canonical_object_id(item.segments)]), detached=tuple(item.segments))
    # The namespaces deepest first, then what each object held ended.
    for namespace in subtree.namespaces:
        try:
            await run_in_threadpool(native.call, ns_conn, "drop_namespace", DropNamespaceRequest(id=namespace))
        except NamespaceNotFoundError:
            # Drift, or an earlier attempt's work: the namespace went before its turn, and refusing
            # here would leave records and grants nobody can end, so the catalog's own records still go.
            log.warning("warehouse_cascade_namespace_absent", extra={"warehouse": delete.warehouse_id, "namespace": namespace})
        yield _Step(landed=_Landed(namespaces_dropped=[fga_ids.canonical_object_id(namespace)]))
    async for count in object_grants.end_removed_subtree(context, root_uuid=subtree.uuid, removed=removed):
        yield _Step(landed=_Landed(revoked=count))


def _partially_deleted(landed: _Landed, delete: _WarehouseDelete) -> PartiallyAppliedError:
    """The refusal a delete that failed part-way answers, naming what landed before it, logged with the failure."""
    log.error(
        "warehouse_delete_partial",
        extra={
            "warehouse": delete.warehouse_id,
            "project": delete.project,
            "bucket": delete.bucket,
            "namespaces_dropped": landed.namespaces_dropped,
            "tables_removed": landed.tables_removed,
            "trash_ended": landed.trash_ended,
            "tuples_revoked": landed.revoked,
            "bucket_purged": landed.bucket_purged,
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
    return PartiallyAppliedError(
        f"warehouse {delete.warehouse_id!r} was PARTIALLY deleted: {len(landed.namespaces_dropped)} namespace(s) dropped, "
        f"{len(landed.tables_removed)} table(s) removed, {len(landed.trash_ended)} trashed object(s) ended and {landed.revoked} tuple(s) "
        "revoked before the failure. Every step is "
        "idempotent — re-issue this call to finish it.",
        extensions={
            "warehouse": delete.warehouse_id,
            "namespaces_dropped": landed.namespaces_dropped,
            "tables_removed": landed.tables_removed,
            "trash_ended": landed.trash_ended,
            "tuples_revoked": landed.revoked,
            "bucket_purged": landed.bucket_purged,
            "partial": True,
        },
    )


@management_router.post("/{warehouse_id}/validate", response_model_exclude_none=True)
async def validate_warehouse_credentials(
    *,
    warehouse_id: str,
    context: AuthzContextDep,
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
    which MUST be refused. Under a second credential, vended at the WRITE tier for the same prefix, it stages a
    manifest and attempts a final manifest and a put under `_refs/`, which MUST both be refused ([[LH-511]]). It removes
    what it made. The refusals are the finding.

    Admin-gated on the warehouse's own project and collapsing denied → 404, the same
    no-existence-oracle rule the lifecycle doors follow (audit #4): this door writes to a tenant's
    bucket, so its gate is the create/lifecycle rung and not a read one.

    Reports every check even after one fails (see :func:`summarize_probe`) — an over-permissive store
    and an unreachable one need different answers, and a first-failure exit cannot tell them apart.
    """
    so = context.settings.storage_options()
    record = await run_in_threadpool(warehouses.get_warehouse, context.settings.registry_root, so, warehouse_id)
    if record is None:
        raise TableNotFoundError(f"warehouse not found: {warehouse_id}")
    try:
        await door_gates.require_can_create_warehouse(context, project=record.project)
    except PermissionDeniedError as exc:
        raise TableNotFoundError(f"warehouse not found: {warehouse_id}") from exc
    # [[LH-205]] The probe vends through the estate's STS and writes through the estate's client, so for
    # a record naming another store it would vend and write against the wrong store entirely.
    require_estate_store(record.endpoint, estate=context.settings.s3_endpoint, subject=f"warehouse {warehouse_id!r}")

    subject = context.token.subject if context.token is not None else ANONYMOUS_SUBJECT
    probe = partial(
        storage_probe.run_scope_probe, record.root_uri, vendor=vendor, store_endpoint=context.settings.s3_endpoint, session_name=role_session_name(subject)
    )
    checks = await run_in_threadpool(probe)
    return summarize_probe(checks)
