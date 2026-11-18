"""A project's roles, kept under the control root ([[LH-557]]).

A role exists while its record stands unended, as a project exists while its record does (`projects.py`): the role
doors write the role's ``project`` edge in OpenFGA and its record together. One record per role name at
``_roles/<project>/<blake2s(name)>.json``: keyed by the name, so the store arbitrates a project's names, and a digest
because a name may hold any character a name may (ADR 0173 §4), which an object key cannot carry as written. The record
holds the role's uuid, its OpenFGA object (``role:<uuid>``), so a role made again under a name an ended role held is
another object and inherits nothing.

A record is never deleted. Ending a role replaces its record with the same record stamped ``ended_at``, under the ETag
of the read that found it (the release rule of the location claims, ADR 0172): of two requests ending one role and a
create taking its name, none removes a role it did not read. A create takes an ended record's key the same way, and
refuses a name a standing role holds. A project holds few roles, so a role is found by its uuid by listing them.

All IO here is blocking; callers threadpool it.
"""

from __future__ import annotations

import json
import logging
import uuid
from datetime import UTC, datetime
from hashlib import blake2s
from typing import Final

import pyarrow.fs as pafs
from lance_namespace import InternalError
from pydantic import Field, ValidationError

from catalog.schemas import RoleRecord
from lakehouse import control_prefixes
from lakehouse.objectfs import fs_and_base
from lakehouse.outage import reads_as_store_outage, store_refusal
from lakehouse.records import RecordChangedError, RecordExistsError, create_json, read_json, replace_json
from lakehouse.storage_options import StorageOptions


log = logging.getLogger(__name__)

_ROLES_PREFIX: Final = control_prefixes.ROLES
#: The relation naming who is in a role.
ASSIGNEE: Final = "assignee"


class StoredRole(RoleRecord):
    """A role's record as the store keeps it: ``ended_at`` is set once the role is ended, and ``project_incarnation``
    names the incarnation of the project it was made in (ADR 0241 §8 B3), so a role a create left across its project's
    delete is listed under no project made again under the id."""

    project_incarnation: str = Field(min_length=1)
    ended_at: datetime | None = None


def role_object(role_id: uuid.UUID | str) -> str:
    """The OpenFGA object of the role ``role_id``."""
    return f"role:{role_id}"


def assignees_of(role_id: uuid.UUID | str) -> str:
    """The userset naming everyone assigned the role ``role_id``."""
    return f"{role_object(role_id)}#{ASSIGNEE}"


def _key(project: str, name: str) -> str:
    return f"{_ROLES_PREFIX}/{project}/{blake2s(name.encode('utf-8')).hexdigest()}.json"


def create_role_record(control_root: str, storage_options: StorageOptions, record: RoleRecord, *, project_incarnation: str) -> None:
    """Write ``record``, made in ``project_incarnation`` of its project, iff no standing role of its project holds its
    name.

    Raises:
        lakehouse.records.RecordExistsError: A standing role holds the name, or another create took it first.
    """
    key = _key(record.project, record.name)
    body = StoredRole(**record.model_dump(), project_incarnation=project_incarnation).model_dump(mode="json")
    try:
        create_json(control_root, storage_options, key, body)
        return
    except RecordExistsError:
        pass
    stored = read_json(control_root, storage_options, key)
    if stored is None or StoredRole.model_validate(stored[0]).ended_at is None:
        raise RecordExistsError(key)
    try:
        replace_json(control_root, storage_options, key, body, etag=stored[1])
    except RecordChangedError as exc:
        raise RecordExistsError(key) from exc


def _read_roles(control_root: str, storage_options: StorageOptions, project: str) -> tuple[list[StoredRole], list[str]]:
    """Every standing role of ``project``, of any incarnation of it, ordered by name, and the path of each record that is
    there and cannot be read as one. A record deleted since the listing named it is no record.

    Raises:
        ServiceUnavailableError: The store did not answer a record's read: one skipped as unreadable would read as a
            corrupt record, which no retry mends ([[LH-572]]).
    """
    fs, base = fs_and_base(control_root, storage_options)
    found: list[StoredRole] = []
    unreadable: list[str] = []
    for info in fs.get_file_info(pafs.FileSelector(f"{base}/{_ROLES_PREFIX}/{project}", allow_not_found=True)):
        if info.type != pafs.FileType.File or not info.path.endswith(".json"):
            continue
        try:
            with fs.open_input_stream(info.path) as stream:
                stored = StoredRole.model_validate(json.loads(stream.readall().decode("utf-8")))
        except FileNotFoundError:
            continue
        except (OSError, ValueError, ValidationError) as exc:
            if reads_as_store_outage(exc):
                raise store_refusal(exc, reading=f"reading {info.path}", so="the project's roles cannot be read") from exc
            log.warning("role_record_unreadable", extra={"path": info.path, "error": str(exc)})
            unreadable.append(info.path)
            continue
        if stored.ended_at is None:
            found.append(stored)
    return sorted(found, key=lambda role: role.name), unreadable


