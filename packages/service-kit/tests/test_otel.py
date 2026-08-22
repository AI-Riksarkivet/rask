from collections.abc import Callable, Iterator

import pytest
from fastapi import FastAPI

from service_kit.config import Settings
from service_kit.otel import setup_otel


def _settings(**env: bool | str) -> Settings:
    return Settings.model_validate({"RASK_VIEWER_INPUT": "/dev/null", "RASK_VIEWER_OUTPUT": "/dev/null", **env})


@pytest.fixture(autouse=True)
def _restore_otel_globals() -> Iterator[None]:
    """`setup_otel` installs PROCESS-GLOBAL state, and this file is the only place that calls it for
    real — so it must hand the process back the way it found it.

    `packages/service-kit/tests` is the third of twenty-one testpaths. Without this, every test in the
    remaining eighteen ran with a live `BatchSpanProcessor` and `PeriodicExportingMetricReader`
    retrying against `http://localhost:4318`. The endpoint is captured when the exporter is
    CONSTRUCTED, so `monkeypatch` putting the variable back at teardown does not disarm it — which is
    why the estate's suite logs `Failed to export … due to timeout, max retries or shutdown` long
    after these two tests finish, and why that noise buried a pytest summary line earlier today.

    Same family as the root `conftest.py`'s OTLP strip: harness state crossing a boundary the suite
    does not control. The strip stops the environment leaking IN; this stops an exporter leaking OUT.

    **IT RECORDS CONSTRUCTION RATHER THAN READING THE GLOBALS, AND THE FIRST VERSION DID NOT.** That
    version shut down `trace.get_tracer_provider()` / `metrics.get_meter_provider()`, which sounds
    equivalent and is not, because OTel's setters are SET-ONCE: `set_meter_provider` "can only be done
    once, a warning will be logged if any further attempt is made". So the global is whatever the
    FIRST `setup_otel` in the process installed, and every later call builds a provider that never
    becomes global — while its reader still joins the SDK's class-level
    `MeterProvider._all_metric_readers` WeakSet and still runs its background export loop. Reading the
    globals therefore disarms exactly one provider and leaves the rest exporting.

    Measured with a probe asserting no live exporter survives this file (`_shutdown is False` on any
    processor or reader):

        no fixture:        BatchSpanProcessor live + 3 PeriodicExportingMetricReader live
        reading globals:   2 PeriodicExportingMetricReader live
        recording (this):  none

    Shut down rather than swapped, for the same set-once reason: restoring by re-setting the global
    would log a warning and silently keep the old one.
    """
    from opentelemetry.sdk._logs import LoggerProvider
    from opentelemetry.sdk.metrics import MeterProvider
    from opentelemetry.sdk.trace import TracerProvider

    built: list[object] = []
    originals = [(cls, cls.__init__) for cls in (TracerProvider, MeterProvider, LoggerProvider)]

    def _recording(original: Callable[..., None]) -> Callable[..., None]:
        def __init__(self: object, *args: object, **kwargs: object) -> None:
            original(self, *args, **kwargs)
            built.append(self)

        return __init__

    for cls, original in originals:
        cls.__init__ = _recording(original)  # ty: ignore[invalid-assignment]
    try:
        yield
    finally:
        for cls, original in originals:
            cls.__init__ = original  # ty: ignore[invalid-assignment]
        for provider in built:
            shutdown = getattr(provider, "shutdown", None)
            if callable(shutdown):
                shutdown()


def test_setup_otel_noop_when_disabled() -> None:
    app = FastAPI()
    settings = _settings(RASK_OTEL_ENABLED=False)
    wired = setup_otel(app, "svc-test", settings)
    assert wired is False


