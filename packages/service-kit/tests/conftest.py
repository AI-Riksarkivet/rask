"""service-kit test configuration.

Pins RASK_API_PREFIX to the canonical /api/v1 default so tests that construct
full paths (e.g. /api/v1/items) work regardless of a local .env override.
"""

import os

import pytest


@pytest.fixture(autouse=True)
def _pin_api_prefix(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RASK_API_PREFIX", "/api/v1")
    monkeypatch.setenv("RASK_VIEWER_INPUT", os.environ.get("RASK_VIEWER_INPUT", "/dev/null"))
    monkeypatch.setenv("RASK_VIEWER_OUTPUT", os.environ.get("RASK_VIEWER_OUTPUT", "/dev/null"))
