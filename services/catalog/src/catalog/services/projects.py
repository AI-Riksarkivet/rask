"""Project registry — a tenant EXISTS when a record says so, not when a warehouse implies it.

The estate used to keep no project records at all: a project "existed" exactly when a warehouse
record claimed it, and the FGA store carried tenant tuples independently. The two drifted the first
time anything wrote one without the other — the live estate held three projects in authz that the
catalog had never heard of, and the projects surface read empty while `/v1/me` listed memberships.

This registry is the fix (`open_hierarchy_lifecycle.md`, Decision 1): existence lives HERE, one JSON
record per tenant, and `POST /v1/projects` writes this record and the FGA tuples in one operation so
the two cannot disagree again. Same stateless-over-object-store shape as the warehouse registry —
`_projects/<id>.json` on the control root, no DB — and the same per-record corruption tolerance:
one unreadable record is skipped with a warning, never allowed to void the tenant listing.

All IO here is blocking; callers threadpool it.
"""

from __future__ import annotations

import json
import logging

import pyarrow.fs as pafs

from catalog.services.warehouses import _read_json, _write_json
from service_kit.lakehouse.objectfs import StorageOptions, fs_and_base
from service_kit.lakehouse.records import create_json


log = logging.getLogger(__name__)

_PROJECTS_PREFIX = "_projects"


def put_project(control_root: str, storage_options: StorageOptions, record: dict[str, str]) -> None:
    """Persist a project record at ``_projects/<id>.json`` (overwrite — for a record whose existence is
    already settled: the sequential idempotent re-POST). The tenant MINT goes through
    :func:`create_project_record` (F1). The caller stamps ``created_at``/``created_by`` (kept out of
    here so unit tests stay deterministic) and carries mutable lifecycle fields (``protected``)
    forward on a re-create, exactly like the warehouse registry does for ``status``.
    """
    _write_json(control_root, storage_options, f"{_PROJECTS_PREFIX}/{record['id']}.json", record)


def create_project_record(control_root: str, storage_options: StorageOptions, record: dict[str, str]) -> None:
    """Mint ``_projects/<id>.json`` iff absent — the STORE arbitrates (F1).

    Raises :class:`service_kit.lakehouse.records.RecordExistsError` on a lost race; the caller
    re-reads and converges on the winner's identity fields (``created_at``/``created_by``), exactly
    like the sequential idempotent re-POST path.
    """
    create_json(control_root, storage_options, f"{_PROJECTS_PREFIX}/{record['id']}.json", record)


def get_project(control_root: str, storage_options: StorageOptions, project_id: str) -> dict[str, str] | None:
    """The project record, or ``None`` if the tenant does not exist."""
    return _read_json(control_root, storage_options, f"{_PROJECTS_PREFIX}/{project_id}.json")


def delete_project_record(control_root: str, storage_options: StorageOptions, project_id: str) -> None:
    """Remove the registry record. Idempotent — deleting an absent record is a no-op, so the
    partial-failure retry path (record removed, tuple revoke failed, caller retries) cannot fail on
    its second pass."""
    fs, base = fs_and_base(control_root, storage_options)
    try:
        fs.delete_file(f"{base}/{_PROJECTS_PREFIX}/{project_id}.json")
    except FileNotFoundError:
        return


def list_projects(control_root: str, storage_options: StorageOptions) -> list[dict[str, str]]:
    """Every readable project record (unordered). An absent registry prefix yields ``[]``.

    One corrupt or unreadable record is SKIPPED with a warning (mirroring ``list_warehouses``):
    the listing feeds the tenant gallery and the existence guard, and a single bad object voiding
    it would turn one tenant's registry corruption into an every-tenant outage.
    """
    fs, base = fs_and_base(control_root, storage_options)
    out: list[dict[str, str]] = []
    for info in fs.get_file_info(pafs.FileSelector(f"{base}/{_PROJECTS_PREFIX}", allow_not_found=True)):
        if info.type != pafs.FileType.File or not info.path.endswith(".json"):
            continue
        try:
            with fs.open_input_stream(info.path) as stream:
                record = json.loads(stream.readall().decode("utf-8"))
        except Exception as exc:
            log.warning("project_record_unreadable", extra={"path": info.path, "error": str(exc)})
            continue
        if isinstance(record, dict) and record.get("id"):
            out.append(record)
        else:
            log.warning("project_record_malformed", extra={"path": info.path})
    return out
