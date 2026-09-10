"""`insert_into_table` must report the version it minted and the rows it wrote — on EITHER path.

§ A12. `InsertIntoTableResponse` declares `version` and `num_inserted_rows`, and the branch path fills
both while the branchless path delegates to the native backend, which answers `{}` — so a spec client
gets `None` for both on the ordinary path and real numbers only if it happens to stage on a branch.
Measured against the installed native backend on pylance 11.0.0: the branchless call returns an empty
dict.

THE ASSERTION IS PARITY, following the sibling gate on this same door
(`test_a_branch_write_answers_the_same_code_as_main.py`): what a caller is owed was decided by the
path that already answers, so pinning the two together states the contract without hard-coding a
number that Lance's own versioning could legitimately revise.

The row deferred this on cost — "an extra open_dataset plus two count_rows on EVERY main-path insert".
That objection is falsified at HEAD: the endpoint already reopens the dataset for its lineage trailer,
and the row count needs no I/O at all because the payload is already decoded to align it to the
schema.
"""

from __future__ import annotations

from pathlib import Path

import pyarrow as pa
import pytest
from lance_namespace import InsertIntoTableRequest, connect

from catalog.services.dataplane import create_table, insert_into_table, open_dataset


lance = pytest.importorskip("lance")

TABLE_ID = ["rows"]
SCHEMA = pa.schema([pa.field("id", pa.int64()), pa.field("s", pa.string())])
BRANCH = "work"


def _ipc(table: pa.Table) -> bytes:
    sink = pa.BufferOutputStream()
    with pa.ipc.new_stream(sink, table.schema) as writer:
        writer.write_table(table)
    return sink.getvalue().to_pybytes()


def _rows(start: int) -> pa.Table:
    return pa.table({"id": pa.array([start, start + 1], pa.int64()), "s": pa.array(["x", "y"])}, schema=SCHEMA)


@pytest.fixture
def ns(tmp_path: Path):  # noqa: ANN201 — LanceNamespace is runtime-only
    namespace = connect("dir", {"root": str(tmp_path / "data")})
    create_table(namespace, {}, TABLE_ID, _ipc(_rows(1)), mode="create")
    open_dataset(namespace, {}, TABLE_ID).create_branch(BRANCH, None)
    return namespace


def test_both_paths_report_the_same_shape_for_the_same_payload(ns) -> None:  # noqa: ANN001
    """Parity, and it subsumes the single-path case rather than sitting beside one.

    A separate "the main path reports its rows" test asserted a strict subset of this — same call, same
    field, one fewer comparison — so it could only ever fail when this one already had. The messages
    below carry which side was wrong, which is the only thing the extra test bought.
    """
    main = insert_into_table(ns, {}, InsertIntoTableRequest(id=TABLE_ID, mode="append"), _ipc(_rows(20)))
    branched = insert_into_table(ns, {}, InsertIntoTableRequest(id=TABLE_ID, mode="append", branch=BRANCH), _ipc(_rows(30)))

    assert main.num_inserted_rows == 2, f"the MAIN path did not report the rows it wrote: {main}"
    assert branched.num_inserted_rows == 2, f"the BRANCH path did not report the rows it wrote: {branched}"
    assert main.version is not None, "the main path reported no version for a write that minted one"
    assert branched.version is not None
