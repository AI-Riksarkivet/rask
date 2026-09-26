"""The catalog's two compaction doors, driven over HTTP against a REAL dataset.

`tests/unit/test_maintenance_runs_on_workers.py` pins the dataplane primitives; this proves the same
three-way split survives the doors: `/compaction_plan` hands back queue-shippable tasks without minting
a version, a worker executes them, and `/compaction_commit` folds the results into one metadata-only
version. Driven through `real_ns_client` so the table, its fragments and the rewrite are all real —
a mocked namespace could not tell a plan that ran from one that was reported.
"""

from __future__ import annotations

import json
import os
from typing import Any, cast

import lance
import pyarrow as pa
import pyarrow.ipc as ipc
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from lance.optimize import CompactionTask


ARROW_STREAM = {"content-type": "application/vnd.apache.arrow.stream"}

#: The executor's three memory bounds, as rask's own sweep sends them by default.
BOUNDS = {"batch_size": 64, "num_threads": 2, "max_source_bytes": 256 * 1024 * 1024}
MIB = 1024 * 1024
#: The door's `max_source_bytes` range: the maintenance setting's own floor and ceiling.
MAX_SOURCE_BYTES_FLOOR = MIB
MAX_SOURCE_BYTES_CEILING = 1024 * MIB
#: The problem titles the framework gives a request no route took; a door's own refusal carries its
#: domain error's name instead (`service_kit.lakehouse.ns_errors.problem_detail`).
_ROUTING_MISSES = ("Not Found", "Method Not Allowed")


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

    response = real_ns_client.post("/management/v1/table/db$t/compaction_plan", json={"target_rows_per_fragment": 10_000, **BOUNDS})

    assert response.status_code == 200, response.text
    body: dict[str, Any] = response.json()
    assert body["read_version"] == before
    assert body["tasks"], "a four-fragment table with a 10k-row target has work to plan"
    # Planning is a metadata read. A version minted here would mean the door did the work itself.
    assert lance.dataset(location).version == before


def test_a_worker_executes_the_plan_and_the_commit_door_lands_it(real_ns_client: TestClient) -> None:
    location = _fragmented_table(real_ns_client)
    before = lance.dataset(location)

    plan = real_ns_client.post("/management/v1/table/db$t/compaction_plan", json={"target_rows_per_fragment": 10_000, **BOUNDS}).json()
    # The WORKER half — a separate process in production, holding vended creds and the task string it
    # read off the queue. Nothing here touches the catalog.
    # pylance's ``optimize.pyi`` stops at ``execute``; ``from_json``/``json`` exist on the Rust class and
    # round-trip (verified 2026-09-03). The alias is where that stub gap is named — see
    # ``dataplane._RewriteResult`` for the production side of the same absence.
    task_cls: Any = CompactionTask
    results = [task_cls.from_json(task).execute(lance.dataset(location)).json() for task in plan["tasks"]]

    response = real_ns_client.post("/management/v1/table/db$t/compaction_commit", json={"results": results})

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
    response = real_ns_client.post("/management/v1/table/db$t/compaction_plan", json={"target_rows_per_fragment": 10_000, **BOUNDS})
    assert response.status_code == 200, response.text
    assert response.json()["tasks"] == []


@pytest.mark.parametrize(
    "knob",
    [
        pytest.param({"io_buffer_size": 8192}, id="io_buffer_size"),
        pytest.param({"materialize_deletions_threadhold": 0.5}, id="a-misspelling-lance-refuses"),
    ],
)
def test_an_option_the_door_does_not_forward_is_refused_at_the_wire(real_ns_client: TestClient, knob: dict[str, Any]) -> None:
    """Pydantic's `extra="forbid"` refuses an unforwarded knob at the wire — before
    `plan_compaction`'s own guard — so a caller tuning something the plan ignores learns it.

    `io_buffer_size` rather than a bound, and the distinction is worth stating: the three executor
    bounds (`batch_size`, `num_threads`, `max_source_bytes`) and the repack mode DO cross this door,
    because Lance bakes them into the task at plan time and `CompactionTask.execute(dataset)` accepts
    no options. `io_buffer_size` is baked the same way, but nothing in this estate sets it, and a door
    widened for a knob nobody uses is a door widened for nothing. `threadhold` is a name pylance 12.0.0
    raises `ValueError: Invalid compaction option` for, so forwarding it would answer 500.
    """
    _fragmented_table(real_ns_client)
    response = real_ns_client.post("/management/v1/table/db$t/compaction_plan", json={**knob, **BOUNDS})
    assert response.status_code == 422, response.text


