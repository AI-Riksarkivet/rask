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

**"BEFORE ANYTHING IMPORTS" IS THE LOAD-BEARING WORD, AND THE FIRST VERSION DID NOT ACHIEVE IT.** The
strip lived only in the session-scoped autouse fixture below, and a session fixture runs on the FIRST
TEST — long after collection has imported every test module, and therefore long after any module-scope
`setup_otel` has already built its exporter. The variable was removed from an environment nothing was
going to read again. That is why the strip now also happens at this module's own import (see
`_strip_harness_otlp()` below the constant), which pytest performs before collection begins, and why
`tests/unit/test_conftest_otlp_strip.py` drives a real subprocess to prove the ordering rather than
asserting it from a comment.

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


def _strip_harness_otlp() -> None:
    """Drop every ambient OTLP variable. Idempotent, and safe to call more than once."""
    for name in _HARNESS_OTLP_VARS:
        os.environ.pop(name, None)


# ── AT IMPORT, and this is the whole point ────────────────────────────────────────────────────────
# The first version of this file did the strip in the session fixture below and nowhere else, which
# does not work for the case it was written for. pytest's order is: load the rootdir conftest →
# COLLECT (which IMPORTS every test module) → run the first test (which is when a session-scoped
# autouse fixture finally executes). The damage this guards against is done at IMPORT: `services/gateway`
# calls `setup_otel(app, service_name=...)` at MODULE scope with no `Settings`, so the exporter is
# already wired by the time any fixture runs, and stripping the variable afterwards cannot un-wire it.
#
# Measured, not reasoned: with the strip in the fixture alone, a module importing the gateway under an
# injected `OTEL_EXPORTER_OTLP_ENDPOINT` still saw the variable set at module scope. Moved here, it
# sees `None`. A conftest at the rootdir is imported before collection begins, which is early enough.
_strip_harness_otlp()


@pytest.fixture(scope="session", autouse=True)
def _no_harness_telemetry() -> None:
    """Re-strip once for the session, AFTER every conftest and plugin has had its turn.

    Kept alongside the import-time call rather than replaced by it, because they close different
    holes. The import-time call is the one that matters and is the one that was missing. This one
    catches a re-introduction: the per-directory conftests build real apps, `create_app` calls
    `load_dotenv()`, and a `.env` on a developer box naming an OTLP endpoint would put the variable
    back after this module was imported.

    Not restored afterwards, on purpose. The values belong to the harness, nothing in the suite reads
    them once removed, and putting them back at teardown would only re-arm the exporter while pytest
    is still writing its report.
    """
    _strip_harness_otlp()
