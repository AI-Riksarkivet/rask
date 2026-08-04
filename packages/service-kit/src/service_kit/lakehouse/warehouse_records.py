"""READ-ONLY access to the warehouse registry, for services that are not the catalog.

The catalog owns the registry and its writes (`catalog/services/warehouses.py` — provisioning,
binding, CAS, delete). Nothing else may import that: services share only cross-cutting libraries,
never each other. But the maintenance sweep genuinely needs to know WHICH buckets exist, and the
answer changes at runtime — a warehouse is provisioned by an API call, not by a config edit.

So the reader lives here, in the shared lakehouse library, deliberately narrow: it lists records and
nothing else. It cannot write, bind, or delete, which keeps the catalog the only writer while giving
the sweep a truthful answer. Same store shape as its siblings (one JSON per record under a prefix) —
see the note in `protection.py` about collapsing these into one primitive.
"""

from __future__ import annotations

import json
import logging
from typing import Any

import pyarrow.fs as pafs

from service_kit.lakehouse.objectfs import StorageOptions, fs_and_base


log = logging.getLogger(__name__)

#: Where the catalog writes warehouse records. Kept in sync with `catalog/services/warehouses.py`.
REGISTRY_PREFIX = "_warehouses"


def list_warehouse_records(control_root: str, storage_options: StorageOptions) -> list[dict[str, Any]]:
    """Every readable warehouse record (unordered). An absent prefix yields ``[]``.

    One unreadable record is skipped with a warning rather than blinding the whole listing: this
    feeds a maintenance sweep, and one bad record must not stop every other tenant being maintained.
    A caller making a DESTRUCTIVE decision must not use this — it is deliberately tolerant.
    """
    fs, base = fs_and_base(control_root, storage_options)
    selector = pafs.FileSelector(f"{base}/{REGISTRY_PREFIX}", allow_not_found=True, recursive=False)
    records: list[dict[str, Any]] = []
    for info in fs.get_file_info(selector):
        if info.type != pafs.FileType.File or not info.path.endswith(".json"):
            continue
        try:
            with fs.open_input_stream(info.path) as stream:
                loaded: Any = json.loads(stream.read().decode())
        except Exception:  # noqa: BLE001 — one bad record must not empty the list
            log.warning("warehouse_record_unreadable", extra={"path": info.path})
            continue
        if isinstance(loaded, dict) and loaded.get("id"):
            records.append(loaded)
    return records


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
