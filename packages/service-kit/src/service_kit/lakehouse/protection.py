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

Same store shape as ``maintenance_policies`` (one JSON per object, keyed by the object's uuid, ADR 0173 §8),
own ``_protection/`` prefix so neither plane can misread the other's records. A record also keeps the object's
name under ``id``, as it was when the record was set, for display. The store mechanics are
:mod:`service_kit.lakehouse.record_store`'s; this module is the protection vocabulary over them.
"""

from __future__ import annotations

from typing import Any, Final

from service_kit.lakehouse.objectfs import StorageOptions
from service_kit.lakehouse.record_store import delete_record, get_record, put_record, record_key


#: Where the records live on the control root, one per object under ``record_key(PREFIX, kind, uuid)``.
PREFIX: Final = "_protection"

#: The event stem for this registry's store warnings (`<stem>_malformed`, `<stem>_unreadable`).
_EVENT = "protection_record"


def set_protection(control_root: str, storage_options: StorageOptions, record: dict[str, Any]) -> None:
    """Persist one protection record, keyed by the object's ``uuid`` (overwrite — setting is idempotent)."""
    put_record(control_root, storage_options, record_key(PREFIX, str(record["kind"]), str(record["uuid"])), record)


def get_protection(control_root: str, storage_options: StorageOptions, kind: str, uuid: str) -> dict[str, Any] | None:
    """The protection record for the object ``uuid`` names, or ``None`` — and None means UNPROTECTED, which is the
    correct fail-open direction for this flag: protection is an opt-in the owner set, not a default
    the store's availability grants. (A store OUTAGE surfaces as an exception from pyarrow, not as
    None — the caller lets that propagate rather than treating it as unprotected.)"""
    return get_record(control_root, storage_options, record_key(PREFIX, kind, uuid), event=_EVENT)


def clear_protection(control_root: str, storage_options: StorageOptions, kind: str, uuid: str) -> bool:
    """Remove one protection record; ``True`` if one existed."""
    return delete_record(control_root, storage_options, record_key(PREFIX, kind, uuid))
