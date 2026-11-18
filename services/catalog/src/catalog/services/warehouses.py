"""The warehouse registry and its bucket provisioning: the admin control plane.

A warehouse is one S3 bucket owned by one project (``lakehouse/governed/auth/model.fga``), holding its own directory
catalog, and its id is the first segment of every name beneath it ([[LH-593]], ADR 0206): a table created as
``acme$bronze$t`` lands in warehouse ``acme``'s bucket and is absent from every other.

The registry is a set of JSON objects under ``<control_root>/_warehouses/``, one ``<id>.json`` per warehouse; there is
no database. Bucket creation uses boto3 ``create_bucket``, idempotent: an existing bucket the catalog owns is a no-op.
All IO here is blocking; callers threadpool it.
"""

from __future__ import annotations

import json
import logging
from contextlib import suppress
from datetime import UTC, datetime, timedelta
from typing import Any, Final

import pyarrow.fs as pafs
from lance_namespace import (
    ConcurrentModificationError,
    CreateNamespaceRequest,
    DescribeNamespaceRequest,
    InternalError,
    LanceNamespace,
    NamespaceAlreadyExistsError,
    NamespaceNotFoundError,
)
from pydantic import BaseModel, ConfigDict

from catalog.core.identifiers import warehouse_of, with_minted_uuid
from catalog.services import native
from catalog.services.control_records import (
    PROJECT_UUID,
    STATE,
    UUID,
    BucketClaimRecord,
    DeleteMode,
    ProtectionChange,
    WarehouseRecord,
    change_creation,
    end_record,
    read_json,
    replace_judged,
    validated,
    validated_or_refuse,
    with_manager,
    with_protection,
    write_json,
)
from lakehouse import control_prefixes, records, trash
from lakehouse.governed import tenancy
from lakehouse.naming import CATALOG_DELIMITER, CONTROL_ID_RE
from lakehouse.objectfs import fs_and_base
from lakehouse.outage import reads_as_store_outage, store_refusal
from lakehouse.record_store import get_record, put_record
from lakehouse.records import RecordChangedError, RecordExistsError, RecordMissingError, Stored, create_json, exchange_json, replace_json
from lakehouse.storage_options import DEFAULT_REGION, StorageOptions, object_client


log = logging.getLogger(__name__)

_REGISTRY_PREFIX = control_prefixes.WAREHOUSES
#: Where a bucket's owning project is recorded, write-once: an immutable claim the store arbitrates
_BUCKET_CLAIMS_PREFIX = f"{_REGISTRY_PREFIX}/bucket-claims"


#: The record fields holding a warehouse's delete profile, as strings like every record field.
DELETE_MODE = "delete_mode"
TRASH_GRACE_DAYS = "trash_grace_days"
#: What the warehouse probe's seal check last measured of the warehouse's store (ADR 0242): ``enforced``, ``unenforced``
#: or ``untaggable``; absent where no check ran (a deployment vending no direct credential).
SEALED = "sealed"
#: When that check measured it, an ISO-8601 time on the catalog's clock: the age ``WarehouseSealUnmeasured`` reads, kept on
#: the record so no restart of a replica resets it (ADR 0242 § Found by review, third round, B2).
SEALED_AT = "sealed_at"
#: When a bucket's claim was released, and why: a released claim is no claim, and a later claim takes its key
#: (:func:`claim_bucket`).
RELEASED_AT: Final = "released_at"
RELEASED_REASON: Final = "released_reason"
#: When a bucket's claim was taken, which the converge tick judges a dead creation's claim no record names by: a create
#: still running between its claim and its record is not ended (:func:`claim_bucket`).
CLAIMED_AT: Final = "claimed_at"
#: The purge that holds a bucket's claim, and since when (ADR 0241 §10 Q1): stamped on the claim by
#: :func:`take_for_purge`, so no claim lands on the bucket until the purge ends.
PURGING: Final = "purging"
PURGING_AT: Final = "purging_at"


def sealed_fields(sealed: str) -> dict[str, str]:
    """The record fields a conclusive seal check writes: what it measured, and when."""
    return {SEALED: sealed, SEALED_AT: datetime.now(UTC).isoformat()}


def sealed_at(record: dict[str, Any]) -> datetime | None:
    """When the seal ``record`` holds was measured; ``None`` where it holds no time it can be read as."""
    at = record.get(SEALED_AT)
    if not isinstance(at, str):
        return None
    try:
        return datetime.fromisoformat(at)
    except ValueError:
        return None


def grace_days(record: dict[str, str] | None, *, estate: float) -> float:
    """The days a drop under the warehouse ``record`` stays recoverable, 0 for a destroying drop.

    A warehouse that names no profile, and the estate's own warehouse, which has no record, take the estate's grace period.
    """
    mode = (record or {}).get(DELETE_MODE)
    if mode is None or mode == DeleteMode.ESTATE:
        return estate
    if mode == DeleteMode.HARD:
        return 0.0
    return float((record or {})[TRASH_GRACE_DAYS])


def grace_days_under(control_root: str, storage_options: StorageOptions, segments: list[str], *, estate: float) -> float:
    """The days a drop of the object ``segments`` names stays recoverable: its warehouse's profile, read now.

    The warehouse is the name's first segment ([[LH-593]]); the estate's own warehouse, which has no record, takes
    ``estate``. Read at the drop and stamped on the trash record, so a profile changed later shortens no window
    already open.
    """
    if not segments or not CONTROL_ID_RE.fullmatch(segments[0]):
        return estate
    return grace_days(get_warehouse(control_root, storage_options, segments[0]), estate=estate)


def provision_bucket(bucket: str, storage_options: StorageOptions) -> None:
    """Create the physical S3 bucket, idempotently: one the catalog already owns is a no-op.

    Uses boto3 against the same endpoint/credentials the catalog already holds. An already-owned/existing
    bucket is a no-op (a re-provision on a warehouse-create retry must not fail). Blocking IO; threadpool it.
    """
    from botocore.exceptions import ClientError

    region = storage_options.region or DEFAULT_REGION
    client = object_client(storage_options)
    # Real AWS S3 REJECTS create_bucket without a LocationConstraint outside us-east-1; RustFS ignores
    # it. Sending it only when region != us-east-1 keeps RustFS working and a real-S3 backend correct.
    # dict[str, object] makes the **splat mismatch every typed create_bucket parameter
    # (boto3-stubs types each one); Any is the correct annotation for a kwargs splat.
    kwargs: dict[str, Any] = {"Bucket": bucket}
    if region != "us-east-1":
        kwargs["CreateBucketConfiguration"] = {"LocationConstraint": region}
    try:
        client.create_bucket(**kwargs)
    except ClientError as exc:
        code = str(exc.response.get("Error", {}).get("Code", ""))
        if code not in ("BucketAlreadyOwnedByYou", "BucketAlreadyExists"):
            raise
        log.info("warehouse_bucket_exists", extra={"bucket": bucket})


