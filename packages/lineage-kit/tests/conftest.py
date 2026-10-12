"""Isolation fixtures: no ambient lineage env may leak between tests."""

from __future__ import annotations

import pytest


_LINEAGE_ENV_VARS = (
    "RASK_LINEAGE_ENDPOINT",
    "RASK_LINEAGE_API_KEY",
    "RASK_LINEAGE_ENDPOINT_PATH",
    "RASK_LINEAGE_TIMEOUT",
    "RASK_LINEAGE_TRANSPORT",
    # The credential. Left ambient, an exported value would decide which file or bearer the emitter
    # presents, and the tests would pass or fail by where they ran.
    "RASK_LINEAGE_IDENTITY_TOKEN_FILE",
    "LINEAGE_TOKEN",
    "OPENLINEAGE_URL",
    "OPENLINEAGE_API_KEY",
    "OPENLINEAGE_ENDPOINT",
)


@pytest.fixture(autouse=True)
def _isolate(monkeypatch: pytest.MonkeyPatch) -> None:
    for var in _LINEAGE_ENV_VARS:
        monkeypatch.delenv(var, raising=False)
