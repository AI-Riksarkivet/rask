"""The schema-metadata write answers the table's map without the internal `lineage.*` keys ([[LH-019]]).

Every body the door takes is written through `dataplane.update_schema_metadata`, on the handle opened at the door's
location ([[LH-530]]), and pylance's `update_schema_metadata` answers the whole map, internal keys included (measured
on pylance 11.0.0 with `lineage.run_id` planted on the table: the native op answered `{'lineage.run_id': 'r-123',
'owner': …}`).

WHY A LEAK HERE IS NOT COSMETIC. `lineage.*` are the coordinates that make the Lance file self-describing, and the
read twin exists precisely to keep them out of the Table Properties editor. A caller that saw them through the write
door would show them as user properties — and the map a caller holds is the map it saves back, which is the one shape
`update_schema_metadata` documents as unable to destroy them only because it MERGES.
"""

from __future__ import annotations

from pathlib import Path

import pyarrow as pa
import pytest
from lance_namespace import CreateNamespaceRequest, connect

from catalog.services import dataplane, table_claims
from catalog.services.table_claims import DoorTable
from service_kit.lakehouse.location_claims import ClaimStore


lance = pytest.importorskip("lance")

TABLE_ID = ["db", "rows"]
#: The stamp a door commits with ([[LH-497]]).
STAMP = {"rask.author": "alice", "rask.operation": "update_schema_metadata"}
SCHEMA = pa.schema([pa.field("id", pa.int64())])
INTERNAL = "lineage.run_id"


@pytest.fixture
def ns(tmp_path: Path):  # noqa: ANN201 — LanceNamespace is runtime-only
    namespace = connect("dir", {"root": str(tmp_path / "data")})
    namespace.create_namespace(CreateNamespaceRequest(id=TABLE_ID[:-1]))
    dataplane.create_table(
        namespace, {}, TABLE_ID, pa.table({"id": pa.array([1], pa.int64())}, schema=SCHEMA), root=str(tmp_path / "data"), mode="create", registry=None
    )
    dataplane.update_schema_metadata(
        table_claims.door_table(namespace, ClaimStore(control_root=str(tmp_path / "control")), TABLE_ID), {}, {INTERNAL: "r-123"}, transaction_properties=STAMP
    )
    return namespace


@pytest.fixture
def table(ns, tmp_path: Path) -> DoorTable:  # noqa: ANN001
    """The table as a door acts on it, resolved on ``ns``."""
    return table_claims.door_table(ns, ClaimStore(control_root=str(tmp_path / "control")), TABLE_ID)


def test_the_write_answers_the_map_without_the_internal_key(table: DoorTable) -> None:
    answer, _ = dataplane.update_schema_metadata(table, {}, {"owner": "bob"}, transaction_properties=STAMP)

    assert answer == {"owner": "bob"}, f"the write answered the internal key, or lost the caller's: {answer}"