def _warehouse_key(warehouse_id: str) -> str:
    """The registry key for one warehouse. One definition because three call sites now build it —
    and a conditional write that targeted a different key from the read would silently do nothing."""
    return f"{_REGISTRY_PREFIX}/{warehouse_id}.json"


def put_warehouse(control_root: str, storage_options: StorageOptions, record: dict[str, str]) -> None:
    """Unconditionally overwrite ``_warehouses/<id>.json``. **A SEEDING primitive — no production caller.**

    It had two, and both were the diff2 F4 defect: an unconditional put of a record assembled from an
    earlier read silently discards whatever landed in between, which is how a quarantine could be
    lifted without anyone calling ``/activate``. Those paths now go through :func:`upsert_warehouse`
    (idempotent re-create) and :func:`set_warehouse_status` (lifecycle flip), both conditional on the
    record's ETag. The id-MINT goes through :func:`create_warehouse_record`, conditional on absence (F1).

    What is left is fixture setup — ~25 test sites that need a record to EXIST and have no concurrency
    to lose to. Kept for them, and pinned by ``tests/unit/test_registry_writes_are_conditional.py``:
    if this appears in a production path again, that test names it. Do not reach for it in service or
    endpoint code; there is a conditional door for every real mutation.

    The caller stamps ``created_at`` (kept out of here so unit tests stay deterministic). The record is written live with
    a fresh uuid unless it names its own, and names the creation of its project its project's record names, where that
    record stands, and otherwise a creation no record holds (ADR 0245): a fixture seeding a warehouse alone gets one that
    routes, and that no project administers.
    """
    seeded = {UUID: tenancy.new_uuid(), STATE: tenancy.LIVE, **record}
    if PROJECT_UUID not in seeded:
        project = read_json(control_root, storage_options, f"{control_prefixes.PROJECTS}/{record.get('project')}.json")
        seeded[PROJECT_UUID] = str(project[UUID]) if project and project.get(UUID) else tenancy.new_uuid()
    write_json(control_root, storage_options, _warehouse_key(record["id"]), seeded)


#: The fields an idempotent re-create OWNS. Everything else on the live record belongs to the
#: record's own lifecycle and is carried forward from the record AS IT STANDS AT WRITE TIME — not as
#: the caller read it, which is the whole of diff2 F4.
#:
#: ``endpoint`` is OPTIONAL and non-secret ([[LH-067]]): the object-store address this warehouse's
#: bucket lives at. Absent means the estate default, and the create door admits no other store
#: ([[LH-205]], ``catalog.core.store_endpoint``). **No credential field belongs beside it** — material never travels in a record;
#: the estate signs with its ServiceAccount token and holds no S3 key (ADR 0170), and a second store's key belongs
#: in the secret store with the record naming a reference.
#:
#: ``delete_mode`` and ``trash_grace_days`` are the warehouse's delete profile (ADR 0146), which its
#: project's admin declares; a request naming one replaces both, so a hard profile keeps no grace.
#:
#: ``sealed`` is what the registration's seal check measured of the store (ADR 0242), and ``sealed_at`` when, replaced by
#: every registration that measured one.
_CALLER_OWNED = frozenset({"id", "bucket", "root_uri", "project", "endpoint", "credential_ref", DELETE_MODE, TRASH_GRACE_DAYS, SEALED, SEALED_AT})


class WarehouseProjectConflict(Exception):
    """The live record belongs to a different project than the caller named.

    Its own type because it is raised from INSIDE the conditional write, where the check and the
    write can no longer be separated by an interleaving. The endpoint's pre-flight guard still runs
    (it produces the better error, earlier, for the ordinary case); this is the one that cannot be
    raced.
    """


class WarehouseOfADeadTenant(Exception):
    """The record names the caller's project id at another creation of it: a create that raced that project's delete
    wrote it (ADR 0245), and it is no warehouse of the tenant holding the id now. Raised from inside the conditional
    write, as :class:`WarehouseProjectConflict` is; the converge tick ends the record."""


def set_warehouse_protection(
    control_root: str, storage_options: StorageOptions, warehouse_id: str, *, uuid: str, change: ProtectionChange
) -> dict[str, str] | None:
    """Apply ``change`` to live warehouse ``warehouse_id``'s record where it names the creation ``uuid`` the caller was
    authorized on, under the ETag of the read that found it (ADR 0245): every other field is the record's as it stands.

    Returns:
        The updated record, or ``None`` where the id no longer names that creation live: one deleted between the
        caller's authorization and this write, or made again under its id.
    """
    return change_creation(
        control_root, storage_options, _warehouse_key(warehouse_id), uuid, lambda record: with_protection(record, change), states=frozenset({tenancy.LIVE})
    )


def set_warehouse_sealed(control_root: str, storage_options: StorageOptions, warehouse_id: str, sealed: str, *, uuid: str) -> dict[str, str] | None:
    """Record what a validate's seal check measured of warehouse ``warehouse_id``'s store (ADR 0242): ``enforced``,
    ``unenforced`` or ``untaggable``, and when. Conditional on a read that finds the creation ``uuid`` the probe ran
    for, every other field as it stands at the write; ``None`` where the id no longer names it."""
    return change_creation(control_root, storage_options, _warehouse_key(warehouse_id), uuid, lambda record: {**record, **sealed_fields(sealed)})


#: Where the seal the catalog's probe last measured of the estate's own warehouse is kept: it has no registry record, and
#: a prefix beneath the registry's, which its listing does not descend into (:func:`read_warehouses`).
_ESTATE_SEAL_PREFIX = f"{_REGISTRY_PREFIX}/estate-seal"


