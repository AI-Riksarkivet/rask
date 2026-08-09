"""Root conftest — make the suite hermetic against the HARNESS's own environment.

There is exactly one rule here, and it exists because of a measured failure rather than a principle.

THE FAILURE. `dagger call test` would not finish: two runs were abandoned, one at 22 minutes and one
at 56, with the pytest worker sitting in `wchan=hrtimer_nanosleep` at ~1.7% CPU. It was not slow, it
was asleep. Dagger injects `OTEL_EXPORTER_OTLP_ENDPOINT` into every container it runs, for its OWN
telemetry — so any code that opts into OpenTelemetry by looking for that variable wired a live
exporter aimed at Dagger's collector, which rejects application metrics (`unknown aggregation from
pb`, in the engine's own log), and the SDK then retried with exponential backoff. Every app any test
built paid for it.

`service_kit.setup_otel` has since been fixed so an explicit `Settings` decides. But the fallback it
keeps is legitimate and load-bearing — `services/gateway` calls `setup_otel(app, service_name=...)`
with no `Settings` at all, at MODULE scope, and opts in through the endpoint alone. So merely
importing the gateway inside a telemetry-injecting harness still starts an exporter. The service is
right; the environment is what is wrong, and it is wrong for the whole session.

Hence: strip the ambient OTLP variables ONCE, for the session, before anything imports. This is the
same instinct as the config-isolation fixtures the per-directory conftests already use ("`create_app`
calls `load_dotenv`, so the suite pins env to stay hermetic") — one scope up, against a variable no
test sets and no test should inherit.

WHAT THIS DELIBERATELY DOES NOT DO: it does not stop a test setting the variable itself.
`monkeypatch.setenv` still works and is still honoured — `test_setup_otel_wires_when_enabled` and
`test_NO_settings_still_opts_in_through_the_endpoint` both depend on that. Removing the ambient value
at session start and letting a test opt back in per-case is exactly the distinction between "the
harness leaked into the run" and "this test is exercising the enabled path".
"""

import os

import pytest


#: The OTLP variables a CI harness may inject. `OTEL_EXPORTER_OTLP_ENDPOINT` is the one Dagger sets
#: and the one `setup_otel` keys its fallback on; the signal-specific overrides and the headers are
#: removed with it so a partially-stripped environment cannot produce a half-configured exporter,
#: which is harder to diagnose than either extreme.
_HARNESS_OTLP_VARS = (
    "OTEL_EXPORTER_OTLP_ENDPOINT",
    "OTEL_EXPORTER_OTLP_TRACES_ENDPOINT",
    "OTEL_EXPORTER_OTLP_METRICS_ENDPOINT",
    "OTEL_EXPORTER_OTLP_LOGS_ENDPOINT",
    "OTEL_EXPORTER_OTLP_HEADERS",
    "OTEL_EXPORTER_OTLP_TRACES_HEADERS",
    "OTEL_EXPORTER_OTLP_PROTOCOL",
)


@pytest.fixture(scope="session", autouse=True)
def _no_harness_telemetry() -> None:
    """Remove the harness's OTLP configuration for the whole session.

    Session-scoped and autouse: the leak is a property of the process, not of any test, and the
    damage is done at import time — a function-scoped fixture would run too late for a module that
    calls `setup_otel` at module scope, which is precisely the case that motivated this.

    Not restored afterwards, on purpose. The values belong to the harness, nothing in the suite reads
    them once removed, and putting them back at teardown would only re-arm the exporter while pytest
    is still writing its report.
    """
    for name in _HARNESS_OTLP_VARS:
        os.environ.pop(name, None)
