"""`GET /v1/ingests/{id}` must reflect what the ENGINE knows, not what the POST handler wrote.

The defect: the first in-cluster run reported

    Orchestration completed with status: COMPLETED

while the API answered `{"status": "ACCEPTED", "units_total": 0, "committed_version": null}` — and
kept answering it. `InMemoryRunStore` is written once, by the POST handler, and nothing ever updates
it. That is not a missing write-back; it is the wrong owner. The workflow's durable history IS the
run's state, so the API reads it rather than keeping a second, staler copy.

These are pure unit tests over the merge: a live sidecar is exercised by the in-cluster lane (A11),
and pinning the merge here is what keeps that lane's failures about infrastructure rather than about
this arithmetic.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

import pytest
from fastapi.testclient import TestClient

from ingest.runs import InMemoryRunStore, RunRecord, merge_workflow_state


if TYPE_CHECKING:
    from collections.abc import Mapping


def _record(**overrides: object) -> RunRecord:
    base = {"run_id": "r1", "project": "demo", "dataset": "pages", "kind": "local-dir"}
    return RunRecord.model_validate({**base, **overrides})


def test_an_absent_instance_leaves_the_accepted_record_alone() -> None:
    """There is a real window where the run exists and the workflow does not.

    The POST handler writes the record and then schedules; between those the engine has no instance.
    Reporting the accepted record is the honest answer — treating it as an error would make a
    correctly-behaving plane look broken for the first few milliseconds of every run.
    """
    assert merge_workflow_state(_record(), None).status == "ACCEPTED"


@pytest.mark.parametrize(
    ("runtime", "expected"),
    [
        ("PENDING", "ACCEPTED"),
        ("RUNNING", "RUNNING"),
        ("SUSPENDED", "RUNNING"),
        ("COMPLETED", "COMPLETE"),
        ("FAILED", "FAILED"),
        # NOT "FAILED": the engine saying TERMINATED is the least ambiguous signal available that a
        # person stopped this run, and mapping it onto FAILED threw that away.
        ("TERMINATED", "TERMINATED"),
    ],
)
def test_engine_runtime_status_maps_onto_the_estate_vocabulary(runtime: str, expected: str) -> None:
    """SUSPENDED reading as RUNNING is the one worth stating: a paused run is not a finished one."""
    assert merge_workflow_state(_record(), {"runtime_status": runtime}).status == expected


def test_an_unknown_runtime_status_does_not_invent_one() -> None:
    """A vocabulary that grows upstream must not silently become a wrong answer here.

    Dapr ships STALLED and UNKNOWN too. Mapping an unrecognised status onto anything terminal would
    be a lie; keeping the last known one is at worst stale, and staleness is visible where a wrong
    terminal state is not.
    """
    assert merge_workflow_state(_record(status="RUNNING"), {"runtime_status": "STALLED"}).status == "RUNNING"


def test_a_completed_run_reports_its_committed_version_and_rows() -> None:
    """The three fields the first in-cluster run could not answer."""
    state = {
        "runtime_status": "COMPLETED",
        "serialized_output": json.dumps({"committed_version": 2, "rows": 4, "errors": {}, "status": "COMPLETE"}),
    }
    merged = merge_workflow_state(_record(), state)

    assert (merged.status, merged.committed_version, merged.units_done) == ("COMPLETE", 2, 4)


def test_COMPLETE_WITH_ERRORS_survives_the_engines_coarser_COMPLETED() -> None:
    """A run that landed 9,997 of 10,000 pages is COMPLETED to Dapr and not to an operator.

    Only the workflow's own outcome knows the difference, so the outcome's status wins over the
    engine's whenever it gave one. Flattening this to COMPLETE is how a partial ingest gets treated
    as a whole one.
    """
    state = {
        "runtime_status": "COMPLETED",
        "serialized_output": json.dumps({"committed_version": 5, "rows": 9997, "errors": {"p3": "corrupt"}, "status": "COMPLETE_WITH_ERRORS"}),
    }
    merged = merge_workflow_state(_record(), state)

    assert merged.status == "COMPLETE_WITH_ERRORS"
    assert merged.errors == {"p3": "corrupt"}


def test_a_workflow_outcome_cannot_downgrade_a_FAILED_run() -> None:
    """The engine is authoritative about failure; a stale output must not overrule it.

    A workflow that emitted an outcome and then failed in a later activity would otherwise report
    COMPLETE from the output it managed to write — the exact "green run, no data" reading this plane
    exists to eliminate.
    """
    state = {"runtime_status": "FAILED", "serialized_output": json.dumps({"status": "COMPLETE", "committed_version": 1})}

    assert merge_workflow_state(_record(), state).status == "FAILED"


def test_output_is_accepted_as_a_JSON_STRING_or_a_dict() -> None:
    """Dapr hands back `serialized_output` as a string; the in-process doubles hand back a dict.

    One code path for both, or the serialization branch that only ever runs in production is the one
    nobody tested — which is how a shape mismatch reaches a live run.
    """
    as_string = merge_workflow_state(_record(), {"runtime_status": "COMPLETED", "serialized_output": json.dumps({"committed_version": 3})})
    as_dict = merge_workflow_state(_record(), {"runtime_status": "COMPLETED", "output": {"committed_version": 3}})

    assert as_string.committed_version == as_dict.committed_version == 3


def test_unparseable_output_still_yields_the_runtime_status() -> None:
    """Half an answer beats none: the operator still learns the run completed."""
    merged = merge_workflow_state(_record(), {"runtime_status": "COMPLETED", "serialized_output": "not json"})

    assert merged.status == "COMPLETE"
    assert merged.committed_version is None


# ── through the HTTP surface ──────────────────────────────────────────────────────────


class _Reader:
    def __init__(self, state: Mapping[str, object] | None) -> None:
        self._state = state

    def state(self, run_id: str) -> Mapping[str, object] | None:
        return self._state


def _client(reader: object | None, record: RunRecord) -> TestClient:
    """The router under a bare app, matching `test_ingest_api.py` — the prefix is the deployment's
    concern, and mounting it here would couple these assertions to it."""
    import asyncio

    from fastapi import FastAPI

    from ingest.api import router

    app = FastAPI()
    app.include_router(router, prefix="/v1")
    store = InMemoryRunStore()
    asyncio.run(store.put(record))
    app.state.run_store = store
    app.state.workflow_reader = reader
    return TestClient(app)


def test_the_endpoint_reports_a_COMPLETED_run_as_complete() -> None:
    """The end-to-end shape of the fix, at the surface the UI actually reads (A20)."""
    state = {"runtime_status": "COMPLETED", "serialized_output": json.dumps({"committed_version": 2, "rows": 4, "status": "COMPLETE"})}
    body = _client(_Reader(state), _record()).get("/v1/ingests/r1").json()

    assert body["status"] == "COMPLETE"
    assert body["committed_version"] == 2
    assert body["units_done"] == 4


def test_the_endpoint_still_answers_when_the_engine_is_unreachable() -> None:
    """A status endpoint is what an operator reaches for when something is wrong.

    Failing precisely then — because the sidecar it queries is the thing that is down — would be the
    worst possible time, so an absent reader degrades to the accepted record.
    """
    response = _client(None, _record()).get("/v1/ingests/r1")

    assert response.status_code == 200
    assert response.json()["status"] == "ACCEPTED"


def test_A8_a_complete_run_with_no_lineage_is_reported_as_a_DEFECT() -> None:
    """'A green sync with no lineage edge is a bug the UI should surface.'

    Asserted through the merge rather than on a hand-built record, because the status that triggers
    the defect check is now the ENGINE's — a run only reaches COMPLETE via this path.
    """
    state = {"runtime_status": "COMPLETED", "serialized_output": json.dumps({"committed_version": 1, "rows": 4, "status": "COMPLETE"})}
    body = _client(_Reader(state), _record(lineage_run_present=False)).get("/v1/ingests/r1").json()

    assert body["defect"] is not None
    assert "provenance" in body["defect"]


def test_a_complete_run_WITH_lineage_reports_no_defect() -> None:
    state = {"runtime_status": "COMPLETED", "serialized_output": json.dumps({"committed_version": 1, "rows": 4, "status": "COMPLETE"})}
    body = _client(_Reader(state), _record(lineage_run_present=True)).get("/v1/ingests/r1").json()

    assert body["defect"] is None


# ── A8's other half: the provenance join ──────────────────────────────────────────────


class _Provenance:
    def __init__(self, answer: bool | None) -> None:
        self._answer = answer

    def has_run(self, run_id: str) -> bool | None:
        return self._answer


def _client_with_provenance(answer: bool | None, record: RunRecord) -> TestClient:
    completed = {"committed_version": 1, "rows": 4, "status": "COMPLETE"}
    client = _client(_Reader({"runtime_status": "COMPLETED", "serialized_output": json.dumps(completed)}), record)
    client.app.state.provenance_reader = _Provenance(answer)
    return client


def test_a_run_the_graph_does_not_know_is_a_DEFECT() -> None:
    """The real A8 case: bronze holds rows nothing can explain the origin of."""
    body = _client_with_provenance(False, _record()).get("/v1/ingests/r1").json()

    assert body["defect"] is not None
    assert "provenance" in body["defect"]


def test_a_run_the_graph_KNOWS_is_not_a_defect() -> None:
    """The happy path — and the one the first green in-cluster lane wrongly failed.

    `lineage_run_present` defaulted to False and nothing ever set it, so EVERY completed run reported
    a provenance defect. A gate that fires on every run is one an operator learns to scroll past, and
    then the single real provenance hole goes by unnoticed among the false ones.
    """
    assert _client_with_provenance(True, _record()).get("/v1/ingests/r1").json()["defect"] is None


def test_an_UNREACHABLE_graph_is_not_reported_as_a_defect() -> None:
    """Absent and unknown are different answers, and only one of them is a bug.

    If the lineage service cannot be reached we do not know whether provenance exists. Claiming a
    defect from ignorance is exactly how the check stops meaning anything.
    """
    assert _client_with_provenance(None, _record()).get("/v1/ingests/r1").json()["defect"] is None


def test_provenance_is_not_consulted_for_a_run_still_in_flight() -> None:
    """A RUNNING run has no lineage terminal yet, so asking is both pointless and misleading."""
    client = _client(_Reader({"runtime_status": "RUNNING"}), _record())
    client.app.state.provenance_reader = _Provenance(False)

    assert client.get("/v1/ingests/r1").json()["defect"] is None


# ── A3: the run must remain observable across a pod death ─────────────────────────────


def test_a_run_the_store_LOST_is_rebuilt_from_the_engine() -> None:
    """A cache miss is not a missing run — the A3 kill-pod finding.

    `InMemoryRunStore` is process-local, so a pod restart drops every accepted record while the
    workflows keep executing durably. The API answered 404 for a run that was still working, which
    is worse than losing progress: an operator watching a long harvest sees it disappear.

    Nothing needs persisting. The POST handler passes the whole RunSpec as the workflow input, so the
    engine already holds the accepted-time record.
    """
    from fastapi import FastAPI

    from ingest.api import router

    app = FastAPI()
    app.include_router(router, prefix="/v1")
    app.state.run_store = InMemoryRunStore()  # deliberately EMPTY — the pod just restarted
    app.state.workflow_reader = _Reader(
        {
            "runtime_status": "RUNNING",
            "serialized_input": json.dumps({"run_id": "r9", "kind": "local-dir", "project": "demo", "dataset": "pages"}),
        }
    )
    body = TestClient(app).get("/v1/ingests/r9").json()

    assert body["run_id"] == "r9"
    assert body["status"] == "RUNNING"


def test_a_run_NEITHER_the_store_nor_the_engine_knows_is_still_404() -> None:
    """The rebuild must not turn every unknown id into a fabricated run."""
    from fastapi import FastAPI

    from ingest.api import router

    app = FastAPI()
    app.include_router(router, prefix="/v1")
    app.state.run_store = InMemoryRunStore()
    app.state.workflow_reader = _Reader(None)

    assert TestClient(app).get("/v1/ingests/nope").status_code == 404


# ── the cascade trigger: the terminal event must be one /bronze-arrival recognises ────


def test_a_completed_run_NAMES_the_table_it_wrote() -> None:
    """A COMPLETE with no outputs records a run that produced nothing — and A8 still passes.

    A8 asks whether the run EXISTS, so a half-recorded provenance survives it: the graph knows the
    run happened and cannot say what it wrote. The WROTE edge is the whole point of the record.
    """
    from ingest.lineage import LineageRecorder, recorded_events, reset_events

    reset_events()
    LineageRecorder().terminal("r1", "COMPLETE", version=2, rows=4, errors={}, project="bronze", dataset="pages")

    event = recorded_events()[-1]
    assert (event.project, event.dataset) == ("bronze", "pages")


def test_the_terminal_event_matches_what_the_CASCADE_HEAD_filters_on() -> None:
    """The medallion's `/bronze-arrival` fires only on `eventType == COMPLETE` whose outputs contain
    its configured `{namespace, name}` pair (`ingest_trigger.py:51-58`).

    So the output name has to be the CATALOG's table id (`bronze$pages`), not the bare dataset name.
    Composing it differently would leave a bronze write that lands its data and wakes nothing —
    indistinguishable, from the outside, from a cascade that is simply switched off.

    Asserted against the head's own predicate rather than against a copy of it, so the two cannot
    drift apart: if the medallion changes what it filters on, this fails.
    """
    from medallion.core.config import MedallionSettings
    from medallion.services.ingest_trigger import _bronze_write_dataset

    settings = MedallionSettings.model_validate({"bronze_namespace": "bronze", "bronze_dataset": "bronze$pages"})
    event = {
        "eventType": "COMPLETE",
        "outputs": [{"namespace": "bronze", "name": "bronze$pages"}],
    }

    assert _bronze_write_dataset(event, settings, "") == "bronze$pages"


def test_a_FAILED_run_does_NOT_wake_the_cascade() -> None:
    """The head filters on COMPLETE for a reason: a FAIL announces that data is NOT there.

    Firing the cascade off one would kick every downstream mover over rows that were never written,
    and each would then fail for its own unrelated-looking reason.
    """
    from medallion.core.config import MedallionSettings
    from medallion.services.ingest_trigger import _bronze_write_dataset

    settings = MedallionSettings.model_validate({"bronze_namespace": "bronze", "bronze_dataset": "bronze$pages"})
    event = {"eventType": "FAIL", "outputs": [{"namespace": "bronze", "name": "bronze$pages"}]}

    assert _bronze_write_dataset(event, settings, "") is None


def test_units_total_comes_from_the_engine_while_the_run_is_IN_FLIGHT() -> None:
    """The denominator has to exist BEFORE the run ends, or a progress bar is impossible.

    `units_total` was declared and never assigned, so the API could say "4 done" and never
    "4 of 500". While a run is in flight there is no output yet — only the custom status — so that is
    where the live total is read from.
    """
    state = {
        "runtime_status": "RUNNING",
        "serialized_custom_status": json.dumps({"units_total": 500, "chunks": 1}),
    }
    merged = merge_workflow_state(_record(), state)

    assert merged.units_total == 500
    assert merged.status == "RUNNING"


def test_units_DONE_also_comes_from_the_engine_while_the_run_is_IN_FLIGHT() -> None:
    """The NUMERATOR needed the same two sources the denominator got, and only had one.

    `units_total` reads from the output when terminal and the custom status while in flight — its own
    comment calls that "what makes '4 of 500' available for the whole life of a run". `units_done` read
    only `rows` from the terminal output, so the numerator was pinned at 0 for the entire run and the
    two halves of one progress bar disagreed about when they were readable.

    Sibling rather than folded into the denominator test: they assert different facts, and a test that
    checks both passes while either is broken.
    """
    state = {
        "runtime_status": "RUNNING",
        "serialized_custom_status": json.dumps({"units_total": 500, "units_done": 320, "finalizing": 4}),
    }
    merged = merge_workflow_state(_record(), state)

    assert merged.units_total == 500
    assert merged.units_done == 320, (
        f"the numerator is still pinned at {merged.units_done} while the run is in flight — it reads only the "
        "terminal output's `rows`, which does not exist until finalize returns, so the progress bar shows "
        "'0 of 500' for the whole run."
    )


def test_units_total_survives_into_the_TERMINAL_record() -> None:
    """Once the run ends, the output carries it permanently — the custom status is the live view."""
    state = {
        "runtime_status": "COMPLETED",
        "serialized_output": json.dumps({"committed_version": 2, "rows": 500, "units_total": 500, "status": "COMPLETE"}),
    }
    merged = merge_workflow_state(_record(), state)

    assert (merged.units_done, merged.units_total) == (500, 500)


def test_a_partial_run_reports_BOTH_numbers() -> None:
    """The case the field exists for: 497 of 500 landed, and an operator can see the 3 that did not."""
    state = {
        "runtime_status": "COMPLETED",
        "serialized_output": json.dumps(
            {"committed_version": 9, "rows": 497, "units_total": 500, "errors": {"p1": "404", "p2": "404", "p3": "corrupt"}, "status": "COMPLETE_WITH_ERRORS"}
        ),
    }
    merged = merge_workflow_state(_record(), state)

    assert (merged.units_done, merged.units_total) == (497, 500)
    assert len(merged.errors) == 3


def test_a_RAISED_failure_reports_its_REASON_not_an_empty_dict() -> None:
    """A run that says FAILED and nothing else is a run nobody can act on.

    Measured 2026-08-06: two runs answered `{"status":"FAILED","units_total":0,"errors":{}}`. No
    reason, no way to tell a source that enumerated nothing from an activity that died on its first
    call — and the operator's only remaining move is to read pod logs and correlate by timestamp.

    The two failure MODES are not symmetric, which is what the original code missed. A workflow that
    fails BY POLICY returns a FAILED outcome and its errors ride `serialized_output` — the deadline
    and unit-ceiling paths both do this. A workflow that RAISES produces no output at all and Dapr
    records the detail in its own failure field. Reading only the first means the LOUDER failure is
    the silent one.
    """
    from ingest.runs import RunRecord, merge_workflow_state

    record = RunRecord(run_id="r1", project="bind86", dataset="pages", kind="s3-prefix")
    raised = {
        "runtime_status": "FAILED",
        "failure_details": {"message": "catalog refused create_table: 403 can_create_table required"},
    }

    merged = merge_workflow_state(record, raised)

    assert merged.status == "FAILED"
    assert merged.errors, "a raised failure reported no reason at all"
    assert "can_create_table" in str(merged.errors), f"the engine's reason did not reach the caller: {merged.errors}"


def test_a_POLICY_failure_keeps_its_OWN_errors() -> None:
    """The outcome's errors win where they exist. The engine's detail is a FALLBACK, not a
    replacement — a deadline refusal explains itself far better than "the workflow returned"."""
    from ingest.runs import RunRecord, merge_workflow_state

    record = RunRecord(run_id="r2", project="bind86", dataset="pages", kind="s3-prefix")
    policy = {
        "runtime_status": "COMPLETED",
        "serialized_output": {"status": "FAILED", "errors": {"run": "exceeded the 2h run deadline"}},
        "failure_details": {"message": "should not be preferred"},
    }

    merged = merge_workflow_state(record, policy)

    assert "deadline" in str(merged.errors)
    assert "should not be preferred" not in str(merged.errors)


def test_an_UNRECORDED_reason_does_not_raise() -> None:
    """The caller is asking why a run failed. A KeyError is a worse answer than "unknown", and the
    field's shape has moved across dapr-ext-workflow versions — it is not a contract this plane owns."""
    from ingest.runs import RunRecord, merge_workflow_state

    record = RunRecord(run_id="r3", project="bind86", dataset="pages", kind="s3-prefix")

    merged = merge_workflow_state(record, {"runtime_status": "FAILED"})

    assert merged.status == "FAILED"  # must not raise


