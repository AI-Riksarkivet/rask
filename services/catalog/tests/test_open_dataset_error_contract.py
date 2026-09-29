"""A registered table whose bytes are gone must answer 404, never a bare 500.

Measured live: `silver$features` was registered at a bucket that no longer held the dataset, and
`POST /publish` answered

    500 Internal Server Error
    ValueError: Dataset at path medallion/silver was not found: Not found: medallion/silver/_versions

The catalog's own error contract forbids that shape — every domain error is a `lance_namespace` typed
error rendered as an RFC 9457 problem body, so a client dispatches on a code rather than parsing a
traceback. A 500 also says the wrong thing: it reads as "the catalog is broken" when the catalog is
fine and the REGISTRATION is stale, which sends whoever is on call to the wrong system.

`TableNotFoundError` is the same error this function already raises when the registration names no
location at all. A registration naming a location with nothing behind it is the same fact, discovered
one step later.
"""

from __future__ import annotations

from pathlib import Path

import pyarrow as pa
import pytest
from lance_namespace import TableNotFoundError, connect

from catalog.core.namespace import open_dataset
from catalog.services.dataplane import create_table


TABLE_ID = ["pages"]


def _table() -> pa.Table:
    return pa.table({"id": pa.array([1, 2, 3], pa.int64())})


@pytest.fixture
def ns(tmp_path: Path):  # noqa: ANN201 — LanceNamespace, runtime-only
    namespace = connect("dir", {"root": str(tmp_path)})
    create_table(namespace, {}, TABLE_ID, _table(), mode="create", registry=None)
    return namespace


def test_a_registration_whose_bytes_are_GONE_is_a_404(ns, tmp_path: Path) -> None:  # noqa: ANN001
    """The live shape: the table is registered, the location resolves, and nothing is there."""
    uri = open_dataset(ns, {}, TABLE_ID).uri
    versions = Path(uri) / "_versions"
    for f in versions.iterdir():
        f.unlink()
    versions.rmdir()

    with pytest.raises(TableNotFoundError, match="pages"):
        open_dataset(ns, {}, TABLE_ID)
