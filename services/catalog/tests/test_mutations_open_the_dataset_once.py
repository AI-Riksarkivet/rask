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

Reading it off the SAME handle is also strictly more correct: the readback can no longer land on a
different ref than the write did.

Counted, not read: the opens are tallied through the real ops against a real ``dir`` namespace.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.ipc as ipc
import pytest
from lance_namespace import (
    AlterTableAddColumnsRequest,
    AlterTableDropColumnsRequest,
    DeleteFromTableRequest,
    LanceNamespace,
    UpdateTableRequest,
    connect,
)

from catalog.services import dataplane


def _ipc(table: pa.Table) -> bytes:
    sink = pa.BufferOutputStream()
    with ipc.new_stream(sink, table.schema) as writer:
        writer.write_table(table)
    return sink.getvalue().to_pybytes()


@pytest.fixture
def ns(tmp_path: Path) -> LanceNamespace:
    backend = connect("dir", {"root": str(tmp_path)})
    dataplane.create_table(backend, {}, ["t"], _ipc(pa.table({"id": [1, 2, 3], "v": ["a", "b", "c"]})), mode="create")
    return backend


@pytest.fixture
def opens(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Tally every ``open_dataset`` the data plane performs, keeping the real behaviour."""
    seen: list[str] = []
    real = dataplane.open_dataset

    def counting(*args: Any, **kwargs: Any) -> Any:
        seen.append("open")
        return real(*args, **kwargs)

    monkeypatch.setattr(dataplane, "open_dataset", counting)
    return seen


def _ops(ns: LanceNamespace) -> dict[str, Any]:
    return {
        "delete_from_table": lambda: dataplane.delete_from_table(ns, {}, DeleteFromTableRequest(id=["t"], predicate="id = 1")),
        "update_table": lambda: dataplane.update_table(ns, {}, UpdateTableRequest(id=["t"], updates=[["v", "'z'"]])),
        "add_columns": lambda: dataplane.add_columns(ns, {}, AlterTableAddColumnsRequest(id=["t"], new_columns=[{"name": "doubled", "expression": "id * 2"}])),
        "drop_columns": lambda: dataplane.drop_columns(ns, {}, AlterTableDropColumnsRequest(id=["t"], columns=["v"])),
        "update_field_metadata": lambda: dataplane.update_field_metadata(ns, {}, ["t"], [{"path": "id", "metadata": {"unit": "px"}}]),
    }


@pytest.mark.parametrize("op", ["delete_from_table", "update_table", "add_columns", "drop_columns", "update_field_metadata"])
def test_a_mutating_op_opens_the_dataset_exactly_once(op: str, ns: LanceNamespace, opens: list[str]) -> None:
    response = _ops(ns)[op]()
    assert opens, f"{op} never opened a dataset — the tally is not seeing the op"
    assert len(opens) == 1, f"{op} opened the dataset {len(opens)}x; the version readback is a second full describe + open"
    assert int(response.version) == 2, f"{op} reported version {response.version!r}, not the version it just committed"
