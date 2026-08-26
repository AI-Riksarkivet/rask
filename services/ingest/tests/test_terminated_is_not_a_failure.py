"""An operator stopping a run is not the run failing, and the record said it was.

MEASURED ON THE LIVE ESTATE 2026-08-26. A real ingest (`acme/u2verify`, 6,636 units enumerated) was
terminated from the run page. It recorded, correctly and legibly:

    run — terminated by operator with 6636 units enumerated

and then rendered as **FAILED**, in red, beside runs that had crashed. The reason is honest and on
the detail page; in a run LIST a deliberate stop is indistinguishable from a defect, which is exactly
the discrimination an operator is scanning that list to make.

TWO PATHS REACH IT and both collapsed into FAILED:

  * the GRACEFUL cancel — `POST /ingests/{id}/terminate` raises an event, the workflow wakes, stops
    scheduling and RETURNS. To Dapr that is a workflow which returned, so `runtime_status` is
    COMPLETED and the outcome carries the real status;
  * the ENGINE terminate — a hard stop, where `runtime_status` is literally TERMINATED.

The second was mapped `"TERMINATED": "FAILED"` in `_RUNTIME_STATUS`, so even Dapr telling us plainly
that someone killed the run was rewritten as a crash.

And the promotion whitelist in `merge_workflow_state` is the trap this file exists to keep shut. It
admits only statuses it names, so a new one is DROPPED rather than rejected — the run then reports
whatever the engine said (COMPLETE), which is the worst direction for this error to point. That is
not hypothetical: it is the defect `test_run_deadline.py` was written for when FAILED was missing
from the same list.
"""

from __future__ import annotations

from collections.abc import Callable

import pytest

from ingest.lineage import LineageRecorder, recorded_events, reset_events
from ingest.runs import _RUNTIME_STATUS, RunRecord, merge_workflow_state


def _record() -> RunRecord:
    return RunRecord(run_id="r", project="p", dataset="d", kind="local-dir")


def test_a_GRACEFUL_terminate_records_TERMINATED_not_FAILED() -> None:
    """The cancel path returns an outcome through a workflow that exited NORMALLY.

    So `runtime_status` is COMPLETED and everything depends on the outcome's own status surviving
    the merge. If it does not, the door answers COMPLETE for a run somebody stopped.
    """
    merged = merge_workflow_state(
        _record(),
        {
            "runtime_status": "COMPLETED",  # the workflow RETURNED — it did not crash
            "serialized_output": {
                "status": "TERMINATED",
                "errors": {"run": "terminated by operator with 6636 units enumerated"},
                "committed_version": None,
            },
        },
    )

    assert merged.status == "TERMINATED", "a stopped run reports as something other than TERMINATED"
    assert "terminated by operator" in merged.errors.get("run", "")


def test_an_ENGINE_terminate_is_not_rewritten_as_a_crash() -> None:
    """Dapr saying TERMINATED is the least ambiguous signal available, and it was discarded."""
    assert _RUNTIME_STATUS["TERMINATED"] == "TERMINATED", "the engine's own TERMINATED is mapped to something else"

    merged = merge_workflow_state(_record(), {"runtime_status": "TERMINATED"})
    assert merged.status == "TERMINATED"


def test_a_REAL_failure_is_still_FAILED() -> None:
    """The point is discrimination, so the other side has to keep working.

    Written because the tempting shape of this change — treat every stop as terminated — would make
    a crashed run read as a deliberate one, which is the same defect pointing the other way and does
    strictly more damage.
    """
    merged = merge_workflow_state(
        _record(),
        {
            "runtime_status": "COMPLETED",
            "serialized_output": {"status": "FAILED", "errors": {"run": "exceeded the 24.0h ceiling"}},
        },
    )
    assert merged.status == "FAILED"

    assert _RUNTIME_STATUS["FAILED"] == "FAILED"


def test_a_TERMINATED_run_is_not_a_provenance_DEFECT() -> None:
    """`is_defective` means "reported success and left no lineage edge".

    A terminated run never claimed success, so it cannot be that defect — and flagging it as one
    would put a red provenance warning on every run an operator deliberately stopped.
    """
    record = _record()
    record.status = "TERMINATED"
    record.lineage_run_present = False

    assert record.is_defective is False


# ── the LINEAGE half, which is where terminating a run could have fired the cascade ──────────────


