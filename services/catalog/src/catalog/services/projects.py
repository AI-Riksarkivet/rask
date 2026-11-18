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

import json
import logging
from contextlib import suppress
from datetime import UTC, datetime

import pyarrow.fs as pafs

from catalog.services.control_records import (
    STATE,
    UUID,
    ProjectRecord,
    ProtectionChange,
    end_creation,
    read_json,
    validated,
    validated_or_refuse,
    with_manager,
    with_protection,
    write_json,
)
from lakehouse import control_prefixes
from lakehouse.governed import tenancy
from lakehouse.objectfs import fs_and_base
from lakehouse.records import RecordMissingError, create_json, mutate_json
from lakehouse.storage_options import StorageOptions


log = logging.getLogger(__name__)

_PROJECTS_PREFIX = control_prefixes.PROJECTS

#: The field a project's delete writes on its record before its first destructive step, beside its state ``deleting``:
#: the principal key of the caller who started it. From then on the project is absent to every door but its delete, which
#: that caller can finish though the delete's revoke took their own admin grant ([[LH-576]]).
DELETING_BY = "deleting_by"


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


def upsert_project(control_root: str, storage_options: StorageOptions, record: dict[str, str]) -> dict[str, str]:
    """Idempotent re-create of an EXISTING project, CONDITIONAL on the record's ETag (diff2 F1 leg c).

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

    `mutate_json` re-reads and re-applies on a lost race, so a concurrent protection change survives.
    Raises ``RecordMissingError`` if the record vanished (a concurrent delete — retryable, never a
    blind re-create), and :class:`ProjectBeingDeletedError` while a delete of the project stands unfinished. The uuid
    and the state are the record's own, which no re-POST changes (ADR 0245).
    """

    def merge(live: dict[str, str]) -> dict[str, str]:
        if live.get(STATE) == tenancy.DELETING:
            raise ProjectBeingDeletedError(f"project {record['id']!r} is being deleted")
        merged = {**live, **{k: v for k, v in record.items() if k in _CALLER_OWNED}}
        # Identity fields belong to the ORIGINAL create; a re-POST never resets a tenant's age or author.
        merged["created_at"] = live.get("created_at") or record.get("created_at", "")
        merged["created_by"] = live.get("created_by") or record.get("created_by", "")
        return merged

    return mutate_json(control_root, storage_options, f"{_PROJECTS_PREFIX}/{record['id']}.json", merge)


def set_project_protection(control_root: str, storage_options: StorageOptions, project_id: str, change: ProtectionChange) -> dict[str, str] | None:
    """Apply ``change`` to project ``project_id``'s record, conditional on its ETag ([[LH-549]]).

    Every other field is the record's as it stands at the write (``mutate_json``).

    Args:
        control_root: The registry's root.
        storage_options: How to reach it.
        project_id: The project to change.
        change: What to apply.

    Returns:
        The updated record, or ``None`` when the project's record is gone: a project deleted between the caller's
        authorization and this write.
    """
    try:
        return mutate_json(control_root, storage_options, f"{_PROJECTS_PREFIX}/{project_id}.json", lambda record: with_protection(record, change))
    except RecordMissingError:
        return None


def set_project_manager(control_root: str, storage_options: StorageOptions, project_id: str, *, manager: str | None) -> dict[str, str] | None:
    """Record who owns project ``project_id``'s declaration ([[XC-213]]): ``git``, or its own doors for ``None``.

    Conditional on the record's ETag, every other field as it stands at the write. ``None`` is answered when the
    record is gone.
    """
    try:
        return mutate_json(control_root, storage_options, f"{_PROJECTS_PREFIX}/{project_id}.json", lambda record: with_manager(record, manager))
    except RecordMissingError:
        return None


def create_project_record(control_root: str, storage_options: StorageOptions, record: dict[str, str]) -> None:
    """Mint ``_projects/<id>.json`` iff absent — the STORE arbitrates (F1). ``record`` names its creation's uuid and is
    ``creating`` (ADR 0245): nothing is written in OpenFGA before it stands.

    Raises :class:`lakehouse.records.RecordExistsError` on a lost race; the caller
    re-reads and converges on the winner's identity fields (``created_at``/``created_by``), exactly
    like the sequential idempotent re-POST path.
    """
    create_json(control_root, storage_options, f"{_PROJECTS_PREFIX}/{record['id']}.json", record)


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
    """The project's record as it stands, a delete it names included, or ``None`` when there is none: what the delete door
    reads to start or finish a delete. A malformed record refuses, as :func:`get_project` refuses it."""
    key = f"{_PROJECTS_PREFIX}/{project_id}.json"
    record = read_json(control_root, storage_options, key)
    return validated_or_refuse(record, ProjectRecord, event="project_record_malformed", path=key) if record is not None else None


