"""The estate's only window onto Ray is itself unmeasured, and it cannot tell a 401 from a dead socket.

Two defects, one module.

FIRST — no series. Neither `ray-kit` nor `services/compute` created a single OpenTelemetry
instrument, so nothing could answer "is the cluster reachable", "how many jobs failed this week", or
"is job history growing back toward the 81,155-job OOM". vmalert evaluates PromQL against GreptimeDB,
so with no series there is no rule that can ever fire.

That gap is not covered by the automatic HTTP metrics either, and the reason is structural: every
`/api/ray/*` route answers **HTTP 200 with `ok=false`** when Ray is down. `FastAPIInstrumentor`'s
`http.server.*` series is therefore blind to Ray failure by construction — a totally dead head renders
as request-rate normal, **error rate 0%**, p95 fine, readiness green, on the only dashboard the estate
ships for it, while the compute zone polls it every 5 seconds.

SECOND — `RAY_TRANSIENT_ERRORS` (dashboard.py:77) is
`(RuntimeError, ConnectionError, requests.exceptions.RequestException, AuthenticationError)`. One
`except` catches all four, so `build_client` answers a rotated token, a missing
`RASK_RAY_AUTH_TOKEN` and a scope mistake with the same fixed literal a dead cluster produces:
"Ray dashboard unreachable". An operator then goes and debugs KubeRay, the node pool and networking,
when the actual fix is a Secret.
"""

from __future__ import annotations

from typing import Any

import pytest


def _reader() -> Any:
    """An in-memory reader bound to a fresh provider, so a test can assert on what was RECORDED.

    Deliberately not a live exporter: `service_kit/otel.py:34-42` documents an OTLP exporter aimed at
    an unreachable endpoint costing ~2.7s per unit test in retry backoff, which is how a suite starts
    sleeping instead of failing.
    """
    from opentelemetry.sdk.metrics import MeterProvider
    from opentelemetry.sdk.metrics.export import InMemoryMetricReader

    reader = InMemoryMetricReader()
    return reader, MeterProvider(metric_readers=[reader])


def _recorded(reader: Any) -> dict[str, list[Any]]:
    """{metric name: [data points]} from one collection."""
    out: dict[str, list[Any]] = {}
    data = reader.get_metrics_data()
    for rm in getattr(data, "resource_metrics", []) or []:
        for sm in rm.scope_metrics:
            for metric in sm.metrics:
                out.setdefault(metric.name, []).extend(metric.data.data_points)
    return out


def test_ray_kit_exposes_a_control_plane_metrics_module() -> None:
    """No instrument existed anywhere in ray-kit or compute — verified by negative grep for
    `get_meter|create_counter|create_histogram` over both trees, which returned nothing."""
    from ray_kit import metrics

    assert hasattr(metrics, "record_probe"), "no way to record whether a Ray call succeeded"
    assert hasattr(metrics, "RayOutcome"), "no closed vocabulary for the outcome label"


def test_the_outcome_vocabulary_separates_UNAUTHORIZED_from_UNREACHABLE() -> None:
    """The whole point. A credential fault and a dead cluster must not share a label value, or the
    metric reproduces exactly the collapse it exists to expose."""
    from ray_kit.metrics import RayOutcome

    values = {o.value for o in RayOutcome}
    assert {"ok", "unauthorized", "unreachable"} <= values, f"outcomes are {values}"


def test_an_AuthenticationError_classifies_as_unauthorized_not_unreachable() -> None:
    """`RAY_TRANSIENT_ERRORS` catches AuthenticationError alongside ConnectionError, so the classifier
    is the only thing that can tell them apart after the fact."""
    from ray.exceptions import AuthenticationError

    from ray_kit.metrics import RayOutcome, classify_ray_error

    assert classify_ray_error(AuthenticationError("401 Unauthorized")) is RayOutcome.UNAUTHORIZED
    assert classify_ray_error(ConnectionError("connection refused")) is RayOutcome.UNREACHABLE
    assert classify_ray_error(RuntimeError("protocol failure")) is RayOutcome.TRANSIENT


def test_a_probe_records_a_series_a_rule_could_fire_on() -> None:
    from ray_kit import metrics
    from ray_kit.metrics import RayOutcome

    reader, provider = _reader()
    metrics.bind_meter_provider(provider)
    metrics.record_probe("health", RayOutcome.UNREACHABLE, duration_seconds=0.25)

    seen = _recorded(reader)
    assert any("probe" in name for name in seen), f"a failed probe recorded nothing — metrics are {list(seen)}"


def test_the_probe_label_set_is_BOUNDED() -> None:
    """Cardinality is the cost driver, and this estate treats it as a security rule too. The op is a
    closed set of call names and the outcome a StrEnum; a dashboard URL, a submission id or an error
    string must never become a label."""
    from ray_kit import metrics
    from ray_kit.metrics import RayOutcome

    reader, provider = _reader()
    metrics.bind_meter_provider(provider)
    metrics.record_probe("health", RayOutcome.OK, duration_seconds=0.1)

    for points in _recorded(reader).values():
        for point in points:
            keys = set(point.attributes or {})
            assert keys <= {"lance.ray.op", "lance.ray.outcome"}, f"unexpected label(s): {keys}"


def test_job_history_growth_is_a_series_so_the_OOM_can_be_seen_coming() -> None:
    """`GET /api/jobs/` takes no parameters at all, so it always returns EVERY job Ray has ever seen —
    measured at 81,155 jobs / 164.7 MB, which OOM-killed the pod. `list_jobs` caps the response, but
    the cap made the growth invisible rather than absent. The prune cron bounds it; nothing said how
    close it was getting."""
    from ray_kit import metrics

    reader, provider = _reader()
    metrics.bind_meter_provider(provider)
    metrics.record_jobs_known(81_155)

    seen = _recorded(reader)
    assert any("jobs" in name for name in seen), f"job-history size is not a series — metrics are {list(seen)}"


@pytest.mark.asyncio
async def test_a_health_probe_against_a_dead_cluster_RECORDS_it() -> None:
    """The end-to-end property: `health()` answering ok=false must leave a trace in the metric plane,
    because the HTTP 200 it returns means the automatic RED series never will."""
    from ray_kit import dashboard, metrics

    reader, provider = _reader()
    metrics.bind_meter_provider(provider)

    result = await dashboard.health(None, "http://ray.invalid:8265")

    assert result.ok is False
    seen = _recorded(reader)
    assert seen, "a dead Ray cluster produced no metric at all — the RED dashboard cannot see it either"
