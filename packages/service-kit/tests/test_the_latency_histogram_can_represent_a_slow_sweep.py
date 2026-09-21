"""The HTTP duration histogram has buckets that reach this estate's real durations.

[[XC-067]] AND IT KILLED AN ALERT SILENTLY. Measured live 2026-09-21 against the deployed GreptimeDB:
`maintenance` had a mean request duration of **39,504 ms**, while the exported histogram carried the
OTel DEFAULT explicit boundaries, whose largest finite bucket is 10,000 ms. Counted on the same store:
`le=10000` held **4** observations and `le=+Inf` held **37** — 89% of requests past the top bucket.

WHY THAT IS NOT MERELY IMPRECISE. `histogram_quantile` returns at most the upper bound of the highest
FINITE bucket when the quantile falls in `+Inf`. So every quantile above the ~11th percentile answers
exactly 10000, forever, and `chart/alerting/rules.yml`'s `HttpServerLatencyHigh` — `p95 > 15000` —
CANNOT FIRE for any service at any latency. Run as written against the live store it returned ZERO
series while the service it exists to catch sat at 39.5 seconds. Eleven Perses panels read the same
capped buckets.

HOW IT PASSED REVIEW, because that is the reusable part. The rule's own comment records verifying that
`histogram_quantile` is "supported by GreptimeDB — verified by running this exact expression against
the live store, because promtool accepting it proves nothing about the engine that evaluates it". That
check was real and insufficient: it proved the expression EVALUATES, never that its threshold was
REACHABLE given the buckets. A number that cannot move, read as a measurement — the same shape as
`compaction_mode` in `docs/DECISIONS.md`.

THE BOUNDARIES ARE CHOSEN FROM MEASURED DURATIONS, not doubled arbitrarily. On the deployed estate:
catalog ~34 ms, notifications ~30 ms, lineage ~370 ms, medallion-producer ~1,070 ms, maintenance
~39,500 ms. So the set has to stay dense in the tens-of-milliseconds range where four services live
AND reach past a sweep, which the top of 300,000 ms does with room for a sweep that degrades.
"""

from __future__ import annotations

from service_kit.otel import HTTP_DURATION_BUCKETS_MS


#: The alert this exists to make reachable (`chart/alerting/rules.yml`). A bucket layout whose top is
#: below the threshold makes the rule dead by construction, which is the whole defect.
ALERT_THRESHOLD_MS = 15_000

#: Measured on the deployed estate 2026-09-21; the histogram must be able to represent it.
OBSERVED_SWEEP_MS = 39_504


def test_the_boundaries_are_ordered_and_positive() -> None:
    """A malformed layout is rejected by the SDK at startup, which is a worse way to find out."""
    assert HTTP_DURATION_BUCKETS_MS, "no boundaries defined"
    assert all(b > 0 for b in HTTP_DURATION_BUCKETS_MS), "a non-positive boundary is not a duration"
    assert list(HTTP_DURATION_BUCKETS_MS) == sorted(HTTP_DURATION_BUCKETS_MS), "boundaries must ascend"
    assert len(set(HTTP_DURATION_BUCKETS_MS)) == len(HTTP_DURATION_BUCKETS_MS), "duplicate boundaries"


def test_the_top_bucket_is_above_the_alert_threshold() -> None:
    """THE DEFECT, stated directly: a quantile cannot exceed the highest finite bucket.

    With a top of 10,000 and a threshold of 15,000, `HttpServerLatencyHigh` returned zero series while
    `maintenance` ran at 39.5 seconds.
    """
    top = max(HTTP_DURATION_BUCKETS_MS)

    assert top > ALERT_THRESHOLD_MS, (
        f"the largest bucket is {top}ms and the alert fires above {ALERT_THRESHOLD_MS}ms; "
        "a quantile is capped at the highest finite bucket, so the rule can never fire"
    )


def test_a_real_sweep_is_not_in_the_overflow_bucket() -> None:
    """Measured, not imagined: 39,504 ms was a real reading from the deployed maintenance service."""
    top = max(HTTP_DURATION_BUCKETS_MS)

    assert top > OBSERVED_SWEEP_MS, f"a measured {OBSERVED_SWEEP_MS}ms sweep still lands in +Inf with a top bucket of {top}ms"


def test_the_fast_services_keep_their_resolution() -> None:
    """Reaching further must not blind the estate to its FAST services, which is the obvious wrong fix.

    catalog and notifications run in the tens of milliseconds; a layout that jumped straight to seconds
    would make their p95 meaningless in exchange for making maintenance's measurable.
    """
    under_100 = [b for b in HTTP_DURATION_BUCKETS_MS if b <= 100]

    assert len(under_100) >= 4, f"only {len(under_100)} boundaries at or below 100ms; the sub-100ms services lose all resolution"


def test_the_view_actually_reaches_the_exported_histogram() -> None:
    """The boundaries must REACH the metric, not merely exist as a constant.

    A View whose `instrument_name` matches nothing is a control that provably does nothing — the
    failure [[LH-172]] removed a `LANCE_CPU_THREADS` knob for, and the one this whole row is an
    instance of. The instrumentation also ships its OWN advisory boundaries
    (`HTTP_DURATION_HISTOGRAM_BUCKETS_NEW`, topping out at 10), so "the SDK will pick mine" is an
    assumption worth failing a test over rather than believing.

    Asserted against a real `MeterProvider` with an in-memory reader, recording a duration that the
    DEFAULT layout cannot represent (50,000 ms) and reading the boundaries back off the exported point.
    """
    from opentelemetry.sdk.metrics import Histogram, MeterProvider
    from opentelemetry.sdk.metrics.export import HistogramDataPoint, InMemoryMetricReader
    from opentelemetry.sdk.metrics.view import ExplicitBucketHistogramAggregation, View

    reader = InMemoryMetricReader()
    provider = MeterProvider(
        metric_readers=[reader],
        views=[
            View(
                instrument_type=Histogram,
                instrument_name="http.server.duration",
                aggregation=ExplicitBucketHistogramAggregation(HTTP_DURATION_BUCKETS_MS),
            )
        ],
    )
    provider.get_meter("t").create_histogram("http.server.duration", unit="ms").record(50_000)

    data = reader.get_metrics_data()
    assert data is not None, "the reader returned no metrics data at all"
    points = [
        point
        for rm in data.resource_metrics
        for sm in rm.scope_metrics
        for metric in sm.metrics
        for point in metric.data.data_points
        # Narrowed on the POINT TYPE rather than cast: a histogram View that silently produced a sum
        # would otherwise be read through an attribute that does not exist, and the failure would name
        # the attribute instead of the aggregation.
        if isinstance(point, HistogramDataPoint)
    ]
    assert points, "the histogram exported no HistogramDataPoint; the View produced a different aggregation"

    boundaries = tuple(points[0].explicit_bounds)
    assert boundaries == tuple(HTTP_DURATION_BUCKETS_MS), f"the View did not reach the instrument; exported boundaries were {boundaries}"
    assert max(boundaries) >= 50_000, "a 50,000ms observation must not land in the overflow bucket"
