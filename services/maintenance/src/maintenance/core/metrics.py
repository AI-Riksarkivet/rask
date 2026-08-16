"""OpenTelemetry domain metrics for the compaction service (exported OTLP-direct to GreptimeDB)."""

from __future__ import annotations

from opentelemetry import metrics


_meter = metrics.get_meter("lance.compaction")

_runs = _meter.create_counter(
    "compaction.runs",
    unit="{run}",
    description="Compaction sweeps triggered by the Dapr cron binding.",
)
_fragments_removed = _meter.create_counter(
    "compaction.fragments.removed",
    unit="{fragment}",
    description="Small Lance fragments merged away by compaction.",
)
_versions_removed = _meter.create_counter(
    "compaction.versions.removed",
    unit="{version}",
    description="Superseded Lance manifest versions GC'd.",
)
_indices_optimized = _meter.create_counter(
    "compaction.indices.optimized",
    unit="{index}",
    description="Secondary indices (vector/scalar/FTS) re-optimized to cover new fragments.",
)
#: #64 — datasets the pass REFUSED because their manifest sets a feature flag it cannot correctly
#: rewrite (base_paths / shallow clone, data overlays, anything unknown). Its own series, not a
#: sub-case of an error counter: nothing failed, and the number that matters is the TREND. The
#: supported-flag set is a whitelist, so a pylance upgrade that adds a legitimate flag shows up here
#: as a step change — and nowhere else — while the sweep otherwise still reports a clean run.
_refused = _meter.create_counter(
    "compaction.datasets.refused",
    unit="{dataset}",
    description="Datasets refused by the maintenance pass because of an unsupported manifest feature flag.",
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


#: F6(d) — datasets left alone because they are IN THE TRASH: dropped with a grace window, recoverable,
#: and therefore frozen. Its own series rather than a fold into `_refused` because the two answer
#: different questions: refused is "this dataset's LAYOUT defeats us", this is "this dataset's
#: GOVERNANCE state forbids us". A step change here without a matching drop count is the signal that the
#: exclusion is keyed wrong — the failure mode where a control-root misconfiguration quietly stops the
#: estate being maintained while every tick still reports zero errors.
_trashed_skipped = _meter.create_counter(
    "compaction.datasets.trashed_skipped",
    unit="{dataset}",
    description="Discovered datasets the maintenance pass left untouched because a trash record covers them.",
)


#: #79 reclamation. Separate series from the compaction counters above because they answer a different
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

#: The record kinds a purge can see (#96 — a recoverable cascade trashes namespaces too). Both series are
#: created on EVERY run so a dashboard has data from the first tick rather than reading "no data" until
#: the first namespace record happens to expire.
_KINDS = ("table", "namespace")


#: The lost-pass detectors. `compaction.runs` fires only AFTER the sweep
#: loop, so a process killed at dataset 400 of 900 was observationally identical to a tick that never
#: arrived. `started` fires before discovery; started minus completed IS the lost-pass count, and
#: `datasets.swept` inside the loop says how far a lost pass got. This pair is also the prerequisite
#: for any sweep-cadence or durability decision: without it nobody can measure how often a pass is
#: actually lost.
_runs_started = _meter.create_counter(
    "compaction.runs.started",
    unit="{run}",
    description="Compaction sweep passes STARTED (before discovery). started minus compaction.runs = passes lost mid-flight.",
)
_datasets_swept = _meter.create_counter(
    "compaction.datasets.swept",
    unit="{dataset}",
    description="Datasets the sweep loop reached this pass (including policy-skips) — the progress heartbeat of a running sweep.",
)


def record_run_started() -> None:
    _runs_started.add(1)


def record_dataset_swept() -> None:
    _datasets_swept.add(1)


def record_run() -> None:
    _runs.add(1)


def record_trash_purge(purged_by_kind: dict[str, int], refused_by_kind: dict[str, int], bytes_reclaimed: int) -> None:
    """Record one tick's reclamation. Always emits — adding 0 is a valid no-op that still CREATES the
    series (the :func:`record_reclaimed` rule), and for a reclaimer the zero is the interesting number:
    "nothing was purged this tick" and "the purge never ran" must not look identical on a dashboard."""
    for kind in _KINDS:
        _trash_purged.add(purged_by_kind.get(kind, 0), {"lance.maintenance.kind": kind})
        _trash_refused.add(refused_by_kind.get(kind, 0), {"lance.maintenance.kind": kind})
    _trash_bytes.add(bytes_reclaimed)


def record_reclaimed(fragments_removed: int, versions_removed: int, indices_optimized: int = 0) -> None:
    """Record what one sweep reclaimed + re-optimized across all datasets. Always emit — adding 0 is a valid
    no-op that still CREATES the counter series, so a dashboard/alert on ``rate(compaction_*_total[5m])``
    has data from the first sweep instead of reading "no data" until the first non-zero reclaim (obs audit
    2026-07-13)."""
    _fragments_removed.add(fragments_removed)
    _versions_removed.add(versions_removed)
    _indices_optimized.add(indices_optimized)


def record_trashed_skipped(datasets: int) -> None:
    """Record how many discovered datasets this tick left alone because they are IN THE TRASH (F6(d)).

    Always emits, including zero, for `record_refused`'s reason applied one rung out: this exclusion is
    keyed on a registry read, so a control-root misconfiguration or a bug in the path comparison could
    start excluding the whole estate — and a sweep that maintains nothing while reporting no errors is
    indistinguishable from a clean one without a series to look at.
    """
    _trashed_skipped.add(datasets)


def record_refused(datasets: int) -> None:
    """Record how many datasets this tick REFUSED on an unsupported manifest feature flag (#64).

    Always emits, for the :func:`record_reclaimed` reason and one sharper one: a whitelist that
    silently starts refusing the whole estate must be visible from the FIRST tick after the upgrade
    that caused it, not from whenever someone reads a cron response body."""
    _refused.add(datasets)


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
