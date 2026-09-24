"""``--require-live``: a live drive that skipped anything is a failed drive, not a pass ([[LH-196]]).

Every suite under `tests/e2e-py` guards each test on the environment it needs, which is what keeps
them runnable at all — and pytest reports a skip as success. So a drive against a half-forwarded
estate reports green having proved nothing: `make e2e-dummy-lane` was measured at `3 passed, 4
skipped` with a zero exit, from the estate's only GPU-free cascade prover.

OPT-IN, because skipping is correct in every other context: an offline `make test` deselects these by
marker, and a developer reaching for one suite wants the others to skip. It is the TARGET that knows
it is driving something live, so it is the target that asks.

BROADER THAN GUARDING THE VARIABLES, which is why the Makefile does both. A guard names the missing
variable early and cannot see a skip that is not about one — a token that is not a project admin, an
estate with no Ray head — and those are the skips that survive a fully-forwarded invocation.

ITS OWN MODULE rather than more lines in `conftest.py`, so the mechanism can be exercised for real.
That conftest also carries a session-scoped cleanup fixture that reaches for a live estate, so a test
copying the whole file to check this option would be standing up the estate to test a flag.
"""

from __future__ import annotations

import pytest


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption(
        "--require-live",
        action="store_true",
        default=False,
        help="fail the run if any test was skipped — for targets that drive a live estate",
    )


def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
    """Turn a skipped live drive into a failure, naming what was skipped.

    Reads the terminal reporter's own tally rather than counting reports by hand: that is the number
    the operator sees, so a second count could disagree with the summary printed above it.

    A run that already failed is left alone — the first failure is the one worth reading, and
    replacing its exit code with this one would bury it.
    """
    if not session.config.getoption("--require-live") or exitstatus != 0:
        return
    reporter = session.config.pluginmanager.get_plugin("terminalreporter")
    skipped = getattr(reporter, "stats", {}).get("skipped", []) if reporter is not None else []
    if not skipped:
        return
    names = sorted({str(getattr(report, "nodeid", "?")) for report in skipped})
    print(f"\n!! --require-live: {len(names)} test(s) skipped, so this drive proved less than it reported:")  # noqa: T201 — the target's own output
    for name in names:
        print(f"   skipped: {name}")  # noqa: T201
    session.exitstatus = pytest.ExitCode.TESTS_FAILED