def set_estate_sealed(control_root: str, storage_options: StorageOptions, warehouse_id: str, sealed: str) -> dict[str, str]:
    """Record what the catalog's seal probe measured of the estate's own warehouse ``warehouse_id`` (ADR 0242), and when;
    the record written."""
    record = sealed_fields(sealed)
    put_record(control_root, storage_options, f"{_ESTATE_SEAL_PREFIX}/{warehouse_id}.json", record)
    return record


def estate_seal(control_root: str, storage_options: StorageOptions, warehouse_id: str) -> dict[str, Any] | None:
    """The record of what the catalog's seal probe last measured of the estate's own warehouse (:data:`SEALED`,
    :data:`SEALED_AT`), ``None`` before its first probe."""
    record = get_record(control_root, storage_options, f"{_ESTATE_SEAL_PREFIX}/{warehouse_id}.json", event="estate_seal_unreadable")
    return record if record is not None and isinstance(record.get(SEALED), str) else None


def set_warehouse_manager(control_root: str, storage_options: StorageOptions, warehouse_id: str, *, uuid: str, manager: str | None) -> dict[str, str] | None:
    """Record who owns live warehouse ``warehouse_id``'s declaration ([[XC-213]]): ``git``, or its own doors for ``None``.

    Conditional on a read that finds the creation ``uuid`` live, every other field as it stands at the write. ``None`` is
    answered where the id no longer names it.
    """
    return change_creation(
        control_root, storage_options, _warehouse_key(warehouse_id), uuid, lambda record: with_manager(record, manager), states=frozenset({tenancy.LIVE})
    )


def upsert_warehouse(
    control_root: str,
    storage_options: StorageOptions,
    record: dict[str, str],
    *,
    protection: ProtectionChange | None = None,
    primary: bool = False,
) -> tuple[dict[str, str], dict[str, str]]:
    """Idempotent re-create of an EXISTING warehouse, CONDITIONAL on the record's ETag (diff2 F4).

    THE BUG THIS CLOSES. The re-create used to build a whole record from a read taken at the top of
    the handler, then `put_warehouse` it unconditionally. Between those two points sit the project
    guard, the reserved-bucket guard, the bucket-claim scan AND `provision_bucket` — a network round
    trip. Anything landing in that window was overwritten by a record that carried the pre-window
    values forward:

        t0  GitOps re-POST of `acme-wh` reads the record        status=active
        t1  operator POSTs /management/v1/warehouses/acme-wh/deactivate    status=deactivated
        t2  the re-POST's put lands                             status=active

    The quarantine is lifted with no `/activate` call and no audit signal. Note that `set_warehouse_status`
    being conditional does NOT help: the stale writer is the re-POST, and it is writing a field it
    never meant to change. A guard on the DECISION cannot catch that; only a guard on the WRITE can.

    So the merge happens INSIDE the conditional write, against the record as it actually is:
    `_CALLER_OWNED` fields come from the caller, every other field is carried from live, and
    `primary` is honoured as a REQUEST (it may arm, never disarm). `protection` is what the
    request names ([[LH-548]]), armed or cleared, and `None`, a request naming nothing, carries the live
    value forward. `exchange_json` re-reads and re-applies on a lost race, so a
    concurrent deactivate is preserved instead of clobbered.

    Args:
        control_root: The registry's root.
        storage_options: How to reach it.
        record: The record the request builds; only its `_CALLER_OWNED` fields are taken.
        protection: Deletion protection to apply, or None to carry the live one.
        primary: Mark the warehouse its project's primary; false carries the live marker.

    Returns:
        The record the landed write replaced and the record it wrote, so a caller announces what the write changed
        rather than what it read earlier.

    Raises:
        WarehouseProjectConflict: The live record moved to another project.
        RecordMissingError: The record vanished, or is no longer the creation ``record`` names (a concurrent delete,
            retryable, never a blind create).
    """
    project = record["project"]

    def merge(live: dict[str, str]) -> dict[str, str]:
        if live.get(UUID) != record[UUID] or live.get(STATE) == tenancy.ENDED:
            raise RecordMissingError(f"warehouse {record['id']!r} is no longer the creation {record[UUID]}")
        if live.get("project") != project:
            raise WarehouseProjectConflict(f"warehouse {record['id']!r} is registered to another project")
        if not tenancy.made_under(live, record[PROJECT_UUID]):
            raise WarehouseOfADeadTenant(f"warehouse {record['id']!r} was made under an earlier creation of project {project!r}")
        if DELETE_MODE in record:
            live = {k: v for k, v in live.items() if k != TRASH_GRACE_DAYS}
        merged = {**live, **{k: v for k, v in record.items() if k in _CALLER_OWNED}}
        if merged.get(DELETE_MODE) == DeleteMode.ESTATE:
            del merged[DELETE_MODE]
        # A record written before the lifecycle feature has no status, and absent means live.
        merged.setdefault("status", "active")
        merged["created_at"] = live.get("created_at") or record.get("created_at", "")
        if protection is not None:
            merged = with_protection(merged, protection)
        # THE THIRD REQUEST FIELD, and it must be one: `merged` takes only `_CALLER_OWNED` keys from
        # the caller's record, so a field that is neither caller-owned nor an explicit request is
        # silently dropped. Setting `record["primary"]` at the endpoint returned 200 and wrote nothing
        # (measured against the deployed catalog 2026-09-06). Arm-never-disarm: a re-POST naming
        # nothing carries the live value forward.
        if primary:
            merged["primary"] = "true"
        return merged

    return exchange_json(control_root, storage_options, _warehouse_key(record["id"]), merge)


def create_warehouse_record(control_root: str, storage_options: StorageOptions, record: dict[str, str]) -> None:
    """Mint ``_warehouses/<id>.json`` iff absent — the STORE arbitrates (F1). ``record`` names its creation's uuid and is
    ``creating`` (ADR 0245): nothing is written in OpenFGA before it stands.

    Raises :class:`lakehouse.records.RecordExistsError` on a lost race. The caller re-reads and re-applies its guards
    against the WINNER's record (the cross-project takeover guard's read may predate the rival's write; this refusal is
    what closes that window).
    """
    create_json(control_root, storage_options, _warehouse_key(record["id"]), record)


