"""READ-ONLY access to the warehouse registry, for services that are not the catalog.

The catalog owns the registry and its writes (`catalog/services/warehouses.py` — provisioning,
binding, CAS, delete). Nothing else may import that: services share only cross-cutting libraries,
never each other. But the maintenance sweep genuinely needs to know WHICH buckets exist, and the
answer changes at runtime — a warehouse is provisioned by an API call, not by a config edit.

So the reader lives here, in the shared lakehouse library, deliberately narrow: it lists records and
nothing else. It cannot write, bind, or delete, which keeps the catalog the only writer while giving
the sweep a truthful answer. Same store shape as its siblings (one JSON per record under a prefix),
and that primitive now exists: :mod:`service_kit.lakehouse.record_store`.
"""

from __future__ import annotations

from typing import Any

from service_kit.lakehouse.objectfs import StorageOptions
from service_kit.lakehouse.record_store import list_records


#: Where the catalog writes warehouse records. Kept in sync with `catalog/services/warehouses.py`.
REGISTRY_PREFIX = "_warehouses"

#: The event stem for this registry's store warnings (`<stem>_malformed`, `<stem>_unreadable`).
_EVENT = "warehouse_record"


def list_warehouse_records(control_root: str, storage_options: StorageOptions) -> list[dict[str, Any]]:
    """Every readable warehouse record (unordered). An absent prefix yields ``[]``.

    One unreadable record is skipped with a warning rather than blinding the whole listing: this
    feeds a maintenance sweep, and one bad record must not stop every other tenant being maintained.
    A caller making a DESTRUCTIVE decision must not use this — it is deliberately tolerant.

    A record with no ``id`` is dropped here rather than in the store: it is readable and well-formed
    JSON, it just names nothing this registry can route to.
    """
    return [record for record in list_records(control_root, storage_options, REGISTRY_PREFIX, event=_EVENT) if record.get("id")]


def measurable_projects(records: list[dict[str, Any]]) -> list[str]:
    """Every project with a live warehouse, sorted and de-duplicated — the tenants a cascade detector
    must measure.

    Beside :func:`maintainable_buckets` because it is the same question asked of the same records: what
    does this estate actually hold right now? A project is minted by ``POST /v1/projects`` at runtime
    with an operator-chosen id, so a statically configured tenant list is stale the moment a tenant is
    onboarded — and a cascade nobody measures is indistinguishable from a cascade with no lag.

    DEACTIVATED warehouses are excluded on the same rule and for a narrower reason: the resolver 403s
    every operation on a quarantined tenant's namespaces, so its edges cannot advance and would report
    a lag that grows forever with nothing anyone is permitted to do about it.
    """
    projects = {str(r["project"]) for r in records if r.get("project") and str(r.get("status", "active")).lower() != "deactivated"}
    return sorted(projects)


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
