"""OpenTelemetry domain metrics for the maintenance service (exported OTLP-direct to GreptimeDB).

A table's pass counts under the ``compaction.*`` prefix the alert rules and dashboards query; maintenance compacts
nothing (ADR 0155), and what they count is its own work: the datasets a pass reached, the versions it reclaimed, the
datasets it refused or failed. The worker counts its tasks under ``maintenance.tasks.*``, the store events and fired
schedules it settles, and samples its streams and durable consumers under ``maintenance.queue.*`` (ADR 0198 §2). Only
the instrumentation scope names the service."""

from __future__ import annotations

from typing import Literal, get_args

from opentelemetry import metrics

from lakehouse.lineage.signing import RefusalReason


#: What parks a TABLE ID, and the closed `refused_by` set of `compaction.tables.parked`: the vend door's 403, its 404
#: naming the table or namespace absent, and a policy record of the table's that cannot be read as one, which may be a
#: legal hold and so is never read as the defaults.
type CatalogRefusal = Literal["vend_denied", "table_not_governed", "policy_unreadable"]

_meter = metrics.get_meter("lance.maintenance")

_versions_removed = _meter.create_counter(
    "compaction.versions.removed",
    unit="{version}",
    description="Superseded Lance manifest versions GC'd.",
)
#: THE NUMBER THE SWEEP EXISTS TO PRODUCE, and until [[LH-099]] it reached three log sites and no
#: series — measured on the deployed estate 2026-09-19, `compaction_bytes_reclaimed_total` returned
#: ZERO series from GreptimeDB while `compaction_runs_total` returned one. Distinct from
#: `maintenance.trash.bytes_reclaimed`, which the PURGE feeds: the two answer different questions (what
#: superseded versions cost versus what dropped tables cost) and summing them would answer neither.
_bytes_reclaimed = _meter.create_counter(
    "compaction.bytes.reclaimed",
    unit="By",
    description="Bytes freed by GC'ing superseded Lance versions in a table's pass.",
)
#: #64 — datasets the pass REFUSED, labelled by the gate that refused them (`record_refused` names each).
#: Its own series, not a sub-case of an error counter: nothing failed, and the number that matters is the
#: TREND. The supported-flag set is a whitelist, so a pylance upgrade that adds a legitimate flag shows up
#: here as a step change in `manifest_flags` — and nowhere else — while the sweep still reports a clean run.
_refused = _meter.create_counter(
    "compaction.datasets.refused",
    unit="{dataset}",
    description="Datasets refused by the maintenance pass, by refused_by gate.",
)

#: PER TABLE, which `_refused` deliberately is not. A table parked on every tick is one row in
#: a refusal share measured at 45.3-46.5% of every sweep (2026-09-24), so only its own id can page on it.
#: `table_id` is bounded by the refused tables and `refused_by` by `CatalogRefusal`. No zero is emitted:
#: an absent series is the healthy estate, and a zero could name no table.
_tables_parked = _meter.create_counter(
    "compaction.tables.parked",
    unit="{dataset}",
    description="Datasets left untouched for the tick because their table id is parked (the catalog refused it, or its policy cannot be read), by table_id and refused_by.",
)

