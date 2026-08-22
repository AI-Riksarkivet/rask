"""A stage's LATENCY is unmeasurable, and the graph and the metric must not be able to disagree.

`medallion/core/metrics.py` had six counters and zero histograms: how many transitions happened, how
many were denied, how many were quality-blocked — and nothing about how LONG one took, how many rows
it moved, or how many bytes it wrote. "The silver stage got slower last week" was unanswerable from
deployed telemetry, on the estate's flagship flow.

`open_batch_process.md` B10 names the second half, and names the Ray stage explicitly:

    Every duration — coordinator activity, Ray stage, commit — uses `time.perf_counter` ... and the
    SAME number lands in the lineage run facet so the graph and the metric cannot disagree.

So a derived estimate is not acceptable here even though one was available: the stage watcher carries
`polls` and a poll interval, and multiplying them would have produced a plausible number that is not
what the lineage facet says. These pin both halves — measured, and identical in both places.
"""

from __future__ import annotations

from typing import Any

from medallion.schemas.events import build_run_event


def _event(**over: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "operation": "embed_features",
        "author": "data_eng",
        "job_namespace": "medallion",
        "inputs": [("bronze", "bronze$events")],
        "output_namespace": "silver",
        "output_name": "silver$features",
    }
    return build_run_event(**{**base, **over})


def test_the_metrics_module_can_record_a_stage_COMPLETION_not_just_a_count() -> None:
    """Six counters and zero histograms: a transition was counted, never timed or sized."""
    from medallion.core import metrics

    assert hasattr(metrics, "record_stage_completion"), (
        "no way to record a stage's duration/rows/bytes — `record_transition` counts that a stage "
        "happened and says nothing about how long it took or how much it moved"
    )


def test_the_duration_reaches_the_LINEAGE_FACET_so_it_cannot_disagree_with_the_metric() -> None:
    """B10's second half. A number in a metric and a different number in the graph is worse than one
    number, because a reader has no way to tell which is lying."""
    event = _event(duration_seconds=12.5)
    lance = event["run"]["facets"]["lance"]

    assert "duration_seconds" in lance, "the run facet carries no duration — the graph cannot corroborate the metric"
    assert lance["duration_seconds"] == 12.5


def test_a_run_with_NO_measured_duration_is_byte_identical_to_before() -> None:
    """Additive and optional, or it is a wire break.

    `tests/unit/test_events_parity.py` freezes the FAIL wire byte-for-byte against the legacy builder,
    and a FAIL never measures a duration (nothing was written). Omitting the key entirely — rather
    than nulling it — is what keeps that parity and follows this module's own silence-is-honest rule
    for absent values.
    """
    assert "duration_seconds" not in _event()["run"]["facets"]["lance"]
    fail = _event(event_type="FAIL", error_message="the Ray stage job ended FAILED")
    assert "duration_seconds" not in fail["run"]["facets"]["lance"]


def test_a_zero_second_stage_still_records_rather_than_vanishing() -> None:
    """0.0 is falsy, and a `if duration_seconds:` guard would silently drop the fastest runs — which
    are exactly the ones a latency histogram's lower buckets are for. Guard on `is not None`."""
    lance = _event(duration_seconds=0.0)["run"]["facets"]["lance"]
    assert lance.get("duration_seconds") == 0.0, "a 0.0s stage lost its duration to a falsy check"


def test_the_watch_span_uses_the_DETERMINISTIC_clock_not_a_wall_clock() -> None:
    """Inside a workflow body, `ctx.current_utc_datetime` is the only legal source of time.

    A wall clock or `perf_counter` returns a different value on every replay, which makes the workflow
    non-deterministic and is how an instance ends up permanently stuck. It is also why the start stamp
    rides `StageJobSpec` rather than a counter: a turn can resume in a different pod, where a carried
    `perf_counter` value means nothing.
    """
    from datetime import UTC, datetime
    from types import SimpleNamespace
    from typing import cast

    from medallion.workflow import _watch_seconds

    ctx = SimpleNamespace(current_utc_datetime=datetime(2026, 8, 22, 12, 30, tzinfo=UTC))
    assert _watch_seconds(cast("Any", ctx), datetime(2026, 8, 22, 12, 0, tzinfo=UTC).isoformat()) == 1800.0


def test_an_unmeasurable_watch_is_NONE_not_a_fabricated_zero() -> None:
    """An instance that started before the stamp existed genuinely does not know how long it ran.
    0.0 would land in the histogram's lowest bucket and read as an instant stage."""
    from datetime import UTC, datetime
    from types import SimpleNamespace
    from typing import cast

    from medallion.workflow import _watch_seconds

    ctx = cast("Any", SimpleNamespace(current_utc_datetime=datetime(2026, 8, 22, 12, 30, tzinfo=UTC)))
    assert _watch_seconds(ctx, "") is None
    assert _watch_seconds(ctx, "not-a-timestamp") is None


def test_the_trigger_REFUSES_an_absurd_duration_it_was_handed() -> None:
    """The trigger is untrusted input — it is re-parsed through the same guard as any bus arrival.
    A negative or absurd value must not reach the histogram, where it would poison the series."""
    import pytest
    from medallion.services.trigger_guards import StageTrigger
    from pydantic import ValidationError

    assert StageTrigger(ray_duration_seconds=42.0).ray_duration_seconds == 42.0
    for bad in (-1.0, 10_000_000.0):
        with pytest.raises(ValidationError):
            StageTrigger(ray_duration_seconds=bad)
