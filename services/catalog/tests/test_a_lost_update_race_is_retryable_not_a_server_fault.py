"""The UPDATE and DELETE doors answer the rows they touched and advance the table's version, and one that loses its race
plans again.

Both commit stamped ([[LH-497]], `dataplane.commit_stamped`): Lance's own operation, planned on the latest version and
finalized as exactly the next one, so the update's row count is the rows its predicate matched on that version. A
writer that commits between the door's open and Lance's staging leaves the plan stale, and the door plans again on the
version that won ([[LH-542]]): measured on pylance 13.0.0, Lance's own delete exhausts its retries against the planned
version, which the door then reads as a race it lost.
"""

from __future__ import annotations

from pathlib import Path

import pyarrow as pa
import pytest
from lance_namespace import CreateNamespaceRequest, DeleteFromTableRequest, UpdateTableRequest, connect

from catalog.services import table_claims
from catalog.services.dataplane import captured, commit_stamped, create_table, delete_from_table, update_table
from catalog.services.table_claims import DoorTable
from service_kit.lakehouse import base_registry
from service_kit.lakehouse.location_claims import ClaimStore


TABLE_ID = ["db", "rows"]
#: The stamp a door commits with ([[LH-497]]).
STAMP = {"rask.author": "alice", "rask.operation": "update"}
SCHEMA = pa.schema([pa.field("id", pa.int64()), pa.field("v", pa.int64())])


@pytest.fixture
def ns(tmp_path: Path):  # noqa: ANN201 — LanceNamespace is runtime-only
    namespace = connect("dir", {"root": str(tmp_path / "data")})
    namespace.create_namespace(CreateNamespaceRequest(id=TABLE_ID[:-1]))
    create_table(
        namespace,
        {},
        TABLE_ID,
        pa.table({"id": pa.array([1, 2, 3], pa.int64()), "v": pa.array([0, 0, 0], pa.int64())}, schema=SCHEMA),
        root=str(tmp_path / "data"),
        mode="create",
        registry=base_registry.BaseRegistry(control_root=str(tmp_path / "control"), storage_options={}),
        holder="$".join(TABLE_ID),
    )
    return namespace


@pytest.fixture
def table(ns, tmp_path: Path) -> DoorTable:  # noqa: ANN001
    """The table as a door acts on it, resolved on ``ns``."""
    return table_claims.door_table(ns, ClaimStore(control_root=str(tmp_path / "control")), TABLE_ID)


def test_the_doors_still_answer_and_advance_the_version(table: DoorTable) -> None:
    updated = update_table(table, {}, UpdateTableRequest(id=TABLE_ID, updates=[["v", "7"]]), transaction_properties=STAMP)
    assert updated.updated_rows == 3, f"update reported {updated.updated_rows} rows, not 3"
    deleted = delete_from_table(table, {}, DeleteFromTableRequest(id=TABLE_ID, predicate="id = 1"), transaction_properties=STAMP)
    # `version` is Optional on the response model; a door that answers None reported no version at all,
    # which is the same defect as reporting a stale one and is asserted rather than narrowed away.
    assert deleted.version is not None, "delete answered no version"
    assert deleted.version > updated.version, "delete did not advance the table version"


def test_a_delete_planned_on_a_version_another_writer_passed_plans_again_and_lands(table: DoorTable) -> None:
    opened = table.open({})
    update_table(table, {}, UpdateTableRequest(id=TABLE_ID, updates=[["v", "7"]]), transaction_properties=STAMP)

    version, _ = commit_stamped(
        table, {}, {**STAMP, "rask.operation": "delete"}, captured(table, {}, lambda handle: handle.delete("id = 1"), branch=None), opened=opened
    )

    landed = table.open({}).to_table()
    assert (version, sorted(landed.column("id").to_pylist()), set(landed.column("v").to_pylist())) == (3, [2, 3], {7})
