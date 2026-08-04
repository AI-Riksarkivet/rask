"""Warehouse registry + runtime bucket provisioning (#3-A) — the admin control plane.

A *warehouse* = one physical S3 bucket owned by a project (the FGA model's catalog-root type,
``service_kit/governed/auth/model.fga``: "A warehouse = exactly one S3 bucket, owned by one project"). Today
the catalog is single-bucket (one ``LANCE_REST_ROOT``); this makes a warehouse a **runtime-provisioned,
physically isolated bucket**, so a table created under warehouse A lands in bucket-a and is ABSENT from
bucket-b — physical multi-tenancy, provisioned through an admin API rather than a static
Helm ``mc mb`` loop.

Stateless-over-object-store, the same shape as ``service_kit/lakehouse/outbox.py``: the registry IS a set of JSON
objects under ``<control_root>/_warehouses/`` — there is no DB to add. A warehouse record lives at
``_warehouses/<id>.json``; a namespace→warehouse binding at ``_warehouses/bindings/<top_ns>.json`` (so any
op on a bound namespace can resolve its physical root). Bucket creation uses boto3 ``create_bucket``,
idempotent like the chart's ``mc mb --ignore-existing``. All IO here is blocking; callers threadpool it.
"""

from __future__ import annotations

import json
import logging
from typing import Any

import pyarrow.fs as pafs

from service_kit.lakehouse.objectfs import StorageOptions, fs_and_base


log = logging.getLogger(__name__)

_REGISTRY_PREFIX = "_warehouses"
_BINDINGS_PREFIX = "_warehouses/bindings"


def provision_bucket(bucket: str, storage_options: StorageOptions) -> None:
    """Create the physical S3 bucket, idempotently (like ``mc mb --ignore-existing``).

    Uses boto3 against the same endpoint/credentials the catalog already holds. An already-owned/existing
    bucket is a no-op (a re-provision on a warehouse-create retry must not fail). Blocking IO; threadpool it.
    """
    import boto3
    from botocore.exceptions import ClientError

    region = storage_options.get("region") or "us-east-1"
    client = boto3.client(
        "s3",
        endpoint_url=storage_options.get("endpoint"),
        aws_access_key_id=storage_options.get("access_key_id"),
        aws_secret_access_key=storage_options.get("secret_access_key"),
        region_name=region,
    )
    # Real AWS S3 REJECTS create_bucket without a LocationConstraint outside us-east-1; RustFS/MinIO ignore
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


def _write_json(root_uri: str, storage_options: StorageOptions, key: str, record: dict[str, str]) -> None:
    fs, base = fs_and_base(root_uri, storage_options)
    parent = f"{base}/{key}".rsplit("/", 1)[0]
    fs.create_dir(parent, recursive=True)  # local FS needs the parent dir; an S3 prefix marker is harmless
    with fs.open_output_stream(f"{base}/{key}") as stream:
        stream.write(json.dumps(record).encode("utf-8"))


def _read_json(root_uri: str, storage_options: StorageOptions, key: str) -> dict[str, str] | None:
    fs, base = fs_and_base(root_uri, storage_options)
    try:
        stream = fs.open_input_stream(f"{base}/{key}")
    except FileNotFoundError:
        return None
    with stream:
        return json.loads(stream.readall().decode("utf-8"))


def put_warehouse(control_root: str, storage_options: StorageOptions, record: dict[str, str]) -> None:
    """Persist a warehouse record at ``_warehouses/<id>.json`` (overwrite — create is idempotent). The
    caller stamps ``created_at`` (kept out of here so unit tests stay deterministic)."""
    _write_json(control_root, storage_options, f"{_REGISTRY_PREFIX}/{record['id']}.json", record)


def get_warehouse(control_root: str, storage_options: StorageOptions, warehouse_id: str) -> dict[str, str] | None:
    """The warehouse record, or ``None`` if unregistered."""
    return _read_json(control_root, storage_options, f"{_REGISTRY_PREFIX}/{warehouse_id}.json")


