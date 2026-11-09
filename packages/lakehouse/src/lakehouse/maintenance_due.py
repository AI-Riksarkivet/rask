"""Which tables need compacting or reindexing: maintenance's decision, a bring-your-own job's work list (ADR 0162).

Maintenance decides and does not rewrite (ADR 0155, ADR 0156 C4 and C6). Each table's pass writes, on the control root,
the table's record when a compaction or an index optimize is due on it, keyed by the table's uuid and carrying the counts
that made it due and the policy record that won, and deletes it once the table is no longer due. The catalog lists them
on ``GET /management/v1/maintenance/due`` to the callers that may maintain each table.

Written by maintenance alone, so a reader ignores a field a newer writer added. Every function is blocking IO.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import TYPE_CHECKING, Any, Final

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from lakehouse import stored_objects
from lakehouse.record_store import delete_record, get_record, put_record, record_key


if TYPE_CHECKING:
    from lakehouse.storage_options import StorageOptions


log = logging.getLogger(__name__)

DUE_PREFIX: Final = "_maintenance_due"


class CompactionDue(BaseModel):
    """What made a table due for a compaction: Lance's own planner, run under the winning policy's fragment size."""

    model_config = ConfigDict(extra="ignore")

    fragments: int = Field(ge=0)
    #: The fragments ``Compaction.plan`` would rewrite, over ``tasks`` tasks.
    candidate_fragments: int = Field(ge=1)
    tasks: int = Field(ge=1)
    #: The size the plan was made for, ``None`` for Lance's own.
    target_rows_per_fragment: int | None = None


class IndexDue(BaseModel):
    """One index an optimize is due on: rows it does not cover, or more delta segments than a query should fan out over."""

    model_config = ConfigDict(extra="ignore")

    name: str
    unindexed_rows: int = Field(ge=0)
    deltas: int = Field(ge=0)
    #: The ``num_indices_to_merge`` the index's optimize passes, set when the index carries more delta segments than
    #: tolerated: a plain ``optimize_indices()`` appends a delta to a vector index each round rather than merging
    #: (measured on pylance 13.0.0, IVF_FLAT one more per round while BTREE and INVERTED stay at 1), so only a merge of
    #: this many clears it. ``None`` when covering the unindexed rows is all the index needs.
    num_indices_to_merge: int | None = Field(default=None, ge=1)


class DueRecord(BaseModel):
    """One table's due work, as maintenance measured it at ``version``."""

    model_config = ConfigDict(extra="ignore")

    #: The table's name as the catalog's ``__manifest`` named it when it was evaluated, for display.
    table: str
    #: The table's uuid, which the record is keyed by (ADR 0173 §8): a rename keeps the record with the table.
    uuid: str
    location: str
    version: int = Field(ge=1)
    compaction: CompactionDue | None = None
    indices: list[IndexDue] = Field(default_factory=list)
    #: The id of the policy record that governed the evaluation, ``None`` when none matched.
    policy: str | None = None
    evaluated_at: datetime


def due_key(table_uuid: str) -> str:
    return record_key(DUE_PREFIX, "table", table_uuid)


def _parsed(raw: dict[str, Any] | None) -> DueRecord | None:
    if raw is None:
        return None
    try:
        return DueRecord.model_validate(raw)
    except ValidationError:
        log.warning("maintenance_due_unreadable", extra={"table": raw.get("table")})
        return None


def write_due(control_root: str, storage_options: StorageOptions, record: DueRecord) -> None:
    put_record(control_root, storage_options, due_key(record.uuid), record.model_dump(mode="json"))


def read_due(control_root: str, storage_options: StorageOptions, table_uuid: str) -> DueRecord | None:
    return _parsed(get_record(control_root, storage_options, due_key(table_uuid), event="maintenance_due"))


def delete_due(control_root: str, storage_options: StorageOptions, table_uuid: str) -> bool:
    return delete_record(control_root, storage_options, due_key(table_uuid))


def due_record_names(control_root: str, storage_options: StorageOptions) -> frozenset[str]:
    """The file name of every due record held now, reading none of them: what a sweep tick may judge at its end."""
    listed = stored_objects.list_under(f"{control_root.rstrip('/')}/{DUE_PREFIX}", storage_options)
    names = (stored.path.rsplit("/", 1)[-1] for stored in listed)
    return frozenset(name for name in names if _is_a_record(name))


def read_due_named(control_root: str, storage_options: StorageOptions, name: str) -> DueRecord | None:
    """The due record held under the file name ``name`` (one :func:`due_record_names` listed), or ``None``."""
    return _parsed(get_record(control_root, storage_options, f"{DUE_PREFIX}/{name}", event="maintenance_due"))


def _is_a_record(name: str) -> bool:
    return name.startswith("table-") and name.endswith(".json")
