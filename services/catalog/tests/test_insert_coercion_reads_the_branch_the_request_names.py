"""The insert pre-coercion aligns to the ref the request NAMES, never to main.

[[LH-019]], the "insert pre-coercion" side effect — found by re-measuring the row's own clause 2 rather
than by working it. That clause asks to reconcile `coerce_insert_arrow`'s `InvalidInputError` (13) with
`_write_schema_errors`' `TableSchemaValidationError` (20) as "the same condition answered two ways".
**IT IS NOT THE SAME CONDITION.** `coerce_insert_arrow` runs at the DOOR (`data.py:294`), before
`insert_into_table` splits on `branch`, so 13 is what BOTH arms answer for a payload that cannot be
aligned at all. 20 is what the branch arm answers for a mismatch that SURVIVES coercion and reaches
pylance. Changing either code would make two different conditions indistinguishable.

WHAT THE RE-MEASUREMENT ACTUALLY FOUND is worse than a code, and it is this file's subject: the
coercion opens the table with NO branch. `branch` is in scope at `data.py:285` and is passed to
`InsertIntoTableRequest` on the very next line, but not to `coerce_insert_arrow`, which does
`open_dataset(ns, so, table_id)` and aligns to MAIN's schema.

THAT IS SILENT DATA LOSS, not a wrong status, because of one documented behaviour: the coercion
"select[s] the table's columns BY NAME (extra columns dropped)". A branch whose schema has evolved has
columns main does not. An insert naming them, against that branch, has them dropped by an alignment to
the wrong schema — and then succeeds. The caller is told nothing; the rows land without the columns.

IT IS THE FAMILY THIS CODEBASE KEEPS REPRODUCING. `test_branch_scoped_mutations_hit_the_branch.py`
records `update` and `delete` rewriting MAIN; `test_a_declared_branch_is_never_silently_dropped.py`
gates every DOOR that accepts a branch. That gate could not see this one: `coerce_insert_arrow` is a
helper, not a door building a branched request model, so it sits in the blind spot between the two.
"""

from __future__ import annotations

from pathlib import Path

import pyarrow as pa
import pytest
from lance_namespace import InsertIntoTableRequest, connect

from catalog.services.dataplane import coerce_insert_arrow, create_table, insert_into_table, open_dataset


lance = pytest.importorskip("lance")


TABLE_ID = ["rows"]
MAIN_SCHEMA = pa.schema([pa.field("id", pa.int64()), pa.field("s", pa.string())])
BRANCH = "work"

#: The column that exists ONLY on the branch. The whole defect is whether a payload naming it survives.
BRANCH_ONLY = "note"


def _ipc(table: pa.Table) -> bytes:
    sink = pa.BufferOutputStream()
    with pa.ipc.new_stream(sink, table.schema) as writer:
        writer.write_table(table)
    return sink.getvalue().to_pybytes()


@pytest.fixture
def ns(tmp_path: Path):  # noqa: ANN201 — LanceNamespace is runtime-only
    """A table on main, plus a branch whose schema has EVOLVED past main's.

    The evolution is the precondition, not incidental: with identical schemas the coercion's target is
    the same either way and nothing can be observed. This is the shape a branch exists for — staging a
    change without touching main.
    """
    namespace = connect("dir", {"root": str(tmp_path / "data")})
    create_table(namespace, {}, TABLE_ID, pa.table({"id": pa.array([1], pa.int64()), "s": pa.array(["a"])}, schema=MAIN_SCHEMA), mode="create")
    dataset = open_dataset(namespace, {}, TABLE_ID)
    dataset.create_branch(BRANCH, None)
    branch_dataset = open_dataset(namespace, {}, TABLE_ID, branch=BRANCH)
    branch_dataset.add_columns({BRANCH_ONLY: "cast(null as string)"})
    return namespace


def test_the_branch_really_has_a_column_main_does_not(ns) -> None:  # noqa: ANN001
    """Without this the test below could pass by measuring two identical schemas."""
    assert BRANCH_ONLY not in open_dataset(ns, {}, TABLE_ID).schema.names
    assert BRANCH_ONLY in open_dataset(ns, {}, TABLE_ID, branch=BRANCH).schema.names


def test_coercion_against_a_BRANCH_keeps_the_branch_only_column(ns) -> None:  # noqa: ANN001
    """THE DEFECT. Aligning to main drops a column the branch has, and nothing says so.

    Asserted on the COERCED BYTES rather than on the final row count, because that is where the loss
    happens: by the time the rows are written the column is already gone and the insert is a truthful
    report of a payload that was quietly rewritten.
    """
    payload = pa.table({"id": pa.array([2], pa.int64()), "s": pa.array(["b"]), BRANCH_ONLY: pa.array(["keep me"])})

    coerced = coerce_insert_arrow(ns, {}, TABLE_ID, _ipc(payload), branch=BRANCH)

    names = pa.ipc.open_stream(coerced).read_all().column_names
    assert BRANCH_ONLY in names, (
        f"the coercion dropped {BRANCH_ONLY!r}, a column the branch HAS, because it aligned to main's schema "
        f"(kept: {names}). The rows then land on the branch without it and the caller is told nothing."
    )


def test_a_branch_insert_lands_the_branch_only_column(ns) -> None:  # noqa: ANN001
    """The same defect seen end to end, so the fix is not a coercion-shaped local truth."""
    payload = pa.table({"id": pa.array([2], pa.int64()), "s": pa.array(["b"]), BRANCH_ONLY: pa.array(["keep me"])})
    data = coerce_insert_arrow(ns, {}, TABLE_ID, _ipc(payload), branch=BRANCH)

    insert_into_table(ns, {}, InsertIntoTableRequest(id=TABLE_ID, mode="append", branch=BRANCH), data)

    written = open_dataset(ns, {}, TABLE_ID, branch=BRANCH).to_table().to_pydict()
    assert "keep me" in (written.get(BRANCH_ONLY) or []), f"the branch row lost its {BRANCH_ONLY!r} value: {written}"


def test_main_is_unchanged_by_the_fix(ns) -> None:  # noqa: ANN001
    """The behaviour the coercion exists for must survive: an extra column against MAIN is still dropped.

    Pinned because the obvious over-fix — stop dropping extra columns — would turn a browser's loose
    payload back into the 500 this function was written to prevent.
    """
    payload = pa.table({"id": pa.array([3], pa.int64()), "s": pa.array(["c"]), "zz": pa.array([1], pa.int64())})

    coerced = coerce_insert_arrow(ns, {}, TABLE_ID, _ipc(payload), branch=None)

    assert "zz" not in pa.ipc.open_stream(coerced).read_all().column_names