def set_live(control_root: str, storage_options: StorageOptions, warehouse_id: str, uuid: str) -> dict[str, str] | None:
    """Flip warehouse ``warehouse_id``'s record from ``creating`` to ``live`` where it still names the creation ``uuid``,
    conditional on its ETag: its seed stands by then (ADR 0245). The record as it stands, ``None`` where it is gone,
    another creation's, or no longer creating or live."""

    def flip(record: dict[str, str]) -> dict[str, str]:
        return {**record, STATE: tenancy.LIVE} if record.get(STATE) == tenancy.CREATING else record

    return change_creation(control_root, storage_options, _warehouse_key(warehouse_id), uuid, flip, states=frozenset({tenancy.CREATING, tenancy.LIVE}))


def mark_deleting(control_root: str, storage_options: StorageOptions, warehouse_id: str, judged: Stored, marked: dict[str, str]) -> bool:
    """Write ``marked``, the warehouse's record as its delete marks it (``tenancy.mark``), only while the record is still
    the one ``judged`` read (ADR 0245): whether it landed."""
    return replace_judged(control_root, storage_options, _warehouse_key(warehouse_id), judged, marked)


def get_warehouse(control_root: str, storage_options: StorageOptions, warehouse_id: str) -> dict[str, str] | None:
    """The warehouse record, or ``None`` if unregistered, still being created or being deleted (ADR 0245).

    A malformed record REFUSES rather than reading as unregistered: every destructive door starts here,
    and "not found" is an answer they act on."""
    record = get_warehouse_record(control_root, storage_options, warehouse_id)
    return None if record is None or record.get(STATE) != tenancy.LIVE else record


def get_warehouse_record(control_root: str, storage_options: StorageOptions, warehouse_id: str) -> dict[str, str] | None:
    """The warehouse record in whatever state it is, or ``None`` if unregistered or stamped ``ended`` (its delete removes
    it next): what its delete and the converge tick read.

    Raises:
        ServiceUnavailableError: The record is there and cannot be read as one.
    """
    key = _warehouse_key(warehouse_id)
    record = read_json(control_root, storage_options, key)
    valid = validated_or_refuse(record, WarehouseRecord, event="warehouse_record_malformed", path=key) if record is not None else None
    return None if valid is None or valid.get(STATE) == tenancy.ENDED else valid


def read_warehouses(control_root: str, storage_options: StorageOptions, *, ended: bool = False) -> tuple[list[dict[str, str]], list[str]]:
    """``(readable warehouse records in every state but ``ended`` (that too with ``ended``: the converge tick, removing one
    whose stamper died), paths that are there and could NOT be read as one)``, unordered. An absent prefix yields
    ``([], [])``.

    The skipped paths are returned because the callers need opposite things from one tolerance: a listing survives one
    bad object, while a decision that no record names something (the last warehouse of a project, another claim on a
    bucket) is a guess while one cannot be read ([[LH-572]]).

    Validated against the DECLARED shape, not eyeballed with ``.get()`` truthiness: a record whose ``bucket`` is a list is
    present, non-empty and structurally useless, so it is skipped and named rather than read as live, logged under
    ``warehouse_record_unreadable`` or ``warehouse_record_malformed``. A record deleted since the listing named it is no
    record. A read the store did not answer raises, tolerant caller or strict: one skipped as unreadable would read as a
    corrupt record, which no retry mends ([[LH-572]]).

    Raises:
        ServiceUnavailableError: The store did not answer a record's read.
    """
    fs, base = fs_and_base(control_root, storage_options)
    out: list[dict[str, str]] = []
    skipped: list[str] = []
    for info in fs.get_file_info(pafs.FileSelector(f"{base}/{_REGISTRY_PREFIX}", allow_not_found=True)):
        if info.type != pafs.FileType.File or not info.path.endswith(".json"):
            continue
        try:
            with fs.open_input_stream(info.path) as stream:
                record = json.loads(stream.readall().decode("utf-8"))
        except FileNotFoundError:
            continue
        except Exception as exc:
            if reads_as_store_outage(exc):
                raise store_refusal(exc, reading=f"reading {info.path}", so="the registry cannot be read") from exc
            log.warning("warehouse_record_unreadable", extra={"path": info.path, "error": str(exc)})
            skipped.append(info.path)
            continue
        if (valid := validated(record, WarehouseRecord, event="warehouse_record_malformed", path=info.path)) is None:
            skipped.append(info.path)
        elif ended or valid.get(STATE) != tenancy.ENDED:
            out.append(valid)
    return out, skipped


def list_warehouses(control_root: str, storage_options: StorageOptions) -> list[dict[str, str]]:
    """Every readable live warehouse record (unordered), the tolerant half of :func:`read_warehouses`, for a caller that
    only enumerates: one being created or deleted is no warehouse to a door (ADR 0245). An absent registry prefix yields
    ``[]``.

    One corrupt record is SKIPPED with a warning, never allowed to 500 the whole listing: a single bad object voiding it
    would turn one tenant's registry corruption into an every-tenant control-plane outage. A read the store did not
    answer raises, as a failed listing does. A caller deciding something destructive or protective asks
    :func:`every_warehouse`.
    """
    return [record for record in read_warehouses(control_root, storage_options)[0] if record.get(STATE) == tenancy.LIVE]


def every_warehouse(control_root: str, storage_options: StorageOptions) -> list[dict[str, str]]:
    """Every warehouse record in every state, read whole, for a caller deciding from the registry that no record names
    something: a record still being created counts (ADR 0245, ``CountsCreating``).

    Raises:
        ServiceUnavailableError: The store did not answer a read of the registry.
        InternalError: A record is there and cannot be read as one, so it may be the one the decision is about.
    """
    records, skipped = read_warehouses(control_root, storage_options)
    if skipped:
        raise InternalError(
            f"{len(skipped)} warehouse record(s) cannot be read as one ({', '.join(sorted(skipped))}), so what the registry "
            "holds cannot be determined. Refusing rather than deciding on a possibly-short list."
        )
    return records


def projects_claiming_bucket(records: list[dict[str, str]], bucket: str) -> set[str]:
    """Every project with a warehouse record claiming ``bucket`` (any lifecycle status — a deactivated
    warehouse still owns its bucket's data, so its claim still blocks a rival registration)."""
    return {str(r["project"]) for r in records if r.get("bucket") == bucket and r.get("project")}


