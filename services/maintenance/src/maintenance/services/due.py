"""Whether a table needs compacting or reindexing: maintenance's decision, written as the table's due record (ADR 0162).

Read-only over the dataset. A compaction is due when the winning policy leaves it enabled, the layout passes the
compaction gate (:func:`service_kit.lakehouse.features.describe_compaction_unsupported_flags`, fed the three base
readings exactly as a rewrite would be judged), and Lance's own planner has a task under the policy's fragment size.
An index optimize is due when the policy leaves it enabled and an index has unindexed rows or too many deltas
(:func:`maintenance.services.index_health.due_indices`). Due on either writes the record; due on neither deletes it.

THE TABLE ID COMES FROM THE CATALOG'S ``__manifest`` ROWS, never from the directory name: a rename moves the pointer
and keeps the bytes (and their ``<hash>_<id>`` label) where they are, so a name read off the path names the table the
data used to be. A dataset no manifest row names — unregistered, trashed, dropped — gets no record.
"""

from __future__ import annotations

import logging
from datetime import datetime

import lance
from lance.optimize import Compaction, CompactionOptions
from pydantic import BaseModel, Field

from maintenance.core.config import MaintenanceSettings, shared_lance_session
from maintenance.services.expiry import DueVerdict
from maintenance.services.index_health import due_indices
from maintenance.services.naming import TableIds
from maintenance.services.purge import manifest_rows, table_location
from service_kit.lakehouse.base_refs import normalise
from service_kit.lakehouse.compaction_gate import compaction_refusal
from service_kit.lakehouse.maintenance_due import CompactionDue, DueRecord, IndexDue, delete_due, write_due
from service_kit.lakehouse.objectfs import StorageOptions


log = logging.getLogger(__name__)


def table_ids(roots: set[str], storage_options: StorageOptions) -> TableIds:
    """Every table each root's ``__manifest`` names, by location. A root that cannot be read leaves the answer incomplete."""
    found = TableIds()
    for root in sorted(roots):
        rows = manifest_rows(root, storage_options)
        if rows is None:
            found.complete = False
            continue
        for row in rows:
            location = table_location(root, row)
            if location is not None and row.get("object_id"):
                found.by_location[normalise(location)] = str(row["object_id"])
    return found


class DueQuestion(BaseModel):
    """One table's due evaluation: where it is, what governs it, and who resolves files through it."""

    table: str
    location: str
    #: The id of the winning policy record, ``None`` when none matched.
    policy: str | None = None
    compact_enabled: bool = True
    optimize_indices_enabled: bool = True
    #: The fragment size the compaction plan is made for, ``None`` for Lance's own.
    target_rows_per_fragment: int | None = None
    #: The datasets that resolve their files through this table, from the last whole-estate pre-pass.
    clones: list[str] = Field(default_factory=list)


def compaction_due(ds: lance.LanceDataset, question: DueQuestion, storage_options: StorageOptions) -> CompactionDue | None:
    """What Lance's own planner would rewrite under the policy's fragment size, or ``None`` when nothing is due."""
    if not question.compact_enabled:
        return None
    if (refusal := compaction_refusal(ds, question.location, storage_options)) is not None:
        log.debug("maintenance_compaction_not_due_refused_layout", extra={"uri": question.location, "reason": refusal})
        return None
    options: CompactionOptions = {}
    if question.target_rows_per_fragment:
        options["target_rows_per_fragment"] = question.target_rows_per_fragment
    plan = Compaction.plan(ds, options)
    if not plan.tasks:
        return None
    return CompactionDue(
        fragments=len(ds.get_fragments()),
        candidate_fragments=sum(len(task.fragments) for task in plan.tasks),
        tasks=len(plan.tasks),
        target_rows_per_fragment=question.target_rows_per_fragment,
    )


def _indices_due(ds: lance.LanceDataset, question: DueQuestion) -> list[IndexDue]:
    return due_indices(ds) if question.optimize_indices_enabled else []


def evaluate_due(question: DueQuestion, storage_options: StorageOptions, *, now: datetime) -> DueRecord | None:
    """The table's due record as of now, or ``None`` when neither a compaction nor an index optimize is due. Raises when
    the dataset cannot be read."""
    ds = lance.dataset(question.location, storage_options=storage_options, session=shared_lance_session())
    compaction = compaction_due(ds, question, storage_options)
    indices = _indices_due(ds, question)
    if compaction is None and not indices:
        return None
    return DueRecord(
        table=question.table,
        location=question.location,
        version=ds.version,
        compaction=compaction,
        indices=indices,
        policy=question.policy,
        clones=question.clones,
        evaluated_at=now,
    )


def refresh_due(question: DueQuestion, settings: MaintenanceSettings, *, now: datetime) -> DueVerdict:
    """Evaluate one table and write or delete its due record. Never raises: a failure leaves the record as it was."""
    options = settings.storage_options()
    try:
        record = evaluate_due(question, options, now=now)
        if record is None:
            delete_due(settings.resolved_control_root, options, question.table)
            return "clear"
        write_due(settings.resolved_control_root, options, record)
    except Exception as exc:  # noqa: BLE001 — one table's evaluation must not cost the tick
        log.warning("maintenance_due_evaluation_failed", extra={"table": question.table, "uri": question.location, "error": f"{type(exc).__name__}: {exc}"})
        return "failed"
    return "due"
