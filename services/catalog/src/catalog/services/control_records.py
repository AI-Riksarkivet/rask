"""Read/write ONE JSON document on the control root — the primitive under every catalog registry.

The warehouse registry and the project registry are the same shape: one JSON object per record, at a plain (unhashed) key under the control root, no DB. The two
IO halves lived as ``warehouses._read_json`` / ``warehouses._write_json``, and the project registry
imported both across the module boundary (CAT-CORE-14) — which made "private" a lie and welded the two
registries together over a helper that is about neither of them.

They are here because they are about neither: "one JSON document on the control root" is the shared
concept, and both registries now import it under a public name. Conditional writes (mint-iff-absent,
ETag-guarded mutate) stay in ``lakehouse.records``; these two are the unconditional pair
those build on.

All IO here is BLOCKING; callers threadpool it.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from contextlib import suppress
from datetime import datetime
from enum import StrEnum
from typing import Any, Final, Self

import pyarrow.fs as pafs
from lance_namespace import ServiceUnavailableError
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from lakehouse import control_prefixes
from lakehouse.governed import tenancy
from lakehouse.objectfs import fs_and_base
from lakehouse.protection import is_protected
from lakehouse.record_store import load_json
from lakehouse.records import RecordChangedError, RecordMissingError, Stored, mutate_json, read_json_at, remove_json, replace_json
from lakehouse.storage_options import StorageOptions


log = logging.getLogger(__name__)

#: The field of a project's or warehouse's record naming the uuid its creation minted, which OpenFGA names it by, and the
#: field of a record belonging to a project naming the uuid of that project's creation (ADR 0245).
UUID: Final = tenancy.UUID
PROJECT_UUID: Final = tenancy.PROJECT_UUID
#: The field naming where a project's or warehouse's record is in its life (``creating``, ``live``, ``deleting``).
STATE: Final = tenancy.STATE


def write_json(root_uri: str, storage_options: StorageOptions, key: str, record: dict[str, str]) -> None:
    """Unconditionally write ``record`` at ``key``, creating the prefix first.

    UNCONDITIONAL, which is why the registries only reach for it on paths with no concurrency to lose
    to (seeding, fixtures): a put assembled from an earlier read silently discards whatever landed in
    between. Every real mutation goes through ``lakehouse.records``' conditional doors.
    """
    fs, base = fs_and_base(root_uri, storage_options)
    parent = f"{base}/{key}".rsplit("/", 1)[0]
    fs.create_dir(parent, recursive=True)  # local FS needs the parent dir; an S3 prefix marker is harmless
    with fs.open_output_stream(f"{base}/{key}") as stream:
        stream.write(json.dumps(record).encode("utf-8"))


def read_json(root_uri: str, storage_options: StorageOptions, key: str) -> dict[str, str] | None:
    """The record at ``key``, or ``None`` when nothing is stored there."""
    fs, base = fs_and_base(root_uri, storage_options)
    try:
        return load_json(fs, f"{base}/{key}")
    except FileNotFoundError:
        return None


# --------------------------------------------------------------------------- #
# The DECLARED shape of each registry record (CAT-CORE-12).
#
# All three listings used to parse into a raw dict and hand-check it with `.get()` truthiness, which
# answers "present and non-empty" and never "the right type". Every consumer treats these fields as
# strings — the bucket-claim guard compares `record["bucket"] == bucket`, the resolver feeds
# `root_uri` straight into a namespace connection — so a record whose `bucket` is a list passed the
# guard, was returned as live, and then matched no claim at all. That is the wrong direction to fail
# in on a guard whose job is refusing a second project's claim on somebody's bucket.
#
# `extra="allow"` because these records carry lifecycle fields the identity check has no business
# knowing about (`status`, `protected`, `primary`, `created_at`, …) and a model that dropped them
# would silently disarm a quarantine. Only the fields every consumer keys on are declared.
# --------------------------------------------------------------------------- #


class DeleteMode(StrEnum):
    """What a drop under a warehouse does (ADR 0146), as its record's ``delete_mode`` keeps it."""

    #: The table and its data are deleted at once.
    HARD = "hard"
    #: The table is deregistered and kept recoverable for the record's ``trash_grace_days``.
    SOFT = "soft"
    #: The warehouse names no profile of its own and takes the estate's grace period; a request naming it clears one,
    #: so a declaration dropping its profile converges back to the estate's.
    ESTATE = "estate"