def _claim_key(bucket: str) -> str:
    return f"{_BUCKET_CLAIMS_PREFIX}/{bucket}.json"


def bucket_claim(control_root: str, storage_options: StorageOptions, *, bucket: str) -> dict[str, str] | None:
    """Who owns ``bucket``, or ``None`` when nobody has claimed it, or its claim was released (:data:`RELEASED_AT`). A
    claim a purge holds (:data:`PURGING`) is answered with its stamp.

    ``None`` rather than an exception so a caller can distinguish "unclaimed" from "claimed by someone"
    without control flow, the same shape :func:`get_warehouse` answers with.
    """
    stored = _stored_claim(control_root, storage_options, bucket)
    return None if stored is None or stored[0].get(RELEASED_AT) else stored[0]


def _stored_claim(control_root: str, storage_options: StorageOptions, bucket: str) -> tuple[dict[str, str], str] | None:
    """The claim on ``bucket`` as stored, released or not, with its ETag; ``None`` where none was ever written.

    Raises:
        ServiceUnavailableError: The claim is there and cannot be read as one.
    """
    key = _claim_key(bucket)
    stored = records.read_json(control_root, storage_options, key)
    if stored is None:
        return None
    claim = validated_or_refuse(stored[0], BucketClaimRecord, event="bucket_claim_malformed", path=key)
    return {str(k): str(v) for k, v in claim.items()}, stored[1]


def read_claims(control_root: str, storage_options: StorageOptions) -> tuple[list[dict[str, str]], list[str]]:
    """``(every bucket claim standing, the path of each that is there and cannot be read as one)``, released ones left
    out: one listing, what the converge tick judges a dead tenant's claims by (ADR 0241 §9 P3). Tolerant, so one claim no
    catalog wrote stops no tick; the tick releases nothing while any cannot be read.

    Raises:
        ServiceUnavailableError: The store did not answer the listing or a read.
    """
    fs, base = fs_and_base(control_root, storage_options)
    claims: list[dict[str, str]] = []
    unreadable: list[str] = []
    for info in fs.get_file_info(pafs.FileSelector(f"{base}/{_BUCKET_CLAIMS_PREFIX}", allow_not_found=True)):
        if info.type != pafs.FileType.File or not info.path.endswith(".json"):
            continue
        try:
            with fs.open_input_stream(info.path) as stream:
                record = json.loads(stream.readall().decode("utf-8"))
        except FileNotFoundError:
            continue
        except Exception as exc:
            if reads_as_store_outage(exc):
                raise store_refusal(exc, reading=f"reading {info.path}", so="the bucket claims cannot be read") from exc
            log.warning("bucket_claim_unreadable", extra={"path": info.path, "error": str(exc)})
            unreadable.append(info.path)
            continue
        if (valid := validated(record, BucketClaimRecord, event="bucket_claim_malformed", path=info.path)) is None:
            unreadable.append(info.path)
        elif not valid.get(RELEASED_AT):
            claims.append({str(k): str(v) for k, v in valid.items()})
    return claims, unreadable


def _names(claim: dict[str, str], *, project: str, project_uuid: str | None) -> bool:
    """Whether ``claim`` names ``project`` at its creation ``project_uuid``."""
    return claim.get("project") == project and tenancy.made_under(claim, project_uuid)


def claim_bucket(control_root: str, storage_options: StorageOptions, *, bucket: str, project: str, project_uuid: str, warehouse_id: str) -> datetime:
    """Record that ``project``, at its creation ``project_uuid``, owns ``bucket``, or refuse because another project
    already does, or another creation of it (ADR 0245): a tenant made again under a deleted project's id does not
    register a bucket that still holds the bytes of the one before. Answers the time the claim was taken, which the
    create's every later write into the bucket is judged by (:func:`require_claim_held`).

    A claim the project at the creation already holds has its ``claimed_at`` moved to now under the read's ETag: the
    converge tick judges a claim by its age, so a create of a second warehouse on the bucket, or a retry, is a create
    the tick does not take the bucket from while it runs. A claim a purge holds (:data:`PURGING`) refuses every create
    on the bucket (409, code 14) until the purge ends (ADR 0241 §10 Q1).

    [[LH-053]] THE LISTING SCAN CANNOT CLOSE THIS AND A BETTER SCAN WOULD NOT EITHER. `create_warehouse`
    reads the warehouse listing, checks it for a rival claim, provisions a bucket over the network and
    only then writes its record — so two concurrent creates naming one bucket under different projects
    each scan a listing taken before the other existed, both pass, and both write. The result is the
    cross-tenant takeover the scan was added to prevent: a project policy set through the second
    warehouse governs, and can destroy version history in, the first project's data.

    So the STORE picks the winner, exactly as :func:`create_warehouse_record` does for a warehouse id: a
    conditional create (`If-None-Match: *` on S3, `O_CREAT|O_EXCL` locally) whose loser surfaces as
    `RecordExistsError` instead of last-writer-wins.

    KEYED BY BUCKET, CARRYING THE PROJECT. One project legitimately backs several warehouses with one
    bucket, which is why a claim already held by the caller's own project is a
    pass rather than a collision, and why keying this by warehouse id would refuse the second half of a
    legitimate pair. It is also what keeps `create_warehouse`'s partial-failure retry idempotent.
    """
    key = _claim_key(bucket)
    now = datetime.now(UTC)
    payload = {
        "bucket": bucket,
        "project": project,
        PROJECT_UUID: project_uuid,
        "warehouse_id": warehouse_id,
        CLAIMED_AT: now.isoformat(),
    }
    for _attempt in range(_CLAIM_ATTEMPTS):
        try:
            create_json(control_root, storage_options, key, payload)
            return now
        except RecordExistsError:
            pass
        # VALIDATED rather than compared raw, the rule every singular read of this registry follows: on a record nobody
        # can parse the comparison merely fails, which would refuse the create (right) while naming a competing project
        # that may not exist (wrong), and send an operator looking for a tenant that is not there.
        stored = _stored_claim(control_root, storage_options, bucket)
        if stored is None:
            continue
        claim, etag = stored
        if claim.get(PURGING) and not claim.get(RELEASED_AT):
            raise ConcurrentModificationError(f"bucket {bucket!r} is being purged; ask again once its purge has ended")
        if claim.get(RELEASED_AT):
            # A released claim is taken under the ETag of the read that found it, so of two creates one wins (ADR 0241 §9 P3).
            replacement = payload
        elif _names(claim, project=project, project_uuid=project_uuid):
            # This project already owns the bucket (a second warehouse, or a retry): its age is this create's.
            replacement = {**claim, CLAIMED_AT: now.isoformat()}
        else:
            raise NamespaceAlreadyExistsError(f"bucket {bucket!r} is already registered to another project's warehouse")
        try:
            replace_json(control_root, storage_options, key, replacement, etag=etag)
            return now
        except RecordChangedError:
            continue
    raise ConcurrentModificationError(f"the claim on bucket {bucket!r} kept changing; ask again")


