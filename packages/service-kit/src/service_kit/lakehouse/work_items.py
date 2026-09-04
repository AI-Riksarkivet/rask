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

import hashlib
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
    #: The catalog identifier (``namespace$table``) this dataset IS, when the producer knows it.
    #:
    #: Carried rather than derived, because the catalog is addressed by IDENTIFIER and never by
    #: location: an executor holding only a URI can do the work but cannot ask for a credential scoped
    #: to it. Recovering the id from the path covers a minority — measured on the live warehouse,
    #: ``table_id_from_uri`` reads six of eleven top-level roots, and the five it cannot read include
    #: ``medallion/``, the cascade. Those five are not unknown to the catalog: ``bronze$events`` at
    #: ``medallion/bronze`` and ``bronze$pages`` at ``bronze/pages`` both answer a write-tier vend 200.
    #: The identity exists; only the parser cannot see it in the path, and the producer always can.
    #:
    #: ``None`` stays expressible on purpose. The bucket sweep starts from a bare URI and may genuinely
    #: not know, and a producer forced to supply something would invent one — which vends a credential
    #: for the WRONG table rather than for none.
    table_id: str | None = None


#: The two index doors, as the WORK ITEM names them. Here rather than in either service because both
#: sides need the same two strings — the catalog to stamp one, the worker to dispatch on it — and a
#: pair of private copies across a broker is exactly the drift `work_items` exists to prevent.
VECTOR_INDEX = "vector"
SCALAR_INDEX = "scalar"


class IndexWorkItem(BaseModel):
    """ONE index build, self-contained — the same shape as :class:`DatasetWorkItem`, different work.

    **The spec asks for this.** `CreateTableIndex` says outright that "index creation is handled
    asynchronously" and that progress is monitored through `ListTableIndices` /
    `DescribeTableIndexStats`; its response carries an optional `transaction_id` and nothing else. A
    door that builds the index before answering is off-spec in the one direction that hurts — the cost
    of the build is a property of the table, not of the request, so it is unbounded work inside a
    handler.

    **It rides its own pubsub COMPONENT, not merely its own topic, and the reason is the ack.** A
    compaction unit is minutes; a vector index over a large table is not, and one `ackWait` cannot
    serve both — sized for the index a stuck compaction stays invisible that long, sized for
    compaction every large build redelivers mid-flight. `ackWait`, `durableName` and `queueGroupName`
    are per-COMPONENT in Dapr's JetStream pubsub, so a second topic on the work component would
    inherit its window and the separation would buy nothing. Separate components also let the two
    scale as independent consumer groups.

    **Why the whole build crosses, rather than a plan.** Compaction splits because `CompactionTask`
    and `RewriteResult` round-trip through `.json()`. Measured on pylance 10.0.0 (2026-09-04), an
    index segment — `create_index_uncommitted`'s return — carries no `json`/`to_json`/`serialize` at
    all, so `create_index_uncommitted` → `merge_existing_index_segments` →
    `commit_existing_index_segments` cannot be spread across processes today. The worker performs all
    three, which still takes the work off the request path; the split becomes available the day those
    segments serialize.
    """

    #: The dataset to index. The worker opens exactly this and derives nothing.
    uri: str
    #: The catalog identifier, for the vended per-table write credential and for the log line. Empty
    #: means the producer could not name it, and the worker falls back to its ambient credential —
    #: the rule `credentials.write_options_for` already states.
    table_id: str = ""
    column: str
    #: ``vector`` or ``scalar`` — WHICH pylance call, decided by the door the caller used rather than
    #: inferred from ``index_type``. The two doors are separate spec operations and a worker guessing
    #: between them would build a different index than the caller asked for.
    kind: str = VECTOR_INDEX
    #: Lance's own index-type vocabulary (``IVF_PQ``, ``BTREE``, ``FTS``, …). OPAQUE here: this package
    #: forwards it and never branches on it, the same rule `transform_specs` states for a task.
    index_type: str = ""
    name: str = ""
    #: pylance's OWN keyword arguments (``metric``, ``num_partitions``, ``base_tokenizer``, …),
    #: already translated from the spec request by the producer.
    #:
    #: TRANSLATED AT THE DOOR rather than here or at the worker, because the spec's field names and
    #: pylance's keyword names differ (the spec says ``distance_type``, pylance says ``metric``) and
    #: the catalog is the party that speaks both. A worker doing it would need a namespace handle
    #: pointed at the catalog's own root — a second writer to ``__manifest`` from a service that is
    #: deliberately a dataset-level worker.
    params: dict[str, Any] = {}

    @property
    def unit_id(self) -> str:
        """A deterministic id for this build, answered to the caller as the spec's ``transaction_id``.

        DERIVED rather than random so a redelivered publish names the same unit: the spec points a
        caller at ``ListTableIndices`` / ``DescribeTableIndexStats`` to follow progress, and an id
        that changed per attempt would make two deliveries of one request look like two builds.
        """
        digest = hashlib.sha256(f"{self.uri}\x00{self.column}\x00{self.kind}\x00{self.index_type}\x00{self.name}".encode()).hexdigest()[:24]
        return f"index-{digest}"
