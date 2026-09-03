"""Table-maintenance policy registry (#50) — the catalog↔compaction contract, so it lives in service_kit
(the same reasoning as the outbox: one service writes the records, another enforces them, and a
per-service copy of the format would drift).

Stateless-over-object-store, the same shape as the warehouse registry — and that shape is now written
ONCE, in :mod:`service_kit.lakehouse.record_store`; this module is the policy vocabulary over it. Each
policy is one JSON record under ``<control_root>/_policies/``, written by the catalog (owner-gated)
and read directly off the bucket by the compaction service on every sweep tick (it has no catalog client by design). A record
carries the *logical* id it was set for plus the *physical* bucket-qualified path (``<bucket>/<path>``
— the sweep spans per-warehouse and multi-base buckets, so a bucket-relative path would let a policy
govern a same-named path in another tenant's bucket), so the two services never need a shared resolver:

* table policy — ``path`` is the dataset directory (resolved from ``describe_table`` at set time);
* namespace policy — ``path`` is the namespace directory prefix, and the id doubles as a logical
  parent-chain match for the catalog's flat ``<uuid>_<table_id>`` layout (see :func:`resolve_policy`);
* project policy (#84) — the tenant-wide default: no ``path``, instead ``buckets`` (the project's
  active warehouse buckets, resolved from the warehouse registry at set time), matched at the bucket
  level so it also covers medallion-nested datasets that carry no logical id.

Resolution order (:func:`resolve_policy`): a table policy wins over a namespace policy wins over a
project policy wins over the sweep's global defaults.

Trust boundary: like the warehouse registry, records are trusted as written — the write path is the
catalog's owner-gated API, and a principal with direct write access to the control bucket is already
inside the storage trust boundary. Known limitation: dropping or renaming a table does not clean up
its policy record; the orphan matches nothing (the path is gone) but lingers until deleted.

Fields: ``retention_days`` / ``retain_versions`` (old-version cleanup overrides — Lance itself exempts
tag-pinned versions from cleanup, so ``blessed`` and friends survive any policy), ``compact_enabled``
(opt a dataset out of maintenance entirely), ``compact_interval_hours`` (cadence — the sweep skips the
dataset until the interval has elapsed since its last maintenance; the sweep records that stamp under
``_policies/state/``, a separate prefix so the two writers never contend). All IO is blocking; callers
threadpool it.
"""

from __future__ import annotations

import logging
from typing import Any

from service_kit.lakehouse.naming import CATALOG_DELIMITER
from service_kit.lakehouse.objectfs import StorageOptions
from service_kit.lakehouse.record_store import delete_record, get_record, list_records, put_record, record_key


log = logging.getLogger(__name__)

_POLICIES_PREFIX = "_policies"

#: The event stem for this registry's store warnings (`<stem>_malformed`, `<stem>_unreadable`).
_EVENT = "maintenance_policy"


def put_policy(control_root: str, storage_options: StorageOptions, record: dict[str, Any]) -> None:
    """Persist one policy record (overwrite — set is idempotent)."""
    put_record(control_root, storage_options, record_key(_POLICIES_PREFIX, record["kind"], record["id"]), record)


def get_policy(control_root: str, storage_options: StorageOptions, kind: str, canonical_id: str) -> dict[str, Any] | None:
    """The policy record for one object, or ``None`` when unset — or when what is stored is not a record.

    The malformed case used to be MISSING here while both sibling registries had it: this returned
    ``json.loads(...)`` unguarded, so a non-object document reached :func:`resolve_policy` as whatever
    JSON was on disk. It now reads as absent, and the sweep falls back to its global defaults.
    """
    return get_record(control_root, storage_options, record_key(_POLICIES_PREFIX, kind, canonical_id), event=_EVENT)


def delete_policy(control_root: str, storage_options: StorageOptions, kind: str, canonical_id: str) -> bool:
    """Remove one policy record; ``False`` when it did not exist (delete is idempotent)."""
    return delete_record(control_root, storage_options, record_key(_POLICIES_PREFIX, kind, canonical_id))