def every_stored_role(control_root: str, storage_options: StorageOptions) -> tuple[list[StoredRole], list[str]]:
    """``(every standing role of every project, the path of each record that cannot be read as one)``, in one listing of
    the roles registry: what the converge tick judges a dead tenant's roles by and reports its failures by (ADR 0241 §9
    P5, §10 Q3). A record that cannot be read is skipped and logged.

    Raises:
        ServiceUnavailableError: The store did not answer a record's read.
    """
    fs, base = fs_and_base(control_root, storage_options)
    found: list[StoredRole] = []
    unreadable: list[str] = []
    for info in fs.get_file_info(pafs.FileSelector(f"{base}/{_ROLES_PREFIX}", allow_not_found=True, recursive=True)):
        if info.type != pafs.FileType.File or not info.path.endswith(".json"):
            continue
        try:
            with fs.open_input_stream(info.path) as stream:
                stored = StoredRole.model_validate(json.loads(stream.readall().decode("utf-8")))
        except FileNotFoundError:
            continue
        except (OSError, ValueError, ValidationError) as exc:
            if reads_as_store_outage(exc):
                raise store_refusal(exc, reading=f"reading {info.path}", so="the roles cannot be read") from exc
            log.warning("role_record_unreadable", extra={"path": info.path, "error": str(exc)})
            unreadable.append(info.path)
            continue
        if stored.ended_at is None:
            found.append(stored)
    return found, unreadable


def _role(stored: StoredRole) -> RoleRecord:
    return RoleRecord(**stored.model_dump(exclude={"ended_at", "project_incarnation"}))


def list_roles(control_root: str, storage_options: StorageOptions, project: str, *, incarnation: str) -> list[RoleRecord]:
    """Every standing role of ``incarnation`` of ``project``, ordered by name: a role another incarnation's create left is
    no role of this one. An unreadable record is skipped and logged, so one bad object does not void the project's roles;
    a caller ending them all asks :func:`every_role`."""
    return [_role(stored) for stored in _read_roles(control_root, storage_options, project)[0] if stored.project_incarnation == incarnation]


def every_role(control_root: str, storage_options: StorageOptions, project: str) -> list[RoleRecord]:
    """Every standing role of ``project``, of any incarnation of it, read whole, for a caller ending them all: a record
    that cannot be read may be one of the roles it must end, so it is refused rather than left standing with its grants
    ([[LH-572]]).

    Raises:
        ServiceUnavailableError: The store did not answer a record's read.
        InternalError: A record is there and cannot be read as one.
    """
    roles, unreadable = _read_roles(control_root, storage_options, project)
    if unreadable:
        raise InternalError(
            f"{len(unreadable)} role record(s) of project {project!r} cannot be read as one ({', '.join(sorted(unreadable))}), so its "
            "roles cannot all be ended. Refusing rather than leaving a role standing with its grants."
        )
    return [_role(stored) for stored in roles]


def find_role(control_root: str, storage_options: StorageOptions, project: str, role_id: uuid.UUID | str, *, incarnation: str | None) -> RoleRecord | None:
    """The standing role ``role_id`` of ``incarnation`` of ``project`` (of any incarnation for ``None``: whether it stands
    at all), or ``None`` when it holds none of that id."""
    found = (stored for stored in _read_roles(control_root, storage_options, project)[0] if str(stored.id) == str(role_id))
    return next((_role(stored) for stored in found if incarnation in (None, stored.project_incarnation)), None)


#: How many times an ending reads the record: once, and once more after another write moved it.
END_READS: Final = 2


def end_role_record(control_root: str, storage_options: StorageOptions, record: RoleRecord) -> None:
    """Stamp ``record`` ended, under the ETag of the read that finds it; a no-op once it is ended or its name holds
    another role.

    A write that lands between the read and the stamp (another ending of the role, a create taking its name once it is
    ended) refuses the stamp, and the ending reads again and judges what it finds, so it never ends a role it did not read.

    Raises:
        lakehouse.records.RecordChangedError: The record changed under both reads.
    """
    key = _key(record.project, record.name)
    for _ in range(END_READS):
        stored = read_json(control_root, storage_options, key)
        if stored is None:
            return
        standing = StoredRole.model_validate(stored[0])
        if standing.id != record.id or standing.ended_at is not None:
            return
        ended = standing.model_copy(update={"ended_at": datetime.now(UTC)})
        try:
            replace_json(control_root, storage_options, key, ended.model_dump(mode="json"), etag=stored[1])
        except RecordChangedError:
            continue
        return
    raise RecordChangedError(f"role record {key!r} changed under both reads")
