"""Project registry — a tenant EXISTS when a record says so, not when a warehouse implies it.

The estate used to keep no project records at all: a project "existed" exactly when a warehouse
record claimed it, and the FGA store carried tenant tuples independently. The two drifted the first
time anything wrote one without the other — the live estate held three projects in authz that the
catalog had never heard of, and the projects surface read empty while `/management/v1/me` listed memberships.

This registry is the fix (`open_hierarchy_lifecycle.md`, Decision 1): existence lives HERE, one JSON
record per tenant, and `POST /management/v1/projects` writes this record and the FGA tuples in one operation so
the two cannot disagree again. Same stateless-over-object-store shape as the warehouse registry —
`_projects/<id>.json` on the control root, no DB — and the same per-record corruption tolerance:
one unreadable record is skipped with a warning, never allowed to void the tenant listing.

All IO here is blocking; callers threadpool it.
"""

from __future__ import annotations

import logging

import pyarrow.fs as pafs

from catalog.services.control_records import (
    STATE,
    UUID,
    ProjectRecord,
    ProtectionChange,
    change_creation,
    read_json,
    replace_judged,
    validated,
    validated_or_refuse,
    with_manager,
    with_protection,
    write_json,
)
from lakehouse import control_prefixes
from lakehouse.governed import tenancy
from lakehouse.objectfs import fs_and_base
from lakehouse.record_store import load_json
from lakehouse.records import Stored, create_json
from lakehouse.storage_options import StorageOptions


log = logging.getLogger(__name__)

_PROJECTS_PREFIX = control_prefixes.PROJECTS


class ProjectBeingDeletedError(Exception):
    """The project's record names a delete that has not finished, so its id is not yet free for another tenant."""


def put_project(control_root: str, storage_options: StorageOptions, record: dict[str, str]) -> None:
    """Unconditionally overwrite ``_projects/<id>.json``. **A SEEDING primitive — no production caller.**

    It had two, and both were the diff2 F1(c) defect: an unconditional put of a record assembled from
    an earlier read silently discards whatever landed in between — on ``protected`` that disarms
    deletion protection with no `/protection` call and no audit signal. Those paths now go through
    :func:`upsert_project`, conditional on the record's ETag; the tenant MINT goes through
    :func:`create_project_record`, conditional on absence (F1).

    What is left is fixture setup, which has no concurrency to lose to. Do not reach for it in service
    or endpoint code; there is a conditional door for every real mutation. The record is written live with a fresh uuid
    unless it names its own (ADR 0245).
    """
    write_json(control_root, storage_options, f"{_PROJECTS_PREFIX}/{record['id']}.json", {UUID: tenancy.new_uuid(), STATE: tenancy.LIVE, **record})


#: The fields an idempotent re-POST OWNS. Everything else belongs to the record's own lifecycle and
#: is carried forward from the record AS IT STANDS AT WRITE TIME — the whole of diff2 F1(c)/F4.
_CALLER_OWNED = frozenset({"id"})


def upsert_project(control_root: str, storage_options: StorageOptions, record: dict[str, str], *, uuid: str) -> dict[str, str] | None:
    """Idempotent re-create of an EXISTING project, the creation ``uuid`` its re-POST read: conditional on the ETag of a
    read that finds that creation (diff2 F1 leg c, ADR 0245), so neither a protection change landing meanwhile nor a
    project made again under the id is written over.

    The same defect F4 closed on the warehouse rung, one rung up. The re-POST built a whole record
    from a read taken earlier in the handler and then `put_project`'d it unconditionally, so anything
    landing in between was silently discarded:

        t0  a GitOps re-POST of project `acme` reads the record       protected=false
        t1  an admin POSTs /management/v1/projects/acme/protection               protected=true
        t2  the re-POST's put lands                                   protected=false

    Deletion protection is disarmed with no `/protection` call and no audit signal — and `protected`
    is the flag the delete door's `force` rule turns on, so losing it silently is the one whose
    consequence is irreversible. Nothing in that sequence is a wrong decision by either writer; the
    re-POST is stale in a field it only carried forward, which is exactly why a guard on the DECISION
    cannot catch it and a guard on the WRITE can.

    Returns:
        The record written, ``None`` where the id no longer names the creation ``uuid`` (a concurrent delete, retryable,
        never a blind re-create).

    Raises:
        ProjectBeingDeletedError: A delete of the project stands unfinished.
    """

    def merge(live: dict[str, str]) -> dict[str, str]:
        if live.get(STATE) == tenancy.DELETING:
            raise ProjectBeingDeletedError(f"project {record['id']!r} is being deleted")
        merged = {**live, **{k: v for k, v in record.items() if k in _CALLER_OWNED}}
        # Identity fields belong to the ORIGINAL create; a re-POST never resets a tenant's age or author.
        merged["created_at"] = live.get("created_at") or record.get("created_at", "")
        merged["created_by"] = live.get("created_by") or record.get("created_by", "")
        return merged

    return change_creation(control_root, storage_options, record_key(record["id"]), uuid, merge)


def set_project_protection(
    control_root: str, storage_options: StorageOptions, project_id: str, change: ProtectionChange, *, uuid: str
) -> dict[str, str] | None:
    """Apply ``change`` to live project ``project_id``'s record where it names the creation ``uuid`` the caller was
    authorized on, under the ETag of the read that found it ([[LH-549]], ADR 0245): every other field is the record's as
    it stands at the write.

    Returns:
        The updated record, or ``None`` where the id no longer names that creation live: one deleted between the
        caller's authorization and this write, or made again under its id.
    """
    return change_creation(
        control_root, storage_options, record_key(project_id), uuid, lambda record: with_protection(record, change), states=frozenset({tenancy.LIVE})
    )


