"""Labels a project defines and attaches to its tables and namespaces, kept under the control root (ADR 0211).

A label classifies an object for the people who find and govern it (`pii`, `domain=finance`, `deprecated`). It grants
nothing, refuses no write and changes no decision any door takes: lance-namespace keeps a namespace's and a table's
free-form properties, and a label is the project's controlled vocabulary over them, held beside the object.

Two records:

- A **definition** per label name of a project, at ``_label_definitions/<project_uuid>/<blake2s(name)>.json``: its name,
  the object kinds it applies to (``scope``), how a value is constrained (``kind``: a ``marker`` carries none,
  ``free_text`` any string, ``enumerated`` one of ``allowed_values``) and who defined it. Filed under the creation of the
  project that defined it (ADR 0245, third review), so a define decided on one creation lands in that creation's
  keyspace alone and never reaches a project made again under the id; keyed there by the name, so the store arbitrates a
  creation's names. A definition evolves only in ways that keep every attached label valid: its scope and its allowed
  values only grow, and its kind never changes.
- An **attachment** record per labelled object, at ``record_key("_labels", kind, uuid)``: every label the object carries
  with its value, keyed by the object's uuid as every record kept for an object is (ADR 0173 §8), so a rename keeps its
  labels, the object's ending ends them (`lakehouse.object_records`), and an object made again under the name carries
  none.

An attachment records the uuid of the definition it was judged against. A definition is deleted only while no standing
object of its project carries it; an attachment that outlives its definition (made while the delete read them, or kept
by an object in the trash) names a uuid no definition holds, so no read shows it (:func:`labels_shown`), a name defined
again never revives it, and it never blocks a delete.

All IO here is blocking; callers threadpool it.
"""

from __future__ import annotations

import logging
import re
import uuid
from collections.abc import Mapping
from datetime import UTC, datetime
from hashlib import blake2s
from typing import Any, Final, Literal

from lance_namespace import InternalError, InvalidInputError
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from lakehouse import control_prefixes
from lakehouse.record_store import delete_record, get_record, list_records, record_key
from lakehouse.records import RecordChangedError, RecordExistsError, RecordMissingError, create_json, exchange_json, mutate_json, remove_json
from lakehouse.storage_options import StorageOptions


log = logging.getLogger(__name__)

type LabelKind = Literal["marker", "free_text", "enumerated"]
type LabelScope = Literal["namespace", "table"]

#: A label's name: lower case, so two spellings of one name cannot both be defined; a `.` may group names
#: (`pii.contact`). `rask` and names under it are the platform's.
NAME_RE: Final = re.compile(r"^[a-z0-9][a-z0-9_-]{0,62}(\.[a-z0-9][a-z0-9_-]{0,62}){0,3}$")
#: The longest value a free-text or enumerated label holds.
VALUE_MAX: Final = 256
#: How often a write is tried again when the record it found went missing under it.
_ROUNDS: Final = 3


class LabelDefinition(BaseModel):
    """One label a project defines."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    #: Minted at the first definition, kept while the name is defined.
    uuid: str
    project: str
    #: The uuid of the creation of the project that defined it (ADR 0245), whose keyspace it is filed in.
    project_uuid: str
    name: str
    description: str | None = None
    #: The object kinds the label may be attached to.
    scope: list[LabelScope] = Field(min_length=1)
    kind: LabelKind
    #: The values an ``enumerated`` label may hold; ``None`` for the other kinds.
    allowed_values: list[str] | None = None
    defined_by: str | None = None
    defined_at: str


class Attachment(BaseModel):
    """The labels one object carries, keyed by the object's uuid."""

    model_config = ConfigDict(extra="forbid")

    kind: LabelScope
    uuid: str
    #: The object's name when its labels last changed, for display; a table's is read again from its claim.
    id: str
    project: str
    #: A table's location, so a listing names the table as it is named now (`table_claims.names_now`).
    location: str | None = None
    #: Each label's value, ``None`` for a marker.
    labels: dict[str, str | None]
    #: The uuid of the definition each label was judged against.
    definitions: dict[str, str] = Field(default_factory=dict)
    set_by: str | None = None


def label_object(definition_uuid: str) -> str:
    """The object a definition's control events name."""
    return f"label:{definition_uuid}"


