"""Maintenance policy registry (#50) — the catalog↔compaction contract, so it lives in lakehouse
(the same reasoning as the outbox: one service writes the records, another enforces them, and a
per-service copy of the format would drift).

Stateless-over-object-store, the same shape as the warehouse registry, written once in
:mod:`lakehouse.record_store`; this module is the policy vocabulary over it. Each policy is one
JSON record under ``<control_root>/_policies/``, written by the catalog (owner-gated) and read directly off
the bucket by the maintenance sweep on every tick (it has no catalog client by design):

* table policy — keyed by the table's ``uuid`` (ADR 0173 §8, [[LH-523]]), so a rename keeps it and a table
  created later under the same name holds none of it;
* namespace policy — keyed by the namespace's ``uuid``, governing every table under the namespace;
* project policy (#84) — the tenant-wide default, keyed by the project id: instead of a uuid it carries
  ``buckets`` (the project's active warehouse buckets, resolved from the warehouse registry at set time),
  matched at the bucket level.

A table or namespace record also keeps the object's name under ``id``, as it was when the policy was set, for
display. Resolution order (:func:`resolve_policy`): a table policy wins over the nearest namespace policy wins
over a project policy wins over the sweep's global defaults.

Trust boundary: like the warehouse registry, records are trusted as written — the write path is the
catalog's owner-gated API, and a principal with direct write access to the control bucket is already
inside the storage trust boundary. A drop or deregister ends a table's record through
:mod:`lakehouse.object_records`.

Fields: ``retention_days`` / ``retain_versions`` (old-version cleanup overrides — Lance itself exempts
tag-pinned versions from cleanup, so ``published`` and every other tag survive any policy), ``compact_enabled``
(opt a dataset out of maintenance entirely), ``compact_interval_hours`` (cadence — the sweep skips the
dataset until the interval has elapsed since its last maintenance; the sweep records that stamp under
``_policies/state/<table uuid>/``, a separate prefix so the two writers never contend, which the table's ending ends). All IO is blocking; callers
threadpool it.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from typing import Any, Final

from lakehouse import stored_objects
from lakehouse.record_store import delete_record, get_record, list_records, put_record, record_key
from lakehouse.storage_options import StorageOptions


log = logging.getLogger(__name__)

#: Where the records live on the control root, one per object under ``record_key(PREFIX, kind, policy_key(record))``.
PREFIX: Final = "_policies"

#: The event stem for this registry's store warnings (`<stem>_malformed`, `<stem>_unreadable`).
_EVENT = "maintenance_policy"


#: The kinds whose records are keyed by the object's uuid; a project's is keyed by its id.
_UUID_KEYED_KINDS: Final = frozenset({"table", "namespace"})


def policy_key(record: dict[str, Any]) -> str:
    """What a record is stored under: its object's ``uuid`` for a table or namespace, its ``id`` for a project."""
    return str(record["uuid"] if record.get("kind") in _UUID_KEYED_KINDS else record["id"])


def put_policy(control_root: str, storage_options: StorageOptions, record: dict[str, Any]) -> None:
    """Persist one policy record (overwrite — set is idempotent)."""
    put_record(control_root, storage_options, record_key(PREFIX, record["kind"], policy_key(record)), record)


def get_policy(control_root: str, storage_options: StorageOptions, kind: str, key: str) -> dict[str, Any] | None:
    """The policy record stored under ``key`` (:func:`policy_key`), or ``None`` when unset — or when what is stored
    is not a record: a malformed one reads as absent, and the sweep falls back to its global defaults.
    """
    return get_record(control_root, storage_options, record_key(PREFIX, kind, key), event=_EVENT)


def delete_policy(control_root: str, storage_options: StorageOptions, kind: str, key: str) -> bool:
    """Remove one policy record; ``False`` when it did not exist (delete is idempotent)."""
    return delete_record(control_root, storage_options, record_key(PREFIX, kind, key))


def list_policies(control_root: str, storage_options: StorageOptions) -> list[dict[str, Any]]:
    """Every readable policy record (unordered) — the sweep's per-tick load. Absent prefix yields ``[]``.

    State stamps live under ``_policies/state/`` and are excluded (non-recursive listing). One corrupt
    or unreadable record is SKIPPED with a warning, never allowed to void the others — a tick that
    silently dropped every policy would maintain opted-out datasets and ignore protective retention.
    """
    out: list[dict[str, Any]] = []
    for record in list_records(control_root, storage_options, PREFIX, event=_EVENT):
        if _record_is_well_formed(record):
            out.append(record)
        else:
            log.warning("maintenance_policy_malformed", extra={"id": record.get("id"), "kind": record.get("kind")})
    return out


def _record_is_well_formed(record: dict[str, Any]) -> bool:
    """Whether a stored record carries the fields its kind needs to ever match a dataset.

    Table/namespace records match by ``uuid``; a project record (#84) matches by ``buckets`` (its warehouse
    buckets, resolved at set time), so it needs a non-empty bucket list instead. A record that could never
    match is malformed, not merely inert — surface it.
    """
    kind = record.get("kind")
    if not kind:
        return False
    if kind == "project":
        buckets = record.get("buckets")
        return bool(record.get("id")) and isinstance(buckets, list) and bool(buckets)
    return bool(record.get("uuid"))


