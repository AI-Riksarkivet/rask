"""A cascade hop reaches the lineage graph as an OPEN run before it reaches it as a finished one.

[[LH-173]]. The medallion emitted FAIL and the COMPLETE default and nothing else, so on the Ray lane
the dispatch branch acked having emitted nothing and the single event for the whole run arrived after
the workflow's wake-up — minutes to hours later. Between those moments NO RUN EXISTED IN THE GRAPH,
and a hop that died before its terminal was indistinguishable there from one that never began.

WHAT IS ASSERTED, AND WHY IT IS NOT A RACE. "A run exists while the job is RUNNING" cannot be checked
without a job slow enough to catch mid-flight, and a test that sleeps into a window is a flake waiting
for a faster machine. The durable proof is in the event stream instead: a hop carries a START **and** a
terminal under ONE `runId`. The START is emitted before the work begins (`_emit_start_run` runs ahead
of `_write_stage`, pinned in the medallion's own suite), so its presence is what makes the run open
from dispatch — the ordering is a unit fact and the arrival is this one.

THE SHARED `runId` IS THE OTHER HALF. `run_id` is derived from `(project, operation, token)`
(`events.py`), and the consumer's `MERGE (r:Run {run_id})` is keyed on the id alone — so a START with
its own id would leave an orphan open run behind every hop instead of one node that closes. That is
the failure this asserts against, not merely the absence of a START.
"""

from __future__ import annotations

import json
import os
from collections import defaultdict

import pytest
import requests


LINEAGE_URL = os.environ.get("LANCE_E2E_LINEAGE_URL", "").rstrip("/")
TOKEN = os.environ.get("LANCE_E2E_TOKEN", "")

pytestmark = pytest.mark.e2e

#: Terminal states a run can close in. RECONCILED is lineage's own repair marker, not an outcome the
#: producer chose, so it does not count as a hop closing itself.
_TERMINAL = {"COMPLETE", "FAIL"}

#: Job-name prefix of the SYNTHETIC events other suites write straight into the feed, excluded because
#: this module is about CASCADE HOPS and those are not one.
#:
#: `test_lineage_e2e.py:596` writes `job="e2e/write.events_feed"` with `author="data_eng"` and event
#: times backdated to 2026-07-06, to exercise the feed's retention and read-audit behaviour — a START
#: with no terminal is part of what it is testing. Measured 2026-09-20 after running that suite's
#: destructive leg for the first time: the fixture's run was the ONLY `e2e/*` job in the 300-event
#: window and the only unterminated run, and it failed two legs here that are about the medallion
#: cascade and had nothing to say about it.
#:
#: SCOPING, NOT SILENCING. The control leg below still requires a real run to be present, so this
#: cannot empty the fixture and pass vacuously; and a cascade hop never carries an `e2e/` job name —
#: the producer names its jobs for the transition it runs.
_SYNTHETIC_JOB_PREFIX = "e2e/"


@pytest.fixture(scope="module")
def runs() -> dict[str, list[str]]:
    """Every run in the feed, as the list of states it was reported in."""
    if not (LINEAGE_URL and TOKEN):
        pytest.skip("set LANCE_E2E_LINEAGE_URL + LANCE_E2E_TOKEN (a deployed lineage service)")
    response = requests.get(f"{LINEAGE_URL}/events?limit=300", headers={"authorization": f"Bearer {TOKEN}"}, timeout=30)
    if response.status_code in (401, 403):
        pytest.skip(f"the lineage feed is governed and this token cannot read it ({response.status_code})")
    assert response.status_code == 200, response.text

    grouped: dict[str, list[str]] = defaultdict(list)
    for event in response.json().get("events", []):
        if str(event.get("job") or "").startswith(_SYNTHETIC_JOB_PREFIX):
            continue
        payload = event.get("event") or {}
        if isinstance(payload, str):
            payload = json.loads(payload)
        run_id = (payload.get("run") or {}).get("runId") or ""
        if run_id:
            grouped[run_id].append(str(event.get("event_type")))
    return dict(grouped)


def test_some_run_was_opened_before_it_finished(runs: dict[str, list[str]]) -> None:
    """Without a START anywhere, the rest of this file passes over an empty set."""
    started = [rid for rid, states in runs.items() if "START" in states]

    assert started, (
        "no run in the feed was ever reported as START, so every hop is invisible in the graph for its "
        "whole runtime and a hop that died before its terminal looks like one that never began"
    )


def test_every_opened_run_is_CLOSED_by_a_terminal(runs: dict[str, list[str]]) -> None:
    """An orphan open run per hop would be worse than no START at all — it would make the graph assert
    that work is in flight which finished long ago."""
    open_runs = {rid: states for rid, states in runs.items() if "START" in states and not (_TERMINAL & set(states))}

    assert not open_runs, f"these runs were opened and never closed under the same runId: {open_runs}"


def test_the_terminal_shares_the_STARTED_run_id_rather_than_minting_one(runs: dict[str, list[str]]) -> None:
    """The derivation is what makes the MERGE close the run the START opened. A START carrying its own
    id would still satisfy the two assertions above while doubling the graph's run nodes."""
    started = {rid: states for rid, states in runs.items() if "START" in states}
    assert started, "nothing started; see the first leg"

    for rid, states in started.items():
        assert _TERMINAL & set(states), f"run {rid} carries a START with no terminal under the SAME id: {states}"
