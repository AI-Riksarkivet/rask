"""A14 is enforced on the path production takes, not only on the one the tests take.

`LocalCatalog.ensure` asserts the creation contract on BOTH its branches — the dataset it just created
and the one that already existed — and says why at the site: the two guarantees cannot be added
afterwards, so refusing at the head of a run is the last moment an operator can still fix it cheaply.

`CatalogServiceClient.ensure` is the same seam against a real catalog, and it is what runs in the
cluster. It has THREE returns — the short-circuit for a table that already exists, the create, and the
409 re-describe — and MEASURED 2026-09-10 none of them checked anything. So a table created or reused
in-cluster was never held to the contract, while every unit test went through `LocalCatalog` and passed.
A gate that runs only on the path that does not ship is the estate's own "a control that cannot fire"
shape.

THESE TESTS DRIVE A REAL DATASET, deliberately. The contract is a question about what pylance recorded
at creation (`has_stable_row_ids`, and whether an `id` column exists), and a double asserting that the
function was CALLED would pass against a call that checks the wrong dataset — which is exactly the
failure mode, since the location comes back from the catalog rather than from the caller.

BLAST RADIUS MEASURED BEFORE ENABLING IT: six live tables across bronze, silver, gold and two tenants
were probed against both clauses on 2026-09-10 and all six pass, so turning the gate on refuses nothing
that exists today. It is a guard against the next writer, not a migration.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

import httpx
import pyarrow as pa
import pytest
import respx

from ingest.catalog import CreationContractError
from ingest.catalog_service import CatalogServiceClient


CATALOG = "http://catalog.test"


def _dataset(tmp: str, *, with_id: bool, stable: bool) -> str:
    import lance

    uri = str(Path(tmp) / "t.lance")
    columns = {"id": pa.array([1, 2, 3])} if with_id else {"other": pa.array([1, 2, 3])}
    lance.write_dataset(pa.table(columns), uri, mode="create", enable_stable_row_ids=stable)
    return uri


def _client() -> CatalogServiceClient:
    return CatalogServiceClient(pa.schema([pa.field("id", pa.int64())]), base_url=CATALOG, token="t")


def _catalog_serves(uri: str) -> None:
    """The catalog answers `describe` with ``uri``, and the etag evolution is a no-op.

    `ensure`'s short-circuit does TWO things — describe, then add the etag column if missing — so a
    mock covering only the describe fails the negative control with a transport error and looks like
    the gate refusing a conforming table.
    """
    respx.post(url__regex=r".*/v1/table/.*/describe").mock(return_value=httpx.Response(200, json={"location": uri}))
    respx.post(url__regex=r".*/v1/table/.*/add_columns").mock(return_value=httpx.Response(400, text="column already exists in schema"))
    # NO credentials route is mocked, and `respx.mock` asserting every route is used is what proves it:
    # these locations are filesystem paths, so the contract check must not ask the vending door for a
    # credential it does not need. The check vends only for a `://` location — see `_contracted`.


@respx.mock
def test_a_table_that_ALREADY_EXISTS_is_held_to_the_contract() -> None:
    """The short-circuit return, and the one that matters most: a table created before the contract —
    or by any other writer — reaches production through exactly this branch, never through the create."""
    with tempfile.TemporaryDirectory() as tmp:
        uri = _dataset(tmp, with_id=True, stable=False)
        _catalog_serves(uri)

        with pytest.raises(CreationContractError) as excinfo:
            _client().ensure("acme-bronze", "events")

        assert "enable_stable_row_ids" in str(excinfo.value)


@respx.mock
def test_a_table_missing_its_id_column_is_refused_on_the_service_path() -> None:
    """The other clause. Without `id` the merge-on-write path has nothing to converge on, so a
    redelivered hop appends duplicates instead of updating."""
    with tempfile.TemporaryDirectory() as tmp:
        uri = _dataset(tmp, with_id=False, stable=True)
        _catalog_serves(uri)

        with pytest.raises(CreationContractError) as excinfo:
            _client().ensure("acme-bronze", "events")

        assert "`id` column" in str(excinfo.value)


@respx.mock
def test_a_CONFORMING_table_passes_through_unchanged() -> None:
    """The negative control, and it is what makes the two above mean anything: the gate must refuse the
    violation and nothing else. Without this a function that raised on every call would look correct."""
    with tempfile.TemporaryDirectory() as tmp:
        uri = _dataset(tmp, with_id=True, stable=True)
        _catalog_serves(uri)

        assert _client().ensure("acme-bronze", "events") == uri