#: How many times a claim's conditional write is tried against a claim that keeps changing before the request is refused.
_CLAIM_ATTEMPTS: Final = 3


def require_claim_held(
    control_root: str, storage_options: StorageOptions, *, bucket: str, project: str, project_uuid: str, since: datetime, bound: timedelta
) -> None:
    """Refuse a create's next write into ``bucket`` once the claim it took at ``since`` is no longer its own, or once it
    has run past ``bound`` (ADR 0241 §10 Q6b).

    A door is not cancelled at its deadline (``lakehouse.deadline``), and the converge tick releases a dead creation's
    claim judged by its age against the same bound, so a create stalled past it may find its bucket released and taken by
    another tenant. It asks this before each write into the bucket and before its record: the claim must stand
    unreleased, unstamped by a purge, naming its project at its creation, and the create must be younger than the
    bound. A sibling create of the same project creation moving ``claimed_at`` keeps it its own.

    Raises:
        ConcurrentModificationError: The claim is no longer the create's, or the create outran the bound; it writes
            nothing more (409, code 14).
    """
    if datetime.now(UTC) - since > bound:
        raise ConcurrentModificationError(f"the create on bucket {bucket!r} ran past {bound}; the tick may have released its claim, so it writes nothing more")
    claim = bucket_claim(control_root, storage_options, bucket=bucket)
    if claim is None or claim.get(PURGING) or not _names(claim, project=project, project_uuid=project_uuid):
        raise ConcurrentModificationError(f"bucket {bucket!r} is no longer claimed by this create; it writes nothing more")


class PurgeHold(BaseModel):
    """A bucket's claim a purge took for itself (:func:`take_for_purge`): the stamp ``holder`` it wrote."""

    model_config = ConfigDict(frozen=True)

    bucket: str
    holder: str


def purge_holder(kind: str, name: str, uuid: str) -> str:
    """The stamp a purge writes on the claim it holds: what it purges for (``warehouse``, ``bucket`` or the tick's
    ``catalog``), its name and the creation's uuid, so only a purge of the same object resumes one that died."""
    return f"{kind}:{name}@{uuid}"


def take_for_purge(control_root: str, storage_options: StorageOptions, *, bucket: str, project: str, project_uuid: str | None, holder: str) -> PurgeHold | None:
    """Take the claim on ``bucket`` for a purge, stamping it :data:`PURGING` ``holder`` under the read's ETag, where it
    names ``project`` at its creation ``project_uuid``; ``None`` where it is released, absent or names anyone else, and then the
    purge deletes no byte (ADR 0241 §10 Q1). A claim this holder stamped already is resumed: a purge that died left it.

    Bytes are deleted only under a claim the purge itself holds: a claim stamped refuses every create on the bucket
    (:func:`claim_bucket`) and every release but the purge's own (:func:`end_purge`).

    Raises:
        ConcurrentModificationError: Another purge holds the claim (409, code 14).
    """
    key = _claim_key(bucket)
    for _attempt in range(_CLAIM_ATTEMPTS):
        stored = _stored_claim(control_root, storage_options, bucket)
        if stored is None:
            return None
        claim, etag = stored
        if claim.get(RELEASED_AT) or not _names(claim, project=project, project_uuid=project_uuid):
            return None
        if stamp := claim.get(PURGING):
            if stamp != holder:
                raise ConcurrentModificationError(f"bucket {bucket!r} is being purged by {stamp}; ask again once that purge has ended")
            return PurgeHold(bucket=bucket, holder=holder)
        try:
            replace_json(control_root, storage_options, key, {**claim, PURGING: holder, PURGING_AT: datetime.now(UTC).isoformat()}, etag=etag)
        except RecordChangedError:
            continue
        log.warning("bucket_claim_taken_for_purge", extra={"bucket": bucket, "holder": holder})
        return PurgeHold(bucket=bucket, holder=holder)
    raise ConcurrentModificationError(f"the claim on bucket {bucket!r} kept changing; ask again")


def holds(control_root: str, storage_options: StorageOptions, hold: PurgeHold) -> bool:
    """Whether the claim on ``hold``'s bucket is still the purge's own: standing, unreleased, stamped by its holder."""
    claim = bucket_claim(control_root, storage_options, bucket=hold.bucket)
    return claim is not None and claim.get(PURGING) == hold.holder


def end_purge(control_root: str, storage_options: StorageOptions, hold: PurgeHold, *, released: str | None) -> bool:
    """End the purge ``hold`` names, under the ETag of the read that finds its stamp: the claim released with when and
    ``released`` as its reason, or, ``released`` ``None``, given back unstamped as it stood. The ONE way a claim is
    released (ADR 0241 §10 Q1): a claim nobody stamped is never released, so no claim another create took is.
    Whether it ended the purge."""
    key = _claim_key(hold.bucket)
    for _attempt in range(_CLAIM_ATTEMPTS):
        stored = _stored_claim(control_root, storage_options, hold.bucket)
        if stored is None or stored[0].get(RELEASED_AT) or stored[0].get(PURGING) != hold.holder:
            return False
        claim = {k: v for k, v in stored[0].items() if k not in (PURGING, PURGING_AT)}
        if released is not None:
            claim = {**claim, RELEASED_AT: datetime.now(UTC).isoformat(), RELEASED_REASON: released}
        try:
            replace_json(control_root, storage_options, key, claim, etag=stored[1])
        except RecordChangedError:
            continue
        return True
    return False


