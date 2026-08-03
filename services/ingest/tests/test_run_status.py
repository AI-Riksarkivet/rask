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

import pytest
from fastapi.testclient import TestClient
from ingest.runs import InMemoryRunStore, RunRecord, merge_workflow_state


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
        ("TERMINATED", "FAILED"),
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
    def __init__(self, state: dict[str, object] | None) -> None:
        self._state = state

    def state(self, run_id: str) -> dict[str, object] | None:
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