def mark_deleting(control_root: str, storage_options: StorageOptions, project_id: str, *, by: str) -> dict[str, str] | None:
    """Mark the project's record ``deleting`` and record that ``by`` started its delete, conditional on the record's
    ETag ([[LH-576]]): from here no door resolves it (ADR 0245).

    A delete another caller started first keeps its starter, so the record names one delete whoever asks again.

    Returns:
        The record as written, or ``None`` when it is gone: a delete finished meanwhile.
    """
    try:
        return mutate_json(
            control_root,
            storage_options,
            f"{_PROJECTS_PREFIX}/{project_id}.json",
            lambda record: record if record.get(DELETING_BY) else {**record, STATE: tenancy.DELETING, DELETING_BY: by},
        )
    except RecordMissingError:
        return None


def set_live(control_root: str, storage_options: StorageOptions, project_id: str, uuid: str) -> dict[str, str] | None:
    """Flip project ``project_id``'s record from ``creating`` to ``live`` where it still names the creation ``uuid``,
    conditional on its ETag: its seed stands by then (ADR 0245). The record as it stands, ``None`` where it is gone or
    another creation's."""

    def flip(record: dict[str, str]) -> dict[str, str]:
        if record.get(UUID) != uuid:
            raise RecordMissingError(f"project {project_id!r} is not the creation {uuid}")
        return {**record, STATE: tenancy.LIVE} if record.get(STATE) == tenancy.CREATING else record

    try:
        return mutate_json(control_root, storage_options, f"{_PROJECTS_PREFIX}/{project_id}.json", flip)
    except RecordMissingError:
        return None


def end_project(control_root: str, storage_options: StorageOptions, record: dict[str, str]) -> None:
    """End the creation ``record`` names, its delete's last step: kept as ended (``control_records.end_creation``) and
    its registry record removed, which frees its id. Idempotent, so a delete asked again after this step finishes."""
    end_creation(control_root, storage_options, tenancy.PROJECT, record, now=datetime.now(UTC))
    fs, base = fs_and_base(control_root, storage_options)
    with suppress(FileNotFoundError):
        fs.delete_file(f"{base}/{_PROJECTS_PREFIX}/{record['id']}.json")


def read_projects(control_root: str, storage_options: StorageOptions) -> tuple[list[dict[str, str]], list[str]]:
    """Every readable project record (unordered), those being deleted included, and the path of each that is there and
    cannot be read as one. An absent registry prefix yields nothing."""
    fs, base = fs_and_base(control_root, storage_options)
    out: list[dict[str, str]] = []
    unreadable: list[str] = []
    for info in fs.get_file_info(pafs.FileSelector(f"{base}/{_PROJECTS_PREFIX}", allow_not_found=True)):
        if info.type != pafs.FileType.File or not info.path.endswith(".json"):
            continue
        try:
            with fs.open_input_stream(info.path) as stream:
                record = json.loads(stream.readall().decode("utf-8"))
        except Exception as exc:
            log.warning("project_record_unreadable", extra={"path": info.path, "error": str(exc)})
            unreadable.append(info.path)
            continue
        if (valid := validated(record, ProjectRecord, event="project_record_malformed", path=info.path)) is not None:
            out.append(valid)
        else:
            unreadable.append(info.path)
    return out, unreadable


def list_projects(control_root: str, storage_options: StorageOptions) -> list[dict[str, str]]:
    """Every readable live project record (unordered): one being created or deleted is no tenant (:func:`get_project`).
    An absent registry prefix yields ``[]``.

    One corrupt or unreadable record is SKIPPED with a warning (mirroring ``list_warehouses``):
    the listing feeds the tenant gallery and the existence guard, and a single bad object voiding
    it would turn one tenant's registry corruption into an every-tenant outage.
    """
    return [record for record in read_projects(control_root, storage_options)[0] if record.get(STATE) == tenancy.LIVE]
