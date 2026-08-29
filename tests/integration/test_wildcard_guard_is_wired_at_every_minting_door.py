"""Every door that MINTS a table location refuses an IAM-wildcard segment — at the DOOR (CAT-CORE-02).

`require_safe_segments` had unit coverage of its own logic (`test_wildcard_segments_are_refused.py`)
while nothing drove the routes: deleting the guard call from any single endpoint left every test
green, because coverage sat at the helper, not the doors. Same failure shape as #118, and the same
fix: drive the HTTP surface so removing the call from one endpoint reds exactly one case here.

There are FOUR minting doors, and the fourth had NO guard at all when this file was written:
`rename_table` mints a brand-new table identifier from the caller-supplied `new_table_name` (its own
comment says so), so `POST /v1/table/ns$t/rename {"new_table_name": "foo*"}` byte-copied the dataset
to a wildcard-named location — the exact prefix that flows unescaped into the vended STS session
policy and widens the credential to siblings (`bucket/foo*/*` matches `foobar`).

Each test creates the parent namespace first so the wildcard guard is the ONLY refusal in play —
without it the door genuinely succeeds (200), never hides behind a parent-missing 404.
"""

from __future__ import annotations

import httpx
import pyarrow as pa
import pyarrow.ipc as ipc
from fastapi.testclient import TestClient


ARROW = {"content-type": "application/vnd.apache.arrow.stream"}


def _ipc(table: pa.Table) -> bytes:
    sink = pa.BufferOutputStream()
    with ipc.new_stream(sink, table.schema) as writer:
        writer.write_table(table)
    return sink.getvalue().to_pybytes()


def _make_namespace(client: TestClient, name: str) -> None:
    made = client.post(f"/v1/namespace/{name}/create", json={})
    assert made.status_code in {200, 409}, made.text


def _assert_wildcard_refusal(resp: httpx.Response) -> None:
    """The refusal must be the GUARD's 400, naming the reserved character — not some other door's no."""
    assert resp.status_code == 400, f"expected the wildcard-segment 400, got {resp.status_code}: {resp.text}"
    assert "reserved" in resp.text, f"the 400 does not name the reserved character: {resp.text}"


def test_the_ARROW_CREATE_door_refuses_a_wildcard_segment(real_ns_client: TestClient) -> None:
    _make_namespace(real_ns_client, "wc")
    rows = pa.table({"id": pa.array([1], pa.int64())})

    refused = real_ns_client.post("/v1/table/wc$evil*/create", content=_ipc(rows), headers=ARROW)

    _assert_wildcard_refusal(refused)
    assert real_ns_client.post("/v1/table/wc$evil*/describe", json={}).status_code == 404, "the refused create left a table behind"


def test_the_DECLARE_door_refuses_a_wildcard_segment(real_ns_client: TestClient) -> None:
    _make_namespace(real_ns_client, "wc")

    refused = real_ns_client.post("/v1/table/wc$evil*/declare", json={})

    _assert_wildcard_refusal(refused)


def test_the_NAMESPACE_CREATE_door_refuses_a_wildcard_segment(real_ns_client: TestClient) -> None:
    refused = real_ns_client.post("/v1/namespace/evil*/create", json={})

    _assert_wildcard_refusal(refused)


def test_the_RENAME_door_refuses_a_wildcard_DESTINATION(real_ns_client: TestClient) -> None:
    """THE FOURTH DOOR (re-audit of cb1c6b11). A rename mints a new location from `new_table_name`
    exactly as declare does from `{id}` — unguarded, it relocated real bytes to `wc/evil*` and the
    next credential vend for that table widened to every `evil…` sibling."""
    _make_namespace(real_ns_client, "wc")
    rows = pa.table({"id": pa.array([1, 2], pa.int64())})
    created = real_ns_client.post("/v1/table/wc$src/create", content=_ipc(rows), headers=ARROW)
    assert created.status_code == 200, created.text

    refused = real_ns_client.post("/v1/table/wc$src/rename", json={"new_table_name": "evil*"})

    _assert_wildcard_refusal(refused)
    # A refused rename is a no-op: the source is untouched and no wildcard-located table exists.
    assert real_ns_client.post("/v1/table/wc$src/describe", json={}).status_code == 200, "the refused rename lost the source"
    assert real_ns_client.post("/v1/table/wc$evil*/describe", json={}).status_code == 404, "the refused rename minted the wildcard table anyway"
