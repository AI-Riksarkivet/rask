"""Table-maintenance policy registry (#50) — the catalog↔compaction contract, so it lives in common
(the same reasoning as the outbox: one service writes the records, another enforces them, and a
per-service copy of the format would drift).

Stateless-over-object-store, the same shape as the warehouse registry: each policy is one JSON record
under ``<control_root>/_policies/``, written by the catalog (owner-gated) and read directly off the
bucket by the compaction service on every sweep tick (it has no catalog client by design). A record
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

import hashlib
import json
import logging
from typing import Any

import pyarrow.fs as pafs

from common.objectfs import StorageOptions, fs_and_base


log = logging.getLogger(__name__)

_POLICIES_PREFIX = "_policies"


def _key(kind: str, canonical_id: str) -> str:
    """A collision-free record key: the id is user-shaped (contains ``$``), so hash it."""
    digest = hashlib.sha256(f"{kind}:{canonical_id}".encode()).hexdigest()[:24]
    return f"{_POLICIES_PREFIX}/{kind}-{digest}.json"


def put_policy(control_root: str, storage_options: StorageOptions, record: dict[str, Any]) -> None:
    """Persist one policy record (overwrite — set is idempotent)."""
    fs, base = fs_and_base(control_root, storage_options)
    key = _key(record["kind"], record["id"])
    parent = f"{base}/{key}".rsplit("/", 1)[0]
    fs.create_dir(parent, recursive=True)
    with fs.open_output_stream(f"{base}/{key}") as stream:
        stream.write(json.dumps(record).encode("utf-8"))


def get_policy(control_root: str, storage_options: StorageOptions, kind: str, canonical_id: str) -> dict[str, Any] | None:
    """The policy record for one object, or ``None`` when unset."""
    fs, base = fs_and_base(control_root, storage_options)
    try:
        stream = fs.open_input_stream(f"{base}/{_key(kind, canonical_id)}")
    except FileNotFoundError:
        return None
    with stream:
        return json.loads(stream.readall().decode("utf-8"))


def delete_policy(control_root: str, storage_options: StorageOptions, kind: str, canonical_id: str) -> bool:
    """Remove one policy record; ``False`` when it did not exist (delete is idempotent)."""
    fs, base = fs_and_base(control_root, storage_options)
    try:
        fs.delete_file(f"{base}/{_key(kind, canonical_id)}")
    except FileNotFoundError:
        return False
    return True


def list_policies(control_root: str, storage_options: StorageOptions) -> list[dict[str, Any]]:
    """Every readable policy record (unordered) — the sweep's per-tick load. Absent prefix yields ``[]``.

    State stamps live under ``_policies/state/`` and are excluded (non-recursive listing). One corrupt
    or unreadable record is SKIPPED with a warning, never allowed to void the others — a tick that
    silently dropped every policy would maintain opted-out datasets and ignore protective retention.
    """
    fs, base = fs_and_base(control_root, storage_options)
    out: list[dict[str, Any]] = []
    selector = pafs.FileSelector(f"{base}/{_POLICIES_PREFIX}", allow_not_found=True, recursive=False)
    for info in fs.get_file_info(selector):
        if info.type != pafs.FileType.File or not info.path.endswith(".json"):
            continue
        try:
            with fs.open_input_stream(info.path) as stream:
                record = json.loads(stream.readall().decode("utf-8"))
        except Exception as exc:
            log.warning("maintenance_policy_unreadable", extra={"path": info.path, "error": str(exc)})
            continue
        if isinstance(record, dict) and _record_is_well_formed(record):
            out.append(record)
        else:
            log.warning("maintenance_policy_malformed", extra={"path": info.path})
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
    delimiter: str = "$",
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
    digest = hashlib.sha256(f"{record['kind']}:{record['id']}:{uri}".encode()).hexdigest()[:24]
    return f"{_POLICIES_PREFIX}/state/{record['kind']}-{digest}.json"


def read_state(control_root: str, storage_options: StorageOptions, record: dict[str, Any], uri: str) -> str | None:
    """The sweep's ``last_maintained_at`` stamp for one dataset under a policy, or ``None``. State lives
    under ``_policies/state/`` — a separate prefix the sweep owns, so the catalog's policy writes and the
    sweep's stamps never contend."""
    fs, base = fs_and_base(control_root, storage_options)
    try:
        stream = fs.open_input_stream(f"{base}/{_state_key(record, uri)}")
    except FileNotFoundError:
        return None
    with stream:
        stamped = json.loads(stream.readall().decode("utf-8"))
    value = stamped.get("last_maintained_at")
    return value if isinstance(value, str) else None


def write_state(control_root: str, storage_options: StorageOptions, record: dict[str, Any], uri: str, at: str) -> None:
    """Persist the sweep's ``last_maintained_at`` stamp for one dataset under a policy (overwrite)."""
    fs, base = fs_and_base(control_root, storage_options)
    key = _state_key(record, uri)
    parent = f"{base}/{key}".rsplit("/", 1)[0]
    fs.create_dir(parent, recursive=True)
    with fs.open_output_stream(f"{base}/{key}") as stream:
        stream.write(json.dumps({"last_maintained_at": at}).encode("utf-8"))
