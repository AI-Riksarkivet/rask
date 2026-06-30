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
