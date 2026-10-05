"""A writer cannot rewrite the provenance a table carries ([[LH-208]]).

Every door below runs at the writer rung, and the Lance Namespace spec treats each as an ordinary
writer operation. Measured on pylance 12.0.0 before the guard: `schema_metadata/update` set or nulled
`lineage.dataset_id` and wrote `rask.*` on both of its routes; a create payload's schema metadata and
its `properties` landed on the table verbatim while lineage emission was off; `drop_columns` and
`alter_columns` dropped, renamed and re-typed the tier's provenance columns and the unenforced primary
key (a re-type strips the key, after which a key-less `merge_insert` refuses the table); and
`nullable=True` on the key, which Lance itself refuses, answered 500.

Driven through the real catalog app on a real `dir` namespace, and each case reads the table back from
disk, because a 400 that still committed the change would be the worst answer of all.
"""

from __future__ import annotations

import io
import json
from typing import Any

import lance
import pyarrow as pa
import pyarrow.ipc as ipc
import pytest
from fastapi.testclient import TestClient


ARROW = {"content-type": "application/vnd.apache.arrow.stream"}
PK = {"lance-schema:unenforced-primary-key": "true"}
DECLARED = {b"lineage.dataset_id": b"m$t"}

#: A governed tier's shape: the key (top level and nested, so ancestors are exercised) and the three
#: provenance columns `service_kit.lakehouse.stage_stamp` names.
SCHEMA = pa.schema(
    [
        pa.field("id", pa.int64(), nullable=False, metadata=PK),
        pa.field("payload", pa.string()),
        pa.field("stage", pa.string()),
        pa.field("lineage", pa.string()),
        pa.field("source_rowid", pa.uint64()),
        pa.field("st", pa.struct([pa.field("k", pa.int64(), nullable=False, metadata=PK)]), nullable=False),
    ]
)


def _ipc(table: pa.Table) -> bytes:
    sink = io.BytesIO()
    with ipc.new_stream(sink, table.schema) as writer:
        writer.write_table(table)
    return sink.getvalue()


def _rows(metadata: dict[bytes, bytes] | None = None) -> pa.Table:
    table = pa.table(
        {"id": [1, 2], "payload": ["a", "b"], "stage": ["silver"] * 2, "lineage": ["{}"] * 2, "source_rowid": [0, 1], "st": [{"k": 1}, {"k": 2}]},
        schema=SCHEMA,
    )
    return table.replace_schema_metadata(metadata) if metadata else table


@pytest.fixture
def tier(real_ns_client: TestClient) -> tuple[TestClient, str]:
    """`m$t` created through the catalog, with the platform's own `lineage.dataset_id` written the way
    the cascade writes it: by pylance, not through a door."""
    assert real_ns_client.post("/v1/namespace/m/create", json={}).status_code == 200
    created = real_ns_client.post("/v1/table/m$t/create?mode=create", content=_ipc(_rows()), headers=ARROW)
    assert created.status_code == 200, created.text
    location = str(created.json()["location"])
    lance.dataset(location).update_schema_metadata({k.decode(): v.decode() for k, v in DECLARED.items()})
    return real_ns_client, location


#: (door, body) for each write a writer must not be able to make. A `create` row writes a NEW table.
CASES: dict[str, tuple[str, Any]] = {
    "set lineage.* on the native route": ("/v1/table/m$t/schema_metadata/update", {"lineage.dataset_id": "victim$payroll"}),
    "null lineage.* on the dataplane route": ("/v1/table/m$t/schema_metadata/update", {"lineage.dataset_id": None}),
    "set rask.* through the spec envelope": (
        "/v1/table/m$t/schema_metadata/update",
        {"id": ["m", "t"], "metadata": {"rask.blob.external_base": "s3://other/"}},
    ),
    "lineage.* in a create payload": ("create", _rows({b"lineage.dataset_id": b"victim$payroll"})),
    "rask.* in a create payload": ("create", _rows({b"rask.blob.external_base": b"s3://other/"})),
    "lineage.* in create properties": ("create?properties=" + json.dumps({"lineage.dataset_id": "victim$payroll"}), _rows()),
    "lineage.* in an insert overwrite payload": ("/v1/table/m$t/insert?mode=overwrite", _rows({b"lineage.dataset_id": b"victim$payroll"})),
    "drop the primary key": ("/v1/table/m$t/drop_columns", {"columns": ["id"]}),
    "drop a struct holding a key field": ("/v1/table/m$t/drop_columns", {"columns": ["st"]}),
    "drop a nested key field": ("/v1/table/m$t/drop_columns", {"columns": ["st.k"]}),
    "drop source_rowid": ("/v1/table/m$t/drop_columns", {"columns": ["source_rowid"]}),
    "drop lineage": ("/v1/table/m$t/drop_columns", {"columns": ["lineage"]}),
    "drop stage": ("/v1/table/m$t/drop_columns", {"columns": ["stage"]}),
    "rename the primary key": ("/v1/table/m$t/alter_columns", {"alterations": [{"path": "id", "rename": "id2"}]}),
    "rename a struct holding a key field": ("/v1/table/m$t/alter_columns", {"alterations": [{"path": "st", "rename": "st2"}]}),
    "rename stage": ("/v1/table/m$t/alter_columns", {"alterations": [{"path": "stage", "rename": "tier"}]}),
    "re-type the primary key to its own type": ("/v1/table/m$t/alter_columns", {"alterations": [{"path": "id", "data_type": {"type": "int64"}}]}),
    "re-type a nested key field": ("/v1/table/m$t/alter_columns", {"alterations": [{"path": "st.k", "data_type": {"type": "int64"}}]}),
    "re-type source_rowid": ("/v1/table/m$t/alter_columns", {"alterations": [{"path": "source_rowid", "data_type": {"type": "int64"}}]}),
    "make the primary key nullable": ("/v1/table/m$t/alter_columns", {"alterations": [{"path": "id", "nullable": True}]}),
}