def test_setup_otel_wires_when_enabled(monkeypatch: object) -> None:
    """ "Wired" means ALL THREE signals, and logs were the one this test never checked.

    It asserted the FastAPI flag and stopped, so it stayed green across the entire life of a seam that
    built a TracerProvider and a MeterProvider and NO LoggerProvider. Repaired rather than supplemented
    for the same reason as the instrumentor test: a test whose name claims the whole wiring and checks a
    third of it is worse than no test, because it occupies the slot a real one would take.

    What the gap cost, measured: `LoggingInstrumentor().instrument(...)` DOES install an OTLP
    `LoggingHandler`, but with no provider argument it binds to the global `ProxyLoggerProvider`, whose
    `ProxyLogger` falls back to `_noop_logger`. The handler's `emit` skips only on `NoOpLogger` and a
    `ProxyLogger` is not one — so the fleet translated every log record into an OTel record and then
    threw it away, paying the full cost for nothing. Root handlers came back as
    `[('rask-stdout', StreamHandler), (None, LoggingHandler)]` with `get_logger_provider()` a
    `ProxyLoggerProvider` that has no `force_flush` at all.

    Asserting the GLOBAL is deterministic here even though these providers are set-once per process:
    every enabled path through `setup_otel` installs the same SDK provider, so whichever test wins the
    race the answer is identical. The only way this reads a proxy is if no enabled call ever ran — and
    this test is one.
    """
    import pytest
    from opentelemetry._logs import get_logger_provider
    from opentelemetry.sdk._logs import LoggerProvider

    assert isinstance(monkeypatch, pytest.MonkeyPatch)
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://localhost:4317")
    app = FastAPI()
    settings = _settings(RASK_OTEL_ENABLED=True)
    wired = setup_otel(app, "svc-test", settings)
    assert wired is True
    # FastAPI instrumentation marks the app
    assert getattr(app, "_is_instrumented_by_opentelemetry", False) is True

    provider = get_logger_provider()
    assert isinstance(provider, LoggerProvider), (
        f"logs are the third signal and it is not wired: get_logger_provider() is {type(provider).__name__}. "
        "A ProxyLoggerProvider means every record the LoggingHandler translates is handed to a no-op and dropped."
    )
    assert hasattr(provider, "force_flush"), "an SDK LoggerProvider force_flushes; a proxy cannot, so nothing survives a crash"


def test_an_explicit_OFF_beats_an_ambient_endpoint(monkeypatch: object) -> None:
    """The regression, and the reason a whole test suite crawled.

    `OTEL_EXPORTER_OTLP_ENDPOINT` is set by the HARNESS, not by the service: `dagger call test` injects
    it into every container for Dagger's own telemetry. Under the old `or`, that ambient variable
    overrode an explicit `RASK_OTEL_ENABLED=false`, so every app a test built wired a live exporter at
    a collector that rejects application metrics — and the SDK retried with exponential backoff. The
    suite did not fail, it slept: ~2.7s per unit test, and a full run sat in `hrtimer_nanosleep` at
    ~1.7% CPU.

    Pinning it here rather than only in `test_setup_otel_noop_when_disabled` because the two assert
    different things: that one says "off means off with a clean environment", this one says "off means
    off even when something else in the environment wants it on" — which is the case that actually
    occurred, and the one a future `or` would quietly re-break.
    """
    import pytest

    assert isinstance(monkeypatch, pytest.MonkeyPatch)
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://collector.invalid:4317")

    assert setup_otel(FastAPI(), "svc-test", _settings(RASK_OTEL_ENABLED=False)) is False


def test_NO_settings_still_opts_in_through_the_endpoint(monkeypatch: object) -> None:
    """The fallback the `or` existed for must survive: `services/gateway` calls `setup_otel` with no
    `Settings` at all and opts in through the endpoint alone. Tightening the rule for callers that DO
    pass settings must not take that away."""
    import pytest

    assert isinstance(monkeypatch, pytest.MonkeyPatch)
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://localhost:4317")

    assert setup_otel(FastAPI(), "svc-test") is True


