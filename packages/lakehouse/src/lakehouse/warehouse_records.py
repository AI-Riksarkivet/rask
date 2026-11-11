"""READ-ONLY access to the warehouse registry, for services that are not the catalog.

The catalog owns the registry and its writes (`catalog/services/warehouses.py` — provisioning,
binding, CAS, delete). Nothing else may import that: services share only cross-cutting libraries,
never each other. But the maintenance sweep genuinely needs to know WHICH buckets exist, and the
answer changes at runtime — a warehouse is provisioned by an API call, not by a config edit.

So the reader lives here, in the shared lakehouse library, deliberately narrow: it reads records and
nothing else. It cannot write, bind, or delete, which keeps the catalog the only writer while giving
the sweep a truthful answer. Same store shape as its siblings (one JSON per record under a prefix),
and that primitive now exists: :mod:`lakehouse.record_store`.
"""

from __future__ import annotations

from typing import Any, Final

from lakehouse.record_store import list_records, read_record
from lakehouse.storage_options import StorageOptions


#: Where the catalog writes warehouse records, a top namespace's binding to its warehouse, and which project owns a bucket.
#: Kept in sync with `catalog/services/warehouses.py`.
REGISTRY_PREFIX = "_warehouses"
_BINDINGS_PREFIX: Final = f"{REGISTRY_PREFIX}/bindings"
_BUCKET_CLAIMS_PREFIX: Final = f"{REGISTRY_PREFIX}/bucket-claims"

#: The event stem for this registry's store warnings (`<stem>_malformed`, `<stem>_unreadable`).
_EVENT = "warehouse_record"


def list_warehouse_records(control_root: str, storage_options: StorageOptions) -> list[dict[str, Any]]:
    """Every readable warehouse record (unordered). An absent prefix yields ``[]``.

    One unreadable record is skipped with a warning rather than blinding the whole listing: this
    feeds a maintenance sweep, and one bad record must not stop every other tenant being maintained.
    A caller making a DESTRUCTIVE decision must not use this — it is deliberately tolerant — and reads
    :func:`read_warehouse_records` instead.

    A record with no ``id`` is dropped here rather than in the store: it is readable and well-formed
    JSON, it just names nothing this registry can route to.
    """
    return [record for record in list_records(control_root, storage_options, REGISTRY_PREFIX, event=_EVENT) if record.get("id")]


def read_warehouse_records(control_root: str, storage_options: StorageOptions) -> list[dict[str, Any]]:
    """Every warehouse record (unordered), read whole, for a caller making a DESTRUCTIVE decision from the listing.

    A record left out may be the one naming the bucket a live table's location is under, so one that cannot be read or is
    not a JSON object raises rather than being skipped as :func:`list_warehouse_records` skips it. A record naming no
    ``id`` is returned as it reads: its bucket is a root the caller reads all the same, which leaving it out would not be.
    An absent prefix yields ``[]``.

    Raises:
        OSError: The store failed a read.
        ValueError: A record is not JSON or not a JSON object.
    """
    return list_records(control_root, storage_options, REGISTRY_PREFIX, event=_EVENT, strict=True)


def maintainable_buckets(records: list[dict[str, Any]]) -> list[str]:
    """The buckets whose datasets the sweep should maintain, sorted and de-duplicated.

    DEACTIVATED warehouses are excluded, and that is a decision rather than an oversight. Deactivate
    is offboarding step one — the resolver already 403s every operation on a quarantined tenant's
    namespaces — so compacting and reclaiming its version history would be the one process still
    rewriting data the estate has said nobody may touch. A reactivated warehouse is swept again on
    the next tick, having lost nothing but a cycle of maintenance.
    """
    buckets = {str(r["bucket"]) for r in records if r.get("bucket") and str(r.get("status", "active")).lower() != "deactivated"}
    return sorted(buckets)


def is_deactivated(record: dict[str, Any]) -> bool:
    """Whether the warehouse ``record`` is quarantined: offboarding's first step, after which nothing rewrites its data."""
    return str(record.get("status", "active")).lower() == "deactivated"


def namespace_warehouse(control_root: str, storage_options: StorageOptions, top_namespace: str) -> dict[str, Any] | None:
    """The record of the warehouse the top-level namespace ``top_namespace`` is bound to, read by its key, or ``None`` when
    the namespace is bound to none (its tables are on the default root).

    Raises:
        ValueError: The binding or the warehouse record cannot be read as one, or the binding names a warehouse with no
            record.
        OSError: The store failed a read.
    """
    binding = read_record(control_root, storage_options, f"{_BINDINGS_PREFIX}/{top_namespace}.json")
    if binding is None:
        return None
    warehouse_id = binding.get("warehouse_id")
    if not isinstance(warehouse_id, str) or not warehouse_id:
        raise ValueError(f"the binding of namespace {top_namespace!r} names no warehouse")
    record = read_record(control_root, storage_options, f"{REGISTRY_PREFIX}/{warehouse_id}.json")
    if record is None:
        raise ValueError(f"namespace {top_namespace!r} is bound to warehouse {warehouse_id!r}, which has no record")
    return record


def bucket_project(control_root: str, storage_options: StorageOptions, bucket: str) -> str | None:
    """The project that owns ``bucket``, as the claim the catalog wrote when a warehouse first took the bucket records it;
    ``None`` when no warehouse took it. A bucket is one project's, which the store arbitrates.

    Raises:
        ValueError: The claim cannot be read as one.
        OSError: The store failed the read.
    """
    claim = read_record(control_root, storage_options, f"{_BUCKET_CLAIMS_PREFIX}/{bucket}.json")
    if claim is None:
        return None
    project = claim.get("project")
    if not isinstance(project, str) or not project:
        raise ValueError(f"the claim on bucket {bucket!r} names no project")
    return project
