"""Both schema-metadata write paths hide the internal `lineage.*` keys, because one of them did not.

[[LH-019]], the "lineage keys in schema metadata" side effect. `update_table_schema_metadata` takes
two routes: a branch or a null-delete goes through `dataplane.update_schema_metadata`, anything else
goes straight to the native op. The dataplane half filters the internal keys; the native half returned
them verbatim.

MEASURED on pylance 11.0.0 with `lineage.run_id` planted on the table:

    read_schema_metadata   {}                                        filtered
    native op              {'lineage.run_id': 'r-123', 'owner': …}   LEAKED
    dataplane op           {'owner': …}                              filtered

WHY A LEAK HERE IS NOT COSMETIC. `lineage.*` are the coordinates that make the Lance file
self-describing, and `read_schema_metadata` exists precisely to keep them out of the Table Properties
editor. A caller that saw them through the write door would show them as user properties — and the map
a caller holds is the map it saves back, which is the one shape `update_schema_metadata` documents as
unable to destroy them only because it MERGES.

THE ASSERTION IS PARITY, not a literal map: which internal prefixes exist is the dataplane's business,
so pinning the two paths together states the contract — one door, one answer — and cannot drift into
disagreement the way a hard-coded expectation would.
"""

from __future__ import annotations

from pathlib import Path

import pyarrow as pa
import pytest
from lance_namespace import UpdateTableSchemaMetadataRequest, connect

from catalog.services import dataplane, native


lance = pytest.importorskip("lance")

TABLE_ID = ["rows"]
SCHEMA = pa.schema([pa.field("id", pa.int64())])
INTERNAL = "lineage.run_id"


@pytest.fixture
def ns(tmp_path: Path):  # noqa: ANN201 — LanceNamespace is runtime-only
    namespace = connect("dir", {"root": str(tmp_path / "data")})
    dataplane.create_table(namespace, {}, TABLE_ID, pa.table({"id": pa.array([1], pa.int64())}, schema=SCHEMA), mode="create")
    dataplane.update_schema_metadata(namespace, {}, TABLE_ID, {INTERNAL: "r-123"})
    return namespace


def test_the_native_path_hides_the_internal_key_like_the_dataplane_path_does(ns) -> None:  # noqa: ANN001
    """THE DEFECT: one door, two routes, and only one of them was clean."""
    native_answer = dataplane.filter_internal_metadata(
        native.call(ns, "update_table_schema_metadata", UpdateTableSchemaMetadataRequest(id=TABLE_ID, metadata={"owner": "alice"})).metadata or {}
    )
    dataplane_answer = dataplane.update_schema_metadata(ns, {}, TABLE_ID, {"owner": "bob"})

    assert INTERNAL not in native_answer, f"the native route still returns the internal key: {native_answer}"
    assert set(native_answer) == set(dataplane_answer), (
        f"the two routes of one door answer different key sets: native={sorted(native_answer)} dataplane={sorted(dataplane_answer)}"
    )


def test_the_filter_is_load_bearing_and_not_decoration(ns) -> None:
    """Without this, the filter could be a no-op over an upstream that never leaked, and nobody would know."""
    raw = native.call(ns, "update_table_schema_metadata", UpdateTableSchemaMetadataRequest(id=TABLE_ID, metadata={"owner": "carol"})).metadata or {}

    assert INTERNAL in raw, "the native op no longer returns the internal key — re-check whether this filter is still needed"


def test_a_user_property_survives_the_filter(ns) -> None:
    """The control: hiding the internal keys must not hide the caller's own."""
    answer = dataplane.filter_internal_metadata(
        native.call(ns, "update_table_schema_metadata", UpdateTableSchemaMetadataRequest(id=TABLE_ID, metadata={"owner": "dan"})).metadata or {}
    )

    assert answer.get("owner") == "dan"