def test_the_deletion_threshold_is_baked_into_every_task(real_ns_client: TestClient) -> None:
    """Forwarded under Lance's own name, `materialize_deletions_threshold` (`lance/optimize.py:44`, pylance 12.0.0)."""
    _fragmented_table(real_ns_client)
    response = real_ns_client.post(
        "/management/v1/table/db$t/compaction_plan",
        json={"target_rows_per_fragment": 1024, **BOUNDS, "materialize_deletions": True, "materialize_deletions_threshold": 0.37},
    )

    assert response.status_code == 200, response.text
    baked = _baked(response.json()["tasks"])
    assert [(options.get("materialize_deletions"), options.get("materialize_deletions_threshold")) for options in baked] == [(True, 0.37)] * len(baked), baked


def _baked(response_tasks: list[str]) -> list[dict[str, Any]]:
    assert response_tasks, "the table has work to plan, so the options below are read from something"
    return [json.loads(task)["options"] for task in response_tasks]


def test_the_executors_own_MEMORY_BOUNDS_are_baked_into_every_task_the_door_plans(real_ns_client: TestClient) -> None:
    """The three that must cross, read back out of the tasks the real door hands the worker.

    Against ~1.8 MB bronze rows, Lance's 8192-row default batch is ~15 GB per compute thread on a
    thread count taken from the host's cores, and Lance's default `max_source_bytes` is no limit at
    all. The maintenance plane bounds all three for the in-pod rewrite; a door that refused or
    misrouted one would leave the distributed path the one route in the estate it could not bound.

    Distinct, non-default numbers, so a door that swapped two or dropped one for Lance's default
    cannot pass. A task's JSON carries the options Lance baked into it (measured on pylance 12.0.0).
    """
    _fragmented_table(real_ns_client)
    source_bytes = 3 * MIB + 7
    response = real_ns_client.post(
        "/management/v1/table/db$t/compaction_plan",
        json={"target_rows_per_fragment": 1024, "batch_size": 37, "num_threads": 3, "max_source_bytes": source_bytes},
    )

    assert response.status_code == 200, response.text
    baked = _baked(response.json()["tasks"])
    found = [(options.get("batch_size"), options.get("num_threads"), options.get("max_source_bytes")) for options in baked]
    assert found == [(37, 3, source_bytes)] * len(baked), baked


def test_max_source_bytes_bounds_how_much_one_plan_takes_on(real_ns_client: TestClient) -> None:
    """Forwarded, and it does what the in-pod rewrite relies on it for: bound one pass in bytes.

    `lance/optimize.py` (pylance 12.0.0): "Tasks are included until adding the next task would exceed
    this limit." Twelve ~1 MiB fragments at a 4000-row target plan three tasks of four fragments
    unbounded, and one task under a 5 MiB bound (measured on pylance 12.0.0).
    """
    assert real_ns_client.post("/v1/namespace/db/create", json={}).status_code == 200
    rows = pa.table({"id": pa.array(range(1000), pa.int64()), "payload": pa.array([os.urandom(1024) for _ in range(1000)], pa.binary())})
    assert real_ns_client.post("/v1/table/db$t/create", content=_arrow_ipc(rows), headers=ARROW_STREAM).status_code == 200
    location = real_ns_client.post("/v1/table/db$t/describe", json={}).json()["location"]
    for _ in range(11):
        more = pa.table({"id": pa.array(range(1000), pa.int64()), "payload": pa.array([os.urandom(1024) for _ in range(1000)], pa.binary())})
        lance.write_dataset(more, location, mode="append")

    def _task_count(max_source_bytes: int) -> int:
        answered = real_ns_client.post(
            "/management/v1/table/db$t/compaction_plan", json={"target_rows_per_fragment": 4000, **BOUNDS, "max_source_bytes": max_source_bytes}
        )
        assert answered.status_code == 200, answered.text
        return len(answered.json()["tasks"])

    assert (_task_count(MAX_SOURCE_BYTES_CEILING), _task_count(5 * MIB)) == (3, 1)


