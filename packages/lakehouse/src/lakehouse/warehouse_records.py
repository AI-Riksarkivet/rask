"""READ-ONLY access to the warehouse registry, for services that are not the catalog.

The catalog owns the registry and its writes (`catalog/services/warehouses.py` — provisioning,
CAS, delete). Nothing else may import that: services share only cross-cutting libraries,
never each other. But maintenance genuinely needs to know WHICH buckets exist, and the
answer changes at runtime — a warehouse is provisioned by an API call, not by a config edit.

So the reader lives here, in the shared lakehouse library, deliberately narrow: it reads records and
nothing else. It cannot write or delete, which keeps the catalog the only writer while giving
maintenance a truthful answer. Same store shape as its siblings (one JSON per record under a prefix),
and that primitive now exists: :mod:`lakehouse.record_store`.
"""

from __future__ import annotations

import logging
from enum import StrEnum
from typing import Final, Self

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from lakehouse import control_prefixes
from lakehouse.naming import CONTROL_ID_RE
from lakehouse.record_store import list_records, read_record
from lakehouse.storage_options import StorageOptions


log = logging.getLogger(__name__)

#: Where the catalog writes warehouse records and which project owns a bucket. Kept in sync with
#: `catalog/services/warehouses.py`.
REGISTRY_PREFIX = control_prefixes.WAREHOUSES
_BUCKET_CLAIMS_PREFIX: Final = f"{REGISTRY_PREFIX}/bucket-claims"

#: The event stem for this registry's store warnings (`<stem>_malformed`, `<stem>_unreadable`).
_EVENT = "warehouse_record"


class DeleteMode(StrEnum):
    """What a drop under a warehouse does (ADR 0146), as its record's ``delete_mode`` keeps it."""

    #: The table and its data are deleted at once.
    HARD = "hard"
    #: The table is deregistered and kept recoverable for the record's ``trash_grace_days``.
    SOFT = "soft"
    #: The warehouse names no profile of its own and takes the estate's grace period; a request naming it clears one,
    #: so a declaration dropping its profile converges back to the estate's.
    ESTATE = "estate"


class WarehouseStatus(StrEnum):
    """Whether a warehouse serves: ``deactivated`` is offboarding's first step, after which every operation beneath it is
    refused and nothing rewrites its data."""

    ACTIVE = "active"
    DEACTIVATED = "deactivated"


class UnreadableWarehouseRecordError(ValueError):
    """A warehouse record is stored and cannot be read as one, so what it routes, quarantines or keeps is unknown. Names
    where the record sits."""


class WarehouseRecord(BaseModel):
    """A warehouse: one physical S3 bucket owned by one project, as the registry keeps it.

    The fields a reader keys on are declared, so a record whose ``bucket`` is a list is no record rather than one that
    matches no claim. Every other field its writer stored rides along (``extra="allow"``), and :meth:`stored` writes
    back exactly the fields the record was read with.
    """

    model_config = ConfigDict(extra="allow", frozen=True)

    id: str = Field(min_length=1)
    bucket: str = Field(min_length=1)
    project: str = Field(min_length=1)
    #: The root every table of the warehouse lives under.
    root_uri: str = Field(min_length=1)
    #: The store the bucket is in, where it is not the estate's own.
    endpoint: str | None = None
    #: The warehouse's delete profile (ADR 0146); absent where it takes the estate's grace period. Declared because a
    #: drop under the warehouse acts on it, so a record whose profile cannot be read is refused at the read rather than
    #: read as some other profile.
    delete_mode: DeleteMode | None = None
    trash_grace_days: str | None = None
    #: Whether it serves; a record that names no status is active. Any other word is no status a door wrote, and the
    #: record is unreadable, so no reader guesses whether a tenant is quarantined.
    status: WarehouseStatus | None = None
    #: The deletion-protection flag (``true`` when armed) and who armed it.
    protected: str | None = None
    protected_by: str | None = None
    #: Who owns the warehouse's declaration (``git``); absent, its own doors do.
    managed_by: str | None = None

    @model_validator(mode="after")
    def _profile_is_whole(self) -> Self:
        if (self.delete_mode is DeleteMode.SOFT) != (self.trash_grace_days is not None):
            raise ValueError("a soft delete profile keeps its trash_grace_days, and no other profile keeps any")
        if self.trash_grace_days is not None and not 0 < float(self.trash_grace_days) <= 365:
            raise ValueError(f"trash_grace_days {self.trash_grace_days!r} is not between 0 and 365 days")
        return self

    @property
    def is_deactivated(self) -> bool:
        """Whether the warehouse is quarantined: offboarding's first step, after which nothing rewrites its data."""
        return self.status is WarehouseStatus.DEACTIVATED

    @property
    def is_protected(self) -> bool:
        """Whether deletion protection is armed: the flag reads ``true`` in any case."""
        return (self.protected or "").lower() == "true"

    def grace_days(self, *, estate: float) -> float:
        """The days a drop under the warehouse stays recoverable, 0 for a destroying drop; ``estate`` where the record
        names no profile of its own."""
        if self.delete_mode is None or self.delete_mode is DeleteMode.ESTATE:
            return estate
        return 0.0 if self.delete_mode is DeleteMode.HARD else float(self.trash_grace_days or 0)

    def stored(self) -> dict[str, str]:
        """The record as it is written: the fields it was built or read with, and no other."""
        return self.model_dump(mode="json", exclude_unset=True)


