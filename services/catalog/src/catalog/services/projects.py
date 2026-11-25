"""Project registry — a tenant EXISTS when a record says so, not when a warehouse implies it.

The estate used to keep no project records at all: a project "existed" exactly when a warehouse
record claimed it, and the FGA store carried tenant tuples independently. The two drifted the first
time anything wrote one without the other — the live estate held three projects in authz that the
catalog had never heard of, and the projects surface read empty while `/management/v1/me` listed memberships.

This registry is the fix: existence lives HERE, one JSON
record per tenant, and `POST /management/v1/projects` writes this record and the FGA tuples in one operation so
the two cannot disagree again. Same stateless-over-object-store shape as the warehouse registry —
`_projects/<id>.json` on the control root, no DB — and the same per-record corruption tolerance:
one unreadable record is skipped with a warning, never allowed to void the tenant listing.

All IO here is blocking; callers threadpool it.
"""

import json
import logging

import pyarrow.fs as pafs
from lance_namespace import InternalError

from catalog.services.control_records import ProtectionChange, read_json, with_manager, with_protection
from lakehouse.problems.outage import reads_as_store_outage, store_refusal
from lakehouse.records import control_prefixes
from lakehouse.records.project_records import ProjectRecord, UnreadableProjectRecordError
from lakehouse.records.project_records import read as read_project_record
from lakehouse.records.record_store import delete_record
from lakehouse.storage.conditional_records import RecordMissingError, create_json, mutate_json
from lakehouse.storage.objectfs import fs_and_base
from lakehouse.storage.options import StorageOptions


log = logging.getLogger(__name__)

_PROJECTS_PREFIX = control_prefixes.PROJECTS

#: The field a project's delete writes on its record before its first destructive step: the principal key of the caller
#: who started it. From then on the project is absent to every door but its delete, which that caller can finish though
#: the delete's revoke took their own admin grant ([[LH-576]]).
DELETING_BY = "deleting_by"


class ProjectBeingDeletedError(Exception):
    """The project's record names a delete that has not finished, so its id is not yet free for another tenant."""


#: The fields an idempotent re-POST OWNS. Everything else belongs to the record's own lifecycle and
#: is carried forward from the record AS IT STANDS AT WRITE TIME — the whole of diff2 F1(c)/F4.
_CALLER_OWNED = frozenset({"id"})


def _record(raw: object, *, path: str) -> ProjectRecord:
    """``raw`` as a project record, for a reader whose only other answer is "absent".

    A record nobody can parse refuses rather than reading as a missing tenant: `require_project_exists` gates warehouse
    creation on the answer and the project's delete acts on it, so "does not exist" is a verdict.
    `InternalError` (code 18), as every record that is there and cannot be read answers
    (`lakehouse.problems.outage.store_refusal`): only a write to the record clears it, so the caller is not told to try again.

    Raises:
        InternalError: The record cannot be read as a project's.
    """
    try:
        return read_project_record(raw, at=path)
    except UnreadableProjectRecordError as exc:
        log.warning("project_record_malformed", extra={"path": path, "error": str(exc)})
        raise InternalError(f"control record {path!r} could not be read as a project's — refusing rather than treating it as absent") from exc


def upsert_project(control_root: str, storage_options: StorageOptions, wanted: ProjectRecord) -> ProjectRecord:
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
    blind re-create), and :class:`ProjectBeingDeletedError` while a delete of the project stands unfinished.
    """

    record = wanted.stored()

    def merge(live: dict[str, str]) -> dict[str, str]:
        if live.get(DELETING_BY):
            raise ProjectBeingDeletedError(f"project {record['id']!r} is being deleted")
        merged = {**live, **{k: v for k, v in record.items() if k in _CALLER_OWNED}}
        # Identity fields belong to the ORIGINAL create; a re-POST never resets a tenant's age or author.
        merged["created_at"] = live.get("created_at") or record.get("created_at", "")
        merged["created_by"] = live.get("created_by") or record.get("created_by", "")
        return merged

    key = record_key(wanted.id)
    return _record(mutate_json(control_root, storage_options, key, mutate=merge), path=key)


def set_project_protection(control_root: str, storage_options: StorageOptions, project_id: str, *, change: ProtectionChange) -> ProjectRecord | None:
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
    key = record_key(project_id)
    try:
        return _record(mutate_json(control_root, storage_options, key, mutate=lambda record: with_protection(record, change)), path=key)
    except RecordMissingError:
        return None


def set_project_manager(control_root: str, storage_options: StorageOptions, project_id: str, *, manager: str | None) -> ProjectRecord | None:
    """Record who owns project ``project_id``'s declaration ([[XC-213]]): ``git``, or its own doors for ``None``.

    Conditional on the record's ETag, every other field as it stands at the write. ``None`` is answered when the
    record is gone.
    """
    key = record_key(project_id)
    try:
        return _record(mutate_json(control_root, storage_options, key, mutate=lambda record: with_manager(record, manager)), path=key)
    except RecordMissingError:
        return None


def create_project_record(control_root: str, storage_options: StorageOptions, record: ProjectRecord) -> None:
    """Mint ``_projects/<id>.json`` iff absent — the STORE arbitrates (F1).

    Raises :class:`lakehouse.storage.conditional_records.RecordExistsError` on a lost race; the caller
    re-reads and converges on the winner's identity fields (``created_at``/``created_by``), exactly
    like the sequential idempotent re-POST path.
    """
    create_json(control_root, storage_options, record_key(record.id), record=record.stored())


def record_key(project_id: str) -> str:
    """Where project ``project_id``'s record lives on the control root."""
    return f"{_PROJECTS_PREFIX}/{project_id}.json"