class _ProgressCtx:
    """A workflow ctx that RECORDS custom status, which the shared doubles discard."""

    def __init__(self) -> None:
        self.statuses: list[str] = []
        self.instance_id = "r-progress"

    def call_activity(self, fn: Any, *, input: Any = None, retry_policy: Any = None) -> None:  # noqa: A002
        return None

    def set_custom_status(self, status: str) -> None:
        self.statuses.append(status)


def _drive_chunk(ctx: Any, chunk: dict[str, Any], drained: dict[str, Any]) -> dict[str, Any]:
    """Run `chunk_run` to completion, feeding it the drain result."""
    from ingest.workflow import chunk_run

    gen = chunk_run(ctx, chunk)
    gen.send(None)  # advance to the publish_units yield
    gen.send(None)  # its result is discarded; advance to the drain_chunk yield
    try:
        gen.send(drained)  # drain_chunk
    except StopIteration as stop:
        return dict(stop.value)
    try:
        gen.send({"errors": {}, "errors_total": 0})  # reconcile_chunk
    except StopIteration as stop:
        return dict(stop.value)
    raise AssertionError("chunk_run did not finish")


def test_a_chunk_reports_its_own_PROGRESS_while_the_fan_out_runs() -> None:
    """The fan-out is the longest phase of a large harvest and it published nothing.

    The parent sets `units_total` before the fan-out and `finalizing` after the fan-in, and in between
    it is blocked on `when_all` — so for the whole fan-out, potentially hours, the run's own status is
    frozen at the value it had before any work started.

    The parent CANNOT fix this itself: a workflow can only set its status between yields, and
    `when_all` is one yield. So the progress has to come from the child, whose status is readable per
    instance while it runs.

    `units_done` also joins `ChunkResult`, because the parent could otherwise not aggregate what its
    children achieved even at fan-in: the drain reports it, `chunk_run` reads it to decide whether to
    reconcile, and then dropped it on the floor.
    """
    ctx = _ProgressCtx()
    chunk = {"run_id": "r-progress", "chunk_id": "c1", "dataset": "d", "keys": [], "offset": 0, "count": 40}

    result = _drive_chunk(ctx, chunk, {"fragments": ["f1"], "errors": {}, "errors_total": 0, "units_done": 40})

    assert ctx.statuses, (
        "the child published no status, so nothing anywhere advances during the fan-out — the run reads "
        "'0 of N' from before the first unit was published until finalize returns."
    )
    published = json.loads(ctx.statuses[-1])
    assert published.get("units_done") == 40, f"the child's status does not carry its progress: {published}"
    assert published.get("chunk_id") == "c1", f"the child's status does not say which chunk it is: {published}"

    assert result.get("units_done") == 40, (
        "ChunkResult drops units_done, so the parent cannot aggregate what its children achieved even at "
        f"fan-in — the drain reports it and chunk_run reads it to decide on reconcile, then discards it: {result}"
    )
