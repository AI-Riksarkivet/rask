"""A caller's SQL fragment with a long AND/OR chain is refused 400 at every door, and the catalog survives it.

Lance's planner recurses once per connective of a flat boolean chain and has no bound: measured on pylance
12.0.0 through the real doors, 150,000 `id = n OR ...` terms end the catalog process with SIGSEGV (exit 139),
and one request is an outage of every request in flight. Lance's own parser bounds NESTING — deep parentheses
answer "recursion limit exceeded" — so the chain is the shape that crashes, not depth in general, and not
size: the same values as one `id IN (...)` list answer normally. Each probe runs in its own process because
the unguarded door takes pytest down with it.
"""

from __future__ import annotations

import io
import json
import os
import subprocess
import sys
from pathlib import Path

import pyarrow as pa
import pytest
from fastapi.testclient import TestClient
from pyarrow import ipc


_OR_CHAIN_TERMS = 150_000
_IN_LIST_VALUES = 200_000

_DRIVER = r"""
import io, json, os, sys
import pyarrow as pa
from pyarrow import ipc

root, door, shape = sys.argv[1], sys.argv[2], sys.argv[3]
os.environ.update({"LANCE_REST_IMPL": "dir", "LANCE_REST_ROOT": root, "LANCE_S3_ACCESS_KEY_ID": "test", "LANCE_S3_SECRET_ACCESS_KEY": "test"})
from fastapi.testclient import TestClient
from lance_namespace import connect
from catalog.api.dependencies import get_namespace, get_storage_options
from catalog.core.config import get_settings
from catalog.main import app

get_settings.cache_clear()
ns = connect("dir", {"root": root})
app.dependency_overrides[get_namespace] = lambda: ns
app.dependency_overrides[get_storage_options] = lambda: {}

def arrow(table):
    sink = io.BytesIO()
    with ipc.new_stream(sink, table.schema) as writer:
        writer.write_table(table)
    return sink.getvalue()

rows = pa.table({"id": pa.array(range(10), pa.int64()), "v": pa.array([str(i) for i in range(10)])})
if shape == "or":
    sql = " OR ".join(f"id = {i}" for i in range(int(sys.argv[4])))
else:
    sql = "id IN (" + ",".join(str(i) for i in range(int(sys.argv[4]))) + ")"
query = {"k": 10, "filter": sql, "vector": {}}
with TestClient(app) as client:
    assert client.post("/v1/namespace/db/create", json={}).status_code == 200
    created = client.post("/v1/table/db$t/create?mode=create", content=arrow(rows), headers={"content-type": "application/vnd.apache.arrow.stream"})
    assert created.status_code == 200, created.text
    if door == "query":
        resp = client.post("/v1/table/db$t/query", json=query)
    elif door == "count_rows":
        resp = client.post("/v1/table/db$t/count_rows", json={"predicate": sql})
    elif door == "explain_plan":
        resp = client.post("/v1/table/db$t/explain_plan", json={"query": query})
    elif door == "analyze_plan":
        resp = client.post("/v1/table/db$t/analyze_plan", json=query)
    elif door == "update":
        resp = client.post("/v1/table/db$t/update", json={"updates": [["v", "'x'"]], "predicate": sql})
    elif door == "delete":
        resp = client.post("/v1/table/db$t/delete", json={"predicate": sql})
    elif door in ("merge_insert", "merge_insert_by_source"):
        filters = {"when_matched_update_all": "true", "when_matched_update_all_filt": sql} if door == "merge_insert" else {"when_not_matched_by_source_delete": "true", "when_not_matched_by_source_delete_filt": sql}
        resp = client.post(
            "/v1/table/db$t/merge_insert",
            params={"on": "id", **filters},
            content=arrow(rows),
            headers={"content-type": "application/vnd.apache.arrow.stream"},
        )
    body = resp.json() if resp.headers.get("content-type", "").endswith("json") else {}
    print(json.dumps({"status": resp.status_code, "code": body.get("code") if isinstance(body, dict) else None, "detail": str(body)[:300]}))
"""

_DOORS = ["query", "count_rows", "explain_plan", "analyze_plan", "update", "delete", "merge_insert"]


def _drive(tmp_path: Path, door: str, shape: str, size: int) -> tuple[int, dict[str, object]]:
    env = {key: value for key, value in os.environ.items() if not key.startswith("LANCE_")}
    done = subprocess.run([sys.executable, "-c", _DRIVER, str(tmp_path), door, shape, str(size)], capture_output=True, text=True, env=env, timeout=600)
    lines = [line for line in done.stdout.splitlines() if line.startswith("{")]
    return done.returncode, (json.loads(lines[-1]) if lines else {"stderr": done.stderr[-600:]})


@pytest.mark.parametrize("door", [door for door in _DOORS if door != "merge_insert"])
def test_a_crash_sized_or_chain_is_refused_and_the_process_survives(tmp_path: Path, door: str) -> None:
    returncode, answer = _drive(tmp_path, door, "or", _OR_CHAIN_TERMS)

    assert returncode == 0, f"{door}: the catalog process died on a {_OR_CHAIN_TERMS}-term OR chain (exit {returncode}): {answer}"
    assert (answer["status"], answer["code"]) == (400, 13), f"{door} must refuse the chain as the caller's input: {answer}"
    assert "conditions with AND/OR" in str(answer["detail"]), f"{door} refused it for another reason: {answer}"


@pytest.mark.parametrize(
    ("door", "field"),
    [("merge_insert", "when_matched_update_all_filt"), ("merge_insert_by_source", "when_not_matched_by_source_delete_filt")],
    ids=["matched-update", "by-source-delete"],
)
def test_merge_insert_refuses_a_chain_one_connective_past_the_bound(tmp_path: Path, door: str, field: str) -> None:
    """Its filters ride the query string, which no server lets reach crash size, so the bound is what is pinned."""
    returncode, answer = _drive(tmp_path, door, "or", 1_002)

    assert (returncode, answer["status"], answer["code"]) == (0, 400, 13), f"{field} must refuse a chain of 1,001 connectives: {answer}"
    assert f"{field} joins 1001 conditions" in str(answer["detail"]), f"{field} was refused for another reason: {answer}"


@pytest.mark.parametrize("door", ["query", "count_rows", "delete"])
def test_the_same_values_as_one_in_list_are_answered(tmp_path: Path, door: str) -> None:
    returncode, answer = _drive(tmp_path, door, "in", _IN_LIST_VALUES)

    assert (returncode, answer["status"]) == (0, 200), f"{door} must answer a {_IN_LIST_VALUES}-value IN list: {answer}"


def test_an_update_EXPRESSION_is_bounded_like_its_predicate(real_ns_client: TestClient) -> None:
    rows = pa.table({"id": pa.array(range(3), pa.int64()), "v": pa.array(["a", "b", "c"])})
    sink = io.BytesIO()
    with ipc.new_stream(sink, rows.schema) as writer:
        writer.write_table(rows)
    assert real_ns_client.post("/v1/namespace/db/create", json={}).status_code == 200
    created = real_ns_client.post("/v1/table/db$t/create?mode=create", content=sink.getvalue(), headers={"content-type": "application/vnd.apache.arrow.stream"})
    assert created.status_code == 200, created.text

    chain = " OR ".join(f"id = {i}" for i in range(1_002))
    resp = real_ns_client.post("/v1/table/db$t/update", json={"updates": [["v", f"CASE WHEN {chain} THEN 'x' ELSE v END"]]})

    assert (resp.status_code, resp.json()["code"]) == (400, 13), resp.text
    assert "updates joins 1001 conditions" in resp.json()["detail"]
