"""The maintenance queue's wire contract: ONE dataset's maintenance, self-contained.

It lives here rather than in ``services/maintenance`` because it has two producers in different
processes and one consumer. The scheduled lane plans in the maintenance service itself
(``sweep.plan_sweep`` / ``plan_one``); the on-demand lane is the catalog's ``maintenance/compact``
door, which cannot import a sibling service. ``base_refs`` moved here first, for the same reason and
with the same consequence if it had not: the catalog's on-demand doors must apply the sweep's refusals
and cannot reach the sweep to borrow them.

A duplicated model would be worse than an import: this crosses a broker, so a field added on one side
and not the other is not a type error anywhere — it is a unit that validates, executes, and quietly
does something other than what its producer asked for.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from pydantic import BaseModel


class DatasetPlan(BaseModel):
    """What the resolved #50/#84 policy says to do with ONE dataset this tick.

    Extracted from ``run_sweep``'s body (MAINT-09), where thirty lines of per-dataset resolution sat
    between the tracing span and the call it configures. ``skipped`` set means the dataset is not
    maintained at all this tick and every other field is irrelevant.
    """

    #: ``policy_disabled`` / ``policy_interval``, or ``None`` to maintain.
    skipped: str | None = None
    #: The matched policy record, or ``None`` when no record applies (or one could not be read).
    policy: dict[str, Any] | None = None
    older_than: timedelta | None = None
    retain_versions: int | None = None
    target_rows_per_fragment: int | None = None
    scan_batch_size: int = 0
    auto_cleanup_interval_commits: int | None = None
    index_columns: list[str] | None = None
    cleanup_enabled: bool = True
    optimize_indices_enabled: bool = True


class DatasetWorkItem(BaseModel):
    """ONE dataset's maintenance, self-contained — everything a worker needs and nothing more.

    Self-containment is the whole point, and it is what lets this ride a queue: a unit that needed a
    value computed across the estate could only ever be executed by the process that computed it, which
    is the shape the sweep has today. The three costs of that shape — an overrunning tick is DROPPED
    rather than queued, a poison dataset stops everything discovered after it, and the single-flight
    guard is an ``asyncio.Lock`` correct only while ``replicas: 1`` stays hardcoded — all dissolve once
    a dataset's maintenance is a unit.

    ``protected_by`` is the reduction that makes that possible. ``_protected_roots`` HAS to be
    whole-estate (a shallow clone in bucket B is the only thing that knows bucket A's dataset must not
    be touched), but ``compact_one`` consumes it through exactly one call — ``is_protected(uri)`` — and
    that answer is one string. So the pre-pass stays whole-estate where it belongs, at planning time,
    and what crosses to the worker is its verdict for this dataset: the referencing root, or ``None``.
    """

    uri: str
    plan: DatasetPlan
    #: The root that some OTHER dataset's manifest resolves through, when this dataset is one or lies
    #: under one. ``None`` means the pre-pass found no referrer — the dataset may be compacted.
    protected_by: str | None = None
