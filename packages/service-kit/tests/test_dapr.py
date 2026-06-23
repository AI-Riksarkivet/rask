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
