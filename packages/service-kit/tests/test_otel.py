from fastapi import FastAPI

from service_kit.config import Settings
from service_kit.otel import setup_otel


def _settings(**env: bool | str) -> Settings:
    return Settings.model_validate({"RASK_VIEWER_INPUT": "/dev/null", "RASK_VIEWER_OUTPUT": "/dev/null", **env})


def test_setup_otel_noop_when_disabled() -> None:
    app = FastAPI()
    settings = _settings(RASK_OTEL_ENABLED=False)
    wired = setup_otel(app, "svc-test", settings)
    assert wired is False


def test_setup_otel_wires_when_enabled(monkeypatch: object) -> None:
    import pytest

    assert isinstance(monkeypatch, pytest.MonkeyPatch)
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://localhost:4317")
    app = FastAPI()
    settings = _settings(RASK_OTEL_ENABLED=True)
    wired = setup_otel(app, "svc-test", settings)
    assert wired is True
    # FastAPI instrumentation marks the app
    assert getattr(app, "_is_instrumented_by_opentelemetry", False) is True


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
    """
    from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
    from opentelemetry.instrumentation.requests import RequestsInstrumentor

    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://localhost:4318")  # ty: ignore[unresolved-attribute]
    assert setup_otel(FastAPI(), "svc-test", _settings(RASK_OTEL_ENABLED=True)) is True

    # `is_instrumented_by_opentelemetry` is the SDK's own flag. Asserting on a patched function's repr
    # does NOT work — measured: the requests instrumentor leaves `Session.request` identical and wraps
    # the send path instead, so a repr check passes vacuously in one direction and fails in the other.
    assert RequestsInstrumentor().is_instrumented_by_opentelemetry, "requests is not instrumented — Ray's Job SDK calls carry no client span"
    assert HTTPXClientInstrumentor().is_instrumented_by_opentelemetry, "httpx is not instrumented"
