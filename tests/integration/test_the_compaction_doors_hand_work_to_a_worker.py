"""The catalog's two compaction doors, driven over HTTP against a REAL dataset.

`tests/unit/test_maintenance_runs_on_workers.py` pins the dataplane primitives; this proves the same
three-way split survives the doors: `/compaction_plan` hands back queue-shippable tasks without minting
a version, a worker executes them, and `/compaction_commit` folds the results into one metadata-only
version. Driven through `real_ns_client` so the table, its fragments and the rewrite are all real —
a mocked namespace could not tell a plan that ran from one that was reported.
"""

from __future__ import annotations

from typing import Any

import lance
import pyarrow as pa
import pyarrow.ipc as ipc
from fastapi.testclient import TestClient
from lance.optimize import CompactionTask


ARROW_STREAM = {"content-type": "application/vnd.apache.arrow.stream"}


def _arrow_ipc(table: pa.Table) -> bytes:
    sink = pa.BufferOutputStream()
    with ipc.new_stream(sink, table.schema) as writer:
        writer.write_table(table)
    return bytes(sink.getvalue().to_pybytes())


def _fragmented_table(client: TestClient, rows_each: int = 50, appends: int = 3) -> str:
    """Create `db$t` and leave it split across `appends + 1` fragments; return its location."""
    assert client.post("/v1/namespace/db/create", json={}).status_code == 200
    first = pa.table({"id": pa.array(range(rows_each), pa.int64())})
    assert client.post("/v1/table/db$t/create?mode=overwrite", content=_arrow_ipc(first), headers=ARROW_STREAM).status_code == 200

    location = client.post("/v1/table/db$t/describe", json={}).json()["location"]
    for i in range(appends):
        start = rows_each * (i + 1)
        chunk = pa.table({"id": pa.array(range(start, start + rows_each), pa.int64())})
        lance.write_dataset(chunk, location, mode="append", data_storage_version="2.2")
    assert len(lance.dataset(location).get_fragments()) == appends + 1
    return str(location)


def test_the_plan_door_hands_back_tasks_without_minting_a_version(real_ns_client: TestClient) -> None:
    location = _fragmented_table(real_ns_client)
    before = lance.dataset(location).version

    response = real_ns_client.post("/v1/table/db$t/compaction_plan", json={"target_rows_per_fragment": 10_000})

    assert response.status_code == 200, response.text
    body: dict[str, Any] = response.json()
    assert body["read_version"] == before
    assert body["tasks"], "a four-fragment table with a 10k-row target has work to plan"
    # Planning is a metadata read. A version minted here would mean the door did the work itself.
    assert lance.dataset(location).version == before


def test_a_worker_executes_the_plan_and_the_commit_door_lands_it(real_ns_client: TestClient) -> None:
    location = _fragmented_table(real_ns_client)
    before = lance.dataset(location)

    plan = real_ns_client.post("/v1/table/db$t/compaction_plan", json={"target_rows_per_fragment": 10_000}).json()
    # The WORKER half — a separate process in production, holding vended creds and the task string it
    # read off the queue. Nothing here touches the catalog.
    # pylance's ``optimize.pyi`` stops at ``execute``; ``from_json``/``json`` exist on the Rust class and
    # round-trip (verified 2026-09-03). The alias is where that stub gap is named — see
    # ``dataplane._RewriteResult`` for the production side of the same absence.
    task_cls: Any = CompactionTask
    results = [task_cls.from_json(task).execute(lance.dataset(location)).json() for task in plan["tasks"]]

    response = real_ns_client.post("/v1/table/db$t/compaction_commit", json={"results": results})

    assert response.status_code == 200, response.text
    body = response.json()
    after = lance.dataset(location)
    assert body["version"] == after.version > before.version
    assert body["fragments_removed"] == 4
    assert body["fragments_added"] == 1
    assert len(after.get_fragments()) == 1
    assert after.count_rows() == 200
    assert after.to_table().sort_by("id")["id"].to_pylist() == list(range(200))


def test_a_healthy_table_plans_no_work_and_is_not_an_error(real_ns_client: TestClient) -> None:
    _fragmented_table(real_ns_client, appends=0)
    response = real_ns_client.post("/v1/table/db$t/compaction_plan", json={"target_rows_per_fragment": 10_000})
    assert response.status_code == 200, response.text
    assert response.json()["tasks"] == []


def test_an_option_the_door_does_not_forward_is_refused_at_the_wire(real_ns_client: TestClient) -> None:
    """`num_threads` is an EXECUTOR knob, and the executor is not this pod.

    Pydantic's `extra="forbid"` refuses it at the wire — before `plan_compaction`'s own guard — so a
    caller tuning a machine the catalog does not own learns it instead of watching the plan ignore it.
    """
    _fragmented_table(real_ns_client)
    response = real_ns_client.post("/v1/table/db$t/compaction_plan", json={"num_threads": 8})
    assert response.status_code == 422, response.text


def test_the_commit_door_refuses_an_empty_result_set(real_ns_client: TestClient) -> None:
    _fragmented_table(real_ns_client)
    response = real_ns_client.post("/v1/table/db$t/compaction_commit", json={"results": []})
    assert response.status_code == 422, response.text


def test_a_malformed_worker_result_is_a_client_error_not_a_crash(real_ns_client: TestClient) -> None:
    # Results arrive off a queue and are client-controlled. A missing field must be a 4xx.
    _fragmented_table(real_ns_client)
    response = real_ns_client.post("/v1/table/db$t/compaction_commit", json={"results": ['{"nope": 1}']})
    assert response.status_code == 400, response.text


def test_neither_door_silently_compacts_MAIN_when_a_BRANCH_is_named(real_ns_client: TestClient) -> None:
    """A branch has its own fragments; planning main and calling it the branch's work is the
    dropped-parameter defect that would compact the wrong dataset with a 200."""
    location = _fragmented_table(real_ns_client)
    before = lance.dataset(location).version

    for door, payload in (("compaction_plan", {}), ("compaction_commit", {"results": ["{}"]})):
        response = real_ns_client.post(f"/v1/table/db$t/{door}?branch=work", json=payload)
        assert response.status_code not in (200, 201), f"{door} answered a branch request from main"
    assert lance.dataset(location).version == before


def test_both_doors_land_on_the_writer_rung() -> None:
    """The authorize gate reaches ``can_write_data`` for these suffixes by falling through the table
    default, and that is the correct rung rather than an accident of naming.

    A compaction preserves every row — Lance commits it as a `Rewrite` — so it is strictly less
    powerful than the `delete` a writer already holds, and gating it higher would mean maintenance
    could only run as an owner. The dangerous direction is the other one: a suffix that happened to
    match the reader vocabulary would publish a version-minting door at the reader rung.
    """
    from catalog.api.fga_deps import _action_relation

    assert _action_relation("table", "compaction_plan") == "can_write_data"
    assert _action_relation("table", "compaction_commit") == "can_write_data"