class _Capture(LineageRecorder):
    """A recorder whose emits run INLINE, so a test can see what reached the transport.

    Same shape as `test_lineage_output_facets._Capture`, and for the same reason: the real `_emit`
    swallows everything (I8 — lineage must never fail a run that landed its data), which is correct
    in production and makes an emit that never happened indistinguishable from one that raised.
    """

    def __init__(self) -> None:
        super().__init__()
        self.calls: list[str] = []

    def _emit(self, emit: Callable[[], object]) -> None:
        emit()


def _terminal_event_type(status: str) -> str:
    reset_events()
    rec = _Capture()
    rec.terminal(run_id="r", project="p", dataset="d", status=status, version=None, rows=0, errors={"run": "x"})
    events = recorded_events()
    assert events, "terminal() recorded no event at all"
    return events[-1].event_type


def test_a_TERMINATED_run_does_NOT_emit_a_lineage_COMPLETE() -> None:
    """THE DANGEROUS ONE, and it is dangerous in the direction that does work rather than none.

    `terminal()` chose its event type with `"FAIL" if status == "FAILED" else "COMPLETE"`. Introducing
    TERMINATED without touching that line sends a stopped run down the `else` — so it would have
    emitted **COMPLETE**, and the medallion's `/bronze-arrival` head fires on exactly an event whose
    `eventType` is COMPLETE carrying the configured output pair (`ingest_trigger.py`). Terminating a
    run would then have STARTED the bronze->silver->gold cascade over a half-finished harvest.

    Strictly worse than the state it replaced: FAIL at least stopped there.
    """
    assert _terminal_event_type("TERMINATED") != "COMPLETE", "a terminated run emits the cascade's own trigger"


def test_a_TERMINATED_run_emits_ABORT() -> None:
    """ABORT is the OpenLineage state for this and `lineage_kit` already implements it —
    `RunTracker.abort` is documented as "the cancelled terminal (an operator stop, a per-chunk stop,
    a shutdown)". Using FAIL instead would keep the graph saying a person's decision was a defect,
    which is the same conflation this whole change removes, one layer down.
    """
    assert _terminal_event_type("TERMINATED") == "ABORT"


def test_the_other_two_terminals_are_unchanged() -> None:
    """The discrimination has to stay three-way, so pin the other two against this change."""
    assert _terminal_event_type("FAILED") == "FAIL"
    assert _terminal_event_type("COMPLETE") == "COMPLETE"


def test_the_ABORT_event_NAMES_the_dataset_the_run_touched(monkeypatch: pytest.MonkeyPatch) -> None:
    """The invisibility bug, pinned.

    The first version of the ABORT branch withheld `outputs`, reasoning that a terminated run's
    fragments are never committed so a WROTE edge would assert a write that did not happen. Measured
    on the live estate: the run landed in the graph correctly — right job, right `source_run_id`,
    right reason, newest by `event_time` — and appeared on the compute zone's run board NOWHERE,
    because a board that finds runs through the dataset they touched cannot see one that names none.

    Withholding the edge bought no safety either. The guard is the READER's `event_type = 'COMPLETE'`
    filter (`lineage/services/repository.py` calls it load-bearing for exactly this: "FAILed runs keep
    WROTE edges (producers() shows the attempt)"), and the cascade head fires only on COMPLETE, so an
    ABORT edge cannot wake bronze->silver->gold.
    """
    seen: dict[str, object] = {}

    class _Spy:
        def abort(self, reason: str, **kwargs: object) -> None:
            seen["reason"] = reason
            seen["outputs"] = list(kwargs.get("outputs") or [])

        def fail(self, *_a: object, **_k: object) -> None:
            raise AssertionError("a terminated run emitted FAIL")

        def complete(self, **_k: object) -> None:
            raise AssertionError("a terminated run emitted COMPLETE — the cascade's own trigger")

    monkeypatch.setattr("ingest.lineage._run", lambda *_a, **_k: _Spy())

    reset_events()
    _Capture().terminal(run_id="r", project="acme", dataset="abortproof", status="TERMINATED", version=None, rows=0, errors={"run": "terminated by operator"})

    assert "outputs" in seen, "the ABORT branch never ran"
    assert seen["outputs"], "ABORT named no dataset — the run is invisible to every by-dataset surface"
    assert "terminated by operator" in str(seen["reason"])