#: Datasets whose manifest carries reader flag 256 (mixed data file versions). Its own series because
#: `_refused` cannot page on one table: measured 2026-09-24, 45.3-46.5% of every sweep is already refused.
#: The flag is sticky and the table is unopenable by pylance 11 and lancedb 0.34, so one is an incident.
#: [[LH-279]] Declared bases the pre-pass judged sanctioned by NOTHING — not the referrer's own root,
#: not configured, not in the catalog's record. Each protects nothing; the count is how an operator
#: learns a writer planted one. Its own series because nothing was refused: the finding is the table
#: that declares the base, while the table it names is maintained as usual.
_unrecorded_bases = _meter.create_counter(
    "compaction.bases.unrecorded",
    unit="{base}",
    description="Declared base paths the maintenance pre-pass found sanctioned by nothing, counted each tick they are seen.",
)
_mixed_file_versions = _meter.create_counter(
    "compaction.datasets.mixed_file_versions",
    unit="{dataset}",
    description="Datasets whose data files are at more than one Lance file version (reader flag 256), counted each time the sweep opens one.",
)
#: [[LH-419]] Files a commit added that the store no longer holds as the catalog recorded them: overwritten in place,
#: which a writer's vend can do and no session policy refuses. Its own series because nothing was refused: the table is
#: maintained as usual while it serves bytes no commit wrote.
_overwritten_files = _meter.create_counter(
    "maintenance.files.overwritten",
    unit="{file}",
    description="Committed files the sweep found changed from what the catalog recorded at their commit, counted each tick they are seen.",
)
#: [[LH-286]] Fragments whose data file sits in their table's own data directory under a deletion vector: the deleted
#: rows' bytes stay there until a compaction rewrites the fragment, which the compaction gate refuses for such a table.
#: Reported rather than reclaimed (owner, 2026-10-24), so the count is how an operator sees what they cost.
_base_fragments_holding_deleted_rows = _meter.create_counter(
    "compaction.base_fragments.holding_deleted_rows",
    unit="{fragment}",
    description="Fragments keeping deleted rows' bytes in their table's data directory on a data base, counted each time the sweep expires their table.",
)


#: THE FAILURE SERIES, and its absence was the gap. Seven instruments here counted what the sweep
#: ACHIEVED and not one counted what it could not do, so a dataset failing every tick forever was
#: indistinguishable from a healthy estate on every surface that can raise an alarm. The error reached
#: OTel as a span status and one aggregate `maintenance_sweep` log line — and vmalert evaluates PromQL,
#: so neither can ever page. A real sweep on 2026-08-16 failed on 11 datasets and nothing anywhere said so.
#:
#: `error_type` is the STABLE class name (the same value the span carries), never the message: messages
#: carry URIs and object ids, which would make the series cardinality unbounded. The dataset itself is
#: deliberately NOT a label for the same reason — "which dataset" is a trace/log question, "is anything
#: failing, and is it getting worse" is the metric question, and only the second one can page.
_failed = _meter.create_counter(
    "compaction.datasets.failed",
    unit="{dataset}",
    description="Datasets the maintenance pass could not complete, by stable error class. A refusal is NOT a failure — see compaction.datasets.refused.",
)


#: #79 reclamation. Separate series from the sweep counters above because they answer a different
#: question: those say how much history was folded away, these say how many DROPPED objects had their
#: bytes destroyed — the only irreversible thing this service does.
_trash_purged = _meter.create_counter(
    "maintenance.trash.purged",
    unit="{record}",
    description="Expired trash records whose bytes were deleted and whose grants were revoked.",
)
_trash_refused = _meter.create_counter(
    "maintenance.trash.purge_refused",
    unit="{record}",
    description="Expired trash records the purge REFUSED (still registered, outside the maintained estate, "
    "a control prefix, or a failed revoke) — a rising refusal rate is drift, not noise.",
)
_trash_bytes = _meter.create_counter(
    "maintenance.trash.bytes_reclaimed",
    unit="By",
    description="Bytes reclaimed by the expired-trash purge.",
)

#: THE DRIFT REPORT, which until now reached a log line and nothing else. It decides whether the purge
#: may run at all, and it is the estate's own answer to "is the storage state understood" — so a count
#: only a reader of pod logs can see is a gate nobody can alert on. A GAUGE rather than a counter:
#: drift is a level that rises and falls, and `delta()` over a counter would read a repaired estate as
#: no change at all.
_drift_items = _meter.create_gauge(
    "maintenance.drift.items",
    unit="{item}",
    description="Items the reconcile found per drift category (`category` label) on the last tick.",
)
_trash_planned = _meter.create_counter(
    "maintenance.trash.purge_planned",
    unit="{record}",
    description="Expired trash records a DRY RUN would have reclaimed — an upper bound, since the catalog's judgement, "
    "a non-dataset location and a failing delete can each still refuse one and none can be evaluated without "
    "asking for or performing the act it guards. Non-zero here with `dry_run=false` nowhere is a preview nobody turned off.",
)