#: What a warehouse's create writes into its bucket before its birth, and nothing else does while it is unborn (ADR 0241
#: §9 P3; read from `establish_warehouse` and measured 2026-11-18 on moto): the storage probe's objects under
#: ``_validate/``, each removed by the probe's cleanup and left only where that cleanup failed, and the warehouse's own
#: catalog, the directory namespace's ``__manifest`` table holding one row, the warehouse's (``ensure_catalog_row``).
CREATE_PREFIXES: Final = ("_validate/", "__manifest/")


def written_by_others(bucket: str, storage_options: StorageOptions, *, warehouse_id: str) -> list[str]:
    """What stands in ``bucket`` that a create of warehouse ``warehouse_id`` did not write (:data:`CREATE_PREFIXES`), at
    most ten keys or rows: empty only where every object is one the create writes and its catalog holds no row but the
    warehouse's own. A bucket that is gone holds nothing.

    Raises:
        botocore.exceptions.ClientError: The store refused the listing.
        OSError: The catalog's manifest could not be read.
    """
    import lance
    from botocore.exceptions import ClientError

    client = object_client(storage_options)
    foreign: list[str] = []
    manifest = False
    try:
        for page in client.get_paginator("list_objects_v2").paginate(Bucket=bucket):
            for obj in page.get("Contents", []):
                key = str(obj["Key"])
                manifest = manifest or key.startswith("__manifest/")
                if not key.startswith(CREATE_PREFIXES):
                    foreign.append(key)
                if len(foreign) >= 10:
                    return foreign
    except ClientError as exc:
        if str(exc.response.get("Error", {}).get("Code", "")) in ("NoSuchBucket", "404"):
            return []
        raise
    if manifest and not foreign:
        rows = (
            lance.dataset(f"s3://{bucket}/__manifest", storage_options=storage_options.in_clear() or None).to_table(columns=["object_id"]).column("object_id")
        )
        foreign += [f"__manifest row {row}" for row in rows.to_pylist() if row != warehouse_id][:10]
    return foreign


def project_of(control_root: str, storage_options: StorageOptions, warehouse_id: str) -> str | None:
    """The tenant owning warehouse ``warehouse_id``, or ``None`` for the estate's own warehouse or an id no record holds.

    A name's project is its first segment's warehouse's ([[LH-593]]): one registry read. Its consumers are the run
    rule's admin, a label's project, a role grant's tenant, and lineage's ``rask_lance.project`` facet, where
    ``None`` costs an event its watchers and never its author.

    Raises:
        ServiceUnavailableError: The record is there and is not a warehouse's.
        ValueError: The record is there and is not JSON.
        OSError: The store failed the read.
    """
    if not CONTROL_ID_RE.fullmatch(warehouse_id):
        return None
    record = get_warehouse(control_root, storage_options, warehouse_id)
    project = record.get("project") if record else None
    return str(project) if project else None


def warehouse_status(control_root: str, storage_options: StorageOptions, warehouse_id: str) -> str | None:
    """A warehouse's lifecycle status: ``"active"`` / ``"deactivated"``. Returns ``"active"`` when the field
    is ABSENT (backward compat — records written before the lifecycle feature have no status and are live),
    and ``None`` only when the warehouse record does not exist. Read live, never cached: status is mutable."""
    record = get_warehouse(control_root, storage_options, warehouse_id)
    if record is None:
        return None
    return record.get("status") or "active"


def set_warehouse_status(control_root: str, storage_options: StorageOptions, warehouse_id: str, status: str, *, uuid: str) -> dict[str, str] | None:
    """Flip live warehouse ``warehouse_id``'s ``status`` (deactivate/activate) where it names the creation ``uuid`` the
    caller was authorized on, and persist it. Returns the updated record, or ``None`` where the id no longer names that
    creation live.

    CONDITIONAL on the ETag of a read that finds that creation (diff2 F4, ADR 0245), not the plain ``get → mutate → put``
    this was. That shape let a QUARANTINE BE SILENTLY LIFTED by interleaving rather than by anyone asking: a GitOps
    re-POST reads the record (status=active), an operator deactivates, and the re-POST's put lands afterwards carrying the
    status it read before. Nothing in that sequence is a wrong decision by either writer — the re-POST is stale in a
    field it only carried forward — which is exactly why a guard on the DECISION cannot catch it and a guard on the WRITE
    can. A concurrent write to any OTHER field is re-read and kept, and this flip still wins on the field it touched.
    """
    return change_creation(
        control_root, storage_options, _warehouse_key(warehouse_id), uuid, lambda record: {**record, "status": status}, states=frozenset({tenancy.LIVE})
    )


# --------------------------------------------------------------------------- #
# Deletion primitives (`open_hierarchy_lifecycle.md` Decision 3). Deletes are bottom-up:
# a container refuses while full, so these only ever run on an emptied object.
# --------------------------------------------------------------------------- #


def record_key(warehouse_id: str) -> str:
    """Where warehouse ``warehouse_id``'s record lives on the control root."""
    return _warehouse_key(warehouse_id)


def end_creating(control_root: str, storage_options: StorageOptions, record: dict[str, str]) -> bool:
    """End the creation a record still ``creating`` names (a create abandoning its own record, the converge tick ending
    one a dead create left): stamped ``ended`` where it is still that creation and creating, kept under ``_ended/`` for
    the tick's sweep, and removed (``control_records.end_record``). Whether this call ended it.

    Deleting the RECORD is not deleting the BUCKET: the bytes survive unless a delete explicitly asked to purge them
    (Decision 3), and a record-less bucket is reported by the reconciler, not silently forgotten.
    """
    return end_record(
        control_root,
        storage_options,
        tenancy.WAREHOUSE,
        _warehouse_key(record["id"]),
        str(record[UUID]),
        now=datetime.now(UTC),
        states=frozenset({tenancy.CREATING}),
    )