@pytest.mark.parametrize(
    ("mode", "baked_as"),
    [("reencode", "Reencode"), ("try_binary_copy", "TryBinaryCopy"), ("force_binary_copy", "ForceBinaryCopy")],
)
def test_the_repack_mode_is_baked_into_every_task(real_ns_client: TestClient, mode: str, baked_as: str) -> None:
    """`compaction_mode` is a plan-time option like the bounds: `execute` takes only the dataset."""
    _fragmented_table(real_ns_client)
    response = real_ns_client.post("/management/v1/table/db$t/compaction_plan", json={"target_rows_per_fragment": 1024, **BOUNDS, "compaction_mode": mode})

    assert response.status_code == 200, response.text
    assert [options.get("compaction_mode") for options in _baked(response.json()["tasks"])] == [baked_as] * len(response.json()["tasks"])


def test_an_absent_repack_mode_leaves_lances_own_default(real_ns_client: TestClient) -> None:
    """Optional, because Lance defines the default and the in-pod rewrite relies on it.

    `lance/optimize.py` (pylance 12.0.0): `"reencode"` is the default. Unset, the task bakes a null
    mode with binary copy off, which is the same re-encode `compact_files` performs in-pod when
    `MAINTENANCE_REPACK_MODE` is unset.
    """
    _fragmented_table(real_ns_client)
    response = real_ns_client.post("/management/v1/table/db$t/compaction_plan", json={"target_rows_per_fragment": 1024, **BOUNDS})

    assert response.status_code == 200, response.text
    for options in _baked(response.json()["tasks"]):
        assert (options.get("compaction_mode"), options.get("enable_binary_copy"), options.get("enable_binary_copy_force")) == (None, False, False), options


@pytest.mark.parametrize("max_source_bytes", [MAX_SOURCE_BYTES_FLOOR, MAX_SOURCE_BYTES_CEILING])
def test_max_source_bytes_is_accepted_across_its_range(real_ns_client: TestClient, max_source_bytes: int) -> None:
    _fragmented_table(real_ns_client)
    response = real_ns_client.post("/management/v1/table/db$t/compaction_plan", json={**BOUNDS, "max_source_bytes": max_source_bytes})
    assert response.status_code == 200, response.text


@pytest.mark.parametrize(
    "body",
    [
        pytest.param({**BOUNDS, "max_source_bytes": 0}, id="zero-bytes"),
        pytest.param({**BOUNDS, "max_source_bytes": MAX_SOURCE_BYTES_FLOOR - 1}, id="below-the-floor"),
        pytest.param({**BOUNDS, "max_source_bytes": MAX_SOURCE_BYTES_CEILING + 1}, id="above-the-ceiling"),
        pytest.param({**BOUNDS, "compaction_mode": "binary_copy"}, id="a-mode-lance-does-not-define"),
    ],
)
def test_a_bound_or_mode_outside_its_range_is_refused_at_the_wire(real_ns_client: TestClient, body: dict[str, Any]) -> None:
    """The same wire validation the other two bounds get: a value the sweep's settings could not hold."""
    _fragmented_table(real_ns_client)
    response = real_ns_client.post("/management/v1/table/db$t/compaction_plan", json=body)
    assert response.status_code == 422, response.text


