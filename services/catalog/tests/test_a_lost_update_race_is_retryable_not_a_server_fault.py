"""The UPDATE and DELETE doors answer the rows they touched and advance the table's version.

They carry no conflict classification, unlike the column ops (`test_a_lost_commit_race_is_retryable_not_a_server_fault.py`),
because no input reaches one: `update` and `delete` are Rewrite transactions that Lance retries against the newer
version, so a writer holding a stale handle rebases and commits rather than losing. Measured 2026-09-08 with two handles
opened at version 1, one committing first: the stale update committed version 3 and the stale delete version 4, with no
conflict raised.
"""

from __future__ import annotations

from pathlib import Path

import pyarrow as pa
import pytest
from lance_namespace import CreateNamespaceRequest, DeleteFromTableRequest, UpdateTableRequest, connect

from catalog.services import table_claims
from catalog.services.dataplane import create_table, delete_from_table, update_table
from catalog.services.table_claims import DoorTable
from service_kit.lakehouse.location_claims import ClaimStore


TABLE_ID = ["db", "rows"]
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
        registry=None,
    )
    return namespace


@pytest.fixture
def table(ns, tmp_path: Path) -> DoorTable:  # noqa: ANN001
    """The table as a door acts on it, resolved on ``ns``."""
    return table_claims.door_table(ns, ClaimStore(control_root=str(tmp_path / "control")), TABLE_ID)


def test_the_doors_still_answer_and_advance_the_version(table: DoorTable) -> None:
    updated = update_table(table, {}, UpdateTableRequest(id=TABLE_ID, updates=[["v", "7"]]))
    assert updated.updated_rows == 3, f"update reported {updated.updated_rows} rows, not 3"
    deleted = delete_from_table(table, {}, DeleteFromTableRequest(id=TABLE_ID, predicate="id = 1"))
    # `version` is Optional on the response model; a door that answers None reported no version at all,
    # which is the same defect as reporting a stale one and is asserted rather than narrowed away.
    assert deleted.version is not None, "delete answered no version"
    assert deleted.version > updated.version, "delete did not advance the table version"
