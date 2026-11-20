"""An append Lance gives up on because the table is busy is a retryable conflict, not a server fault.

Lance rebases an append over the commits that land before it and tries again; when writers keep landing first it stops
with `OSError("Too many concurrent writers. Attempted N times, but failed on retry_timeout of 30.000 seconds.")`.
Measured on the dev estate 2026-11-16 (pylance 13.0.0, helm rev 27): 32 clients appending to one table through four
catalog replicas, 16 appends ended so and the door answered 500 `unhandled_error`, which tells a client the server
broke and pages an operator for contention that clears by itself. Nothing was committed, so sending the append again
is safe, and the spec's answer for it is `ConcurrentModification` (code 14, 409).

The error is handed to the door as Lance raised it there, since which writer loses a race is not reproducible on demand;
the table and the door are real.
"""

from __future__ import annotations

from pathlib import Path

import lance
import pyarrow as pa
import pytest
from lance_namespace import ConcurrentModificationError, CreateNamespaceRequest, InsertIntoTableRequest, connect
from tables_by_name import open_by_name

from catalog.services.dataplane import create_table, insert_into_table
from lakehouse import base_registry
from lakehouse.location_claims import ClaimStore
from lakehouse.storage.options import StorageOptions
from tests.door_tables import door_table


TABLE_ID = ["wh", "busy"]
#: Lance's own message, as the catalog logged it on the estate.
_GAVE_UP = "Too many concurrent writers. Attempted 6 times, but failed on retry_timeout of 30.000 seconds., /home/runner/work/lance/lance/rust/lance/src/io/commit.rs:128:5"


def _ipc(table: pa.Table) -> bytes:
    sink = pa.BufferOutputStream()
    with pa.ipc.new_stream(sink, table.schema) as writer:
        writer.write_table(table)
    return sink.getvalue().to_pybytes()


def test_an_append_lance_gives_up_on_is_answered_as_a_conflict_to_retry(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    namespace = connect("dir", {"root": str(tmp_path / "data")})
    namespace.create_namespace(CreateNamespaceRequest(id=TABLE_ID[:-1]))
    create_table(
        namespace,
        StorageOptions(),
        TABLE_ID,
        pa.table({"id": pa.array([1], pa.int64())}),
        root=str(tmp_path / "data"),
        mode="create",
        registry=base_registry.BaseRegistry(control_root=str(tmp_path / "control"), storage_options={}),
        holder="$".join(TABLE_ID),
    )
    table = door_table(namespace, ClaimStore(control_root=str(tmp_path / "control")), TABLE_ID, announces=lambda _operation: True)

    def gives_up(*_args: object, **_kwargs: object) -> None:
        raise OSError(_GAVE_UP)

    monkeypatch.setattr(lance.LanceDataset, "insert", gives_up)

    with pytest.raises(ConcurrentModificationError, match="send the write again"):
        insert_into_table(
            table,
            StorageOptions(),
            InsertIntoTableRequest(id=TABLE_ID, mode="append"),
            _ipc(pa.table({"id": pa.array([2], pa.int64())})),
            max_bytes=1 << 20,
            transaction_properties={},
        )

    assert open_by_name(namespace, StorageOptions(), TABLE_ID).to_table().to_pydict() == {"id": [1]}