class _Record(BaseModel):
    """Base for the registry record shapes: identity is validated, everything else rides along."""

    model_config = ConfigDict(extra="allow")


class WarehouseRecord(_Record):
    """A warehouse: one physical S3 bucket owned by one project."""

    id: str = Field(min_length=1)
    bucket: str = Field(min_length=1)
    project: str = Field(min_length=1)
    #: The uuid this warehouse's creation minted, which OpenFGA names it by, and the uuid of the creation of its project
    #: it was made in (ADR 0245): a record naming either not is not one this catalog wrote, and does not parse.
    uuid: str = Field(pattern=rf"^{tenancy.UUID_PATTERN}$")
    project_uuid: str = Field(pattern=rf"^{tenancy.UUID_PATTERN}$")
    state: tenancy.State
    #: The warehouse's delete profile (ADR 0146); absent where it takes the estate's grace period. Declared because a
    #: drop under the warehouse acts on it, so a record whose profile cannot be read is refused at the read rather than
    #: read as some other profile.
    delete_mode: DeleteMode | None = None
    trash_grace_days: str | None = None

    @model_validator(mode="after")
    def _profile_is_whole(self) -> Self:
        if (self.delete_mode is DeleteMode.SOFT) != (self.trash_grace_days is not None):
            raise ValueError("a soft delete profile keeps its trash_grace_days, and no other profile keeps any")
        if self.trash_grace_days is not None and not 0 < float(self.trash_grace_days) <= 365:
            raise ValueError(f"trash_grace_days {self.trash_grace_days!r} is not between 0 and 365 days")
        return self


class BucketClaimRecord(_Record):
    """Which PROJECT owns a bucket, written once so the store arbitrates rival claims.

    Keyed by bucket and carrying the project rather than the warehouse id, because one project
    legitimately backs several warehouses with one bucket (one project's several warehouses on one bucket) — the same asymmetry
    `projects_claiming_bucket` encodes by subtracting the caller's own project.
    """

    bucket: str = Field(min_length=1)
    project: str = Field(min_length=1)
    #: The uuid of the creation of the project that claimed it (:data:`PROJECT_UUID`): a tenant made again under the
    #: project's id is another claimant.
    project_uuid: str = Field(pattern=rf"^{tenancy.UUID_PATTERN}$")
    warehouse_id: str = Field(min_length=1)


class ProjectRecord(_Record):
    """A tenant. Existence lives in the record, so the id is the whole of its identity."""

    id: str = Field(min_length=1)
    #: The uuid this project's creation minted, which OpenFGA names it by (ADR 0245).
    uuid: str = Field(pattern=rf"^{tenancy.UUID_PATTERN}$")
    state: tenancy.State
    #: The person seeded as its admin, which the converge tick writes where the create died before its seed; ``None``
    #: with authorization off, where nothing is seeded.
    admin: str | None = None
    #: The principal key of the caller who started its delete, while that delete is unfinished ([[LH-576]]).
    deleting_by: str | None = Field(default=None, min_length=1)


#: The record fields deletion protection writes on a warehouse or project record: the flag
#: `fga_deps.require_not_protected` reads, and who armed it.
PROTECTION_FIELDS = ("protected", "protected_by")


class ProtectionChange(BaseModel):
    """A request to arm or clear the deletion protection a warehouse or project record carries ([[LH-548]], [[LH-549]]).

    Attributes:
        protected: Arm the protection when true, clear it when false.
        by: The principal making the change, ``user:<subject>``, recorded as ``protected_by`` when it arms.
    """

    model_config = ConfigDict(frozen=True)

    protected: bool
    by: str


#: The record field naming who owns a project's or warehouse's declaration, and its one value ([[XC-213]], ADR 0206):
#: absent, its doors do.
MANAGED_BY = "managed_by"
GIT = "git"


def with_manager(record: dict[str, str], manager: str | None) -> dict[str, str]:
    """``record`` as managed by ``manager``, or by its own doors for ``None``, which carries no such field."""
    unmanaged = {key: value for key, value in record.items() if key != MANAGED_BY}
    return unmanaged if manager is None else {**unmanaged, MANAGED_BY: manager}


