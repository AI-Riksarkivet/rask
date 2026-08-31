"""`UpdateTable` and `DeleteFromTable` must mutate the BRANCH the caller named, not main.

THE DEFECT. Both requests carry `branch`, the served OpenAPI advertises it, and both handlers opened
the dataset without it — so a caller working on a branch mutated **main**, received a 200, and the
lineage WROTE edge recorded main's version. Nothing anywhere was red. Every sibling mutation on the
same object (`add_columns`, `alter_columns`, `drop_columns`, `update_field_metadata`) passes
`branch=req.branch`; these two were the exceptions.

It is a REGRESSION, not an original omission: `test_mutations_open_the_dataset_once.py` quotes the
pre-refactor line as carrying `branch=req.branch` and names update and delete among the operations it
changed. The suite that would have caught the loss is the one that documents it.

Silent wrong-target writes are the worst failure shape a table format can have — worse than a refusal,
because a branch exists precisely so that work can be staged WITHOUT touching main, and the caller's
whole reason for using one is the guarantee this broke.

Driven against a real `dir` namespace and real pylance writes, because the subject is which dataset the
bytes landed in — a mock would only prove the argument was forwarded.
"""

from __future__ import annotations

from pathlib import Path

import pyarrow as pa
import pytest
from lance_namespace import connect

from catalog.services.dataplane import create_table, delete_from_table, open_dataset, update_table


lance = pytest.importorskip("lance")

from lance_namespace_urllib3_client.models.delete_from_table_request import DeleteFromTableRequest  # noqa: E402
from lance_namespace_urllib3_client.models.update_table_request import UpdateTableRequest  # noqa: E402


TABLE_ID = ["rows"]
SCHEMA = pa.schema([pa.field("id", pa.int64()), pa.field("label", pa.string())])
BRANCH = "staging"


def _ipc(ids: list[int], label: str) -> bytes:
    table = pa.table({"id": pa.array(ids, pa.int64()), "label": pa.array([label] * len(ids))}, schema=SCHEMA)
    sink = pa.BufferOutputStream()
    with pa.ipc.new_stream(sink, table.schema) as writer:
        writer.write_table(table)
    return sink.getvalue().to_pybytes()


@pytest.fixture
def ns(tmp_path: Path):  # noqa: ANN201 — LanceNamespace is runtime-only
    namespace = connect("dir", {"root": str(tmp_path / "data")})
    create_table(namespace, {}, TABLE_ID, _ipc([1, 2, 3], "main"), mode="create")
    dataset = open_dataset(namespace, {}, TABLE_ID)
    dataset.create_branch(BRANCH) if hasattr(dataset, "create_branch") else pytest.skip("pylance has no branch API here")
    return namespace


def test_an_update_on_a_branch_leaves_MAIN_untouched(ns) -> None:  # noqa: ANN001
    """The headline: a branch-scoped update must not rewrite main's rows."""
    update_table(ns, {}, UpdateTableRequest(id=TABLE_ID, branch=BRANCH, updates=[["label", "'edited'"]]))

    main = open_dataset(ns, {}, TABLE_ID).to_table().column("label").to_pylist()
    assert main == ["main", "main", "main"], f"a branch-scoped update rewrote MAIN: {main}"


def test_a_delete_on_a_branch_leaves_MAIN_untouched(ns) -> None:  # noqa: ANN001
    """Same defect, the destructive half — and the one where a silent wrong target loses data."""
    delete_from_table(ns, {}, DeleteFromTableRequest(id=TABLE_ID, branch=BRANCH, predicate="id = 1"))

    main = open_dataset(ns, {}, TABLE_ID)
    assert main.count_rows() == 3, f"a branch-scoped delete removed rows from MAIN: {main.count_rows()} left"


def test_the_update_actually_reaches_the_branch(ns) -> None:  # noqa: ANN001
    """The other half — the fix must not become 'ignore the branch differently'."""
    update_table(ns, {}, UpdateTableRequest(id=TABLE_ID, branch=BRANCH, updates=[["label", "'edited'"]]))

    on_branch = open_dataset(ns, {}, TABLE_ID, branch=BRANCH).to_table().column("label").to_pylist()
    assert on_branch == ["edited", "edited", "edited"], f"the update never reached the branch: {on_branch}"
