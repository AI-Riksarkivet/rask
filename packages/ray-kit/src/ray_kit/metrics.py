"""Domain metrics for the Ray CONTROL path — the estate's only window onto Ray, previously unmeasured.

Neither `ray-kit` nor `services/compute` created a single OpenTelemetry instrument, and the gap is not
covered by the automatic HTTP series. Every `/api/ray/*` route answers **HTTP 200 with `ok=false`**
when Ray is down, so `FastAPIInstrumentor`'s `http.server.*` metrics are blind to Ray failure *by
construction*: a totally dead head renders as request-rate normal, error rate 0%, p95 fine and
readiness green — on the only dashboard the estate ships for it — while the compute zone polls it
every five seconds. vmalert evaluates PromQL against GreptimeDB, so with no series there is no rule
that can ever fire.

WHY A SEPARATE OUTCOME VOCABULARY. `dashboard.RAY_TRANSIENT_ERRORS` is
`(RuntimeError, ConnectionError, requests.exceptions.RequestException, AuthenticationError)` — one
`except` for four causes — so a rotated token, a missing `RASK_RAY_AUTH_TOKEN` and a scope mistake all
surface as the same fixed literal a dead cluster produces, "Ray dashboard unreachable". An operator
then debugs KubeRay, the node pool and networking when the fix is a Secret. `classify_ray_error`
recovers the distinction after the fact, and it is a CLOSED enum so the label set stays bounded.

CARDINALITY. Only two attribute keys, both closed: `lance.ray.op` (a call name from this package) and
`lance.ray.outcome` (the enum below). A dashboard URL, a submission id or an exception string must
never become a label — that is the estate's own rule, stated as a security rule and not merely a cost
one in `services/notifications/src/notifications/api/metrics.py`. Per-call detail belongs on spans and
logs, where it is indexed rather than multiplied.
"""

from __future__ import annotations

from enum import StrEnum
from typing import TYPE_CHECKING

from opentelemetry import metrics as _otel_metrics


if TYPE_CHECKING:  # pragma: no cover — typing only
    from opentelemetry.sdk.metrics import MeterProvider


class RayOutcome(StrEnum):
    """How a Ray control call ended. CLOSED — the label vocabulary is the cardinality bound.

    `UNAUTHORIZED` and `UNREACHABLE` are separate members on purpose: collapsing them is the defect
    this module exists to expose, and a metric that reproduced the collapse would be worthless.
    """

    OK = "ok"
    #: A 401/403 from the dashboard — a credential fault. The cluster is fine; a Secret is not.
    UNAUTHORIZED = "unauthorized"
    #: Nothing listening: connection refused, DNS failure, a socket that never opened.
    UNREACHABLE = "unreachable"
    #: Reachable and authenticated, but the call failed — a protocol error or an HTTP error status.
    TRANSIENT = "transient"


def classify_ray_error(exc: BaseException) -> RayOutcome:
    """Recover WHICH of `RAY_TRANSIENT_ERRORS`' four causes actually fired.

    Ordered most-specific first. `AuthenticationError` is a `RayError`, and `ConnectionError` is a
    builtin that `requests.exceptions.ConnectionError` also subclasses, so a bare `isinstance` chain
    in the wrong order would silently reclassify a credential fault as a dead socket — which is the
    original defect wearing a different hat.
    """
    from ray.exceptions import AuthenticationError

    if isinstance(exc, AuthenticationError):
        return RayOutcome.UNAUTHORIZED
    if isinstance(exc, ConnectionError):
        return RayOutcome.UNREACHABLE
    return RayOutcome.TRANSIENT


_meter = _otel_metrics.get_meter("lance.ray")

_probes = _meter.create_counter(
    "ray.control.probes",
    unit="{probe}",
    description="Ray control-plane calls by operation and outcome — the series a dead cluster shows up in.",
)
_probe_duration = _meter.create_histogram(
    "ray.control.duration",
    unit="s",
    description="Wall-clock seconds a Ray control-plane call took.",
    # SECOND-scale. The SDK's default advisory is millisecond web latency; `list_jobs` against a large
    # cluster measured whole seconds, so the defaults would put every real call in one bucket.
    explicit_bucket_boundaries_advisory=[0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0, 60.0],
)
_jobs_known = _meter.create_gauge(
    "ray.control.jobs_known",
    unit="{job}",
    description="Jobs Ray's dashboard reported in one listing, BEFORE this package's cap — the OOM early warning.",
)


def bind_meter_provider(provider: MeterProvider) -> None:
    """Rebind this module's instruments to ``provider`` — for tests, which need an in-memory reader.

    Module-level `get_meter` returns a PROXY that binds when a provider is set globally, which is what
    makes import-time instrument creation safe in production. A test needs the opposite: its own
    provider, without touching the global one and without a live exporter (an OTLP exporter aimed at
    an unreachable endpoint costs ~2.7s per test in retry backoff — see `service_kit/otel.py`).
    """
    global _meter, _probes, _probe_duration, _jobs_known
    _meter = provider.get_meter("lance.ray")
    _probes = _meter.create_counter("ray.control.probes", unit="{probe}", description="Ray control-plane calls by operation and outcome.")
    _probe_duration = _meter.create_histogram(
        "ray.control.duration",
        unit="s",
        description="Wall-clock seconds a Ray control-plane call took.",
        explicit_bucket_boundaries_advisory=[0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0, 60.0],
    )
    _jobs_known = _meter.create_gauge("ray.control.jobs_known", unit="{job}", description="Jobs Ray's dashboard reported in one listing.")


def record_probe(op: str, outcome: RayOutcome, *, duration_seconds: float | None = None) -> None:
    """Record that a Ray control call happened, and how it ended.

    ``op`` is a call name from this package (``health``, ``list_jobs``, ``submit``, ``prune``, …) —
    a closed set by construction, since only this package calls it.
    """
    attrs = {"lance.ray.op": op, "lance.ray.outcome": outcome.value}
    _probes.add(1, attrs)
    if duration_seconds is not None:
        _probe_duration.record(duration_seconds, {"lance.ray.op": op})


def record_jobs_known(total: int) -> None:
    """Record how many jobs the dashboard reported before the cap.

    A GAUGE, not a counter: this is a point-in-time size, and it goes DOWN when the prune cron runs.
    It exists because `GET /api/jobs/` accepts no parameters at all — no limit, no offset, no status
    filter — so it always returns every job Ray has ever seen. That measured 81,155 jobs / 164.7 MB
    and OOM-killed the pod. `list_jobs` caps what it validates, which fixed the crash and made the
    GROWTH invisible; this is the series that shows it coming back.
    """
    _jobs_known.set(total)