def with_protection(record: dict[str, str], change: ProtectionChange) -> dict[str, str]:
    """Apply ``change`` to a warehouse or project record.

    Arming an armed record leaves it as it is, so it keeps naming who armed it and a declaration re-applied on every
    reconcile writes nothing new.

    Args:
        record: The record as read.
        change: What to apply.

    Returns:
        A new record: armed carries the string ``"true"`` beside who armed it, and cleared carries neither field, as a
        record never armed carries neither.
    """
    if change.protected and is_protected(record):
        return dict(record)
    unprotected = {key: value for key, value in record.items() if key not in PROTECTION_FIELDS}
    return {**unprotected, "protected": "true", "protected_by": change.by} if change.protected else unprotected


def validated_or_refuse(record: object, model: type[_Record], *, event: str, path: str) -> dict[str, str]:
    """``record`` as a plain dict if it matches ``model``, else REFUSE — the strict sibling of
    :func:`validated`, for a reader that has no skip list to hand back.

    THE TOLERANT FORM CANNOT SERVE A SINGULAR READ, and the difference is not strictness for its own
    sake. `validated` answers ``None`` on a malformed record, which is exactly right for a listing —
    one tenant's corruption must not void an estate-wide result, and the destructive callers get their
    fail-closed behaviour from being told WHICH paths were skipped. A singular reader has no such
    channel: its only return value is the record, so ``None`` there does not mean "unreadable", it
    means ABSENT, and the caller acts on that.

    THE CONSEQUENCE IS NOT SYMMETRIC BETWEEN CALLERS, which is why this raises rather than leaving each
    one to decide. For a warehouse's own doors an absent `get_warehouse` is a 404 and fail-closed. For the
    router it is "no such warehouse" ([[LH-593]]), and a name whose warehouse is absent is answered as absent,
    so a record nobody can parse would silently answer every one of a tenant's names as not there.

    `ServiceUnavailableError` and not a new type, so one unreadable record answers the same way whichever
    door reached it.
    """
    try:
        return model.model_validate(record).model_dump(mode="json", exclude_unset=True)
    except ValidationError as exc:
        log.warning(event, extra={"path": path, "error": exc.errors(include_url=False)})
        raise ServiceUnavailableError(f"control record {path!r} could not be read as {model.__name__} — refusing rather than treating it as absent") from exc


def validated(record: object, model: type[_Record], *, event: str, path: str) -> dict[str, str] | None:
    """``record`` as a plain dict if it matches ``model``, else ``None`` with a warning.

    TOLERANT BY DESIGN, and the tolerance is the point: these registries feed listings whose own
    docstrings promise that one bad object never voids the whole result (a single tenant's corruption
    would otherwise be an estate-wide control-plane outage). So a refusal is a SKIP, logged under the
    caller's own event name, never an exception — the destructive callers get their fail-closed
    behaviour from being told WHICH paths were skipped, not from this raising.

    Returns the validated dump rather than the input, so a consumer can rely on the declared fields
    being the declared types.
    """
    try:
        return model.model_validate(record).model_dump(mode="json", exclude_unset=True)
    except ValidationError as exc:
        log.warning(event, extra={"path": path, "error": exc.errors(include_url=False)})
        return None


class DeleteAsked(BaseModel):
    """What a delete was asked to do, as its point of no return keeps it (ADR 0245, fourth review): written once, by the
    delete that passed the stamp, and never read from a later caller. A delete asked again over the stamp, or the converge
    tick, finishes exactly this: it purges a bucket only where the delete that passed the stamp was asked to, and the
    ``force`` that turned the protection it judged before the stamp is kept with it, though past the stamp no protection
    is judged again."""

    model_config = ConfigDict(frozen=True)

    cascade: bool = False
    purge_bucket: bool = False
    force: bool = False


class EndedCreation(BaseModel):
    """A project's or a warehouse's creation its delete ended (ADR 0245): kept until the converge tick has swept what a
    write racing the delete left on its object, then forgotten.

    A creation a delete ended names the delete by the run that passed its point of no return (``no_return``) and what it
    was asked, and a warehouse's whether its bucket's purge was done: the converge tick releases a bucket's claim stamped
    ``purging`` by that run, once the record is gone, as purged only where this says the purge was done
    (``tenancy_converge._finish_purge``)."""

    model_config = ConfigDict(frozen=True)

    kind: tenancy.Kind
    id: str
    uuid: str = Field(pattern=rf"^{tenancy.UUID_PATTERN}$")
    ended_at: str
    #: Set by the first sweep that found nothing on the object; the sweep past the converge tick's bound forgets it.
    swept_at: str | None = None
    #: The run that passed the delete's point of no return; ``None`` for a creation that never went live (its create ended).
    no_return: str | None = None
    asked: DeleteAsked | None = None
    #: For a warehouse whose delete was asked to purge its bucket: whether every byte of it went (``False``: the purge
    #: stood down and kept them). ``None`` where no purge was asked.
    bucket_purged: bool | None = None


