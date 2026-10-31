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
from lance_namespace import CreateNamespaceRequest, connect
from tables_by_name import open_by_name

from catalog.services import table_claims
from catalog.services.dataplane import create_table, delete_from_table, update_table
from catalog.services.table_claims import DoorTable
from service_kit.lakehouse import base_registry
from service_kit.lakehouse.location_claims import ClaimStore
from service_kit.lakehouse.objectfs import StorageOptions


lance = pytest.importorskip("lance")

from lance_namespace_urllib3_client.models.delete_from_table_request import DeleteFromTableRequest  # noqa: E402
from lance_namespace_urllib3_client.models.update_table_request import UpdateTableRequest  # noqa: E402


TABLE_ID = ["db", "rows"]
#: The stamp a door commits with ([[LH-497]]).
STAMP = {"rask.author": "alice", "rask.operation": "update"}
SCHEMA = pa.schema([pa.field("id", pa.int64()), pa.field("label", pa.string())])
BRANCH = "staging"


def _table(ids: list[int], label: str) -> pa.Table:
    return pa.table({"id": pa.array(ids, pa.int64()), "label": pa.array([label] * len(ids))}, schema=SCHEMA)


@pytest.fixture
def ns(tmp_path: Path):  # noqa: ANN201 — LanceNamespace is runtime-only
    root = str(tmp_path / "data")
    namespace = connect("dir", {"root": root})
    namespace.create_namespace(CreateNamespaceRequest(id=TABLE_ID[:-1]))
    registry = base_registry.BaseRegistry(control_root=str(tmp_path / "control"), storage_options={})
    create_table(namespace, StorageOptions(), TABLE_ID, _table([1, 2, 3], "main"), root=root, mode="create", registry=registry, holder="$".join(TABLE_ID))
    dataset = open_by_name(namespace, StorageOptions(), TABLE_ID)
    dataset.create_branch(BRANCH) if hasattr(dataset, "create_branch") else pytest.skip("pylance has no branch API here")
    return namespace


@pytest.fixture
def table(ns, tmp_path: Path) -> DoorTable:  # noqa: ANN001
    """The table as a door acts on it, resolved on ``ns``."""
    return table_claims.door_table(ns, ClaimStore(control_root=str(tmp_path / "control")), TABLE_ID)


def test_an_update_on_a_branch_leaves_MAIN_untouched(ns, table: DoorTable) -> None:  # noqa: ANN001
    """The headline: a branch-scoped update must not rewrite main's rows."""
    update_table(table, StorageOptions(), UpdateTableRequest(id=TABLE_ID, branch=BRANCH, updates=[["label", "'edited'"]]), transaction_properties=STAMP)

    main = open_by_name(ns, StorageOptions(), TABLE_ID).to_table().column("label").to_pylist()
    assert main == ["main", "main", "main"], f"a branch-scoped update rewrote MAIN: {main}"


def test_a_delete_on_a_branch_leaves_MAIN_untouched(ns, table: DoorTable) -> None:  # noqa: ANN001
    """Same defect, the destructive half — and the one where a silent wrong target loses data."""
    delete_from_table(table, StorageOptions(), DeleteFromTableRequest(id=TABLE_ID, branch=BRANCH, predicate="id = 1"), transaction_properties=STAMP)

    main = open_by_name(ns, StorageOptions(), TABLE_ID)
    assert main.count_rows() == 3, f"a branch-scoped delete removed rows from MAIN: {main.count_rows()} left"
