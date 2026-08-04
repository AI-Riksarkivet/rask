"""Deletion-protection records for tables and namespaces (#73, open_lakehouse_diff §1.1).

The ``protected`` flag warehouses and projects carry on their REGISTRY records, extended to the two
rungs that had none — where the gap was sharpest, because a table drop deletes bytes and (until the
trash-namespace undrop lands) nothing stands behind it.

The marker lives HERE, in a control-root record, and deliberately NOT in the table's schema
metadata:

- One contract: the same ``require_not_protected`` guard reads a record dict for every kind, with
  the same ``force`` semantics — no second implementation to drift.
- No coupling to the properties write door (#78): schema metadata is getting a user-facing update
  endpoint; protection in that keyspace would make "unprotect" reachable through a data-plane door
  and force a reserved-key policy to exist before that endpoint does.
- Protect/unprotect is a control-plane admin op: it emits a control event and must NOT create a
  table version, which a schema-metadata commit would.
- The drop door's guard runs BEFORE the native call and must stay answerable even when the dataset
  itself is corrupted — which is exactly when someone needs to drop it.

Same store shape as ``maintenance_policies`` (one JSON per object, hashed key — ids are
user-shaped and contain ``$``), own ``_protection/`` prefix so neither plane can misread the
other's records.
"""

from __future__ import annotations

import hashlib
import json
import logging
from typing import Any

from service_kit.lakehouse.objectfs import StorageOptions, fs_and_base


log = logging.getLogger(__name__)

_PROTECTION_PREFIX = "_protection"


def _key(kind: str, canonical_id: str) -> str:
    """A collision-free record key: the id is user-shaped (contains ``$``), so hash it."""
    digest = hashlib.sha256(f"{kind}:{canonical_id}".encode()).hexdigest()[:24]
    return f"{_PROTECTION_PREFIX}/{kind}-{digest}.json"


def set_protection(control_root: str, storage_options: StorageOptions, record: dict[str, Any]) -> None:
    """Persist one protection record (overwrite — setting is idempotent)."""
    fs, base = fs_and_base(control_root, storage_options)
    key = _key(str(record["kind"]), str(record["id"]))
    fs.create_dir(f"{base}/{_PROTECTION_PREFIX}", recursive=True)
    with fs.open_output_stream(f"{base}/{key}") as stream:
        stream.write(json.dumps(record).encode())


def get_protection(control_root: str, storage_options: StorageOptions, kind: str, canonical_id: str) -> dict[str, Any] | None:
    """The protection record for one object, or ``None`` — and None means UNPROTECTED, which is the
    correct fail-open direction for this flag: protection is an opt-in the owner set, not a default
    the store's availability grants. (A store OUTAGE surfaces as an exception from pyarrow, not as
    None — the caller lets that propagate rather than treating it as unprotected.)"""
    fs, base = fs_and_base(control_root, storage_options)
    try:
        with fs.open_input_stream(f"{base}/{_key(kind, canonical_id)}") as stream:
            loaded: Any = json.loads(stream.read().decode())
    except FileNotFoundError:
        return None
    if not isinstance(loaded, dict):
        log.warning("protection_record_malformed", extra={"kind": kind, "id": canonical_id})
        return None
    return loaded


def clear_protection(control_root: str, storage_options: StorageOptions, kind: str, canonical_id: str) -> bool:
    """Remove one protection record; ``True`` if one existed."""
    fs, base = fs_and_base(control_root, storage_options)
    try:
        fs.delete_file(f"{base}/{_key(kind, canonical_id)}")
    except FileNotFoundError:
        return False
    return True
