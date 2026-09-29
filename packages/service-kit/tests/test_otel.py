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

    It pins "off means off even when something else in the environment wants it on", which is the case
    that actually occurred, and the one a future `or` would quietly re-break.
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
