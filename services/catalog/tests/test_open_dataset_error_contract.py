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
from catalog.core.namespace import open_dataset
from catalog.services.dataplane import create_table
from lance_namespace import TableNotFoundError, connect


TABLE_ID = ["pages"]


def _ipc() -> bytes:
    table = pa.table({"id": pa.array([1, 2, 3], pa.int64())})
    sink = pa.BufferOutputStream()
    with pa.ipc.new_stream(sink, table.schema) as writer:
        writer.write_table(table)
    return sink.getvalue().to_pybytes()


@pytest.fixture
def ns(tmp_path: Path):  # noqa: ANN201 — LanceNamespace, runtime-only
    namespace = connect("dir", {"root": str(tmp_path)})
    create_table(namespace, {}, TABLE_ID, _ipc(), mode="create")
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


def test_a_healthy_table_still_opens(ns) -> None:  # noqa: ANN001
    """Without this the fix could be 'always raise' and the test above would still pass."""
    assert open_dataset(ns, {}, TABLE_ID).count_rows() == 3


def test_the_error_names_the_LOCATION_so_a_stale_registration_is_actionable(ns, tmp_path: Path) -> None:  # noqa: ANN001
    """Knowing the table is missing is not enough to fix it — the bucket it points at is the finding."""
    uri = open_dataset(ns, {}, TABLE_ID).uri
    versions = Path(uri) / "_versions"
    for f in versions.iterdir():
        f.unlink()
    versions.rmdir()

    with pytest.raises(TableNotFoundError) as exc:
        open_dataset(ns, {}, TABLE_ID)

    assert str(tmp_path) in str(exc.value) or "pages" in str(exc.value)
