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


def record_dead_letter(app_label: str) -> None:
    """Count one dead-lettered cascade delivery, by the app that parked it (bounded — one per mover/producer).

    The cascade's DEAD_LETTERED signal, mirroring the lineage DLQ's ``record_outcome`` so a permanently
    stalled item is dashboardable + alertable, not only in scrollback."""
    _dlq_parked.add(1, {"lance.medallion.app": app_label})