class EndedBy(BaseModel):
    """The delete that ends a creation, as its ended-creation record names it."""

    model_config = ConfigDict(frozen=True)

    no_return: str
    asked: DeleteAsked
    bucket_purged: bool | None = None


def ended_key(kind: tenancy.Kind, uuid: str) -> str:
    """Where the ended creation ``uuid`` of a ``kind`` is kept."""
    return f"{control_prefixes.ENDED}/{kind}/{uuid}.json"


def end_creation(
    root_uri: str, storage_options: StorageOptions, kind: tenancy.Kind, object_id: str, uuid: str, *, now: datetime, by: EndedBy | None = None
) -> None:
    """Keep creation ``uuid`` of ``object_id`` as ended, by the delete ``by`` names where a delete ended it, before its
    registry record is removed, so the converge tick sweeps its object whatever wrote on it after the delete read it.
    Unconditional: the key is the uuid's, which no other creation names. Blocking."""
    ended = EndedCreation(kind=kind, id=object_id, uuid=uuid, ended_at=now.isoformat(), **(by.model_dump() if by is not None else {}))
    write_json(root_uri, storage_options, ended_key(kind, ended.uuid), ended.model_dump(mode="json", exclude_none=True))


def ended_creation(root_uri: str, storage_options: StorageOptions, kind: tenancy.Kind, uuid: str) -> EndedCreation | None:
    """The ended creation ``uuid`` of a ``kind``, as kept; ``None`` where none is kept (never ended, or forgotten).

    Raises:
        ServiceUnavailableError: It is there and cannot be read as one.
    """
    key = ended_key(kind, uuid)
    try:
        stored = read_json(root_uri, storage_options, key)
        return None if stored is None else EndedCreation.model_validate(stored)
    except ValueError as exc:
        raise ServiceUnavailableError(f"ended creation {key!r} could not be read as one") from exc


def read_ended(root_uri: str, storage_options: StorageOptions) -> tuple[list[EndedCreation], list[str]]:
    """Every ended creation kept, and the path of each that is there and cannot be read as one. Blocking."""
    fs, base = fs_and_base(root_uri, storage_options)
    out: list[EndedCreation] = []
    unreadable: list[str] = []
    for info in fs.get_file_info(pafs.FileSelector(f"{base}/{control_prefixes.ENDED}", allow_not_found=True, recursive=True)):
        if info.type != pafs.FileType.File or not info.path.endswith(".json"):
            continue
        try:
            out.append(EndedCreation.model_validate(load_json(fs, info.path)))
        except FileNotFoundError:
            continue
        except (ValidationError, ValueError, OSError) as exc:
            log.warning("ended_creation_unreadable", extra={"path": info.path, "error": str(exc)[:300]})
            unreadable.append(info.path)
    return out, unreadable


def mark_swept(root_uri: str, storage_options: StorageOptions, ended: EndedCreation, *, now: datetime) -> None:
    """Record that a sweep of ``ended``'s object found nothing standing. Blocking."""
    swept = ended.model_copy(update={"swept_at": now.isoformat()})
    write_json(root_uri, storage_options, ended_key(ended.kind, ended.uuid), swept.model_dump(mode="json", exclude_none=True))


def forget_ended(root_uri: str, storage_options: StorageOptions, ended: EndedCreation) -> None:
    """Forget ``ended`` once no write can still land on its object. Idempotent. Blocking."""
    fs, base = fs_and_base(root_uri, storage_options)
    with suppress(FileNotFoundError):
        fs.delete_file(f"{base}/{ended_key(ended.kind, ended.uuid)}")


# --------------------------------------------------------------------------- #
# THE ONE CONDITIONAL WAY a project's or a warehouse's record is changed or removed (ADR 0245, reviews T2 and S3).
#
# A record is keyed by its id, and a delete frees the id for another tenant's creation. A change decided on one creation
# must therefore never land on the next: every change is a write under the ETag of a read that found the creation's
# uuid, and its removal is ONE DeleteObject under the ETag of a read that found it (`lakehouse.records.remove_json`), so a
# writer paused between its read and its removal removes nothing another creation wrote at the key since.
# --------------------------------------------------------------------------- #

