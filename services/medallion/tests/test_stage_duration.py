"""A stage's LATENCY is unmeasurable, and the graph and the metric must not be able to disagree.

`medallion/core/metrics.py` had six counters and zero histograms: how many transitions happened, how
many were denied, how many were quality-blocked — and nothing about how LONG one took, how many rows
it moved, or how many bytes it wrote. "The silver stage got slower last week" was unanswerable from
deployed telemetry, on the estate's flagship flow.

`docs/architecture/batch-processing-invariants.md` B10 names the second half, and names the Ray stage explicitly:

    Every duration — coordinator activity, Ray stage, commit — uses `time.perf_counter` ... and the
    SAME number lands in the lineage run facet so the graph and the metric cannot disagree.

So a derived estimate is not acceptable here: the Ray lane's duration is the plan's span from submit to
outcome, handed to pass 2 on the trigger, and the same number lands in the facet and the histogram. These
pin both halves — measured, and identical in both places.
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


def test_the_trigger_REFUSES_an_absurd_duration_it_was_handed() -> None:
    """The trigger is untrusted input — it is re-parsed through the same guard as any bus arrival.
    A negative or absurd value must not reach the histogram, where it would poison the series."""
    import pytest
    from pydantic import ValidationError

    from medallion.services.trigger_guards import StageTrigger

    assert StageTrigger(ray_duration_seconds=42.0).ray_duration_seconds == 42.0
    for bad in (-1.0, 10_000_000.0):
        with pytest.raises(ValidationError):
            StageTrigger(ray_duration_seconds=bad)
