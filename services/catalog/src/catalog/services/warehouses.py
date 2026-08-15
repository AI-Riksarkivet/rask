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
from lance_namespace import NamespaceAlreadyExistsError, ServiceUnavailableError

from service_kit.lakehouse.objectfs import StorageOptions, fs_and_base
from service_kit.lakehouse.records import RecordExistsError, RecordMissingError, create_json, mutate_json


log = logging.getLogger(__name__)

_REGISTRY_PREFIX = "_warehouses"
_BINDINGS_PREFIX = "_warehouses/bindings"


def _bucket_client(storage_options: StorageOptions) -> Any:  # noqa: ANN401 — boto3 client has no public stub
    """The S3 client the bucket lifecycle ops share, built from the catalog's own credentials.

    Kept in ONE place so provisioning and purging can never address different endpoints — creating a
    bucket on one backend and deleting it on another is a failure mode that reports success.
    """
    import boto3

    return boto3.client(
        "s3",
        endpoint_url=storage_options.get("endpoint"),
        aws_access_key_id=storage_options.get("access_key_id"),
        aws_secret_access_key=storage_options.get("secret_access_key"),
        region_name=storage_options.get("region") or "us-east-1",
    )


def provision_bucket(bucket: str, storage_options: StorageOptions) -> None:
    """Create the physical S3 bucket, idempotently (like ``mc mb --ignore-existing``).

    Uses boto3 against the same endpoint/credentials the catalog already holds. An already-owned/existing
    bucket is a no-op (a re-provision on a warehouse-create retry must not fail). Blocking IO; threadpool it.
    """
    from botocore.exceptions import ClientError

    region = storage_options.get("region") or "us-east-1"
    client = _bucket_client(storage_options)
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

    The caller stamps ``created_at`` (kept out of here so unit tests stay deterministic).
    """
    _write_json(control_root, storage_options, _warehouse_key(record["id"]), record)


#: The fields an idempotent re-create OWNS. Everything else on the live record belongs to the
#: record's own lifecycle and is carried forward from the record AS IT STANDS AT WRITE TIME — not as
#: the caller read it, which is the whole of diff2 F4.
_CALLER_OWNED = frozenset({"id", "bucket", "root_uri", "project"})


class WarehouseProjectConflict(Exception):
    """The live record belongs to a different project than the caller named.

    Its own type because it is raised from INSIDE the conditional write, where the check and the
    write can no longer be separated by an interleaving. The endpoint's pre-flight guard still runs
    (it produces the better error, earlier, for the ordinary case); this is the one that cannot be
    raced.
    """


def upsert_warehouse(
    control_root: str,
    storage_options: StorageOptions,
    record: dict[str, str],
    *,
    serving: str | None = None,
    protect: bool = False,
) -> dict[str, str]:
    """Idempotent re-create of an EXISTING warehouse, CONDITIONAL on the record's ETag (diff2 F4).

    THE BUG THIS CLOSES. The re-create used to build a whole record from a read taken at the top of
    the handler, then `put_warehouse` it unconditionally. Between those two points sit the project
    guard, the reserved-bucket guard, the bucket-claim scan AND `provision_bucket` — a network round
    trip. Anything landing in that window was overwritten by a record that carried the pre-window
    values forward:

        t0  GitOps re-POST of `acme-wh` reads the record        status=active
        t1  operator POSTs /v1/warehouses/acme-wh/deactivate    status=deactivated
        t2  the re-POST's put lands                             status=active

    The quarantine is lifted with no `/activate` call and no audit signal. Note that `set_warehouse_status`
    being conditional does NOT help: the stale writer is the re-POST, and it is writing a field it
    never meant to change. A guard on the DECISION cannot catch that; only a guard on the WRITE can.

    So the merge happens INSIDE the conditional write, against the record as it actually is:
    `_CALLER_OWNED` fields come from the caller, every other field is carried from live, and
    `serving`/`protect` are honoured as REQUESTS (they may arm, never disarm — same rule the
    sequential path always had). `mutate_json` re-reads and re-applies on a lost race, so a
    concurrent deactivate is preserved instead of clobbered.

    Raises :class:`WarehouseProjectConflict` if the live record moved to another project, and
    ``RecordMissingError`` if it vanished (a concurrent delete — retryable, never a blind create).
    """
    project = record["project"]

    def merge(live: dict[str, str]) -> dict[str, str]:
        if live.get("project") != project:
            raise WarehouseProjectConflict(f"warehouse {record['id']!r} is registered to another project")
        merged = {**live, **{k: v for k, v in record.items() if k in _CALLER_OWNED}}
        # A record written before the lifecycle feature has no status, and absent means live.
        merged.setdefault("status", "active")
        merged["created_at"] = live.get("created_at") or record.get("created_at", "")
        if serving:
            merged["serving"] = serving
        if protect:
            merged["protected"] = "true"
        return merged

    return mutate_json(control_root, storage_options, _warehouse_key(record["id"]), merge)


def create_warehouse_record(control_root: str, storage_options: StorageOptions, record: dict[str, str]) -> None:
    """Mint ``_warehouses/<id>.json`` iff absent — the STORE arbitrates (F1).

    Raises :class:`service_kit.lakehouse.records.RecordExistsError` on a lost race. The caller
    re-reads and re-applies its guards against the WINNER's record (the cross-project takeover
    guard's read may predate the rival's write; this refusal is what closes that window).
    """
    create_json(control_root, storage_options, _warehouse_key(record["id"]), record)


def get_warehouse(control_root: str, storage_options: StorageOptions, warehouse_id: str) -> dict[str, str] | None:
    """The warehouse record, or ``None`` if unregistered."""
    return _read_json(control_root, storage_options, _warehouse_key(warehouse_id))


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

    A binding is immutable (a namespace's warehouse never changes), so a resolver may cache it forever —
    which is exactly why the write is WRITE-ONCE AT THE STORE (F1): the endpoint's pre-check reads then
    decides, and two concurrent binds of one ``top_ns`` both used to pass it, the overwrite re-routing
    tenant A's tables at tenant B's bucket with the forever-positive caches pinning the damage. Now the
    second create is refused by the store regardless of what its caller read. Same-binding re-runs stay
    idempotent (the partial-failure retry path: binding written, a later step failed, caller retries);
    a DIFFERENT binding raises ``NamespaceAlreadyExistsError`` (code 2 → 409).
    """
    payload = {"top_ns": top_ns, "warehouse_id": warehouse_id, "root_uri": root_uri}
    key = f"{_BINDINGS_PREFIX}/{top_ns}.json"
    try:
        create_json(control_root, storage_options, key, payload)
    except RecordExistsError:
        existing = _read_json(control_root, storage_options, key)
        if existing is not None and existing.get("warehouse_id") == warehouse_id and existing.get("root_uri") == root_uri:
            return  # the retry converging on its own earlier write
        raise NamespaceAlreadyExistsError(f"namespace {top_ns!r} is already bound to another warehouse") from None


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
    ``None`` if the warehouse does not exist.

    CONDITIONAL on the record's ETag (diff2 F4), not the plain ``get → mutate → put`` this was. That
    shape let a QUARANTINE BE SILENTLY LIFTED by interleaving rather than by anyone asking: a GitOps
    re-POST reads the record (status=active), an operator deactivates, and the re-POST's put lands
    afterwards carrying the status it read before. Nothing in that sequence is a wrong decision by
    either writer — the re-POST is stale in a field it only carried forward — which is exactly why a
    guard on the DECISION cannot catch it and a guard on the WRITE can.

    ``mutate_json`` re-reads and re-applies on a lost race, so a concurrent write to any OTHER field
    is preserved rather than clobbered, and this flip still wins. That is the right convergence for a
    field-level toggle: last writer wins on the field they actually touched, nobody wins on a field
    they merely read.
    """
    try:
        return mutate_json(
            control_root,
            storage_options,
            _warehouse_key(warehouse_id),
            lambda record: {**record, "status": status},
        )
    except RecordMissingError:
        return None


# --------------------------------------------------------------------------- #
# Deletion primitives (`open_hierarchy_lifecycle.md` Decision 3). Deletes are bottom-up:
# a container refuses while full, so these only ever run on an emptied object.
# --------------------------------------------------------------------------- #


def read_bindings(control_root: str, storage_options: StorageOptions) -> tuple[list[dict[str, str]], list[str]]:
    """``(readable bindings, paths that could NOT be read)``. An absent prefix yields ``([], [])``.

    The skipped paths are RETURNED, not merely logged, because the two callers need opposite things
    from the same tolerance and a plain list cannot serve both:

    * a LISTING wants to survive one bad object (the :func:`list_warehouses` posture);
    * the DELETE door must fail closed on one. A binding it cannot read is a namespace it cannot
      see, so an unreadable object turns "this warehouse is empty" into a guess — and with
      ``?purge_bucket=true`` that guess destroys the tables of a namespace nobody could enumerate.

    An earlier version of this function only logged the skip and its own docstring claimed the delete
    path "treats a skip as serious". It could not: nothing crossed the return boundary to treat.
    """
    fs, base = fs_and_base(control_root, storage_options)
    out: list[dict[str, str]] = []
    skipped: list[str] = []
    for info in fs.get_file_info(pafs.FileSelector(f"{base}/{_BINDINGS_PREFIX}", allow_not_found=True)):
        if info.type != pafs.FileType.File or not info.path.endswith(".json"):
            continue
        try:
            with fs.open_input_stream(info.path) as stream:
                record = json.loads(stream.readall().decode("utf-8"))
        except Exception as exc:
            log.warning("binding_record_unreadable", extra={"path": info.path, "error": str(exc)})
            skipped.append(info.path)
            continue
        if isinstance(record, dict) and record.get("top_ns") and record.get("warehouse_id"):
            out.append(record)
        else:
            log.warning("binding_record_malformed", extra={"path": info.path})
            skipped.append(info.path)
    return out, skipped


def list_bindings(control_root: str, storage_options: StorageOptions) -> list[dict[str, str]]:
    """Every READABLE namespace→warehouse binding (unordered), skipping what it cannot parse.

    The tolerant half of :func:`read_bindings`, for callers that only enumerate. Anything that makes
    a DESTRUCTIVE decision must use ``read_bindings`` and refuse on a non-empty skip list.
    """
    return read_bindings(control_root, storage_options)[0]


def namespaces_bound_to(control_root: str, storage_options: StorageOptions, warehouse_id: str) -> list[str]:
    """The top-level namespaces physically living in ``warehouse_id``, sorted.

    The warehouse's *contents* for lifecycle purposes: non-empty ⇒ the delete refuses 409 and names
    them; ``?cascade=true`` drops them bottom-up in this order.

    Raises ``ServiceUnavailableError`` if ANY binding object is unreadable — including ones belonging
    to other warehouses. That looks over-broad and is deliberate: the unreadable object's own content
    is what says which warehouse it binds, so "it probably wasn't this one's" is precisely the
    assumption that cannot be made. Refusing is recoverable; a wrong emptiness answer is not.
    """
    bindings, skipped = read_bindings(control_root, storage_options)
    if skipped:
        raise ServiceUnavailableError(
            f"{len(skipped)} namespace binding record(s) could not be read ({', '.join(sorted(skipped))}), so this "
            f"warehouse's contents cannot be determined. Refusing rather than reporting a possibly-empty warehouse."
        )
    return sorted(str(b["top_ns"]) for b in bindings if b.get("warehouse_id") == warehouse_id)


def unbind_namespace(control_root: str, storage_options: StorageOptions, top_ns: str) -> None:
    """Remove a namespace→warehouse binding. Idempotent — an absent binding is a no-op, so the
    partial-failure retry path (binding removed, a later step failed, caller retries) converges."""
    fs, base = fs_and_base(control_root, storage_options)
    try:
        fs.delete_file(f"{base}/{_BINDINGS_PREFIX}/{top_ns}.json")
    except FileNotFoundError:
        return


def evict_stale_bindings(cache: dict[str, dict[str, str]], *, action: str, object_id: str, extra: dict[str, Any], delimiter: str) -> list[str]:
    """Evict every binding-cache entry a control event just invalidated; return the evicted keys (#46).

    The cache's premise — a binding is immutable, cache positives forever — is broken by exactly
    three mutations, and each replica hears about all of them on the broadcast control-event
    subscription (no queueGroup: every replica, including the publisher, receives every event):

    - ``warehouse_deleted``: the event's ``namespaces_dropped`` names the unbound namespaces, and a
      warehouse-id SCAN backs it up — a partial delete (the door's Decision-3 path) can unbind more
      than the event managed to record, and a stale entry here routes a tenant's table at a deleted
      (possibly purged) bucket.
    - ``warehouse_bound``: a top-level namespace was just bound. The only way this replica holds a
      cached entry for it is a re-bind after a delete it never processed — evict so the next request
      reads the authoritative record.
    - ``namespace_dropped``: the cache is keyed by TOP-LEVEL namespace, so evict the id's first
      segment (dropping a nested namespace does not move its warehouse).

    Deactivation needs no entry here: warehouse STATUS is read live on every request by design.
    Mutates ``cache`` in place — it is the live ``app.state.warehouse_binding_cache`` dict; per-key
    ``pop`` keeps concurrent resolvers safe (dict ops are atomic under the GIL; a racing resolver
    re-reads the registry, which is the correct outcome).
    """
    evicted: list[str] = []

    def _pop(key: str) -> None:
        if cache.pop(key, None) is not None:
            evicted.append(key)

    if action == "warehouse_deleted":
        warehouse_id = object_id.removeprefix("warehouse:")
        dropped = extra.get("namespaces_dropped")
        for top_ns in dropped if isinstance(dropped, list) else []:
            _pop(str(top_ns))
        for top_ns in [k for k, b in cache.items() if b.get("warehouse_id") == warehouse_id]:
            _pop(top_ns)
    elif action == "warehouse_bound":
        namespace = extra.get("namespace")
        if isinstance(namespace, str) and namespace:
            _pop(namespace)
    elif action == "namespace_dropped":
        top_ns = object_id.removeprefix("namespace:").split(delimiter)[0]
        if top_ns:
            _pop(top_ns)
    return evicted


def delete_warehouse_record(control_root: str, storage_options: StorageOptions, warehouse_id: str) -> None:
    """Remove the registry record. Idempotent for the same retry reason as :func:`unbind_namespace`.

    Deleting the RECORD is not deleting the BUCKET: the bytes survive unless the caller explicitly
    asked to purge them (Decision 3 — a catalog entry is recoverable, a customer's bucket is not, and
    the two never share a default). A record-less bucket is reported by the reconciler, not silently
    forgotten.
    """
    fs, base = fs_and_base(control_root, storage_options)
    try:
        fs.delete_file(f"{base}/{_REGISTRY_PREFIX}/{warehouse_id}.json")
    except FileNotFoundError:
        return


def purge_bucket(bucket: str, storage_options: StorageOptions) -> int:
    """Delete every object in ``bucket`` and then the bucket itself. Returns the object count removed.

    The IRREVERSIBLE half of a warehouse delete, reached only through an explicit ``?purge_bucket=true``.
    S3 refuses to delete a non-empty bucket, so the contents go first (paginated — a warehouse holds far
    more than one page of objects). An already-absent bucket is a no-op (0), so a retry after a partial
    purge converges instead of erroring.
    """
    from botocore.exceptions import ClientError

    client = _bucket_client(storage_options)
    removed = 0
    try:
        paginator = client.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=bucket):
            keys = [{"Key": obj["Key"]} for obj in page.get("Contents", [])]
            if not keys:
                continue
            # delete_objects caps at 1000 keys per call, which is exactly the paginator's page size.
            client.delete_objects(Bucket=bucket, Delete={"Objects": keys})
            removed += len(keys)
        client.delete_bucket(Bucket=bucket)
    except ClientError as exc:
        code = str(exc.response.get("Error", {}).get("Code", ""))
        if code not in ("NoSuchBucket", "404"):
            raise
        log.info("warehouse_bucket_already_absent", extra={"bucket": bucket})
    log.info("warehouse_bucket_purged", extra={"bucket": bucket, "objects": removed})
    return removed
