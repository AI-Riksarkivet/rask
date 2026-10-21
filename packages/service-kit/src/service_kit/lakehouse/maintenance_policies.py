"""Table-maintenance policy registry (#50) — the catalog↔compaction contract, so it lives in service_kit
(the same reasoning as the outbox: one service writes the records, another enforces them, and a
per-service copy of the format would drift).

Stateless-over-object-store, the same shape as the warehouse registry, written once in
:mod:`service_kit.lakehouse.record_store`; this module is the policy vocabulary over it. Each policy is one
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
:mod:`service_kit.lakehouse.object_records`.

Fields: ``retention_days`` / ``retain_versions`` (old-version cleanup overrides — Lance itself exempts
tag-pinned versions from cleanup, so ``blessed`` and friends survive any policy), ``compact_enabled``
(opt a dataset out of maintenance entirely), ``compact_interval_hours`` (cadence — the sweep skips the
dataset until the interval has elapsed since its last maintenance; the sweep records that stamp under
``_policies/state/``, a separate prefix so the two writers never contend). All IO is blocking; callers
threadpool it.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from typing import Any, Final

from service_kit.lakehouse.objectfs import StorageOptions
from service_kit.lakehouse.record_store import delete_record, get_record, list_records, put_record, record_key


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


def _state_key(record: dict[str, Any], uri: str) -> str:
    # Per (policy, DATASET): a namespace policy covers many datasets, and a shared stamp would let the
    # first success starve every sibling until the interval elapsed (audit 2026-07-16). The policy half is
    # the record's own key, its object's uuid, so a rename keeps the stamp with the policy.
    return record_key(f"{PREFIX}/state", str(record["kind"]), f"{policy_key(record)}:{uri}")


def read_state(control_root: str, storage_options: StorageOptions, record: dict[str, Any], uri: str) -> str | None:
    """The sweep's ``last_maintained_at`` stamp for one dataset under a policy, or ``None``. State lives
    under ``_policies/state/`` — a separate prefix the sweep owns, so the catalog's policy writes and the
    sweep's stamps never contend."""
    stamped = get_record(control_root, storage_options, _state_key(record, uri), event="maintenance_policy_state")
    if stamped is None:
        return None
    value = stamped.get("last_maintained_at")
    return value if isinstance(value, str) else None


def write_state(control_root: str, storage_options: StorageOptions, record: dict[str, Any], uri: str, at: str) -> None:
    """Persist the sweep's ``last_maintained_at`` stamp for one dataset under a policy (overwrite)."""
    put_record(control_root, storage_options, _state_key(record, uri), {"last_maintained_at": at})


#: The event lane's per-TABLE stamp, keyed by the table's uuid. Not `_state_key`, which derives from a
#: policy record: that key exists only for datasets carrying a policy with an interval, so an unpoliced
#: table would have nowhere to stamp, and unpoliced is the default. Same `state/` prefix, its own kind, so
#: both live in one place an operator can list.
_EVENT_STATE_KIND = "table"


def read_planned_version(control_root: str, storage_options: StorageOptions, table_uuid: str) -> int | None:
    """The version of the table ``table_uuid`` names this lane last PLANNED at, or ``None`` if it never has.

    ``None`` also covers an unreadable or malformed stamp, and that is the safe direction: it maintains.
    Treating an unreadable stamp as "recently planned" would silence maintenance for that table until
    someone noticed, which is the failure mode the cadence stamp's own docstring warns about. Keyed by the
    uuid, so a table registered later at the same location starts unstamped.
    """
    record = get_record(control_root, storage_options, record_key(f"{PREFIX}/state", _EVENT_STATE_KIND, table_uuid), event="maintenance_event_state")
    value = record.get("last_planned_version") if record else None
    return value if isinstance(value, int) else None


def write_planned_version(control_root: str, storage_options: StorageOptions, table_uuid: str, version: int) -> None:
    """Stamp the version this lane planned at (overwrite — only the latest matters)."""
    put_record(control_root, storage_options, record_key(f"{PREFIX}/state", _EVENT_STATE_KIND, table_uuid), {"last_planned_version": version})