@pytest.mark.parametrize("case", list(CASES))
def test_a_writer_cannot_rewrite_a_tables_provenance(tier: tuple[TestClient, str], case: str) -> None:
    client, location = tier
    door, body = CASES[case]
    before = lance.dataset(location)

    if door.startswith("create"):
        response = client.post(f"/v1/table/m$u/{door}", content=_ipc(body), headers=ARROW)
    elif isinstance(body, pa.Table):
        response = client.post(door, content=_ipc(body), headers=ARROW)
    else:
        response = client.post(door, json=body)

    assert response.status_code == 400, f"{case}: {response.status_code} {response.text}"
    assert response.json()["code"] == 13, response.json()  # INVALID_INPUT
    after = lance.dataset(location)
    assert after.version == before.version, f"{case}: refused, but the table still moved to v{after.version}"
    assert after.schema.equals(before.schema, check_metadata=True), f"{case}: refused, but the schema changed"
    assert client.post("/v1/table/m$u/exists", json={}).status_code == 404, f"{case}: a refused create still landed a table"


def _ref(location: str, branch: str | None) -> lance.LanceDataset:
    dataset = lance.dataset(location)
    return dataset.checkout_version((branch, None)) if branch else dataset


@pytest.mark.parametrize("loose", [False, True], ids=["exact-types", "cast"])
@pytest.mark.parametrize("branch", [None, "work"])
def test_an_insert_overwrite_keeps_the_tables_provenance(tier: tuple[TestClient, str], branch: str | None, loose: bool) -> None:
    """An overwrite whose payload carries no metadata is a legal write, and it must not erase the stamp
    or the primary key: Lance takes an overwrite's schema from its payload. Both arms of the door, the
    native one (main) and the in-process one (a branch), and both coercions: a payload already in the
    table's types, and one the door casts (a browser sends float64 for every number)."""
    client, location = tier
    query = "mode=overwrite"
    if branch:
        assert client.post("/v1/table/m$t/branches/create", json={"name": branch}).status_code == 200
        query += f"&branch={branch}"
    payload = pa.Table.from_arrays(_rows().columns, schema=pa.schema([f.remove_metadata() for f in SCHEMA]))
    if loose:
        payload = payload.set_column(0, pa.field("id", pa.float64(), nullable=False), pa.array([1.0, 2.0]))
    before = _ref(location, branch).schema

    response = client.post(f"/v1/table/m$t/insert?{query}", content=_ipc(payload), headers=ARROW)

    assert response.status_code == 200, response.text
    after = _ref(location, branch)
    assert after.count_rows() == 2
    assert after.schema.equals(before, check_metadata=True), (
        f"the overwrite rewrote the table's provenance: {after.schema.metadata} {after.schema.field('id').metadata}"
    )


def test_a_stage_output_is_created_from_a_stamped_upstream_schema(real_ns_client: TestClient, tmp_path: Any) -> None:
    """The cascade creates each tier from its UPSTREAM's schema, and every governed upstream carries the
    stage stamp's `lineage.dataset_id`. The create must still succeed, against the real catalog door."""
    from medallion.services.catalog_register import ensure_stage_output

    assert real_ns_client.post("/v1/namespace/m/create", json={}).status_code == 200
    token = tmp_path / "token"
    token.write_text("t")
    upstream = SCHEMA.with_metadata({b"lineage.dataset_id": b"m$bronze"})

    location = ensure_stage_output(catalog_url="http://testserver", table_id="m$silver", schema=upstream, identity_token_file=str(token), client=real_ns_client)

    assert location, "the catalog vended no location"
    assert b"lineage.dataset_id" not in (lance.dataset(location).schema.metadata or {}), "the upstream's stamp landed on the new tier"
