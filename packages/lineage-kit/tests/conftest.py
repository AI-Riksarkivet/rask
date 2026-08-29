"""Isolation fixtures: no ambient env/emitter state may leak between tests."""

from __future__ import annotations

import pytest
from lineage_kit import RecordingEmitter, set_default_emitter
from lineage_kit.config import lineage_settings
from lineage_kit.context import CONTEXT_ENV_VAR


_LINEAGE_ENV_VARS = (
    "RASK_LINEAGE_ENDPOINT",
    "RASK_LINEAGE_API_KEY",
    "RASK_LINEAGE_ENDPOINT_PATH",
    "RASK_LINEAGE_NAMESPACE",
    "RASK_LINEAGE_TIMEOUT",
    "RASK_LINEAGE_TRANSPORT",
    # The service-door credentials. APP_API_TOKEN especially: it is mounted on every fleet pod and is
    # plausibly set on a dev machine, so leaving it ambient would let the environment decide whether the
    # emitter sends auth headers — the tests would then pass or fail based on where they ran.
    "RASK_LINEAGE_APP_TOKEN",
    "RASK_LINEAGE_SERVICE_IDENTITY",
    "LINEAGE_SERVICE_TOKEN",
    "LINEAGE_SERVICE_ID",
    "APP_API_TOKEN",
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
    # The namespace default is read once per PROCESS (`config.lineage_settings`), so a test that set
    # RASK_LINEAGE_NAMESPACE would otherwise decide the default for every test after it.
    lineage_settings.cache_clear()
    set_default_emitter(None)
    yield
    lineage_settings.cache_clear()
    set_default_emitter(None)


@pytest.fixture
def recording() -> RecordingEmitter:
    return RecordingEmitter()