def resolve_policy(records: list[dict[str, Any]], uri: str, *, table_uuid: str, namespace_uuids: Sequence[str]) -> dict[str, Any] | None:
    """The policy governing the table ``table_uuid`` names, whose dataset is ``uri`` (``s3://<bucket>/<path>``).

    The table's own record wins, else the record of the nearest namespace in ``namespace_uuids`` (the table's
    ancestors, outermost first), else a project record (#84) whose ``buckets`` contain the dataset's bucket,
    else ``None`` (the sweep's global defaults apply). A branch is its table's dataset too, so its table's
    record governs it.

    The project record is strictly the LAST fallback. A bucket belongs to one project, so DISTINCT project
    records both claiming the dataset's bucket are a registry misconfiguration (a possible cross-tenant
    claim): the tier then resolves to NO match with a warning — never first-encountered-wins, which would let
    record ordering decide whose retention policy destroys whose version history (audit 2026-07-23).
    """
    tables = _by_uuid(records, "table")
    if table_uuid in tables:
        return tables[table_uuid]
    namespaces = _by_uuid(records, "namespace")
    for namespace_uuid in reversed(namespace_uuids):
        if namespace_uuid in namespaces:
            return namespaces[namespace_uuid]
    return _project_policy(records, uri)


def keeps_whole_history(policy: dict[str, Any] | None) -> bool:
    """Whether the resolved ``policy`` keeps the whole version history of what it governs (``cleanup_enabled`` false: a
    legal hold, or a time-travel window that is the product), so nothing of that history is reclaimed."""
    return policy is not None and not bool(policy.get("cleanup_enabled", True))


def _by_uuid(records: list[dict[str, Any]], kind: str) -> dict[str, dict[str, Any]]:
    return {str(record["uuid"]): record for record in records if record.get("kind") == kind and record.get("uuid")}


def _project_policy(records: list[dict[str, Any]], uri: str) -> dict[str, Any] | None:
    """The one project record whose ``buckets`` contain the dataset's bucket, ``None`` when none or several do."""
    bucket = uri.removeprefix("s3://").split("/", 1)[0]
    hits = [record for record in records if record.get("kind") == "project" and bucket in (record.get("buckets") or [])]
    if len(hits) > 1:
        log.warning("maintenance_policy_project_overlap", extra={"bucket": bucket, "projects": sorted(str(record.get("id")) for record in hits)})
        return None
    return hits[0] if hits else None


#: The sweep's and the event lane's stamps, one directory per table uuid ([[LH-562]]), so a table's ending ends them all
#: (:func:`end_table_state`, at the control root, which is the policy root the stamps are written at, as the policy
#: records the catalog writes there already require) and none outlives the table it paced. A table registered later at
#: the same location holds another uuid and starts unstamped.
STATE_PREFIX: Final = f"{PREFIX}/state"


def _table_state(table_uuid: str) -> str:
    return f"{STATE_PREFIX}/{table_uuid}"


def _cadence_key(table_uuid: str, uri: str) -> str:
    # Per DATASET: a branch is a dataset of its own under its table's uuid, and a shared stamp would let one ref's pass
    # starve the others until the interval elapsed (audit 2026-07-16).
    return record_key(_table_state(table_uuid), "cadence", uri)


def _policy_of(record: dict[str, Any]) -> str:
    return f"{record['kind']}:{policy_key(record)}"


def read_state(control_root: str, storage_options: StorageOptions, record: dict[str, Any], *, table_uuid: str, uri: str) -> str | None:
    """A pass's ``last_maintained_at`` stamp for the dataset ``uri`` of the table ``table_uuid`` under the policy
    ``record``, or ``None``. A stamp taken under another policy paces nothing: the policy that governs now has not
    maintained the dataset yet."""
    stamped = get_record(control_root, storage_options, _cadence_key(table_uuid, uri), event="maintenance_policy_state")
    if stamped is None or stamped.get("policy") != _policy_of(record):
        return None
    value = stamped.get("last_maintained_at")
    return value if isinstance(value, str) else None


def write_state(control_root: str, storage_options: StorageOptions, record: dict[str, Any], *, table_uuid: str, uri: str, at: str) -> None:
    """Persist a pass's ``last_maintained_at`` stamp for one dataset under the policy ``record`` (overwrite)."""
    put_record(control_root, storage_options, _cadence_key(table_uuid, uri), {"policy": _policy_of(record), "last_maintained_at": at})


def end_table_state(control_root: str, storage_options: StorageOptions, table_uuid: str) -> bool:
    """Delete every stamp the table ``table_uuid`` holds; whether any existed. Writes no directory marker
    (:func:`stored_objects.delete_objects`), since maintenance, which ends a purged table's records, may put none here."""
    listed = stored_objects.list_under(f"{control_root.rstrip('/')}/{_table_state(table_uuid)}", storage_options)
    stored_objects.delete_objects(control_root, storage_options, [stored.path for stored in listed])
    return bool(listed)