#: The record kinds a purge can see (#96 — a recoverable cascade trashes namespaces too). Both series are
#: created on EVERY run so a dashboard has data from the first tick rather than reading "no data" until
#: the first namespace record happens to expire.
_KINDS = ("table", "namespace")


#: Every dataset a table's pass reached, policy-skips included: the denominator `MaintenanceRefusalsRising` divides by.
_datasets_swept = _meter.create_counter(
    "compaction.datasets.swept",
    unit="{dataset}",
    description="Datasets a table's pass reached, including those its policy skipped.",
)


#: The bus doors that check an event's signature ([[XC-078]]), as the closed `door` label of the two series below.
type SignatureDoor = Literal["maintenance-arrival"]

#: Events a door refused because no listed signer's published key verifies them. A refusal is acknowledged and acts on
#: nothing, so this count and one log line are all it leaves. `reason` is `lakehouse.lineage.signing`'s closed `RefusalReason`, never the
#: event's own text, so the series stays a handful.
_signature_refused = _meter.create_counter(
    "maintenance.signature.refused",
    unit="{event}",
    description="Events a door refused because no listed signer's published key verifies their signature, by door and reason. A refusal is acked and acts on nothing.",
)
#: What a door in observe mode acted on and enforcing would have refused, or could not decide because the keys were
#: unreadable (`keys_unavailable`). Its own series, so the soak that decides whether enforcing is safe is never read as
#: refusals that happened.
_signature_would_refuse = _meter.create_counter(
    "maintenance.signature.would_refuse",
    unit="{event}",
    description="Events a door in observe mode acted on although enforcing would have refused them, or could not read the keys to decide, by door and reason.",
)


def create_signature_refused_series() -> None:
    """Create every door and reason series of `maintenance.signature.refused` at 0.

    The alert over this counter is `rate(...) > 0`, and a cumulative counter exports nothing for a series before its first
    add: a series born at 1 has no 0 before it, so `rate()` sees no increase and the first refusal never pages. Called
    when the door is registered, at import, after `opentelemetry-instrument` installed the MeterProvider at interpreter
    start: an add through the proxy meter before then is dropped. Both measured on opentelemetry-sdk 1.44.0.
    """
    for door in get_args(SignatureDoor.__value__):
        for reason in get_args(RefusalReason.__value__):
            _signature_refused.add(0, {"door": door, "reason": reason})


def record_signature_refused(*, door: SignatureDoor, reason: str) -> None:
    """Count one event ``door`` refused. Keyword-only: both are strings, and a swapped pair would put a reason in the door label."""
    _signature_refused.add(1, {"door": door, "reason": reason})


def record_signature_would_refuse(*, door: SignatureDoor, reason: str) -> None:
    """Count one event ``door`` acted on in observe mode that enforcing would have refused for ``reason``."""
    _signature_would_refuse.add(1, {"door": door, "reason": reason})


def record_dataset_swept() -> None:
    _datasets_swept.add(1)


def record_trash_purge(
    *,
    purged_by_kind: dict[str, int],
    refused_by_kind: dict[str, int],
    bytes_reclaimed: int,
    planned_by_kind: dict[str, int] | None = None,
    dry_run: bool = False,
) -> None:
    """Record one tick's reclamation. Always emits — adding 0 is a valid no-op that still CREATES the
    series (the :func:`record_reclaimed` rule), and for a reclaimer the zero is the interesting number:
    "nothing was purged this tick" and "the purge never ran" must not look identical on a dashboard.

    EVERY POINT CARRIES ``dry_run``, and that dimension is load-bearing in two directions. A preview
    still counts its refusals, so without it a preview's refusals read as a real pass's and the tick
    appears to be reclaiming while it reclaims nothing. And a `trashPurge: true` deployment that left
    `trashPurgeDryRun` on gets a PERMANENT preview — the dry run wins by design — which nothing else in
    the estate would ever reveal.
    """
    attributes = {"lance.maintenance.dry_run": dry_run}
    planned_by_kind = planned_by_kind or {}
    for kind in _KINDS:
        by_kind = {**attributes, "lance.maintenance.kind": kind}
        _trash_purged.add(purged_by_kind.get(kind, 0), by_kind)
        _trash_refused.add(refused_by_kind.get(kind, 0), by_kind)
        _trash_planned.add(planned_by_kind.get(kind, 0), by_kind)
    _trash_bytes.add(bytes_reclaimed, attributes)