def get_project(control_root: str, storage_options: StorageOptions, project_id: str) -> ProjectRecord | None:
    """The project record, or ``None`` if the tenant does not exist or is being deleted ([[LH-576]]).

    A malformed record REFUSES rather than reading as a missing tenant: `require_project_exists` gates
    warehouse creation on this answer, and project delete acts on it, so "does not exist" is a verdict
    rather than an absence of information. A project whose delete has started is no tenant for any door
    but that delete's (:func:`get_project_record`), so no warehouse, role, grant or policy is made for it
    while its delete revokes and ends what it held."""
    record = get_project_record(control_root, storage_options, project_id)
    return None if record is None or record.is_being_deleted else record


def get_project_record(control_root: str, storage_options: StorageOptions, project_id: str) -> ProjectRecord | None:
    """The project's record as it stands, a delete it names included, or ``None`` when there is none: what the delete door
    reads to start or finish a delete. A malformed record refuses, as :func:`get_project` refuses it."""
    key = f"{_PROJECTS_PREFIX}/{project_id}.json"
    record = read_json(control_root, storage_options, key)
    return _record(record, path=key) if record is not None else None


def mark_deleting(control_root: str, storage_options: StorageOptions, project_id: str, *, by: str) -> ProjectRecord | None:
    """Record on the project's record that ``by`` started its delete, conditional on the record's ETag ([[LH-576]]).

    A delete another caller started first keeps its starter, so the record names one delete whoever asks again.

    Returns:
        The record as written, or ``None`` when it is gone: a delete finished meanwhile.
    """
    key = record_key(project_id)
    try:
        return _record(
            mutate_json(control_root, storage_options, key, mutate=lambda record: record if record.get(DELETING_BY) else {**record, DELETING_BY: by}), path=key
        )
    except RecordMissingError:
        return None


def delete_project_record(control_root: str, storage_options: StorageOptions, project_id: str) -> None:
    """Remove the registry record. Idempotent — deleting an absent record is a no-op, so the
    partial-failure retry path (record removed, tuple revoke failed, caller retries) cannot fail on
    its second pass."""
    delete_record(control_root, storage_options, f"{_PROJECTS_PREFIX}/{project_id}.json")


def read_projects(control_root: str, storage_options: StorageOptions) -> tuple[list[ProjectRecord], list[str]]:
    """``(readable project records, paths that are there and could NOT be read as one)``, unordered, a project being
    deleted among the records. An absent prefix yields ``([], [])``.

    The skipped paths are returned because the callers need opposite things from one tolerance, as a warehouse's do
    (:func:`catalog.services.warehouses.read_warehouses`): a listing survives one bad object, while a decision that no
    record names a project is a guess while one cannot be read. A record deleted since the listing named it is no
    record. A read the store did not answer raises, tolerant caller or strict.

    Raises:
        ServiceUnavailableError: The store did not answer a record's read.
    """
    fs, base = fs_and_base(control_root, storage_options)
    out: list[ProjectRecord] = []
    skipped: list[str] = []
    for info in fs.get_file_info(pafs.FileSelector(f"{base}/{_PROJECTS_PREFIX}", allow_not_found=True)):
        if info.type != pafs.FileType.File or not info.path.endswith(".json"):
            continue
        try:
            with fs.open_input_stream(info.path) as stream:
                record = json.loads(stream.readall().decode("utf-8"))
        except FileNotFoundError:
            continue
        except Exception as exc:
            if reads_as_store_outage(exc):
                raise store_refusal(exc, reading=f"reading {info.path}", so="the project registry cannot be read") from exc
            log.warning("project_record_unreadable", extra={"path": info.path, "error": str(exc)})
            skipped.append(info.path)
            continue
        try:
            out.append(read_project_record(record, at=info.path))
        except UnreadableProjectRecordError as exc:
            log.warning("project_record_malformed", extra={"path": info.path, "error": str(exc)})
            skipped.append(info.path)
    return out, skipped


def list_projects(control_root: str, storage_options: StorageOptions) -> list[ProjectRecord]:
    """Every readable project record (unordered) but those being deleted, which are no tenant (:func:`get_project`): the
    tolerant half of :func:`read_projects`, for a caller that only enumerates. An absent registry prefix yields ``[]``.

    One corrupt record is SKIPPED with a warning: the listing feeds the tenant gallery, and a single bad object voiding
    it would turn one tenant's registry corruption into an every-tenant outage. A read the store did not answer raises.
    A caller deciding from the registry that no record names a project reads :func:`read_projects` and its skipped paths.
    """
    return [record for record in read_projects(control_root, storage_options)[0] if not record.is_being_deleted]
