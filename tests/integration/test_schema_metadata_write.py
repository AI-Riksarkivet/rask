"""``POST /v1/table/{id}/schema_metadata/update`` — the WRITE half of table properties (#78).

Driven through the REAL ``dir`` backend and real pylance writes, because the whole question here is what
Lance ACTUALLY stores. A mocked namespace can only echo the map we handed it, and every defect these tests
pin was invisible to exactly that: the op **merges** while the spec text says "Replace", so omitting a key
never removed it, and a ``null`` value was stringified into the literal ``"None"`` on disk.

The contract, in one line: **upsert, with ``null`` meaning DELETE this key** — the table-level twin of
``update_field_metadata``'s dialect, and the only shape that can express removal without a ``replace=True``
that would drop the #21 ``lineage.*`` coordinates the caller never sees.
"""

from __future__ import annotations

import io

import lance
import pyarrow as pa
import pyarrow.ipc as ipc
from fastapi.testclient import TestClient

from catalog.core.lineage_metadata import build_lineage_metadata, inject_into_arrow_stream


ARROW = {"content-type": "application/vnd.apache.arrow.stream"}


def _ipc(table: pa.Table) -> bytes:
    sink = io.BytesIO()
    with ipc.new_stream(sink, table.schema) as writer:
        writer.write_table(table)
    return sink.getvalue()


def _create(client: TestClient, table: str, *, stamp_lineage: bool = False) -> None:
    """Create ``m1$<table>`` through the catalog's own Arrow create path."""
    client.post("/v1/namespace/m1/create", json={})
    rows = pa.table({"id": pa.array([1, 2], pa.int64())})
    data = _ipc(rows)
    if stamp_lineage:
        # The SAME helper the create endpoint calls when lineage emission is on (it is off by default in
        # this fixture, and turning it on would demand a live lineage service). Stamping it here keeps the
        # keys real rather than hand-written, so the assertion is about the delete, not about the stamp.
        data = inject_into_arrow_stream(data, build_lineage_metadata(table_id=f"m1${table}", namespace="m1", run_id="run-1"))
    created = client.post(f"/v1/table/m1${table}/create?mode=overwrite", content=data, headers=ARROW)
    assert created.status_code == 200, created.text


def _properties(client: TestClient, table: str) -> dict[str, str]:
    """The table's user-visible properties, read back the way the console reads them."""
    resp = client.post(f"/v1/table/m1${table}/describe?load_detailed_metadata=true", json={})
    assert resp.status_code == 200, resp.text
    return resp.json().get("metadata") or {}


def _location(client: TestClient, table: str) -> str:
    resp = client.post(f"/v1/table/m1${table}/describe", json={})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    return str(body.get("table_uri") or body["location"])


def test_null_value_deletes_the_key(real_ns_client: TestClient) -> None:
    # THE gap: the properties editor's remove button omitted the key and reported "Saved." — and the key
    # stayed. Deletion has to be something a caller can SAY, which is what the null carries.
    _create(real_ns_client, "t1")
    seeded = real_ns_client.post("/v1/table/m1$t1/schema_metadata/update", json={"owner": "alice", "tier": "gold"})
    assert seeded.status_code == 200, seeded.text
    assert _properties(real_ns_client, "t1") == {"owner": "alice", "tier": "gold"}

    resp = real_ns_client.post("/v1/table/m1$t1/schema_metadata/update", json={"tier": None})
    assert resp.status_code == 200, resp.text
    assert _properties(real_ns_client, "t1") == {"owner": "alice"}
    # The response echoes the table's NEW full map, so a caller never has to re-read to know what stuck.
    assert resp.json() == {"owner": "alice"}  # the DIRECT map (A2): the body IS the updated metadata, not an envelope


def test_omitting_a_key_is_a_merge_not_a_replace(real_ns_client: TestClient) -> None:
    # The spec's own wording ("Replace schema metadata") is wrong about the backend, and three source
    # comments repeated it. Pin the real semantic so the next reader can't be misled by the spec text.
    _create(real_ns_client, "t2")
    real_ns_client.post("/v1/table/m1$t2/schema_metadata/update", json={"owner": "alice", "tier": "gold"})

    resp = real_ns_client.post("/v1/table/m1$t2/schema_metadata/update", json={"owner": "bob"})
    assert resp.status_code == 200, resp.text
    assert _properties(real_ns_client, "t2") == {"owner": "bob", "tier": "gold"}


def test_delete_does_not_drop_lineage_coordinates(real_ns_client: TestClient) -> None:
    # The map a caller edits comes from read_schema_metadata, which EXCLUDES lineage.* — so any write path
    # that replaces rather than merges silently destroys the #21 self-describing coordinates. Assert on the
    # FILE, not on the API's (filtered) view, because that is where the loss would happen.
    _create(real_ns_client, "t3", stamp_lineage=True)
    real_ns_client.post("/v1/table/m1$t3/schema_metadata/update", json={"owner": "alice"})

    resp = real_ns_client.post("/v1/table/m1$t3/schema_metadata/update", json={"owner": None})
    assert resp.status_code == 200, resp.text
    assert _properties(real_ns_client, "t3") == {}
    assert resp.json() == {}  # the echo is filtered the same way the read twin is; direct map since A2

    on_disk = lance.dataset(_location(real_ns_client, "t3")).schema.metadata or {}
    assert on_disk.get(b"lineage.dataset_id") == b"m1$t3", on_disk
    assert on_disk.get(b"lineage.create_run_id") == b"run-1", on_disk


def test_explicit_null_is_not_written_as_the_string_None(real_ns_client: TestClient) -> None:
    # `str(None) == "None"`: the endpoint stringified every value, so `{"description": null}` from a client
    # landed on disk as the four characters None — a property nobody asked for, on the key that matters most.
    _create(real_ns_client, "t4")

    resp = real_ns_client.post("/v1/table/m1$t4/schema_metadata/update", json={"description": None})
    assert resp.status_code == 200, resp.text
    assert "description" not in _properties(real_ns_client, "t4")