def record_reclaimed(*, versions_removed: int, bytes_removed: int = 0) -> None:
    """Record what expiry reclaimed. Always emit — adding 0 is a valid no-op that still CREATES the counter series, so a
    dashboard/alert on ``rate(compaction_*_total[5m])`` has data from the first sweep instead of reading "no data"
    until the first non-zero reclaim (obs audit 2026-07-13).

    ``bytes_removed`` is the one an operator asks for: a version count says how much WORK happened, never how much
    disk came back. A reclaimer whose byte series is flat at zero is either idle or broken, and those must be
    distinguishable from a series that does not exist.
    """
    _versions_removed.add(versions_removed)
    _bytes_reclaimed.add(bytes_removed)


def record_refused(datasets: int, refused_by: str | None = None) -> None:
    """Record how many datasets this tick REFUSED, by WHICH GATE refused them.

    Always emits, for the :func:`record_reclaimed` reason and one sharper one: a whitelist that
    silently starts refusing the whole estate must be visible from the FIRST tick after the upgrade
    that caused it, not from whenever someone reads a cron response body.

    THE GATE IS A LABEL BECAUSE THE MERGED NUMBER IS NOT ACTIONABLE, and the estate proved it:
    measured 2026-09-24, **45.3-46.5% of every sweep is refused**, steady across fourteen hours and
    ~600 datasets a tick. A number that large and that flat is a standing condition, and the
    unlabelled series could not say whether it was somebody else's clone (`protected_base`, true
    forever), a manifest feature a pylance upgrade would support (`manifest_flags`), an unparseable
    branch directory (`invalid_ref`), a denied credential (`vend_denied`), an
    id the catalog governs no table by (`table_not_governed`), an id the catalog governs at another
    location (`governed_elsewhere`) or maintenance's own service credential rejected with a 401
    (`unauthenticated`). `DatasetResult.refused_by`
    already carried the answer and `summarize_refusals` already counted by it — it reached the sweep's
    WARNING and the response body and stopped there, which is the same shape as
    `compaction.bytes.reclaimed` before [[LH-099]].

    CARDINALITY IS BOUNDED BY CONSTRUCTION: the gates are a closed set named in `DatasetResult`, not a
    message and never the dataset — the same rule `record_failed` states for `error_type`.

    The ZERO stays UNLABELLED. Emitting it against a gate would require knowing which gates exist this
    tick, which the caller cannot know (`record_failed` documents the same limit), and inventing a
    `refused_by="none"` would put a value in the label set that names no gate. `sum(rate(...))` is
    unaffected; `sum by (refused_by)` reads the breakdown with the zeros in the empty-label series.
    """
    if datasets and refused_by:
        _refused.add(datasets, {"refused_by": refused_by})
        return
    _refused.add(datasets)


#: A task its worker ran to the end and acknowledged, by kind: what tells that the hourly audit and the daily scan run,
#: and that a worker runs at all (`MaintenanceAuditNotCompleting`, `MaintenanceTaskMetricsMissing`).
_tasks_completed = _meter.create_counter(
    "maintenance.tasks.completed",
    unit="{task}",
    description="Maintenance tasks their worker ran to the end and acknowledged, by kind.",
)
#: A task its worker ended on its last delivery (ADR 0198 §2), by kind: a pass, a release or an audit that failed every
#: time it was tried, or a message no task parses (``unreadable``). Its table waits for its next commit or the audit.
_tasks_ended = _meter.create_counter(
    "maintenance.tasks.ended",
    unit="{task}",
    description="Maintenance tasks ended on their last delivery, by kind.",
)
#: A task still running past ``MAINTENANCE_MAX_PASS_SECONDS``, by kind: until its replica ends itself at twice that, the
#: count is the only sign.
_passes_overran = _meter.create_counter(
    "maintenance.passes.overran",
    unit="{task}",
    description="Maintenance tasks still running past their bound, by kind.",
)