#: The fields a delete writes on its record: the principal who started it (``deleting_by``); the token of the request
#: whose mark stands (``delete_run``) and when that mark was written (``marked_at``: the converge tick settles a mark
#: whose run is older than its bound); its point of no return (``no_return``, the run that stamped it: stamped before its
#: first irreversible step; a failure before it puts the record back live); and what the delete was asked to do
#: (``delete_asked``, stamped with ``no_return``), which the tick finishes a stamped delete by and never guesses.
DELETING_BY: Final = "deleting_by"
DELETE_RUN: Final = "delete_run"
MARKED_AT: Final = "marked_at"
NO_RETURN: Final = "no_return"
DELETE_ASKED: Final = "delete_asked"
#: Every field a delete writes, which a record put back live carries none of.
DELETE_FIELDS: Final = (DELETING_BY, DELETE_RUN, MARKED_AT, NO_RETURN, DELETE_ASKED)


class NotTheCreation(Exception):
    """The record a change was asked of is not the creation the caller holds: gone, another creation's, or in a state the
    change does not act on."""


def read_creation(root_uri: str, storage_options: StorageOptions, key: str) -> Stored | None:
    """The record at ``key`` with the ETag a change judged on this read is conditional on; ``None`` when absent. Blocking."""
    return read_json_at(root_uri, storage_options, key).stored


def change_creation(
    root_uri: str,
    storage_options: StorageOptions,
    key: str,
    uuid: str,
    mutate: Callable[[dict[str, Any]], dict[str, Any]],
    *,
    states: frozenset[str] = frozenset({tenancy.CREATING, tenancy.LIVE, tenancy.DELETING}),
) -> dict[str, Any] | None:
    """Apply ``mutate`` to the record at ``key`` where it names the creation ``uuid`` in one of ``states``, under the ETag
    of the read that found it (``mutate_json``); the record written, ``None`` where it is not that creation. ``mutate``
    may raise :class:`NotTheCreation` to refuse, and is pure: it runs once per read. Blocking."""

    def guarded(record: dict[str, Any]) -> dict[str, Any]:
        if record.get(UUID) != uuid or record.get(STATE) not in states:
            raise NotTheCreation(f"{key} does not hold creation {uuid} in {sorted(states)}")
        return mutate(record)

    try:
        return mutate_json(root_uri, storage_options, key, guarded)
    except (RecordMissingError, NotTheCreation):
        return None


def replace_judged(root_uri: str, storage_options: StorageOptions, key: str, judged: Stored, record: dict[str, Any]) -> bool:
    """Write ``record`` over the record ``judged`` was read as, only while it is still that record (its ETag); whether it
    landed. For a write decided on a judgement of the whole record, such as a delete's mark after its checks. Blocking."""
    try:
        replace_json(root_uri, storage_options, key, record, etag=judged.etag)
    except RecordChangedError:
        return False
    return True


def end_record(
    root_uri: str,
    storage_options: StorageOptions,
    kind: tenancy.Kind,
    key: str,
    object_id: str,
    uuid: str,
    *,
    now: datetime,
    run: str | None = None,
    by: EndedBy | None = None,
    states: frozenset[str] = frozenset({tenancy.DELETING}),
) -> bool:
    """End creation ``uuid`` of ``object_id``, whose record is at ``key``: kept under ``_ended/`` for the tick's sweep, then
    its record removed by ONE DeleteObject under the ETag of a read that found it in one of ``states`` (marked
    ``deleting`` by the delete ``run`` where one is named, which ``by`` names as the ended creation keeps it; ``creating``,
    for a create abandoning its own record or the converge tick ending one a dead create left), which frees its id.
    Whether this call removed it. Blocking.

    The creation is kept first, so one whose removal fails is still swept once it goes; the sweep leaves a creation whose
    record still stands (``tenancy_converge``), so keeping one that this call then does not remove is harmless."""

    def ends(record: dict[str, Any]) -> bool:
        return record.get(UUID) == uuid and record.get(STATE) in states and (run is None or record.get(DELETE_RUN) == run)

    end_creation(root_uri, storage_options, kind, object_id, uuid, now=now, by=by)
    return remove_json(root_uri, storage_options, key, ends) is not None
