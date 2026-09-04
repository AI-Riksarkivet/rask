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

from collections import OrderedDict
from typing import Final

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
_stage_media_underivable = _meter.create_counter(
    "medallion.stage.media_underivable",
    unit="{transition}",
    description="Stage triggers DROPped on deterministic bad media (the payload matched the content probe but cannot decode); no quality assertion ran.",
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

#: HOW FAR BEHIND its source a destination tier is, in source versions. A LEVEL, not an event: true
#: continuously and read by asking, so a gauge evaluated with `for:` rather than a per-tick counter —
#: row 23 of `open_estate-verification.md` records what the other choice costs, one gap counted 1210
#: times with every other service's errors buried under it.
#:
#: SYNCHRONOUS, not observable. The value comes from a catalog read and a lineage query; an observable
#: gauge would run that IO inside an SDK collection callback, where it blocks the exporter and fails
#: invisibly. The cron tick does the reads and sets the level it found.
#:
#: An UNKNOWN lag publishes no point at all — see `cascade_lag.record_edge_lag` for why no sentinel is
#: safe here.
_cascade_lag = _meter.create_gauge(
    "medallion.cascade.lag",
    unit="{version}",
    description="Source versions a destination tier has not yet consumed — the LOSS detector a refusal counter cannot be.",
)

_stage_refused = _meter.create_counter(
    "medallion.stage.refused",
    unit="{trigger}",
    description="Stage triggers REFUSED before any read or write, by reason (malformed|unconfined_uri|bad_project|routing_disabled|unresolvable_lane).",
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


_stage_outcomes = _meter.create_counter(
    "medallion.stage.outcome",
    unit="{run}",
    description="Stage-watch runs by terminal VERDICT (succeeded|failed|abandoned|unnotified) — the failure signal Dapr's own family cannot carry.",
)
_train_outcomes = _meter.create_counter(
    "medallion.train.outcome",
    unit="{run}",
    description="Training-watch runs by terminal verdict.",
)
_promotion_outcomes = _meter.create_counter(
    "medallion.promotion.outcome",
    unit="{review}",
    description="Promotion reviews by decision.",
)


def record_stage_outcome(verdict: str, *, duration_seconds: float | None = None) -> None:
    """Count one stage-watch run by its terminal verdict, and record what it COST even when it failed.

    WHY THIS EXISTS WHEN DAPR ALREADY COUNTS WORKFLOWS. `dapr_runtime_workflow_execution_count_total`
    carries a `status` label, and for this codebase that label is FALSE: all three workflow services
    convert failure into a RETURNED value rather than raising, so the orchestrator completes normally
    and the sidecar records `status="success"`. Measured across the live estate — every app_id holds
    `success` and nothing else, including runs whose activities the sidecar separately labels `failed`.
    An alert on `status="failed"` therefore reads green while every run dies. The verdict is a fact only
    the application knows, so only the application can record it.

    DURATION ON THE NON-SUCCESS PATHS, which is the point of taking it here. `_watch_seconds` is
    computed for the abandoned and failed verdicts too, and only the success path
    (`publish_stage_ready`) ever recorded it — so p95 stage latency was survivorship-biased BY
    CONSTRUCTION: the runs that take longest are exactly the ones that hit the watch ceiling and get
    excluded. Reuses `medallion.stage.duration` rather than opening a second histogram, so the success
    and failure paths remain comparable in one series.

    `verdict` is a CLOSED vocabulary owned by `StageJobOutcome` (succeeded|failed|abandoned|unnotified)
    — never a value off a payload. Submission ids, tokens and datasets stay on spans and logs.
    """
    attrs = {"lance.medallion.verdict": verdict}
    _stage_outcomes.add(1, attrs)
    if duration_seconds is not None:
        _stage_duration.record(duration_seconds, attrs)


def record_train_outcome(verdict: str) -> None:
    """Count one training-watch run by verdict. Same argument as `record_stage_outcome`."""
    _train_outcomes.add(1, {"lance.medallion.verdict": verdict})


def record_promotion_outcome(decision: str) -> None:
    """Count one promotion review by decision — a closed set owned by the review activity."""
    _promotion_outcomes.add(1, {"lance.medallion.decision": decision})


#: Volume keys already counted, so a re-run of pass 2 does not add a stage's output twice.
#:
#: Bounded and FIFO because this is a metrics guard, not a ledger: an unbounded set in a long-lived
#: mover is a leak, and the duplicates worth catching arrive seconds apart (an activity retry, or an
#: at-least-once redelivery of `sub_topic`).
_counted_volume: Final[OrderedDict[str, None]] = OrderedDict()
_COUNTED_VOLUME_MAX: Final[int] = 4096


def record_stage_completion(
    transition: str,
    *,
    duration_seconds: float,
    rows: int | None = None,
    size_bytes: int | None = None,
    volume_key: str | None = None,
) -> None:
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

    # VOLUME IS DEDUPED, LATENCY IS NOT, and the asymmetry is deliberate. A duplicated pass 2 -- an
    # activity retry, or ordinary at-least-once redelivery of `sub_topic` -- re-runs to completion,
    # because a same-version re-publish is ACCEPTED by `publication.publish`. Rows and bytes are
    # cumulative, so counting them twice over-reports by a whole stage's output; a duration is a
    # histogram observation, and a second sample of real work done is not a lie.
    #
    # Process-local, and that limit is stated rather than hidden: a redelivery landing on a DIFFERENT
    # replica still double-counts. Catching the same-process case is most of the value at none of the
    # cost of a shared store for a metric.
    if volume_key is not None:
        if volume_key in _counted_volume:
            return
        _counted_volume[volume_key] = None
        while len(_counted_volume) > _COUNTED_VOLUME_MAX:
            _counted_volume.popitem(last=False)

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


def record_media_underivable(transition: str) -> None:
    """Count one stage trigger DROPped on deterministic bad media (undecodable payload).

    Its OWN counter, not ``record_quality_blocked``: the media path shares the gate's OUTCOME contract
    (record the FAIL run, DROP so redelivery cannot re-read broken bytes) but runs no data-quality
    assertion, and folding it into ``medallion.stage.quality_blocked`` made the gate's series report
    blocks the gate never issued — bad bytes in bronze and a tuned-too-tight gate are different pages.
    """
    _stage_media_underivable.add(1, {"lance.medallion.transition": transition})


def cascade_lag_gauge() -> object:
    """The lag gauge itself, for `cascade_lag.record_edge_lag` to set.

    Handed out rather than wrapped, because the decision this metric turns on — publish, or stay
    silent — belongs with the arithmetic that knows whether the value is known, not here.
    """
    return _cascade_lag


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

    The same argument `record_other_lane` makes, for every PRE-FLIGHT refusal: a DROP is an ack, so
    Dapr neither redelivers nor dead-letters and nothing downstream records the event. A rejected
    `from_uri` is the signal that someone is publishing triggers this mover should not honour, and a
    tenant trigger arriving with registry resolution off is a deployment gap that halts that tenant's
    cascade permanently — both are worth an alert, and neither raises one from a log line.

    THIS COUNTS EVERY UNCOUNTED HALT, not merely the shape guard's two. It said "the two most
    security-relevant refusals … are the only DROPs that leave no trace", which described the
    vocabulary of the day rather than the handler: three more refusals had a log line and nothing
    else. They are the reason the medallion's lineage lane must NOT carry them — a repeating
    operational condition is a metric, not an event (`docs/DECISIONS.md`, 2026-08-16) — so this is
    where they land.

    `reason` is a CLOSED vocabulary set by this module's callers — `malformed`, `unconfined_uri`,
    `bad_project`, `routing_disabled`, `unresolvable_lane` — and never a value off the payload, the
    estate's bounded-cardinality rule. The offending token, URI, project and dataset name stay on the
    log line, where per-event data belongs; a counter labelled by a caller-supplied string is an
    unbounded series and, here, would also publish attacker-chosen strings into the metrics store.
    """
    _stage_refused.add(1, {"lance.medallion.transition": transition, "lance.medallion.reason": reason})


def record_dead_letter(app_label: str) -> None:
    """Count one dead-lettered cascade delivery, by the app that parked it (bounded — one per mover/producer).

    The cascade's DEAD_LETTERED signal, mirroring the lineage DLQ's ``record_outcome`` so a permanently
    stalled item is dashboardable + alertable, not only in scrollback."""
    _dlq_parked.add(1, {"lance.medallion.app": app_label})