#: A task its worker handed back for another delivery after its run failed, by kind: a store or a catalog refusing a
#: table's pass every time shows here long before its last delivery ends it.
_tasks_retried = _meter.create_counter(
    "maintenance.tasks.retried",
    unit="{task}",
    description="Maintenance tasks handed back for another delivery after their run failed, by kind.",
)
#: One task's run, from the claim on its table to its acknowledgement, by kind: a task that completed. Second-scale
#: boundaries up to the pass bound's order (`maintenance.queue.maxPassSeconds`, 1800 s by default), since the SDK's
#: default boundaries are millisecond-tuned and would put every pass above 10 s in one bucket.
_task_duration = _meter.create_histogram(
    "maintenance.tasks.duration",
    unit="s",
    description="Seconds from the claim on a task's table to the task's acknowledgement, by kind, for tasks that completed.",
    explicit_bucket_boundaries_advisory=[0.1, 0.5, 1.0, 5.0, 15.0, 60.0, 300.0, 900.0, 1800.0, 3600.0],
)

#: How the worker settled one store event it pulled (:meth:`maintenance.services.queue.Worker.run_store_events`), the
#: closed `outcome` set of `maintenance.store_events.settled`: acknowledged with a table's pass held (``held``),
#: acknowledged holding nothing (``ignored``: a key under no table, a delete, or a table whose follow-up was held
#: already), handed back for another delivery (``retried``), ended at once as no store event (``unreadable``), and ended
#: on its last delivery, or on the one past it after its worker died every time (``ended``).
type StoreEventOutcome = Literal["held", "ignored", "retried", "unreadable", "ended"]

_store_events_settled = _meter.create_counter(
    "maintenance.store_events.settled",
    unit="{event}",
    description="Store events the worker settled, by outcome: held, ignored, retried, unreadable, ended.",
)
#: A fired schedule the queue did not take (:meth:`maintenance.services.queue.Worker._move`): it is delivered again
#: after a delay, and a refusal that stands is a table whose time never comes.
_schedules_not_queued = _meter.create_counter(
    "maintenance.schedules.not_queued",
    unit="{schedule}",
    description="Fired schedules the work queue refused or did not answer, each given back to be moved again.",
)

#: What the worker's sampler reads off the bus every minute (:func:`maintenance.services.queue.sample_queue`). Synchronous
#: gauges: one exports nothing until it is first set, so a web pod, which imports this module and runs no sampler,
#: exports no constant 0 beside the worker's truth, and once set it exports its last value at every collection
#: (measured on opentelemetry-sdk 1.45.2, cumulative temporality). A sample the bus does not answer leaves a gauge at
#: its last value, logged (`maintenance_queue_unsampled`).
_queue_messages = _meter.create_gauge(
    "maintenance.queue.messages",
    unit="{message}",
    description="Messages a maintenance stream holds, by stream, as the worker last sampled it.",
)
#: Named without its unit: the exporter appends the unit, so the series is `maintenance_queue_oldest_seconds`, as
#: `outbox.oldest_age` (unit ``s``) is `outbox_oldest_age_seconds` (deploy/alerting/rules.yml).
_queue_oldest = _meter.create_gauge(
    "maintenance.queue.oldest",
    unit="s",
    description="Age of the oldest message a work-queue stream holds (0 when it holds none), by stream, as the worker last sampled it.",
)
_queue_ack_pending = _meter.create_gauge(
    "maintenance.queue.ack_pending",
    unit="{message}",
    description="Deliveries a maintenance durable consumer awaits an acknowledgement for, by consumer, as the worker last sampled it.",
)
_queue_redelivered = _meter.create_gauge(
    "maintenance.queue.redelivered",
    unit="{message}",
    description="Messages a maintenance durable consumer has delivered more than once and not yet settled, by consumer, as the worker last sampled it.",
)


