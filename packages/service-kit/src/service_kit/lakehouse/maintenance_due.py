"""Which tables need compacting or reindexing: maintenance's decision, a bring-your-own job's work list (ADR 0162).

Maintenance decides and does not rewrite (ADR 0155, ADR 0156 C4 and C6). Each sweep tick writes, on the control root,
one record per table a compaction or an index optimize is due on, carrying the counts that made it due, the policy
record that won and the datasets that resolve their files through it, and deletes the record of a table that is no
longer due. The catalog lists them on ``GET /management/v1/maintenance/due`` to the callers that may maintain each table.

The tick also writes the clone list its whole-estate base-reference pre-pass found, once
(:class:`CloneList`). The catalog's commit judge reads it before it finalizes a compaction, and maintenance's write-event
lane reads it to fill a record between ticks: no per-table check can find a dataset's referrers, which only the
referring side's manifest names (:mod:`service_kit.lakehouse.base_refs`).

Both are written by maintenance alone, so a reader ignores a field a newer writer added. Every function is blocking IO.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import TYPE_CHECKING, Any, Final

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from service_kit.lakehouse.base_refs import BaseRefs
from service_kit.lakehouse.record_store import delete_record, get_record, list_records, put_record, record_key


if TYPE_CHECKING:
    from service_kit.lakehouse.objectfs import StorageOptions


log = logging.getLogger(__name__)

DUE_PREFIX: Final = "_maintenance_due"
#: Under a child prefix, so the record listing never reads it as a table's record.
_CLONE_LIST_KEY: Final = f"{DUE_PREFIX}/state/clones.json"


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


class DueRecord(BaseModel):
    """One table's due work, as maintenance measured it at ``version``."""

    model_config = ConfigDict(extra="ignore")

    #: The table's canonical id, as the catalog's ``__manifest`` names it: the FGA object's id.
    table: str
    location: str
    version: int = Field(ge=1)
    compaction: CompactionDue | None = None
    indices: list[IndexDue] = Field(default_factory=list)
    #: The id of the policy record that governed the evaluation, ``None`` when none matched.
    policy: str | None = None
    #: The datasets that resolve their files through this table, from the last whole-estate pre-pass.
    clones: list[str] = Field(default_factory=list)
    evaluated_at: datetime


class CloneList(BaseModel):
    """The last sweep's base references, reduced to what a compaction of one table needs to be judged."""

    model_config = ConfigDict(extra="ignore")

    taken_at: datetime
    #: Roots another dataset resolves its files through unconditionally.
    protected: list[str] = Field(default_factory=list)
    #: Roots every referrer reaches through a pinned recorded relation -> the tags that pin them.
    pinned: dict[str, list[str]] = Field(default_factory=dict)
    #: Each referenced root -> the datasets that resolve through it.
    referrers: dict[str, list[str]] = Field(default_factory=dict)

    @classmethod
    def taken(cls, refs: BaseRefs, *, at: datetime) -> CloneList:
        return cls(
            taken_at=at,
            protected=sorted(refs.protected),
            pinned={root: sorted(tags) for root, tags in refs.pinned.items()},
            referrers={root: sorted(datasets) for root, datasets in refs.referrers.items()},
        )

    def refs(self) -> BaseRefs:
        """The :class:`BaseRefs` this list was taken from, for :func:`service_kit.lakehouse.base_refs.rewrite_refusal`."""
        return BaseRefs(
            protected=set(self.protected),
            pinned={root: set(tags) for root, tags in self.pinned.items()},
            referrers={root: set(datasets) for root, datasets in self.referrers.items()},
        )

    def clones_of(self, location: str) -> list[str]:
        """The datasets that resolve their files through the root protecting ``location``."""
        root = self.refs().is_protected(location)
        return self.referrers.get(root, []) if root is not None else []


def due_key(table: str) -> str:
    return record_key(DUE_PREFIX, "table", table)


def _parsed(raw: dict[str, Any] | None) -> DueRecord | None:
    if raw is None:
        return None
    try:
        return DueRecord.model_validate(raw)
    except ValidationError:
        log.warning("maintenance_due_unreadable", extra={"table": raw.get("table")})
        return None


def write_due(control_root: str, storage_options: StorageOptions, record: DueRecord) -> None:
    put_record(control_root, storage_options, due_key(record.table), record.model_dump(mode="json"))


def read_due(control_root: str, storage_options: StorageOptions, table: str) -> DueRecord | None:
    return _parsed(get_record(control_root, storage_options, due_key(table), event="maintenance_due"))


def delete_due(control_root: str, storage_options: StorageOptions, table: str) -> bool:
    return delete_record(control_root, storage_options, due_key(table))


def list_due(control_root: str, storage_options: StorageOptions) -> list[DueRecord]:
    """Every readable record, in table order; one that does not parse is logged and left out."""
    records = (_parsed(raw) for raw in list_records(control_root, storage_options, DUE_PREFIX, event="maintenance_due"))
    return sorted((record for record in records if record is not None), key=lambda record: record.table)


def write_clone_list(control_root: str, storage_options: StorageOptions, clones: CloneList) -> None:
    put_record(control_root, storage_options, _CLONE_LIST_KEY, clones.model_dump(mode="json"))


def read_clone_list(control_root: str, storage_options: StorageOptions) -> CloneList | None:
    """The last sweep's clone list, or ``None`` when no sweep has written one or it does not parse."""
    raw = get_record(control_root, storage_options, _CLONE_LIST_KEY, event="maintenance_clone_list")
    if raw is None:
        return None
    try:
        return CloneList.model_validate(raw)
    except ValidationError:
        log.warning("maintenance_clone_list_unreadable")
        return None
