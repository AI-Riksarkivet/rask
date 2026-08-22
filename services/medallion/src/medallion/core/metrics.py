"""OpenTelemetry domain metrics for the medallion pipeline.

The golden signal for the cascade: how many stage transitions each mover completed. Exported via the
OTel SDK (``opentelemetry-instrument``) over OTLP to GreptimeDB, queryable in PromQL / Perses. Bounded
cardinality — only the namespaced ``lance.medallion.transition`` label (e.g. ``bronze->silver``); per-run ids
stay on spans/logs. (Dot-namespaced under the project's `lance.*` convention —
deliberately NOT the otel skill's reverse-DNS letter, pinned in todo_fable; in PromQL the dots
become underscores →
``lance_medallion_transition``.)
"""

from __future__ import annotations

from opentelemetry import metrics


_meter = metrics.get_meter("lance.medallion")

_stage_transitions = _meter.create_counter(
    "medallion.stage.transitions",
    unit="{transition}",
    description="Medallion stage transitions completed, by transition (e.g. bronze->silver).",
)
_stage_denied = _meter.create_counter(
    "medallion.stage.denied",
    unit="{transition}",
    description="Stage transitions DENIED by the FGA gate (the mover lacked the required role).",
)
_stage_quality_blocked = _meter.create_counter(
    "medallion.stage.quality_blocked",
    unit="{transition}",
    description="Stage transitions BLOCKED by the quality gate (a data-quality assertion failed).",
)
_dlq_parked = _meter.create_counter(
    "medallion.dlq.parked",
    unit="{delivery}",
    description="Cascade deliveries DEAD-LETTERED — parked after the Dapr retry schedule was exhausted.",
)

_stage_other_lane = _meter.create_counter(
    "medallion.stage.other_lane",
    unit="{trigger}",
    description="Stage triggers DROPped as another ingest lane's (the arrived dataset is not this mover's input).",
)

_stage_refused = _meter.create_counter(
    "medallion.stage.refused",
    unit="{trigger}",
    description="Stage triggers DROPped by the shape guard — malformed payload, or a from_uri outside this stage's storage root.",
)


#: SECOND-scale boundaries, set explicitly because the SDK's default advisory is tuned for
#: MILLISECOND web latency (its top bucket is 10s) and a medallion stage runs for minutes to hours —
#: every run would land in `+Inf` and every quantile would read flat. `lineage/core/metrics.py:35-38`
#: documents having been bitten by exactly this, which is why it is copied rather than rediscovered.
_STAGE_DURATION_BUCKETS = [1.0, 5.0, 15.0, 60.0, 300.0, 900.0, 1800.0, 3600.0, 7200.0, 21600.0, 86400.0]

_stage_duration = _meter.create_histogram(
    "medallion.stage.duration",
    unit="s",
    description="Wall-clock seconds a completed stage transition took, from dispatch to terminal outcome.",
    explicit_bucket_boundaries_advisory=_STAGE_DURATION_BUCKETS,
)
_stage_rows = _meter.create_counter(
    "medallion.stage.rows",
    unit="{row}",
    description="Rows written by completed stage transitions.",
)
_stage_bytes = _meter.create_counter(
    "medallion.stage.bytes",
    unit="By",
    description="Bytes written by completed stage transitions.",
)


def record_stage_completion(transition: str, *, duration_seconds: float, rows: int | None = None, size_bytes: int | None = None) -> None:
    """Record what a completed transition COST — its latency, and what it moved.

    ``duration_seconds`` must be a MEASURED ``time.perf_counter`` delta, and the caller must hand the
    SAME number to ``build_run_event(duration_seconds=…)``. That pairing is `docs/architecture/batch-processing-invariants.md`
    B10, and it exists so the graph and the metric cannot disagree: a derived estimate (the stage
    watcher's ``polls × poll_interval``, say) is plausible, cheap, and produces a number the lineage
    facet does not agree with — leaving a reader no way to tell which of the two is lying.

    ``rows``/``size_bytes`` are optional because only a MEASURED write knows them; a stage that
    committed nothing records its latency and no volume, rather than a misleading zero.

    Attributes carry only the bounded ``lance.medallion.transition`` key, exactly like every counter
    above — per-run ids stay on spans and logs. Rows and bytes are unbounded VALUES, which is fine;
    it is unbounded LABELS that multiply series.
    """
    attrs = {"lance.medallion.transition": transition}
    _stage_duration.record(duration_seconds, attrs)
    if rows is not None:
        _stage_rows.add(rows, attrs)
    if size_bytes is not None:
        _stage_bytes.add(size_bytes, attrs)


def record_transition(transition: str) -> None:
    """Increment the stage-transition counter for ``transition`` (``"<from>-><to>"``)."""
    _stage_transitions.add(1, {"lance.medallion.transition": transition})


def record_denied(transition: str) -> None:
    """Increment the denied counter (the mover was not authorized to produce the target stage)."""
    _stage_denied.add(1, {"lance.medallion.transition": transition})


def record_quality_blocked(transition: str) -> None:
    """Increment the quality-blocked counter (the produced data failed a quality assertion → not promoted)."""
    _stage_quality_blocked.add(1, {"lance.medallion.transition": transition})


def record_other_lane(transition: str) -> None:
    """Count one trigger DROPped as another ingest lane's (the arrived dataset is not this mover's input).

    Labelled by transition only, never by the arrived dataset name — a dataset is caller-supplied and
    would make this counter's cardinality unbounded.

    This exists because the drop is otherwise invisible. DROP is an ack, so Dapr neither redelivers nor
    dead-letters, and the app records nothing. Before the lane guard, a ``bronze$pages`` arrival drove
    the events mover into a deterministic FAIL — and that FAIL is precisely the evidence
    ``docs/architecture/live-proof-2026-07-28.md`` used to show the page lane had no consumer. Fixing
    the wrong behaviour must not also delete the signal that revealed it.
    """
    _stage_other_lane.add(1, {"lance.medallion.transition": transition})


def record_refused(transition: str, reason: str) -> None:
    """Count one trigger DROPped by the shape guard, by transition and REASON.

    The same argument `record_other_lane` makes, for the guard's two refusals: a DROP is an ack, so
    Dapr neither redelivers nor dead-letters and nothing downstream records the event. Without this
    the two most security-relevant refusals in the handler — a malformed payload, and a `from_uri`
    naming a location outside this stage's storage root — are the only DROPs that leave no trace,
    which is exactly backwards: a rejected `from_uri` is the signal that someone is publishing
    triggers this mover should not honour, and it is worth an alert.

    `reason` is a CLOSED vocabulary set by this module's callers (`malformed`, `unconfined_uri`) and
    never a value off the payload — the estate's bounded-cardinality rule. The offending token, URI
    and dataset name stay on the log line, where per-event data belongs; a counter labelled by a
    caller-supplied string is an unbounded series and, here, would also publish attacker-chosen
    strings into the metrics store.
    """
    _stage_refused.add(1, {"lance.medallion.transition": transition, "lance.medallion.reason": reason})


def record_dead_letter(app_label: str) -> None:
    """Count one dead-lettered cascade delivery, by the app that parked it (bounded — one per mover/producer).

    The cascade's DEAD_LETTERED signal, mirroring the lineage DLQ's ``record_outcome`` so a permanently
    stalled item is dashboardable + alertable, not only in scrollback."""
    _dlq_parked.add(1, {"lance.medallion.app": app_label})
