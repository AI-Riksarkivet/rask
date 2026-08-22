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

    # Bound the OTLP timeout, or this suite SLEEPS. `test_otel.py` deliberately builds REAL exporters
    # (that is what it is testing) against an endpoint nothing serves, and `setup_otel` now registers
    # `atexit` shutdown hooks, so at process exit every batch processor tries to deliver its buffer and
    # waits out the SDK's 10s default per attempt. Measured on this suite: 3.22s -> 10.19s once the
    # hooks landed, and back to 3.08s with this line. That is the same failure the `otel.py` docstring
    # records from `dagger call test` — a suite that does not fail, it sleeps.
    #
    # Deliberately NOT a production default: a service wants the standard timeout, because flushing the
    # last records before SIGTERM is the entire reason the hooks exist, and the 30s termination grace
    # period covers it. This is a statement about the test environment, not about the seam.
    #
    # Works despite being function-scoped monkeypatch: the OTLP exporters read this env var in
    # `__init__`, so the value is baked into the exporter built during the test and still governs the
    # drain at exit, long after monkeypatch has reverted it.
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_TIMEOUT", os.environ.get("OTEL_EXPORTER_OTLP_TIMEOUT", "1"))