def _definitions_of(project_uuid: str) -> str:
    return f"{control_prefixes.LABEL_DEFINITIONS}/{project_uuid}"


def _definition_key(project_uuid: str, name: str) -> str:
    return f"{_definitions_of(project_uuid)}/{blake2s(name.encode('utf-8')).hexdigest()}.json"


def _attachment_key(kind: str, object_uuid: str) -> str:
    return record_key(control_prefixes.LABELS, kind, object_uuid)


def require_name(name: str) -> str:
    """``name`` if it can name a label.

    Raises:
        InvalidInputError: It cannot.
    """
    if not NAME_RE.match(name):
        raise InvalidInputError(f"label name {name!r} must match {NAME_RE.pattern}")
    if name == "rask" or name.startswith("rask."):
        raise InvalidInputError(f"label name {name!r} is the platform's: names under rask are reserved")
    return name


def proposed(
    *,
    project: str,
    project_uuid: str,
    name: str,
    description: str | None,
    scope: list[LabelScope],
    kind: LabelKind,
    allowed_values: list[str] | None,
    actor: str | None,
) -> LabelDefinition:
    """The definition a caller asks for, judged on its own (its name, its scope, its values against its kind).

    Raises:
        InvalidInputError: The name cannot name a label, the scope is empty or repeats a kind, or the values do not
            suit the kind: an ``enumerated`` label names one value at least, each at most ``VALUE_MAX`` characters,
            and no other kind names any.
    """
    require_name(name)
    if not scope or len(set(scope)) != len(scope):
        raise InvalidInputError("a label's scope names each object kind it applies to once, and at least one")
    if kind == "enumerated":
        if not allowed_values or len(set(allowed_values)) != len(allowed_values):
            raise InvalidInputError("an enumerated label names each of its allowed values once, and at least one")
        if any(not value or len(value) > VALUE_MAX for value in allowed_values):
            raise InvalidInputError(f"an allowed value holds 1 to {VALUE_MAX} characters")
    elif allowed_values is not None:
        raise InvalidInputError(f"a {kind} label names no allowed values")
    return LabelDefinition(
        uuid=str(uuid.uuid4()),
        project=project,
        project_uuid=project_uuid,
        name=name,
        description=description,
        scope=sorted(scope),
        kind=kind,
        allowed_values=allowed_values,
        defined_by=actor,
        defined_at=datetime.now(UTC).isoformat(),
    )


def _evolved(stored: LabelDefinition, asked: LabelDefinition) -> LabelDefinition:
    """``stored`` as ``asked`` changes it, in the ways that keep every label already attached valid.

    Raises:
        InvalidInputError: ``asked`` changes the kind, drops a kind from the scope, or drops an allowed value.
    """
    if asked.kind != stored.kind:
        raise InvalidInputError(f"label {stored.name!r} is {stored.kind}: a label's kind never changes; define another name")
    if dropped := sorted(set(stored.scope) - set(asked.scope)):
        raise InvalidInputError(f"label {stored.name!r} applies to {', '.join(dropped)}: a label's scope only grows")
    if dropped_values := sorted(set(stored.allowed_values or []) - set(asked.allowed_values or [])):
        raise InvalidInputError(f"label {stored.name!r} allows {', '.join(dropped_values)}: a label's allowed values only grow")
    return stored.model_copy(update={"description": asked.description, "scope": asked.scope, "allowed_values": asked.allowed_values})


def define(control_root: str, storage_options: StorageOptions, asked: LabelDefinition) -> tuple[LabelDefinition, bool]:
    """Define ``asked``, or evolve the definition its name holds; the definition that stands and whether this changed it.

    A definition applied again as it stands changes nothing, so a producer that declares its labels on every reconcile
    announces only what moved. It is filed under the creation ``asked`` names (ADR 0245, third review): a define decided
    on a creation that ended meanwhile lands in that creation's keyspace, which the converge tick sweeps, and never
    reaches the definition of a project made again under the id.

    Raises:
        InvalidInputError: The evolution would leave an attached label invalid (:func:`_evolved`).
        lakehouse.records.RecordChangedError: The definition kept changing under the evolution.
    """
    key = _definition_key(asked.project_uuid, asked.name)
    # A definition deleted between the refused create and the evolution is created on the next round.
    for _ in range(_ROUNDS):
        try:
            create_json(control_root, storage_options, key, asked.model_dump(mode="json"))
        except RecordExistsError:
            pass
        else:
            return asked, True
        try:
            replaced, written = exchange_json(
                control_root, storage_options, key, lambda current: _evolved(_parsed(current, key=key), asked).model_dump(mode="json")
            )
        except RecordMissingError:
            continue
        return _parsed(written, key=key), written != replaced
    raise RecordChangedError(key)