def set_project_manager(control_root: str, storage_options: StorageOptions, project_id: str, *, uuid: str, manager: str | None) -> dict[str, str] | None:
    """Record who owns live project ``project_id``'s declaration ([[XC-213]]): ``git``, or its own doors for ``None``.

    Conditional on a read that finds the creation ``uuid`` live, every other field as it stands at the write. ``None`` is
    answered where the id no longer names it.
    """
    return change_creation(
        control_root, storage_options, record_key(project_id), uuid, lambda record: with_manager(record, manager), states=frozenset({tenancy.LIVE})
    )


def create_project_record(control_root: str, storage_options: StorageOptions, record: dict[str, str]) -> None:
    """Mint ``_projects/<id>.json`` iff absent — the STORE arbitrates (F1). ``record`` names its creation's uuid and is
    ``creating`` (ADR 0245): nothing is written in OpenFGA before it stands.

    Raises :class:`lakehouse.records.RecordExistsError` on a lost race; the caller
    re-reads and converges on the winner's identity fields (``created_at``/``created_by``), exactly
    like the sequential idempotent re-POST path.
    """
    create_json(control_root, storage_options, record_key(record["id"]), record)


def record_key(project_id: str) -> str:
    """Where project ``project_id``'s record lives on the control root."""
    return f"{_PROJECTS_PREFIX}/{project_id}.json"


def get_project(control_root: str, storage_options: StorageOptions, project_id: str) -> dict[str, str] | None:
    """The project record, or ``None`` if the tenant does not exist, is still being created, or is being deleted
    ([[LH-576]], ADR 0245).

    A malformed record REFUSES rather than reading as a missing tenant: `require_project_exists` gates
    warehouse creation on this answer, and project delete acts on it, so "does not exist" is a verdict
    rather than an absence of information. A project whose delete has started is no tenant for any door
    but that delete's (:func:`get_project_record`), so no warehouse, role, grant or policy is made for it
    while its delete revokes and ends what it held."""
    record = get_project_record(control_root, storage_options, project_id)
    return None if record is None or record.get(STATE) != tenancy.LIVE else record


def get_project_record(control_root: str, storage_options: StorageOptions, project_id: str) -> dict[str, str] | None:
    """The project's record as it stands, a delete it names included, or ``None`` when there is none: what the delete
    door reads to start or finish a delete. A malformed record refuses, as :func:`get_project` refuses it."""
    key = record_key(project_id)
    record = read_json(control_root, storage_options, key)
    valid = validated_or_refuse(record, ProjectRecord, event="project_record_malformed", path=key) if record is not None else None
    return valid


def mark_deleting(control_root: str, storage_options: StorageOptions, project_id: str, judged: Stored, marked: dict[str, str]) -> bool:
    """Write ``marked``, the project's record as its delete marks it (``tenancy.mark``), only while the record is still
    the one ``judged`` read ([[LH-576]], ADR 0245): whether it landed."""
    return replace_judged(control_root, storage_options, record_key(project_id), judged, marked)


def set_live(control_root: str, storage_options: StorageOptions, project_id: str, uuid: str) -> dict[str, str] | None:
    """Flip project ``project_id``'s record from ``creating`` to ``live`` where it still names the creation ``uuid``,
    conditional on its ETag: its seed stands by then (ADR 0245). The record as it stands, ``None`` where it is gone,
    another creation's, or no longer creating or live."""

    def flip(record: dict[str, str]) -> dict[str, str]:
        return {**record, STATE: tenancy.LIVE} if record.get(STATE) == tenancy.CREATING else record

    return change_creation(control_root, storage_options, record_key(project_id), uuid, flip, states=frozenset({tenancy.CREATING, tenancy.LIVE}))


def read_projects(control_root: str, storage_options: StorageOptions) -> tuple[list[dict[str, str]], list[str]]:
    """Every readable project record (unordered), those being created and deleted included, and the path of each that is
    there and cannot be read as one. An absent registry prefix yields nothing."""
    fs, base = fs_and_base(control_root, storage_options)
    out: list[dict[str, str]] = []
    unreadable: list[str] = []
    for info in fs.get_file_info(pafs.FileSelector(f"{base}/{_PROJECTS_PREFIX}", allow_not_found=True)):
        if info.type != pafs.FileType.File or not info.path.endswith(".json"):
            continue
        try:
            record = load_json(fs, info.path)
        except Exception as exc:
            log.warning("project_record_unreadable", extra={"path": info.path, "error": str(exc)})
            unreadable.append(info.path)
            continue
        if (valid := validated(record, ProjectRecord, event="project_record_malformed", path=info.path)) is None:
            unreadable.append(info.path)
        else:
            out.append(valid)
    return out, unreadable


def list_projects(control_root: str, storage_options: StorageOptions) -> list[dict[str, str]]:
    """Every readable live project record (unordered): one being created or deleted is no tenant (:func:`get_project`).
    An absent registry prefix yields ``[]``.

    One corrupt or unreadable record is SKIPPED with a warning (mirroring ``list_warehouses``):
    the listing feeds the tenant gallery and the existence guard, and a single bad object voiding
    it would turn one tenant's registry corruption into an every-tenant outage.
    """
    return [record for record in read_projects(control_root, storage_options)[0] if record.get(STATE) == tenancy.LIVE]