#: How the hourly audit found each governed bucket's store events (:mod:`maintenance.services.bucket_events`): configured
#: for every object write, without a rule for them, or unknown, the store refusing or failing the read. A counter, added
#: once per bucket per audit: whichever replica runs an audit, the replicas' series sum to what the audits found, and a
#: fix stops the increase, so an alert on it clears within its window; seeded at 0 by outcome when a worker starts.
type BucketEventsOutcome = Literal["configured", "without", "unknown"]

_store_event_buckets_checked = _meter.create_counter(
    "maintenance.store_event_buckets.checked",
    unit="{bucket}",
    description="Governed buckets the hourly audit asked the store about, by outcome: configured, without, unknown.",
)


def record_store_event_bucket_checked(*, outcome: BucketEventsOutcome) -> None:
    """Record how the audit found one governed bucket's store events."""
    _store_event_buckets_checked.add(1, {"outcome": outcome})


def create_task_series(kinds: tuple[str, ...]) -> None:
    """Create each kind's series of the worker's task counters at 0 when it starts, the `unreadable` one of the ended
    count, every outcome of the store-event count and of the bucket-events count, and the not-queued count: a series born at 1 has no 0 before it, so
    `increase()` sees no increase and the first task ended or overrun never pages, and a worker that has completed
    nothing yet would read "no data"."""
    for kind in kinds:
        for counter in (_tasks_completed, _tasks_ended, _passes_overran, _tasks_retried):
            counter.add(0, {"kind": kind})
    _tasks_ended.add(0, {"kind": "unreadable"})
    for outcome in get_args(StoreEventOutcome.__value__):
        _store_events_settled.add(0, {"outcome": outcome})
    _schedules_not_queued.add(0)
    for outcome in get_args(BucketEventsOutcome.__value__):
        _store_event_buckets_checked.add(0, {"outcome": outcome})


def record_task_retried(*, kind: str) -> None:
    """Record a task handed back for another delivery after its run failed."""
    _tasks_retried.add(1, {"kind": kind})


def record_task_duration(*, kind: str, seconds: float) -> None:
    """Record how long a completed task held its claim, until its acknowledgement."""
    _task_duration.record(seconds, {"kind": kind})


def record_store_event_settled(*, outcome: StoreEventOutcome) -> None:
    """Record how the worker settled one store event."""
    _store_events_settled.add(1, {"outcome": outcome})


def record_schedule_not_queued() -> None:
    """Record a fired schedule the queue did not take."""
    _schedules_not_queued.add(1)


def record_stream_depth(*, stream: str, messages: int, oldest_seconds: float | None) -> None:
    """Set a stream's sampled depth; ``oldest_seconds`` only for a stream whose age is read (a work queue a backlog ages
    on), None for one that holds messages for their time (the schedules)."""
    _queue_messages.set(messages, {"stream": stream})
    if oldest_seconds is not None:
        _queue_oldest.set(oldest_seconds, {"stream": stream})


def record_consumer_backlog(*, consumer: str, ack_pending: int, redelivered: int) -> None:
    """Set a durable consumer's sampled backlog."""
    _queue_ack_pending.set(ack_pending, {"consumer": consumer})
    _queue_redelivered.set(redelivered, {"consumer": consumer})


def record_task_completed(*, kind: str) -> None:
    """Record a task its worker ran to the end and acknowledged."""
    _tasks_completed.add(1, {"kind": kind})


def record_task_ended(*, kind: str) -> None:
    """Record a task its worker ended on its last delivery, or one whose message no task parses."""
    _tasks_ended.add(1, {"kind": kind})


def record_pass_overran(*, kind: str) -> None:
    """Record a task still running past its bound."""
    _passes_overran.add(1, {"kind": kind})


def record_table_parked(*, table_id: str, refused_by: CatalogRefusal) -> None:
    """Record that ``table_id`` is parked this tick: the catalog refused it at one door, or its policy cannot be read.

    Keyword-only: both are strings, and a swapped pair would put a door name in the table label.
    """
    _tables_parked.add(1, {"table_id": table_id, "refused_by": refused_by})


