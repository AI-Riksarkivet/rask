"""service-kit Dapr wiring — config gating + client factory (no sidecar needed)."""

import os

import pytest

from service_kit.config import Settings


def _settings(**env: str) -> Settings:
    return Settings.model_validate(
        {"RASK_VIEWER_INPUT": "/dev/null", "RASK_VIEWER_OUTPUT": "/dev/null", **env}
    )


def test_dapr_disabled_by_default() -> None:
    s = _settings()
    assert s.dapr_enabled is False
    assert s.dapr_http_port == "3500"


def test_dapr_enabled_from_env() -> None:
    s = _settings(RASK_DAPR_ENABLED="true", DAPR_HTTP_PORT="3555")
    assert s.dapr_enabled is True
    assert s.dapr_http_port == "3555"


def test_build_dapr_client_none_when_disabled() -> None:
    from service_kit import build_dapr_client

    assert build_dapr_client(_settings()) is None


def test_build_dapr_client_builds_when_enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    import service_kit

    captured: dict[str, str] = {}

    class FakeDaprClient:
        def __init__(self, address: str) -> None:
            captured["address"] = address

        def close(self) -> None:
            captured["closed"] = "yes"

    # Patch the lazy import target so no real dapr package / sidecar is needed.
    monkeypatch.setattr(service_kit, "_import_dapr_client", lambda: FakeDaprClient, raising=True)

    client = service_kit.build_dapr_client(_settings(RASK_DAPR_ENABLED="true", DAPR_HTTP_PORT="3500"))
    assert isinstance(client, FakeDaprClient)
    assert captured["address"] == "http://127.0.0.1:3500"