def test_NO_settings_and_NO_endpoint_stays_off(monkeypatch: object) -> None:
    """The other half of the fallback: absent both signals, nothing is wired."""
    import pytest

    assert isinstance(monkeypatch, pytest.MonkeyPatch)
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_ENDPOINT", raising=False)

    assert setup_otel(FastAPI(), "svc-test") is False


def test_every_client_transport_the_estate_uses_gets_an_instrumentor(monkeypatch: object) -> None:
    """The seam's instrumentor list is the FLEET's complete instrumentation — there is no launcher.

    `chart/templates/fleet.yaml` and `controlplane.yaml` run `command: ["uvicorn"]`, not
    `opentelemetry-instrument`, so unlike the lakehouse half nothing auto-loads the installed
    entry points. Whatever this function names is all the fleet gets.

    `requests` was the expensive omission. Ray's `JobSubmissionClient` performs every call through it,
    so `list_jobs` — the call that measured 164.7 MB / 81,155 jobs and OOM-killed the compute pod —
    and the pruner's per-job DELETE loop carried no client span at all, while the cheap httpx reads
    did. That does not read as a gap in a trace view; it reads as those calls being instantaneous.

    `grpc` and `aiohttp` are the two that decide whether the DAPR plane joins up at all, and they are
    why this test is being repaired rather than left as it was. Now that `lance-tracing` names an
    exporter (d5744a9c) the sidecars finally emit spans — but the app->sidecar leg carries no
    `traceparent` without these, so the sidecar's span ROOTS A NEW TRACE. That fresh id is what gets
    stamped into the CloudEvent envelope and persisted as `ExecutionStartedEvent.ParentTraceContext`,
    so every activity, lineage event and notification downstream inherits the orphan. The damage is a
    severed subtree, not a missing span, and it looks like a sampling problem rather than a missing
    instrumentor.

      * grpc  — `dapr.aio.clients.DaprClient` rides `grpc.aio` (publish, state, bindings, workflow
        schedule); `dapr-ext-workflow`'s `DaprWorkflowClient` rides SYNC grpc. BOTH variants are
        needed: they patch different symbols, and installing one looks configured while doing nothing.
      * aiohttp — `ActorProxy` -> `DaprActorHttpClient`, i.e. EVERY actor call in the estate, and the
        `openfga_sdk`, i.e. every authorization check on every governed door.
    """
    from opentelemetry.instrumentation.aiohttp_client import AioHttpClientInstrumentor
    from opentelemetry.instrumentation.grpc import GrpcAioInstrumentorClient, GrpcInstrumentorClient
    from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
    from opentelemetry.instrumentation.requests import RequestsInstrumentor

    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://localhost:4318")  # ty: ignore[unresolved-attribute]
    assert setup_otel(FastAPI(), "svc-test", _settings(RASK_OTEL_ENABLED=True)) is True

    # `is_instrumented_by_opentelemetry` is the SDK's own flag. Asserting on a patched function's repr
    # does NOT work — measured: the requests instrumentor leaves `Session.request` identical and wraps
    # the send path instead, so a repr check passes vacuously in one direction and fails in the other.
    assert RequestsInstrumentor().is_instrumented_by_opentelemetry, "requests is not instrumented — Ray's Job SDK calls carry no client span"
    assert HTTPXClientInstrumentor().is_instrumented_by_opentelemetry, "httpx is not instrumented"
    assert GrpcAioInstrumentorClient().is_instrumented_by_opentelemetry, (
        "grpc.aio is not instrumented — dapr.aio.clients.DaprClient sends no traceparent, so the sidecar roots a NEW trace"
    )
    assert GrpcInstrumentorClient().is_instrumented_by_opentelemetry, "sync grpc is not instrumented — DaprWorkflowClient's schedule call is an orphan"
    assert AioHttpClientInstrumentor().is_instrumented_by_opentelemetry, (
        "aiohttp is not instrumented — every ActorProxy call and every OpenFGA check is invisible"
    )
