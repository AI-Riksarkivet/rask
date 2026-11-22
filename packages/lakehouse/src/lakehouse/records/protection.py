"""Deletion-protection records for tables and namespaces (#73).

The ``protected`` flag warehouses and projects carry on their REGISTRY records, extended to the two
rungs that had none — where the gap was sharpest, because a table drop deletes bytes and (until the
trash-namespace undrop lands) nothing stands behind it.

The marker lives HERE, in a control-root record, and deliberately NOT in the table's schema
metadata:

- One contract: the same ``require_not_protected`` guard is handed the flag for every kind, with
  the same ``force`` semantics — no second implementation to drift.
- No coupling to the properties write door (#78): schema metadata is getting a user-facing update
  endpoint; protection in that keyspace would make "unprotect" reachable through a data-plane door
  and force a reserved-key policy to exist before that endpoint does.
- Protect/unprotect is a control-plane admin op: it emits a control event and must NOT create a
  table version, which a schema-metadata commit would.
- The drop door's guard runs BEFORE the native call and must stay answerable even when the dataset
  itself is corrupted — which is exactly when someone needs to drop it.

Same store shape as ``maintenance_policies`` (one JSON per object, keyed by the object's uuid, ADR 0173 §8),
own ``_protection/`` prefix so neither plane can misread the other's records. A record also keeps the object's
name under ``id``, as it was when the record was set, for display. The store mechanics are
:mod:`lakehouse.records.record_store`'s; this module is the protection vocabulary over them.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Final, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from lakehouse.records import control_prefixes
from lakehouse.records.record_store import delete_record, put_record, read_record, record_key
from lakehouse.storage.options import StorageOptions


__all__ = [
    "PREFIX",
    "ProtectedKind",
    "ProtectionRecord",
    "UnreadableProtectionRecordError",
    "clear_protection",
    "get_protection",
    "is_protected",
    "set_protection",
]


#: Where the records live on the control root, one per object under ``record_key(PREFIX, kind, uuid)``.
PREFIX: Final = control_prefixes.PROTECTION

#: What a record of this registry protects; a warehouse's and a project's flag rides their registry record.
type ProtectedKind = Literal["table", "namespace"]


class UnreadableProtectionRecordError(ValueError):
    """A protection record is filed for the object and cannot be read as one, so whether its owner armed it is unknown
    and nothing the flag guards may go ahead. Names the key the record is filed under."""


class ProtectionRecord(BaseModel):
    """The deletion protection a table's or a namespace's owner set on it.

    The flag is read one way by every reader, the guard and the door that answers it: ``true`` in any case, or a JSON
    true, is armed; ``false`` is not; any other value is no flag anyone set, and the record is unreadable. Who armed it
    is bookkeeping no decision reads, so a value that is not text is read as unknown.
    """

    model_config = ConfigDict(extra="allow", frozen=True)

    kind: ProtectedKind
    #: The object's name as it was when the record was set, for display.
    id: str = ""
    #: The object's uuid, which keys the record (ADR 0173 §8).
    uuid: str = Field(min_length=1)
    protected: bool
    #: Who armed it, ``user:<subject>``; ``None`` where the record does not say.
    set_by: str | None = None

    @field_validator("protected", mode="before")
    @classmethod
    def _true_or_false_in_any_case(cls, value: object) -> object:
        if isinstance(value, str) and value.lower() in ("true", "false"):
            return value.lower() == "true"
        if isinstance(value, bool):
            return value
        raise ValueError("the flag is neither true nor false")

    @field_validator("set_by", mode="before")
    @classmethod
    def _someone_or_unknown(cls, value: object) -> object:
        return value if isinstance(value, str) and value else None

    def stored(self) -> dict[str, Any]:
        """The record as it is written: the flag as the text every registry record spells it."""
        return {**self.model_dump(exclude_none=True), "protected": "true" if self.protected else "false"}


def is_protected(record: Mapping[str, Any]) -> bool:
    """Whether a project's or a warehouse's registry ``record`` holds deletion protection: its ``protected`` reads
    ``true`` in any case. Any other value, and none, reads as unprotected here, where :class:`ProtectionRecord` refuses
    a flag that is neither true nor false.

    Args:
        record: A registry record as read; one carrying no ``protected`` is unprotected.

    Returns:
        True when the record is armed.
    """
    return str(record.get("protected") or "").lower() == "true"


def set_protection(control_root: str, storage_options: StorageOptions, record: ProtectionRecord) -> None:
    """Persist one protection record, keyed by the object's ``uuid`` (overwrite — setting is idempotent)."""
    put_record(control_root, storage_options, record_key(PREFIX, record.kind, record.uuid), record=record.stored())


def get_protection(control_root: str, storage_options: StorageOptions, kind: ProtectedKind, *, uuid: str) -> ProtectionRecord | None:
    """The protection record for the object ``uuid`` names, or ``None`` when none is filed, which means UNPROTECTED:
    protection is an opt-in the owner set, not a default the store's availability grants.

    A destructive door decides from the answer, so a record that is filed and cannot be read raises: read as absent, a
    table its owner armed would drop.

    Raises:
        UnreadableProtectionRecordError: A record is filed there and is no protection record.
        OSError: The store failed the read.
    """
    key = record_key(PREFIX, kind, uuid)
    try:
        raw = read_record(control_root, storage_options, key)
        return ProtectionRecord.model_validate(raw) if raw is not None else None
    except ValueError as exc:  # not JSON, not an object, or not a protection record (pydantic's error is one)
        detail = f"{exc.error_count()} field(s) are not what a record holds" if isinstance(exc, ValidationError) else str(exc)
        raise UnreadableProtectionRecordError(f"the protection record at {key} cannot be read: {detail}") from exc


def clear_protection(control_root: str, storage_options: StorageOptions, kind: ProtectedKind, *, uuid: str) -> bool:
    """Remove one protection record; ``True`` if one existed."""
    return delete_record(control_root, storage_options, record_key(PREFIX, kind, uuid))
