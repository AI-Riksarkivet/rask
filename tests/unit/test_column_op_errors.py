"""``_column_op`` classifies CLIENT mistakes and nothing else (#101).

The HTTP side of this contract is `tests/integration/test_column_errors.py`, driven through the real
backend. This file pins the other half — the failures that must NOT be reclassified — because they cannot
be provoked through the real store: the whole point is what happens when the OBJECT STORE fails, not the
request.

The discipline is an allowlist, not `except Exception`. `caller_sql` could key on Lance's single
``Invalid user input`` marker; `_column_op` cannot, because `drop_columns` reports both of its user errors
as UNMARKED ``ValueError``s — so the match had to widen, and widening is exactly how a translator starts
blaming callers for outages.
"""

from __future__ import annotations

from pathlib import Path

import pyarrow as pa
import pytest
from lance_namespace import (
    AlterTableDropColumnsRequest,
    CreateNamespaceRequest,
    LanceNamespaceError,
    connect,
)

from catalog.services import table_claims
from catalog.services.dataplane import create_table, drop_columns
from catalog.services.table_claims import DoorTable
from service_kit.lakehouse.location_claims import ClaimStore
from service_kit.lakehouse.objectfs import StorageOptions


#: The stamp a door commits with ([[LH-497]]).
STAMP = {"rask.author": "alice", "rask.operation": "update"}


@pytest.fixture
def table(tmp_path: Path) -> DoorTable:
    ns = connect("dir", {"root": str(tmp_path)})
    ns.create_namespace(CreateNamespaceRequest(id=["db"]))
    create_table(ns, StorageOptions(), ["db", "t"], pa.table({"id": [1, 2, 3], "v": ["a", "b", "c"]}), root=str(tmp_path), mode="create", registry=None)
    return table_claims.door_table(ns, ClaimStore(control_root=str(tmp_path / "control")), ["db", "t"])


def test_a_storage_failure_on_a_column_op_is_not_blamed_on_the_caller(table: DoorTable, monkeypatch: pytest.MonkeyPatch) -> None:
    """An ``OSError`` with no user-error signature propagates untranslated — it is a 500, and it stays one.

    ``drop_columns``' real user errors arrive as bare ``ValueError``/``OSError`` with no marker, so the
    translator matches on MESSAGE. The failure mode of that design is swallowing the class: every write
    would answer "fix your columns" while the object store was down.
    """

    def boom(*_a: object, **_k: object) -> None:
        raise OSError("object store unreachable")

    monkeypatch.setattr("catalog.services.table_claims.open_resolved_dataset", boom)
    with pytest.raises(OSError, match="unreachable") as caught:
        drop_columns(table, StorageOptions(), AlterTableDropColumnsRequest(id=["db", "t"], columns=["v"]), transaction_properties=STAMP)
    assert not isinstance(caught.value, LanceNamespaceError), "a storage outage was minted into a domain error"
