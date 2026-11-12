"""An insert carrying a column the table does not have is refused, naming the column, as a missing column is.

`coerce_insert_arrow` selects the table's columns by name and casts each to the table's type, which keeps a browser
client working (apache-arrow infers `float64` for every JS number). A column the table lacks has nowhere to go, and
Lance's own append refuses one ("Append with different schema: fields did not match, unexpected=[extra]", measured on
pylance 13.0.0), so the door refuses it too ([[LH-576]] E10f) rather than discard values a caller believes were stored.
Code 13: the payload cannot be aligned at the door, which `test_insert_coercion_reads_the_branch_the_request_names.py`
keeps distinct from code 20, a payload that aligns and that pylance refuses.
"""

from __future__ import annotations

from pathlib import Path

import pyarrow as pa
import pytest
from lance_namespace import CreateNamespaceRequest, InvalidInputError, connect

from catalog.services import table_claims
from catalog.services.dataplane import coerce_insert_arrow, create_table
from catalog.services.table_claims import DoorTable
from lakehouse.location_claims import ClaimStore
from lakehouse.storage_options import StorageOptions


#: The catalog's default body cap (LANCE_MAX_BODY_BYTES), far above every body here.
_BODY_LIMIT = 256 * 1024 * 1024


lance = pytest.importorskip("lance")

TABLE_ID = ["db", "rows"]
SCHEMA = pa.schema([pa.field("id", pa.int64()), pa.field("s", pa.string())])


def _ipc(table: pa.Table) -> bytes:
    sink = pa.BufferOutputStream()
    with pa.ipc.new_stream(sink, table.schema) as writer:
        writer.write_table(table)
    return sink.getvalue().to_pybytes()


@pytest.fixture
def ns(tmp_path: Path):  # noqa: ANN201 — LanceNamespace is runtime-only
    namespace = connect("dir", {"root": str(tmp_path / "data")})
    namespace.create_namespace(CreateNamespaceRequest(id=TABLE_ID[:-1]))
    create_table(
        namespace,
        StorageOptions(),
        TABLE_ID,
        pa.table({"id": pa.array([1], pa.int64()), "s": pa.array(["a"])}, schema=SCHEMA),
        root=str(tmp_path / "data"),
        mode="create",
        registry=None,
    )
    return namespace


@pytest.fixture
def table(ns, tmp_path: Path) -> DoorTable:  # noqa: ANN001
    """The table as a door acts on it, resolved on ``ns``."""
    return table_claims.door_table(ns, ClaimStore(control_root=str(tmp_path / "control")), TABLE_ID, announces=lambda _operation: True)


def test_a_column_the_table_lacks_is_refused_by_name(table: DoorTable) -> None:
    payload = pa.table({"id": pa.array([2], pa.int64()), "s": pa.array(["b"]), "OOPS": pa.array(["gone"])})

    with pytest.raises(InvalidInputError, match="column\\(s\\) the table does not have: OOPS"):
        coerce_insert_arrow(table, StorageOptions(), _ipc(payload), max_bytes=_BODY_LIMIT)
