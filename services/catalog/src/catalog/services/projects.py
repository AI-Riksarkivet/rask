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

from catalog.services.control_records import ProjectRecord, read_json, validated, write_json
from service_kit.lakehouse.objectfs import StorageOptions, fs_and_base
from service_kit.lakehouse.records import create_json, mutate_json


log = logging.getLogger(__name__)

_PROJECTS_PREFIX = "_projects"


def put_project(control_root: str, storage_options: StorageOptions, record: dict[str, str]) -> None:
    """Unconditionally overwrite ``_projects/<id>.json``. **A SEEDING primitive — no production caller.**

    It had two, and both were the diff2 F1(c) defect: an unconditional put of a record assembled from
    an earlier read silently discards whatever landed in between — on ``protected`` that disarms
    deletion protection with no `/protection` call and no audit signal. Those paths now go through
    :func:`upsert_project`, conditional on the record's ETag; the tenant MINT goes through
    :func:`create_project_record`, conditional on absence (F1).

    What is left is fixture setup, which has no concurrency to lose to. Do not reach for it in service
    or endpoint code; there is a conditional door for every real mutation.
    """
    write_json(control_root, storage_options, f"{_PROJECTS_PREFIX}/{record['id']}.json", record)


#: The fields an idempotent re-POST OWNS. Everything else belongs to the record's own lifecycle and
#: is carried forward from the record AS IT STANDS AT WRITE TIME — the whole of diff2 F1(c)/F4.
_CALLER_OWNED = frozenset({"id"})


def upsert_project(control_root: str, storage_options: StorageOptions, record: dict[str, str]) -> dict[str, str]:
    """Idempotent re-create of an EXISTING project, CONDITIONAL on the record's ETag (diff2 F1 leg c).

    The same defect F4 closed on the warehouse rung, one rung up. The re-POST built a whole record
    from a read taken earlier in the handler and then `put_project`'d it unconditionally, so anything
    landing in between was silently discarded:

        t0  a GitOps re-POST of project `acme` reads the record       protected=false
        t1  an admin POSTs /v1/projects/acme/protection               protected=true
        t2  the re-POST's put lands                                   protected=false

    Deletion protection is disarmed with no `/protection` call and no audit signal — and `protected`
    is the flag the delete door's `force` rule turns on, so losing it silently is the one whose
    consequence is irreversible. Nothing in that sequence is a wrong decision by either writer; the
    re-POST is stale in a field it only carried forward, which is exactly why a guard on the DECISION
    cannot catch it and a guard on the WRITE can.

    `mutate_json` re-reads and re-applies on a lost race, so a concurrent protection change survives.
    Raises ``RecordMissingError`` if the record vanished (a concurrent delete — retryable, never a
    blind re-create).
    """

    def merge(live: dict[str, str]) -> dict[str, str]:
        merged = {**live, **{k: v for k, v in record.items() if k in _CALLER_OWNED}}
        # Identity fields belong to the ORIGINAL create; a re-POST never resets a tenant's age or author.
        merged["created_at"] = live.get("created_at") or record.get("created_at", "")
        merged["created_by"] = live.get("created_by") or record.get("created_by", "")
        # Absent on a record written before protection existed, and absent means unprotected.
        merged.setdefault("protected", "false")
        return merged

    return mutate_json(control_root, storage_options, f"{_PROJECTS_PREFIX}/{record['id']}.json", merge)


def create_project_record(control_root: str, storage_options: StorageOptions, record: dict[str, str]) -> None:
    """Mint ``_projects/<id>.json`` iff absent — the STORE arbitrates (F1).

    Raises :class:`service_kit.lakehouse.records.RecordExistsError` on a lost race; the caller
    re-reads and converges on the winner's identity fields (``created_at``/``created_by``), exactly
    like the sequential idempotent re-POST path.
    """
    create_json(control_root, storage_options, f"{_PROJECTS_PREFIX}/{record['id']}.json", record)


def get_project(control_root: str, storage_options: StorageOptions, project_id: str) -> dict[str, str] | None:
    """The project record, or ``None`` if the tenant does not exist."""
    return read_json(control_root, storage_options, f"{_PROJECTS_PREFIX}/{project_id}.json")


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
        if (valid := validated(record, ProjectRecord, event="project_record_malformed", path=info.path)) is not None:
            out.append(valid)
    return out
