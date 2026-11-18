"""READ-ONLY access to the warehouse registry, for services that are not the catalog.

The catalog owns the registry and its writes (`catalog/services/warehouses.py` — provisioning,
CAS, delete). Nothing else may import that: services share only cross-cutting libraries,
never each other. But the maintenance sweep genuinely needs to know WHICH buckets exist, and the
answer changes at runtime — a warehouse is provisioned by an API call, not by a config edit.

So the reader lives here, in the shared lakehouse library, deliberately narrow: it reads records and
nothing else. It cannot write or delete, which keeps the catalog the only writer while giving
the sweep a truthful answer. Same store shape as its siblings (one JSON per record under a prefix),
and that primitive now exists: :mod:`lakehouse.record_store`.
"""

from __future__ import annotations

from typing import Any, Final

from lakehouse import control_prefixes
from lakehouse.governed.tenancy import LIVE, PROJECT_UUID, STATE
from lakehouse.naming import CONTROL_ID_RE
from lakehouse.record_store import list_records, read_record
from lakehouse.storage_options import StorageOptions


#: Where the catalog writes warehouse records and which project owns a bucket. Kept in sync with
#: `catalog/services/warehouses.py`.
REGISTRY_PREFIX = control_prefixes.WAREHOUSES
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
    """The buckets whose datasets the sweep should maintain, sorted and de-duplicated: those of LIVE warehouses.

    DEACTIVATED warehouses are excluded, and that is a decision rather than an oversight. Deactivate
    is offboarding step one — the resolver already 403s every operation on a quarantined tenant's
    namespaces — so compacting and reclaiming its version history would be the one process still
    rewriting data the estate has said nobody may touch. A reactivated warehouse is swept again on
    the next tick, having lost nothing but a cycle of maintenance.

    A warehouse being created or deleted is excluded too (ADR 0245, second review S4): one being created holds nothing
    yet, and one being deleted is emptied by its own delete, which ends its trash and purges its bytes under the claim it
    holds; maintenance rewriting or reclaiming beneath it would race that delete.
    """
    buckets = {str(r["bucket"]) for r in records if r.get("bucket") and r.get(STATE) == LIVE and not is_deactivated(r)}
    return sorted(buckets)


def is_deactivated(record: dict[str, Any]) -> bool:
    """Whether the warehouse ``record`` is quarantined: offboarding's first step, after which nothing rewrites its data."""
    return str(record.get("status", "active")).lower() == "deactivated"


def warehouse_record(control_root: str, storage_options: StorageOptions, warehouse_id: str) -> dict[str, Any] | None:
    """The record of warehouse ``warehouse_id`` while it is live, read by its key, or ``None`` when no record holds that
    id live: a warehouse being created or deleted is maintained by no one but its own create or delete (ADR 0245, second
    review S4), and no drop files trash beneath one (``lakehouse.trash.put``).

    A warehouse is the first segment of every name beneath it ([[LH-593]]), so a table's warehouse is read here by the
    table's own first segment, one read whatever the registry holds. The estate's own warehouse has no record, so the
    caller tells it apart before asking. An id that is not a control id is no record's (it would name another key).

    Raises:
        ValueError: The record cannot be read as one.
        OSError: The store failed the read.
    """
    if not CONTROL_ID_RE.fullmatch(warehouse_id):
        return None
    record = read_record(control_root, storage_options, f"{REGISTRY_PREFIX}/{warehouse_id}.json")
    return record if record is not None and record.get(STATE) == LIVE else None


def bucket_claimant(control_root: str, storage_options: StorageOptions, bucket: str) -> tuple[str, str] | None:
    """The project that owns ``bucket`` and the creation of it that claimed it, as the claim the catalog wrote when a
    warehouse first took the bucket records them; ``None`` when no warehouse took it, or its claim was released (the
    catalog stamps ``released_at``, ADR 0241 §9 P3). A bucket is one project's, which the store arbitrates.

    Raises:
        ValueError: The claim cannot be read as one.
        OSError: The store failed the read.
    """
    claim = read_record(control_root, storage_options, f"{_BUCKET_CLAIMS_PREFIX}/{bucket}.json")
    if claim is None or claim.get("released_at"):
        return None
    project, project_uuid = claim.get("project"), claim.get(PROJECT_UUID)
    if not isinstance(project, str) or not project:
        raise ValueError(f"the claim on bucket {bucket!r} names no project")
    if not isinstance(project_uuid, str) or not project_uuid:
        raise ValueError(f"the claim on bucket {bucket!r} names no creation of project {project!r}")
    return project, project_uuid


def bucket_project(control_root: str, storage_options: StorageOptions, bucket: str) -> str | None:
    """The project that owns ``bucket`` (:func:`bucket_claimant`); ``None`` when no warehouse took it.

    Raises:
        ValueError: The claim cannot be read as one.
        OSError: The store failed the read.
    """
    claimant = bucket_claimant(control_root, storage_options, bucket)
    return None if claimant is None else claimant[0]