_ALL_THREE = ["batch_size", "num_threads", "max_source_bytes"]


@pytest.mark.parametrize(
    ("content", "missing"),
    [
        pytest.param(None, _ALL_THREE, id="no body"),
        pytest.param("null", _ALL_THREE, id="a JSON null body"),
        pytest.param('{"target_rows_per_fragment": 1024}', _ALL_THREE, id="policy but no bounds"),
        pytest.param('{"target_rows_per_fragment": 1024, "batch_size": 64}', ["num_threads", "max_source_bytes"], id="batch_size alone"),
        pytest.param('{"target_rows_per_fragment": 1024, "num_threads": 2}', ["batch_size", "max_source_bytes"], id="num_threads alone"),
        pytest.param('{"target_rows_per_fragment": 1024, "max_source_bytes": 268435456}', ["batch_size", "num_threads"], id="max_source_bytes alone"),
        pytest.param('{"target_rows_per_fragment": 1024, "batch_size": 64, "num_threads": 2}', ["max_source_bytes"], id="max_source_bytes missing"),
    ],
)
def test_a_plan_WITHOUT_the_executors_bounds_is_refused_400_naming_every_bound(real_ns_client: TestClient, content: str | None, missing: list[str]) -> None:
    """The door refuses a plan that would bake Lance's defaults into every task.

    400 with the spec's `InvalidInput` code (13) rather than FastAPI's 422: the request parsed, and
    what is wrong with it is a domain rule `plan_compaction` owns, so the refusal is the same for an
    HTTP caller and an in-process one. The detail names all three bounds so a bring-your-own executor
    learns the whole set it has to state, and lists exactly the ones this request left out. The
    published schema marks the body and the bounds required, and that stays a declaration: the door
    still answers 400, never FastAPI's 422.
    """
    location = _fragmented_table(real_ns_client)
    before = lance.dataset(location).version

    response = real_ns_client.post("/management/v1/table/db$t/compaction_plan", content=content, headers={"content-type": "application/json"})

    assert response.status_code == 400, response.text
    problem = response.json()
    assert problem["code"] == 13, problem
    assert "memory bounds" in problem["detail"], f"refused for another reason than a missing bound: {problem}"
    assert all(bound in problem["detail"] for bound in ("batch_size", "num_threads", "max_source_bytes")), problem
    assert f"(missing: {missing})" in problem["detail"], f"the detail does not list exactly the missing bounds {missing}: {problem['detail']}"
    assert lance.dataset(location).version == before


def test_the_published_contract_marks_the_body_and_every_bound_required(real_ns_client: TestClient) -> None:
    """A client generated from the OpenAPI learns the rule from its types, not from the first 400."""
    spec = cast("FastAPI", real_ns_client.app).openapi()

    operation = spec["paths"]["/management/v1/table/{id}/compaction_plan"]["post"]
    assert operation["requestBody"].get("required") is True, operation["requestBody"]
    body = operation["requestBody"]["content"]["application/json"]["schema"]
    assert body.get("$ref") == "#/components/schemas/CompactionPlanRequest" and "anyOf" not in body, f"the body is published as nullable: {body}"
    schema = spec["components"]["schemas"]["CompactionPlanRequest"]
    assert sorted(schema.get("required", [])) == ["batch_size", "max_source_bytes", "num_threads"], schema.get("required")
    for bound, floor, ceiling in (("batch_size", 1, 8192), ("num_threads", 1, 64), ("max_source_bytes", MAX_SOURCE_BYTES_FLOOR, MAX_SOURCE_BYTES_CEILING)):
        published = schema["properties"][bound]
        assert (published.get("type"), published.get("minimum"), published.get("maximum")) == ("integer", floor, ceiling), published
        assert "anyOf" not in published and "default" not in published, f"{bound} is published as optional or nullable: {published}"
    mode = schema["properties"]["compaction_mode"]
    assert "compaction_mode" not in schema["required"], "Lance defines the mode's default, so the door must not require it"
    assert [alternative.get("enum") for alternative in mode.get("anyOf", []) if alternative.get("type") == "string"] == [
        ["reencode", "try_binary_copy", "force_binary_copy"]
    ], mode