def list_warehouses(control_root: str, storage_options: StorageOptions) -> list[dict[str, str]]:
    """Every readable warehouse record (unordered). An absent registry prefix yields ``[]``.

    One corrupt or unreadable record is SKIPPED with a warning, never allowed to 500 the whole listing
    (mirrors ``maintenance_policies.list_policies``): the list feeds the project-policy set and the
    bucket-claim guards, and a single bad object voiding it would turn one tenant's registry corruption
    into an every-tenant control-plane outage.
    """
    fs, base = fs_and_base(control_root, storage_options)
    out: list[dict[str, str]] = []
    for info in fs.get_file_info(pafs.FileSelector(f"{base}/{_REGISTRY_PREFIX}", allow_not_found=True)):
        if info.type != pafs.FileType.File or not info.path.endswith(".json"):
            continue
        try:
            with fs.open_input_stream(info.path) as stream:
                record = json.loads(stream.readall().decode("utf-8"))
        except Exception as exc:
            log.warning("warehouse_record_unreadable", extra={"path": info.path, "error": str(exc)})
            continue
        if isinstance(record, dict) and record.get("id") and record.get("bucket") and record.get("project"):
            out.append(record)
        else:  # missing the identity fields every consumer keys on (id/bucket/project) → skip, don't 500
            log.warning("warehouse_record_malformed", extra={"path": info.path})
    return out


def projects_claiming_bucket(records: list[dict[str, str]], bucket: str) -> set[str]:
    """Every project with a warehouse record claiming ``bucket`` (any lifecycle status — a deactivated
    warehouse still owns its bucket's data, so its claim still blocks a rival registration)."""
    return {str(r["project"]) for r in records if r.get("bucket") == bucket and r.get("project")}


def bind_namespace(control_root: str, storage_options: StorageOptions, top_ns: str, warehouse_id: str, root_uri: str) -> None:
    """Record that top-level namespace ``top_ns`` physically lives in ``warehouse_id`` (root ``root_uri``).

    A binding is immutable (a namespace's warehouse never changes), so a resolver may cache it forever.
    """
    _write_json(
        control_root,
        storage_options,
        f"{_BINDINGS_PREFIX}/{top_ns}.json",
        {"top_ns": top_ns, "warehouse_id": warehouse_id, "root_uri": root_uri},
    )


def warehouse_for_namespace(control_root: str, storage_options: StorageOptions, top_ns: str) -> str | None:
    """The physical ``root_uri`` for top-level namespace ``top_ns``, or ``None`` when unbound (→ default
    root). This is the routing lookup on the request hot path; callers cache the (immutable) result."""
    record = _read_json(control_root, storage_options, f"{_BINDINGS_PREFIX}/{top_ns}.json")
    return record.get("root_uri") if record else None


def binding_for_namespace(control_root: str, storage_options: StorageOptions, top_ns: str) -> dict[str, str] | None:
    """The FULL binding record (``{top_ns, warehouse_id, root_uri}``) for a top-level namespace, or ``None``
    when unbound. The resolver needs ``warehouse_id`` (not just ``root_uri``) to check the warehouse's
    lifecycle status; the binding itself is immutable, so the record is safe to cache."""
    return _read_json(control_root, storage_options, f"{_BINDINGS_PREFIX}/{top_ns}.json")


def warehouse_status(control_root: str, storage_options: StorageOptions, warehouse_id: str) -> str | None:
    """A warehouse's lifecycle status: ``"active"`` / ``"deactivated"``. Returns ``"active"`` when the field
    is ABSENT (backward compat — records written before the lifecycle feature have no status and are live),
    and ``None`` only when the warehouse record does not exist. Read LIVE on the routing path (status is
    mutable, so unlike ``root_uri`` it must never be cached)."""
    record = get_warehouse(control_root, storage_options, warehouse_id)
    if record is None:
        return None
    return record.get("status") or "active"


def set_warehouse_status(control_root: str, storage_options: StorageOptions, warehouse_id: str, status: str) -> dict[str, str] | None:
    """Flip a warehouse's ``status`` (deactivate/activate) and persist it. Returns the updated record, or
    ``None`` if the warehouse does not exist. Overwrite-safe like ``put_warehouse`` (idempotent re-runs)."""
    record = get_warehouse(control_root, storage_options, warehouse_id)
    if record is None:
        return None
    record["status"] = status
    put_warehouse(control_root, storage_options, record)
    return record
