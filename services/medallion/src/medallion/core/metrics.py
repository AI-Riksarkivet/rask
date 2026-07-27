"""OpenTelemetry domain metrics for the medallion pipeline.

The golden signal for the cascade: how many stage transitions each mover completed. Exported via the
OTel SDK (``opentelemetry-instrument``) over OTLP to GreptimeDB, queryable in PromQL / Perses. Bounded
cardinality — only the namespaced ``lance.medallion.transition`` label (e.g. ``raw->bronze``); per-run ids
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
    description="Medallion stage transitions completed, by transition (e.g. raw->bronze).",
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


def record_transition(transition: str) -> None:
    """Increment the stage-transition counter for ``transition`` (``"<from>-><to>"``)."""
    _stage_transitions.add(1, {"lance.medallion.transition": transition})


def record_denied(transition: str) -> None:
    """Increment the denied counter (the mover was not authorized to produce the target stage)."""
    _stage_denied.add(1, {"lance.medallion.transition": transition})


def record_quality_blocked(transition: str) -> None:
    """Increment the quality-blocked counter (the produced data failed a quality assertion → not promoted)."""
    _stage_quality_blocked.add(1, {"lance.medallion.transition": transition})


def record_dead_letter(app_label: str) -> None:
    """Count one dead-lettered cascade delivery, by the app that parked it (bounded — one per mover/producer).

    The cascade's DEAD_LETTERED signal, mirroring the lineage DLQ's ``record_outcome`` so a permanently
    stalled item is dashboardable + alertable, not only in scrollback."""
    _dlq_parked.add(1, {"lance.medallion.app": app_label})