def _parsed(record: Mapping[str, Any], *, key: str) -> LabelDefinition:
    try:
        return LabelDefinition.model_validate(record)
    except ValidationError as exc:
        raise InternalError(f"the label definition at {key} cannot be read as one; write it again") from exc


def definition(control_root: str, storage_options: StorageOptions, name: str, *, project_uuid: str) -> LabelDefinition | None:
    """The definition the project's creation ``project_uuid`` holds under ``name``, or ``None``.

    Raises:
        InternalError: A record is there and cannot be read as one.
    """
    key = _definition_key(project_uuid, name)
    record = get_record(control_root, storage_options, key, event="label_definition")
    return None if record is None else _parsed(record, key=key)


def definitions(control_root: str, storage_options: StorageOptions, *, project_uuid: str) -> list[LabelDefinition]:
    """Every label the project's creation ``project_uuid`` defines, by name. A record that cannot be read is skipped and
    logged by the listing."""
    found = []
    for record in list_records(control_root, storage_options, _definitions_of(project_uuid), event="label_definition"):
        try:
            found.append(LabelDefinition.model_validate(record))
        except ValidationError:
            log.warning("label_definition_unreadable", extra={"project_uuid": project_uuid, "name": record.get("name")})
    return sorted(found, key=lambda defined: defined.name)


def attachments(control_root: str, storage_options: StorageOptions, *, project: str) -> list[Attachment]:
    """Every attachment record of ``project``'s objects, unordered. A record that cannot be read is skipped and logged."""
    found: list[Attachment] = []
    for record in list_records(control_root, storage_options, control_prefixes.LABELS, event="labels"):
        try:
            attachment = Attachment.model_validate(record)
        except ValidationError:
            log.warning("labels_record_unreadable", extra={"uuid": record.get("uuid")})
            continue
        if attachment.project == project:
            found.append(attachment)
    return found


def carriers(control_root: str, storage_options: StorageOptions, defined: LabelDefinition) -> list[Attachment]:
    """Every attachment record of the project that carries the label ``defined``, judged against that definition."""
    return [attachment for attachment in attachments(control_root, storage_options, project=defined.project) if carries(attachment, defined)]


def carries(attachment: Attachment, defined: LabelDefinition) -> bool:
    """Whether ``attachment`` carries the label ``defined``, attached under that very definition."""
    return defined.name in attachment.labels and attachment.definitions.get(defined.name) == defined.uuid


def delete_definition(control_root: str, storage_options: StorageOptions, defined: LabelDefinition) -> None:
    """Delete the definition ``defined``, where the record at its key is still that definition (its ``uuid``), under the
    ETag of the read that found it so: a definition made again under the name since is another's. Its caller has found no
    standing object carrying it (:func:`carriers`)."""
    remove_json(control_root, storage_options, _definition_key(defined.project_uuid, defined.name), lambda record: record.get("uuid") == defined.uuid)


def end_definitions(control_root: str, storage_options: StorageOptions, *, project_uuid: str) -> int:
    """Delete every definition the project's creation ``project_uuid`` holds, once that creation goes; how many went. The
    keyspace is that creation's alone (ADR 0245, third review), so nothing another creation of the id defined is reached;
    the converge tick ends what a define held across the creation's end wrote there after it."""
    ended = 0
    for defined in definitions(control_root, storage_options, project_uuid=project_uuid):
        ended += delete_record(control_root, storage_options, _definition_key(project_uuid, defined.name))
    return ended


def _attachment(record: Mapping[str, Any], *, key: str) -> Attachment:
    """``record`` read as an attachment.

    Raises:
        InternalError: It cannot be read as one, which only writing the labels again mends.
    """
    try:
        return Attachment.model_validate(record)
    except ValidationError as exc:
        raise InternalError(f"the labels record at {key} cannot be read as one; write the labels again") from exc