def test_the_commit_door_refuses_an_empty_result_set(real_ns_client: TestClient) -> None:
    _fragmented_table(real_ns_client)
    response = real_ns_client.post("/management/v1/table/db$t/compaction_commit", json={"results": []})
    assert response.status_code == 422, response.text


def test_a_malformed_worker_result_is_a_client_error_not_a_crash(real_ns_client: TestClient) -> None:
    # Results arrive off a queue and are client-controlled. A missing field must be a 4xx.
    _fragmented_table(real_ns_client)
    response = real_ns_client.post("/management/v1/table/db$t/compaction_commit", json={"results": ['{"nope": 1}']})
    assert response.status_code == 400, response.text


def test_neither_door_silently_compacts_MAIN_when_a_BRANCH_is_named(real_ns_client: TestClient) -> None:
    """A branch has its own fragments; planning main and calling it the branch's work is the
    dropped-parameter defect that would compact the wrong dataset with a 200.

    Both requests would SUCCEED against main — a plan main has work for, and results a worker really
    produced from main's plan — so the only thing that can refuse them is the door opening the named
    ref, `work`, which does not exist. Read through a client that returns a server error as a status:
    what is asserted is that the DOOR answered (a domain problem, never the framework's routing miss)
    and that main was not answered or written, not which error the absent ref earns (LH-271).
    """
    location = _fragmented_table(real_ns_client)
    main_plan = real_ns_client.post("/management/v1/table/db$t/compaction_plan", json={"target_rows_per_fragment": 10_000, **BOUNDS})
    assert main_plan.status_code == 200 and main_plan.json()["tasks"], main_plan.text
    task_cls: Any = CompactionTask
    results = [task_cls.from_json(task).execute(lance.dataset(location)).json() for task in main_plan.json()["tasks"]]
    before = lance.dataset(location).version
    client = TestClient(real_ns_client.app, raise_server_exceptions=False)

    for door, payload in (("compaction_plan", {"target_rows_per_fragment": 10_000, **BOUNDS}), ("compaction_commit", {"results": results})):
        response = client.post(f"/management/v1/table/db$t/{door}?branch=work", json=payload)
        assert response.status_code not in (200, 201), f"{door} answered a branch request from main: {response.text[:300]}"
        # A routing miss is titled with the HTTP phrase; a door's own refusal with its domain error.
        assert response.json().get("title") not in _ROUTING_MISSES, f"{door} was never reached, so nothing here tested it: {response.text[:300]}"
    assert lance.dataset(location).version == before, "a request naming a branch committed onto main"


def test_both_doors_land_on_the_maintainer_rung() -> None:
    """These two doors are gated on ``can_maintain``, declared in ``_MAINTENANCE_ACTIONS``.

    A door only a maintainer calls asks for the maintainer rung outright. Unlike ``credentials`` — a
    data read for every other caller, which is why that one keeps ``can_read_data`` as its primary —
    ``compaction_plan``/``compaction_commit`` have no audience but maintenance, and the sweep reaches
    them as ``maintainer from parent`` rather than as a data writer. Measured on the deployed estate
    2026-09-09: at the writer rung the distributed lane was refused 1 818 times and every dataset
    was compacted in-pod (4 274/4 274, the memory ceiling the feature exists to remove); with the
    explicit mapping, 728 units ran ``distributed`` and denials fell to 0.

    A suffix no map declares is refused rather than given a rung, which
    ``services/catalog/tests/test_an_undeclared_door_is_refused.py`` pins for every mounted route.
    """
    from catalog.api.fga_deps import _action_relation

    assert _action_relation("table", "compaction_plan") == "can_maintain"
    assert _action_relation("table", "compaction_commit") == "can_maintain"