def migrate_policy(control_root: str, storage_options: StorageOptions, kind: str, old_id: str, new_id: str) -> bool:
    """Move one policy record to a new canonical id; ``False`` when there was nothing to move.

    For RENAME, where deleting would be wrong in both directions. The record key is a hash of
    ``kind:canonical_id`` (see :func:`service_kit.lakehouse.record_store.record_key`), so a renamed
    object's policy no longer resolves under its
    new id while the old key lingers forever matching nothing — the operator's retention window,
    fragment sizing and cleanup toggles silently revert to defaults on an operation that is supposed to
    move a table, not reconfigure it.

    Migration rather than deletion is what the sibling records already do: ``rename_table`` migrates the
    FGA tuples from the old id to the new, on the principle that a rename relocates an object and keeps
    everything attached to it. The policy is attached to the object, not to the name.

    Writes the destination BEFORE removing the source, so a failure mid-way leaves a duplicate (both ids
    resolve, the new one correctly) rather than a table with no policy at all — the same
    seed-then-revoke ordering, and for the same reason: of the two imperfect failure states, the
    recoverable one is the one that keeps configuration alive.
    """
    record = get_policy(control_root, storage_options, kind, old_id)
    if record is None:
        return False
    put_policy(control_root, storage_options, {**record, "id": new_id})
    delete_policy(control_root, storage_options, kind, old_id)
    return True


def list_policies(control_root: str, storage_options: StorageOptions) -> list[dict[str, Any]]:
    """Every readable policy record (unordered) — the sweep's per-tick load. Absent prefix yields ``[]``.

    State stamps live under ``_policies/state/`` and are excluded (non-recursive listing). One corrupt
    or unreadable record is SKIPPED with a warning, never allowed to void the others — a tick that
    silently dropped every policy would maintain opted-out datasets and ignore protective retention.
    """
    out: list[dict[str, Any]] = []
    for record in list_records(control_root, storage_options, _POLICIES_PREFIX, event=_EVENT):
        if _record_is_well_formed(record):
            out.append(record)
        else:
            log.warning("maintenance_policy_malformed", extra={"id": record.get("id"), "kind": record.get("kind")})
    return out


def _record_is_well_formed(record: dict[str, Any]) -> bool:
    """Whether a stored record carries the fields its kind needs to ever match a dataset.

    Table/namespace records match by ``path``; a project record (#84) has no single path — it matches
    by ``buckets`` (its warehouse buckets, resolved at set time), so it needs a non-empty bucket list
    instead. A record that could never match is malformed, not merely inert — surface it.
    """
    kind = record.get("kind")
    if not kind:
        return False
    if kind == "project":
        buckets = record.get("buckets")
        return bool(record.get("id")) and isinstance(buckets, list) and bool(buckets)
    return bool(record.get("path"))