def read(raw: object, *, at: str) -> WarehouseRecord:
    """``raw`` as a warehouse record; ``at`` names where it sits, for the refusal.

    Raises:
        UnreadableWarehouseRecordError: It is no warehouse record.
    """
    try:
        return WarehouseRecord.model_validate(raw)
    except ValidationError as exc:
        fields = ", ".join(sorted({".".join(str(part) for part in error["loc"]) or "the record" for error in exc.errors(include_url=False)}))
        raise UnreadableWarehouseRecordError(f"the warehouse record at {at} cannot be read: {fields} is not what a record holds") from exc


def _listed_at(raw: object) -> str:
    """Where a listed record sits, as far as the record itself says: the registry, and the id it names."""
    named = raw.get("id") if isinstance(raw, dict) else None
    return f"{REGISTRY_PREFIX}/ (a record naming {named!r})"


def read_warehouse_records(control_root: str, storage_options: StorageOptions) -> list[WarehouseRecord]:
    """Every warehouse record (unordered), read whole, for a caller making a DESTRUCTIVE decision from the listing.

    A record left out may be the one naming the bucket a live table's location is under, so one that cannot be read, or
    is no warehouse record, raises rather than being skipped. An absent prefix yields ``[]``.

    Raises:
        OSError: The store failed a read.
        ValueError: A record is not JSON, not a JSON object, or not a warehouse record
            (:class:`UnreadableWarehouseRecordError`).
    """
    return [read(raw, at=_listed_at(raw)) for raw in list_records(control_root, storage_options, REGISTRY_PREFIX, event=_EVENT, strict=True)]


def maintainable_buckets(records: list[WarehouseRecord]) -> list[str]:
    """The buckets whose datasets are maintained, sorted and de-duplicated.

    DEACTIVATED warehouses are excluded, and that is a decision rather than an oversight. Deactivate
    is offboarding step one — the resolver already 403s every operation on a quarantined tenant's
    namespaces — so compacting and reclaiming its version history would be the one process still
    rewriting data the estate has said nobody may touch. A reactivated warehouse is swept again on
    the next tick, having lost nothing but a cycle of maintenance.
    """
    return sorted({record.bucket for record in records if not record.is_deactivated})


def warehouse_record(control_root: str, storage_options: StorageOptions, warehouse_id: str) -> WarehouseRecord | None:
    """The record of warehouse ``warehouse_id``, read by its key, or ``None`` when no record holds that id.

    A warehouse is the first segment of every name beneath it ([[LH-593]]), so a table's warehouse is read here by the
    table's own first segment, one read whatever the registry holds. The estate's own warehouse has no record, so the
    caller tells it apart before asking. An id that is not a control id is no record's (it would name another key).

    Raises:
        ValueError: The record cannot be read as one (:class:`UnreadableWarehouseRecordError` where it is JSON).
        OSError: The store failed the read.
    """
    if not CONTROL_ID_RE.fullmatch(warehouse_id):
        return None
    key = f"{REGISTRY_PREFIX}/{warehouse_id}.json"
    raw = read_record(control_root, storage_options, key)
    return read(raw, at=key) if raw is not None else None


def bucket_project(control_root: str, storage_options: StorageOptions, bucket: str) -> str | None:
    """The project that owns ``bucket``, as the claim the catalog wrote when a warehouse first took the bucket records it;
    ``None`` when no warehouse took it. A bucket is one project's, which the store arbitrates.

    Raises:
        ValueError: The claim cannot be read as one.
        OSError: The store failed the read.
    """
    claim = read_record(control_root, storage_options, f"{_BUCKET_CLAIMS_PREFIX}/{bucket}.json")
    if claim is None:
        return None
    project = claim.get("project")
    if not isinstance(project, str) or not project:
        raise ValueError(f"the claim on bucket {bucket!r} names no project")
    return project
