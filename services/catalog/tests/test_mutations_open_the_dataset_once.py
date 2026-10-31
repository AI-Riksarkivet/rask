"""CAT-CORE-09 — a mutating table op opens its dataset ONCE, not twice.

Every in-place mutation in the data plane ended the same way::

    dataset = open_dataset(ns, so, table_id, branch=req.branch)   # the operation
    ...
    return XResponse(version=_version(ns, so, table_id, req.branch))   # a SECOND full open

and ``_version`` is ``open_dataset(...).version``. So each op paid for two ``describe_table`` round
trips against the namespace backend and two ``lance.dataset()`` opens against object storage, to learn
a number the handle it already held was carrying. Measured against pylance: ``delete``, ``update``,
``add_columns``, ``alter_columns``, ``drop_columns`` and ``update_field_metadata`` all mutate their
dataset object IN PLACE, so ``dataset.version`` after the call IS the committed version.

Each op now commits stamped ([[LH-497]], ``dataplane.commit_stamped``), which answers the version it finalized, and
plans its first attempt on the handle the door opened, so the door's checked open is still the only one.

Counted, not read: the opens are tallied through the real ops against a real ``dir`` namespace.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pyarrow as pa
import pytest
from lance_namespace import (
    AlterTableAddColumnsRequest,
    AlterTableDropColumnsRequest,
    CreateNamespaceRequest,
    DeleteFromTableRequest,
    UpdateTableRequest,
    connect,
)

from catalog.services import dataplane, table_claims
from catalog.services.table_claims import DoorTable
from service_kit.lakehouse import base_registry
from service_kit.lakehouse.location_claims import ClaimStore
from service_kit.lakehouse.objectfs import StorageOptions


@pytest.fixture
def table(tmp_path: Path) -> DoorTable:
    """``db$t`` as a door acts on it, resolved on a real ``dir`` namespace."""
    backend = connect("dir", {"root": str(tmp_path)})
    backend.create_namespace(CreateNamespaceRequest(id=["db"]))
    registry = base_registry.BaseRegistry(control_root=str(tmp_path / "control"), storage_options={})
    rows = pa.table({"id": [1, 2, 3], "v": ["a", "b", "c"]})
    dataplane.create_table(backend, StorageOptions(), ["db", "t"], rows, root=str(tmp_path), mode="create", registry=registry, holder="db$t")
    return table_claims.door_table(backend, ClaimStore(control_root=str(tmp_path / "control")), ["db", "t"])


@pytest.fixture
def opens(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Tally every open of the door's table the data plane performs, keeping the real behaviour."""
    seen: list[str] = []
    real = table_claims.open_resolved_dataset

    def counting(*args: Any, **kwargs: Any) -> Any:
        seen.append("open")
        return real(*args, **kwargs)

    monkeypatch.setattr(table_claims, "open_resolved_dataset", counting)
    return seen


#: The stamp a door commits with ([[LH-497]]).
STAMP = {"rask.author": "alice", "rask.operation": "update"}


def _ops(table: DoorTable) -> dict[str, Any]:
    return {
        "delete_from_table": lambda: dataplane.delete_from_table(
            table, StorageOptions(), DeleteFromTableRequest(id=["db", "t"], predicate="id = 1"), transaction_properties=STAMP
        ),
        "update_table": lambda: dataplane.update_table(
            table, StorageOptions(), UpdateTableRequest(id=["db", "t"], updates=[["v", "'z'"]]), transaction_properties=STAMP
        ),
        "add_columns": lambda: dataplane.add_columns(
            table,
            StorageOptions(),
            AlterTableAddColumnsRequest(id=["db", "t"], new_columns=[{"name": "doubled", "expression": "id * 2"}]),
            transaction_properties=STAMP,
        ),
        "drop_columns": lambda: dataplane.drop_columns(
            table, StorageOptions(), AlterTableDropColumnsRequest(id=["db", "t"], columns=["v"]), transaction_properties=STAMP
        ),
        "update_field_metadata": lambda: dataplane.update_field_metadata(
            table, StorageOptions(), [{"path": "id", "metadata": {"unit": "px"}}], transaction_properties=STAMP
        ),
    }


@pytest.mark.parametrize("op", ["delete_from_table", "update_table", "add_columns", "drop_columns", "update_field_metadata"])
def test_a_mutating_op_opens_the_dataset_exactly_once(op: str, table: DoorTable, opens: list[str]) -> None:
    response = _ops(table)[op]()
    assert opens, f"{op} never opened a dataset — the tally is not seeing the op"
    assert len(opens) == 1, f"{op} opened the dataset {len(opens)}x; the version readback is a second full describe + open"
    assert int(response.version) == 2, f"{op} reported version {response.version!r}, not the version it just committed"