def record_mixed_file_versions(datasets: int) -> None:
    """Record how many datasets carry mixed data file versions (reader flag 256).

    Always emits, including zero, so the series exists before the first mix and the alert's
    `increase()` sees that mix as a step from 0 rather than as a series appearing.
    """
    _mixed_file_versions.add(datasets)


def record_overwritten_files(files: int) -> None:
    """Record how many committed files the sweep found overwritten in place ([[LH-419]]).

    Always emits, including zero, for `record_mixed_file_versions`' reason.
    """
    _overwritten_files.add(files)


def record_base_fragments_holding_deleted_rows(fragments: int) -> None:
    """Record how many fragments keep deleted rows' bytes in their table's data directory ([[LH-286]]).

    Always emits, including zero, for `record_mixed_file_versions`' reason.
    """
    _base_fragments_holding_deleted_rows.add(fragments)


def record_unrecorded_bases(bases: int) -> None:
    """Record how many declared bases the pre-pass found sanctioned by nothing.

    Always emits, including zero, for `record_mixed_file_versions`' reason: a plant must read as a step
    from 0, not as a series appearing.
    """
    _unrecorded_bases.add(bases)


def record_failed(errors_by_type: dict[str, int]) -> None:
    """Record this tick's per-dataset FAILURES, keyed by stable error class.

    Emits one point per class rather than a total so "eleven datasets failed" and "one dataset failed
    eleven ways" are distinguishable, and so a NEW class appearing is visible as a new series rather
    than as a bump in an existing one.

    Always emits — including zero-valued classes it has seen before is not possible here (the caller
    only knows this tick's classes), which is exactly why the alert rule keys on `increase()` over a
    window rather than on the instantaneous value: a series that stops being written must read as
    "stopped failing", and only a windowed function gives that.
    """
    for error_type, count in errors_by_type.items():
        _failed.add(count, {"error.type": error_type})


def record_drift(counts: dict[str, int]) -> None:
    """Record the last tick's drift, one series per CHECKED category.

    ONLY WHAT WAS CHECKED, which is the report's own discipline carried onto the wire: `counts` omits a
    category that was unavailable or skipped rather than zeroing it, precisely so a 0 can never read as
    "clean". Emitting 0 for an absent category here would undo that where it matters most — on the
    series an alert fires from.
    """
    for category, value in counts.items():
        _drift_items.set(value, {"category": category})


#: What a table's orphan check found, the closed `outcome` set of `maintenance.orphans.checks`
#: (:mod:`maintenance.services.orphan_check`); a check whose read failed is counted ``incomplete`` before it raises.
type OrphanCheckOutcome = Literal[
    "clean",
    "reclaimable",
    "stranded",
    "raised",
    "excluded",
    "unnamed",
    "policy_unreadable",
    "ungoverned",
    "trashed",
    "gone",
    "disabled",
    "incomplete",
]

#: Each table's orphan check, by what it found. No table label: which table is the check's log line's question, and a
#: series per table would grow with the estate.
_orphan_checks = _meter.create_counter(
    "maintenance.orphans.checks",
    unit="{check}",
    description="Table orphan checks, by outcome.",
)
#: Files a check found above its table's listing floor, which Lance cannot see until the floor is raised.
_stranded_files = _meter.create_counter(
    "maintenance.orphans.stranded",
    unit="{file}",
    description="Unreferenced files a table's orphan check found above its listing floor, which Lance's cleanup cannot list.",
)


def create_orphan_check_series() -> None:
    """Create every outcome's series of the check counter, and the stranded-file counter, at 0 when the worker starts,
    for `create_task_series`' reason: a series born at 1 has no 0 before it."""
    for outcome in get_args(OrphanCheckOutcome.__value__):
        _orphan_checks.add(0, {"outcome": outcome})
    _stranded_files.add(0)


def record_orphan_check(*, outcome: OrphanCheckOutcome, stranded: int) -> None:
    """Record one table's orphan check and the stranded files it found."""
    _orphan_checks.add(1, {"outcome": outcome})
    _stranded_files.add(stranded)