def attachment_of(control_root: str, storage_options: StorageOptions, kind: LabelScope, object_uuid: str) -> Attachment | None:
    """The attachment record of the object ``object_uuid``, or ``None`` when it was never labelled.

    Raises:
        InternalError: Its record cannot be read as one.
    """
    key = _attachment_key(kind, object_uuid)
    record = get_record(control_root, storage_options, key, event="labels")
    return None if record is None else _attachment(record, key=key)


def judged_value(defined: LabelDefinition, *, kind: LabelScope, value: str | None) -> str | None:
    """``value`` if the label ``defined`` may carry it on an object of ``kind``.

    Raises:
        InvalidInputError: The label does not apply to the kind, or the value does not suit the label's kind.
    """
    if kind not in defined.scope:
        raise InvalidInputError(f"label {defined.name!r} applies to {', '.join(defined.scope)}, not to a {kind}")
    if defined.kind == "marker":
        if value is not None:
            raise InvalidInputError(f"label {defined.name!r} is a marker: it carries no value")
        return None
    if not value or len(value) > VALUE_MAX:
        raise InvalidInputError(f"label {defined.name!r} carries a value of 1 to {VALUE_MAX} characters")
    if defined.kind == "enumerated" and value not in (defined.allowed_values or []):
        raise InvalidInputError(f"label {defined.name!r} takes one of {', '.join(defined.allowed_values or [])}")
    return value


def attach(
    control_root: str,
    storage_options: StorageOptions,
    base: Attachment,
    *,
    defined: LabelDefinition,
    value: str | None,
) -> None:
    """Attach the label ``defined`` with ``value`` to the object ``base`` names, replacing the value it carried.

    ``base`` names the object (its kind, uuid, name, project and a table's location), as the door resolved it now.

    Raises:
        lakehouse.records.RecordChangedError: The record kept changing under the write.
    """
    key = _attachment_key(base.kind, base.uuid)

    def _with(current: dict[str, Any]) -> dict[str, Any]:
        stored = _attachment(current, key=key) if current else None
        labels = {**(stored.labels if stored else {}), defined.name: value}
        judged = {**(stored.definitions if stored else {}), defined.name: defined.uuid}
        return base.model_copy(update={"labels": dict(sorted(labels.items())), "definitions": dict(sorted(judged.items()))}).model_dump(mode="json")

    # A record ended between the refused create and the write is created on the next round.
    for _ in range(_ROUNDS):
        try:
            create_json(control_root, storage_options, key, _with({}))
            return
        except RecordExistsError:
            pass
        try:
            mutate_json(control_root, storage_options, key, _with)
            return
        except RecordMissingError:
            continue
    raise RecordChangedError(key)


def forget(control_root: str, storage_options: StorageOptions, kind: LabelScope, object_uuid: str) -> None:
    """Delete the attachment record of an object that ended while a label was being attached to it."""
    delete_record(control_root, storage_options, _attachment_key(kind, object_uuid))


def detach(control_root: str, storage_options: StorageOptions, kind: LabelScope, object_uuid: str, *, name: str, actor: str | None) -> bool:
    """Remove the label ``name`` from the object ``object_uuid``; whether it carried it.

    The record stays when its last label goes, holding none: deleting it could remove a label another request attached
    between this write and the delete. The object's ending deletes it.
    """
    key = _attachment_key(kind, object_uuid)
    current = get_record(control_root, storage_options, key, event="labels")
    if current is None or name not in _attachment(current, key=key).labels:
        return False

    def _without(stored: dict[str, Any]) -> dict[str, Any]:
        attachment = _attachment(stored, key=key)
        kept = {
            "labels": {k: v for k, v in attachment.labels.items() if k != name},
            "definitions": {k: v for k, v in attachment.definitions.items() if k != name},
        }
        return attachment.model_copy(update={**kept, "set_by": actor}).model_dump(mode="json")

    try:
        mutate_json(control_root, storage_options, key, _without)
    except RecordMissingError:
        return False
    return True


def labels_shown(attachment: Attachment | None, defined: Mapping[str, LabelDefinition]) -> dict[str, str | None]:
    """The labels a read shows: those attached under the definition that stands under their name."""
    if attachment is None:
        return {}
    return {name: value for name, value in attachment.labels.items() if name in defined and carries(attachment, defined[name])}
