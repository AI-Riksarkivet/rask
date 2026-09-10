"""A write that fails must answer the same spec code whether or not it named a branch.

the lakehouse register, row A5 (drained 2026-09-10; in git history), the "column/data ops never mint 20" half.

THE DEFECT IS AN ASYMMETRY, not a missing feature, and that is what makes it worth a test of this
shape. `insert_into_table` and `merge_insert_into_table` split on `branch`: without one they delegate
to the native backend, which maps a schema mismatch to `TableSchemaValidationError` (20 -> 400); with
one they run pylance in-process, where the identical mismatch escaped as a bare `OSError` and was
reported `Internal 18` (500). Measured 2026-09-07 across three payload shapes — a wrong Arrow type, an
extra column, a wholly unrelated schema — main answered 20 for all three and the branch answered 500
for all three.

A second one sat beside it: `dataset.merge_insert(on)` is where Lance rejects a key column that does
not exist, and it was constructed OUTSIDE `_user_sql`'s guard — so the one door whose entire job is
matching on that column reported `Internal 18` for naming it wrongly, while the branchless path
answered 13.

THE ASSERTION IS PARITY, DELIBERATELY, rather than a hard-coded code per case. Which code a schema
failure deserves was already decided by the branchless door, so pinning the two together states the
real contract — a caller must not get a different answer for the same mistake because they staged it
on a branch — and it cannot drift into disagreement with the native backend the way a literal `20`
would if upstream ever revised it.

This estate has shipped the branch-path-is-worse bug before: `test_branch_scoped_mutations_hit_the_branch.py`
records `update` and `delete` silently rewriting MAIN. Same door family, same asymmetry, caught late
because nothing compared the two paths.
"""

from __future__ import annotations

from pathlib import Path

import pyarrow as pa
import pytest
from lance_namespace import MergeInsertIntoTableRequest, connect

from catalog.services.dataplane import create_table, merge_insert_into_table, open_dataset


lance = pytest.importorskip("lance")


TABLE_ID = ["rows"]
SCHEMA = pa.schema([pa.field("id", pa.int64()), pa.field("s", pa.string())])
BRANCH = "work"


def _ipc(table: pa.Table) -> bytes:
    sink = pa.BufferOutputStream()
    with pa.ipc.new_stream(sink, table.schema) as writer:
        writer.write_table(table)
    return sink.getvalue().to_pybytes()


def _base() -> pa.Table:
    return pa.table({"id": pa.array([1, 2, 3], pa.int64()), "s": pa.array(["a", "b", "c"])}, schema=SCHEMA)


@pytest.fixture
def ns(tmp_path: Path):  # noqa: ANN201 — LanceNamespace is runtime-only
    namespace = connect("dir", {"root": str(tmp_path / "data")})
    create_table(namespace, {}, TABLE_ID, _ipc(_base()), mode="create")
    open_dataset(namespace, {}, TABLE_ID).create_branch(BRANCH, None)
    return namespace


def _code(ns, *, branch: str | None, on: str, payload: pa.Table) -> int | None:  # noqa: ANN001
    """Drive merge_insert and return the spec code it answered, or None if it did not fail."""
    request = MergeInsertIntoTableRequest(id=TABLE_ID, on=on, when_matched_update_all=True, branch=branch)
    try:
        merge_insert_into_table(ns, {}, request, _ipc(payload))
    except Exception as exc:  # noqa: BLE001 — the CODE is the subject; the class is only how it carries one
        return getattr(exc, "code", None)
    return None


@pytest.mark.parametrize(
    ("shape", "on", "payload"),
    [
        ("a column of the right name but the wrong Arrow type", "id", pa.table({"id": pa.array([1], pa.int64()), "s": pa.array([9], pa.int64())})),
        ("an extra column the table does not have", "id", pa.table({"id": pa.array([1], pa.int64()), "s": pa.array(["x"]), "zz": pa.array([1], pa.int64())})),
        ("a payload schema wholly unrelated to the table", "id", pa.table({"q": pa.array([1], pa.int64())})),
        # Not a schema failure — the KEY column. Included because it is the same asymmetry through a
        # different guard, and because a door that matches on a column must say so when it is absent.
        ("an `on` key column that does not exist", "nosuchcol", _base()),
    ],
)
def test_a_failed_merge_insert_answers_one_code_on_both_paths(ns, shape: str, on: str, payload: pa.Table) -> None:  # noqa: ANN001
    """The headline: branch and main must agree, and must both be a real code rather than Internal."""
    main = _code(ns, branch=None, on=on, payload=payload)
    branch = _code(ns, branch=BRANCH, on=on, payload=payload)

    assert main is not None and branch is not None, f"{shape}: a bad write SUCCEEDED (main={main}, branch={branch})"
    assert branch == main, f"{shape}: branch answered code {branch} where main answered {main} — the same mistake, two answers"
    assert branch != 18, f"{shape}: both paths answered Internal 18 — a client cannot tell a server fault from its own bad payload"
