"""service-kit test configuration.

Sets required env vars so Settings can be constructed in tests regardless of
the local environment. RASK_API_PREFIX is intentionally NOT pinned here —
tests resolve the prefix dynamically from build_settings().api_prefix.
"""

import os

import pytest


@pytest.fixture(autouse=True)
def _set_required_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RASK_VIEWER_INPUT", os.environ.get("RASK_VIEWER_INPUT", "/dev/null"))
    monkeypatch.setenv("RASK_VIEWER_OUTPUT", os.environ.get("RASK_VIEWER_OUTPUT", "/dev/null"))