def resolve_policy(
    records: list[dict[str, Any]],
    uri: str,
    *,
    logical_id: str | None = None,
    delimiter: str = CATALOG_DELIMITER,
) -> dict[str, Any] | None:
    """The policy governing dataset ``uri`` (``s3://<bucket>/<path>``): an exact table match wins,
    else the longest-matching namespace record, else a project record (#84) whose ``buckets`` contain
    the dataset's bucket, else ``None`` (the sweep's global defaults apply).

    Record paths are bucket-qualified (``<bucket>/<path>``) — the sweep spans multiple buckets
    (per-warehouse, multi-base), and a bucket-relative path would let a policy in one bucket govern a
    same-named path in another tenant's bucket.

    A namespace record matches two ways, because the catalog's physical layout is *flat*: a table
    ``db$users`` lives at ``<bucket>/<uuid>_db$users``, never under a ``db/`` directory, so a directory
    prefix alone would make namespace policies dead letters for every catalog-created table (audit
    2026-07-16). The caller therefore also passes the dataset's ``logical_id`` (from the ``<uuid>_``
    layout) and the record matches when its id is on the logical parent chain. The directory-prefix
    match stays for nested layouts (the medallion zones), where there is no logical id to derive.

    A project record (#84) matches when the dataset's BUCKET is in the record's ``buckets`` — the
    bucket-level match needs no logical id, so it also covers a project bucket's medallion-nested
    datasets. It is strictly the LAST fallback: any table or namespace match shadows it. A bucket
    belongs to one project, so DISTINCT project records both claiming the dataset's bucket are a
    registry misconfiguration (a possible cross-tenant claim): the tier then resolves to NO match
    with a warning — never first-encountered-wins, which would let record ordering decide whose
    retention policy destroys whose version history (audit 2026-07-23).
    """
    rel = uri.removeprefix("s3://").rstrip("/") if uri.startswith("s3://") else uri.rstrip("/")
    bucket = rel.split("/", 1)[0]
    parents: set[str] = set()
    if logical_id:
        segments = logical_id.split(delimiter)
        parents = {delimiter.join(segments[:i]) for i in range(1, len(segments))}
    best: dict[str, Any] | None = None
    best_len = -1
    project_hits: list[dict[str, Any]] = []
    for record in records:
        path = str(record.get("path", "")).rstrip("/")
        if record.get("kind") == "table" and path and rel == path:
            return record
        if record.get("kind") == "namespace":
            by_path = bool(path) and rel.startswith(path + "/")
            by_id = record.get("id") in parents
            # len(path) orders both match modes: a deeper namespace always has the longer path.
            if (by_path or by_id) and len(path) > best_len:
                best, best_len = record, len(path)
        elif record.get("kind") == "project":
            buckets = record.get("buckets")
            if isinstance(buckets, list) and bucket in buckets:
                project_hits.append(record)
    if best is not None:
        return best
    if len(project_hits) > 1:
        log.warning(
            "maintenance_policy_project_overlap",
            extra={"bucket": bucket, "projects": sorted(str(r.get("id")) for r in project_hits)},
        )
        return None
    return project_hits[0] if project_hits else None


def _state_key(record: dict[str, Any], uri: str) -> str:
    # Per (policy, DATASET): a namespace policy covers many datasets, and a shared stamp would let the
    # first success starve every sibling until the interval elapsed (audit 2026-07-16).
    #
    # The same deriver as every other record here, under the sweep's own `state/` prefix: the composite
    # `<id>:<uri>` is what the stamp is FOR, so it takes the canonical-id slot and the digest is
    # byte-identical to the hand-rolled sha256 this used to carry (pinned in the drain suite, because
    # changing it orphans every stamp already written and re-maintains every dataset once).
    return record_key(f"{_POLICIES_PREFIX}/state", str(record["kind"]), f"{record['id']}:{uri}")


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


#: The event lane's per-DATASET stamp, keyed by uri alone. Deliberately NOT `_state_key`, which derives
#: from a policy record: that key exists only for datasets carrying a policy with an interval, so an
#: unpoliced dataset has nowhere to stamp — and unpoliced is the default. Same `state/` prefix, a
#: reserved `dataset` kind, so both live in one place an operator can list; the existing derivation is
#: untouched, because changing it would orphan every stamp already written.
_EVENT_STATE_KIND = "dataset"


def read_planned_version(control_root: str, storage_options: StorageOptions, uri: str) -> int | None:
    """The dataset version this lane last PLANNED at, or ``None`` if it never has.

    ``None`` also covers an unreadable or malformed stamp, and that is the safe direction: it maintains.
    Treating an unreadable stamp as "recently planned" would silence maintenance for that dataset until
    someone noticed, which is the failure mode the cadence stamp's own docstring warns about.
    """
    record = get_record(control_root, storage_options, record_key(f"{_POLICIES_PREFIX}/state", _EVENT_STATE_KIND, uri), event="maintenance_event_state")
    value = record.get("last_planned_version") if record else None
    return value if isinstance(value, int) else None


def write_planned_version(control_root: str, storage_options: StorageOptions, uri: str, version: int) -> None:
    """Stamp the version this lane planned at (overwrite — only the latest matters)."""
    put_record(control_root, storage_options, record_key(f"{_POLICIES_PREFIX}/state", _EVENT_STATE_KIND, uri), {"last_planned_version": version})
