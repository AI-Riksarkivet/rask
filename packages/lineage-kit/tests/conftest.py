"""Isolation fixtures: no ambient env/emitter state may leak between tests."""

from __future__ import annotations

import pytest
from lineage_kit import RecordingEmitter, set_default_emitter
from lineage_kit.context import CONTEXT_ENV_VAR


_LINEAGE_ENV_VARS = (
    "RASK_LINEAGE_ENDPOINT",
    "RASK_LINEAGE_API_KEY",
    "RASK_LINEAGE_ENDPOINT_PATH",
    "RASK_LINEAGE_NAMESPACE",
    "RASK_LINEAGE_TIMEOUT",
    "RASK_LINEAGE_TRANSPORT",
    "OPENLINEAGE_URL",
    "OPENLINEAGE_API_KEY",
    "OPENLINEAGE_ENDPOINT",
    "OPENLINEAGE_NAMESPACE",
    CONTEXT_ENV_VAR,
)


@pytest.fixture(autouse=True)
def _isolate(monkeypatch: pytest.MonkeyPatch):
    for var in _LINEAGE_ENV_VARS:
        monkeypatch.delenv(var, raising=False)
    set_default_emitter(None)
    yield
    set_default_emitter(None)


@pytest.fixture
def recording() -> RecordingEmitter:
    return RecordingEmitter()