def trash_owned_by(control_root: str, storage_options: StorageOptions, record: dict[str, str]) -> list[dict[str, Any]]:
    """The trash records a delete of the warehouse creation ``record`` names ends, each child's before its parent's.

    THE TRASH GOES WITH THE WAREHOUSE. A recoverable drop keeps a record, the object's grants, its policy and its
    location's claim for an undrop; deleting the warehouse removes what made those locations maintainable and, on
    ``?purge_bucket=true``, the bytes. Measured on the live estate 2026-09-20: 869 of 877 expired records were refused
    `outside the maintained estate`, across 45 warehouses deleted with their trash left behind, and on 2026-10-15 the
    grants such records kept were the reconciler's 17 `ghost_tables`.

    A record is this warehouse's when the drop that filed it was under this creation of it: its name begins with the
    warehouse's id ([[LH-593]]) and it names the creation's uuid, which ``lakehouse.trash.put`` stamps from the
    warehouse's record (``warehouse_uuid``). A sibling warehouse on the same bucket holds its own names, and a warehouse
    made again under the id holds its own creation, so neither's records are among them (ADR 0245, review T2).

    Raises:
        ServiceUnavailableError: The store did not answer a read of the trash.
        InternalError: A trash record is there and cannot be read as one.
    """
    try:
        records = trash.every_record(control_root, storage_options)
    except (OSError, ValueError) as exc:
        # A record that cannot be read may be this warehouse's own, which the delete would leave holding its grants and
        # claim for a warehouse that no longer exists ([[LH-572]]).
        raise store_refusal(exc, reading="reading the trash", so="this warehouse's own trash cannot be determined") from exc
    warehouse_id, uuid = str(record["id"]), record.get(UUID)
    owned = [r for r in records if warehouse_of(str(r.get("id") or "")) == warehouse_id and r.get(trash.WAREHOUSE_UUID) == uuid]
    return sorted(owned, key=lambda r: (r.get("kind") == "namespace", -len(str(r.get("id") or "").split(CATALOG_DELIMITER))))


def purge_bucket(
    control_root: str, storage_options: StorageOptions, hold: PurgeHold, *, only: tuple[str, ...] | None = None, remove_bucket: bool = True
) -> int:
    """Delete the objects in ``hold``'s bucket (those under the prefixes ``only`` names, or every one) and then, with
    ``remove_bucket``, the bucket itself; the object count removed. THE ONE WAY a bucket's bytes are deleted, and only
    under a claim the purge itself holds (ADR 0241 §10 Q1): the claim is read again before the first delete and between
    batches, and a claim no longer the purge's own (:func:`holds`) stops it.

    The IRREVERSIBLE half of a warehouse delete, reached only through an explicit ``?purge_bucket=true``, and of the
    converge tick's removal of what an unborn create wrote. S3 refuses to delete a non-empty bucket, so the contents go
    first (paginated). An already-absent bucket is a no-op (0), so a retry after a partial purge converges.

    Raises:
        ConcurrentModificationError: The claim is no longer the purge's own; nothing more is deleted.
    """
    from botocore.exceptions import ClientError

    client = object_client(storage_options)
    removed = 0
    bucket = hold.bucket
    try:
        paginator = client.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=bucket):
            keys = [{"Key": obj["Key"]} for obj in page.get("Contents", []) if only is None or str(obj["Key"]).startswith(only)]
            if not keys:
                continue
            if not holds(control_root, storage_options, hold):
                raise ConcurrentModificationError(f"the claim on bucket {bucket!r} is no longer this purge's; {removed} object(s) were deleted")
            # delete_objects caps at 1000 keys per call, which is exactly the paginator's page size.
            client.delete_objects(Bucket=bucket, Delete={"Objects": keys})
            removed += len(keys)
        if remove_bucket:
            if not holds(control_root, storage_options, hold):
                raise ConcurrentModificationError(f"the claim on bucket {bucket!r} is no longer this purge's; {removed} object(s) were deleted")
            client.delete_bucket(Bucket=bucket)
    except ClientError as exc:
        code = str(exc.response.get("Error", {}).get("Code", ""))
        if code not in ("NoSuchBucket", "404"):
            raise
        log.info("warehouse_bucket_already_absent", extra={"bucket": bucket})
    log.info("warehouse_bucket_purged", extra={"bucket": bucket, "objects": removed, "holder": hold.holder})
    return removed


def bucket_exists(bucket: str, storage_options: StorageOptions) -> bool:
    """Whether ``bucket`` stands: a purge that deleted it and died before its record went is asked again on a warehouse
    whose bucket, its catalog with it, is gone.

    Raises:
        botocore.exceptions.ClientError: The store refused the request.
    """
    from botocore.exceptions import ClientError

    try:
        object_client(storage_options).head_bucket(Bucket=bucket)
    except ClientError as exc:
        if str(exc.response.get("Error", {}).get("Code", "")) in ("NoSuchBucket", "404"):
            return False
        raise
    return True


def holds_anything(bucket: str, storage_options: StorageOptions) -> bool:
    """Whether any object stands in ``bucket``; a bucket that is gone holds nothing.

    Raises:
        botocore.exceptions.ClientError: The store refused the listing.
    """
    from botocore.exceptions import ClientError

    try:
        return bool(object_client(storage_options).list_objects_v2(Bucket=bucket, MaxKeys=1).get("Contents"))
    except ClientError as exc:
        if str(exc.response.get("Error", {}).get("Code", "")) in ("NoSuchBucket", "404"):
            return False
        raise


def served_ids(control_root: str, storage_options: StorageOptions, *, estate: str) -> list[str]:
    """Every warehouse a name may begin with, the estate's own among the registered ones, sorted: the order a
    paged listing's cursor walks.

    Tolerant, for a listing: a record that cannot be read as one is left out (:func:`list_warehouses`).
    """
    return sorted({estate, *(str(record["id"]) for record in list_warehouses(control_root, storage_options))})


def ensure_catalog_row(ns: LanceNamespace, warehouse_id: str) -> None:
    """Give warehouse ``warehouse_id`` its own row in its directory catalog ``ns``, keeping one that is there.

    Every name beneath a warehouse begins with its id ([[LH-593]]), and the directory catalog refuses a namespace whose
    parent has no row. The row carries a uuid as every namespace's does, so no reader of the index meets a row without
    one. Read before it is written: a row that stands costs one read and commits no version of the catalog's index.
    """
    try:
        native.call(ns, "describe_namespace", DescribeNamespaceRequest(id=[warehouse_id]))
    except NamespaceNotFoundError:
        # Lost to another replica making the same row, it is there all the same.
        with suppress(NamespaceAlreadyExistsError):
            native.call(ns, "create_namespace", CreateNamespaceRequest(id=[warehouse_id], properties=with_minted_uuid(None)))
