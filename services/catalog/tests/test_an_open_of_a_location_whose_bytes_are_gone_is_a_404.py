"""A registered table whose bytes are gone must answer 404, never a bare 500.

Measured live: `silver$features` was registered at a bucket that no longer held the dataset, and
`POST /publish` answered

    500 Internal Server Error
    ValueError: Dataset at path medallion/silver was not found: Not found: medallion/silver/_versions

The catalog's own error contract forbids that shape — every domain error is a `lance_namespace` typed
error rendered as an RFC 9457 problem body, so a client dispatches on a code rather than parsing a
traceback. A 500 also says the wrong thing: it reads as "the catalog is broken" when the catalog is
fine and the REGISTRATION is stale, which sends whoever is on call to the wrong system.

So the open every door makes at its resolved location (`namespace.open_resolved_dataset`) answers a location with
nothing behind it as `TableNotFoundError`: the table the name resolved to is not there.
"""

from __future__ import annotations

from pathlib import Path
from urllib.parse import urlparse

import pyarrow as pa
import pytest
from lance_namespace import CreateNamespaceRequest, TableNotFoundError, connect
from tables_by_name import open_by_name

from catalog.core.namespace import open_resolved_dataset
from catalog.services.dataplane import create_table


TABLE_ID = ["db", "pages"]


def _table() -> pa.Table:
    return pa.table({"id": pa.array([1, 2, 3], pa.int64())})


@pytest.fixture
def ns(tmp_path: Path):  # noqa: ANN201 — LanceNamespace, runtime-only
    namespace = connect("dir", {"root": str(tmp_path)})
    namespace.create_namespace(CreateNamespaceRequest(id=TABLE_ID[:-1]))
    create_table(namespace, {}, TABLE_ID, _table(), root=str(tmp_path), mode="create", registry=None)
    return namespace


def test_a_registration_whose_bytes_are_GONE_is_a_404(ns, tmp_path: Path) -> None:  # noqa: ANN001
    """The live shape: the table is registered, the location resolves, and nothing is there."""
    uri = open_by_name(ns, {}, TABLE_ID).uri
    versions = Path(urlparse(uri).path) / "_versions"
    for f in versions.iterdir():
        f.unlink()
    versions.rmdir()

    with pytest.raises(TableNotFoundError, match=Path(urlparse(uri).path).name):
        open_resolved_dataset(uri, {}, TABLE_ID)
