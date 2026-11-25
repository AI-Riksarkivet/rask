"""Read ONE JSON document on the control root — the primitive under every catalog registry.

The warehouse registry and the project registry are the same shape: one JSON object per record, at a plain (unhashed)
key under the control root, no DB. Reading one is about neither registry, so both import it from here. Every write is
conditional (mint-iff-absent, ETag-guarded mutate) and lives in ``lakehouse.storage.conditional_records``.

All IO here is BLOCKING; callers threadpool it.
"""

import json
import logging

from lance_namespace import InternalError
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from lakehouse.records.protection import is_protected
from lakehouse.storage.objectfs import fs_and_base
from lakehouse.storage.options import StorageOptions


log = logging.getLogger(__name__)


def read_json(root_uri: str, storage_options: StorageOptions, key: str) -> dict[str, str] | None:
    """The record at ``key``, or ``None`` when nothing is stored there."""
    fs, base = fs_and_base(root_uri, storage_options)
    try:
        stream = fs.open_input_stream(f"{base}/{key}")
    except FileNotFoundError:
        return None
    with stream:
        return json.loads(stream.readall().decode("utf-8"))


# The declared shape of a bucket claim. A project's and a warehouse's records are `lakehouse.records.project_records` and
# `lakehouse.records.warehouse_records`. The fields a consumer keys on are declared, so a claim whose `bucket` is a list is no
# claim rather than one that matches no bucket; every other field rides along (`extra="allow"`).


class BucketClaimRecord(BaseModel):
    """Which PROJECT owns a bucket, written once so the store arbitrates rival claims.

    Keyed by bucket and carrying the project rather than the warehouse id, because one project
    legitimately backs several warehouses with one bucket (one project's several warehouses on one bucket) — the same asymmetry
    `projects_claiming_bucket` encodes by subtracting the caller's own project.
    """

    model_config = ConfigDict(extra="allow")

    bucket: str = Field(min_length=1)
    project: str = Field(min_length=1)
    warehouse_id: str = Field(min_length=1)


#: The record fields deletion protection writes on a warehouse or project record: the flag
#: `hierarchy.require_not_protected` reads, and who armed it.
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


def validated_or_refuse(record: object, model: type[BucketClaimRecord], *, event: str, path: str) -> dict[str, str]:
    """``record`` as a plain dict if it is a bucket claim, else REFUSE.

    A claim read by its key has no skip list to hand back: its only other answer is "unclaimed", and a bucket whose
    claim nobody can parse would then be free for a second project to take. `InternalError` (code 18), as every
    record that is there and cannot be read answers (`lakehouse.problems.outage.store_refusal`): only a write to the record
    clears it, so the caller is not told to try again.
    """
    try:
        return model.model_validate(record).model_dump(mode="json", exclude_unset=True)
    except ValidationError as exc:
        log.warning(event, extra={"path": path, "error": exc.errors(include_url=False)})
        raise InternalError(f"control record {path!r} could not be read as {model.__name__} — refusing rather than treating it as absent") from exc
