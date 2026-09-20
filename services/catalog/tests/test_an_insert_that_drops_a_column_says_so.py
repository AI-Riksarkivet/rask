"""An insert that discards a column the table does not have must SAY so.

[[LH-019]], the "insert pre-coercion" side effect. `coerce_insert_arrow` selects the table's columns
by name, so a payload carrying a column the table lacks is accepted, the column is discarded, and the
caller gets a success. The client has no way to learn its data went nowhere.

DROPPING IS NOT THE DEFECT — SILENCE IS, and that is why this fixes the report rather than the
contract. The table has no such column, so there is nowhere to put the values: the only choices are
refuse or discard. Discarding keeps a browser client working (the reason the coercion exists at all:
apache-arrow infers `float64` for every JS number, which would otherwise hit the native append as a
bare 500), and flipping a live door from accept to refuse is a contract change, not a bug fix. What
costs nothing is making the discard auditable.

Note the sibling three lines up: a MISSING column already raises. So the function had opposite
answers for the two halves of one schema mismatch, and only one of them was visible.

Deliberately not asserting the status code. Which code a door owes for a schema mismatch is settled
and load-bearing — `test_insert_coercion_reads_the_branch_the_request_names.py` records why 13 (cannot
align at the door) and 20 (survives coercion, refused by pylance) must stay distinguishable.
"""

from __future__ import annotations

import logging
from pathlib import Path

import pyarrow as pa
import pytest
from lance_namespace import connect

from catalog.services.dataplane import coerce_insert_arrow, create_table


lance = pytest.importorskip("lance")

TABLE_ID = ["rows"]
SCHEMA = pa.schema([pa.field("id", pa.int64()), pa.field("s", pa.string())])


def _ipc(table: pa.Table) -> bytes:
    sink = pa.BufferOutputStream()
    with pa.ipc.new_stream(sink, table.schema) as writer:
        writer.write_table(table)
    return sink.getvalue().to_pybytes()


@pytest.fixture
def ns(tmp_path: Path):  # noqa: ANN201 — LanceNamespace is runtime-only
    namespace = connect("dir", {"root": str(tmp_path / "data")})
    create_table(namespace, {}, TABLE_ID, _ipc(pa.table({"id": pa.array([1], pa.int64()), "s": pa.array(["a"])}, schema=SCHEMA)), mode="create")
    return namespace


def test_a_dropped_column_is_reported_by_name(ns, caplog: pytest.LogCaptureFixture) -> None:  # noqa: ANN001
    payload = pa.table({"id": pa.array([2], pa.int64()), "s": pa.array(["b"]), "OOPS": pa.array(["gone"])})

    with caplog.at_level(logging.WARNING):
        coerce_insert_arrow(ns, {}, TABLE_ID, _ipc(payload))

    assert any("OOPS" in record.getMessage() or "OOPS" in str(getattr(record, "columns", "")) for record in caplog.records), (
        "the column was discarded and nothing said so, so a caller cannot tell a stored value from a lost one"
    )


def test_an_aligned_insert_reports_nothing(ns, caplog: pytest.LogCaptureFixture) -> None:
    """The control: a payload that drops nothing must not produce a warning anyone learns to ignore."""
    payload = pa.table({"id": pa.array([3], pa.int64()), "s": pa.array(["c"])}, schema=SCHEMA)

    with caplog.at_level(logging.WARNING):
        coerce_insert_arrow(ns, {}, TABLE_ID, _ipc(payload))

    assert not caplog.records, f"an exactly-aligned insert warned anyway: {[r.getMessage() for r in caplog.records]}"
