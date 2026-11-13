"""A table writer writes every branch of the table (ADR 0159 D3, [[LH-056]]).

A branch is a parameter of a table operation (`lance_docs/ns_catalog/spec.yaml`, the `branch` query parameter of
`InsertIntoTable` and `MergeInsertIntoTable`), not an object with an authorization of its own, so every door that writes
a branch asks `can_write_data` on the table, as it does for main. Creating or deleting a branch, the tag doors and restore
stay above the writer rung (`tests/integration/test_authz.py::test_table_branch_and_tag_create_are_owner_tier`).

Against the catalog as deployed, a loopback Dex signing callers in and a real OpenFGA deciding on a store of the test's
own ([[LH-235]]): alice makes `lance_catalog$db$t` and its branch `exp` through their doors, so she owns both, and grants
bob `writer` on the table alone. Each door runs to its write, so a rung asked anywhere on its way refuses bob.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import lance
import pyarrow as pa
import pytest

from catalog.api.dependencies import get_vendor
from catalog.core.vending import VendedSession
from lakehouse.arrow_ipc import ARROW_STREAM_MEDIA_TYPE, encode_arrow_stream
from lakehouse.governed.store_credential import S3KeyPair


TABLE = "lance_catalog$db$t"


class _Vendor:
    """Records each vend it is asked for, and mints nothing: the claim is whom the door admits to it."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def vend(self, **kwargs: Any) -> VendedSession:  # noqa: ANN401 — the vendor protocol's keyword arguments
        self.calls.append(kwargs)
        return VendedSession(key_pair=S3KeyPair(access_key_id="AK", secret_access_key="SK"), endpoint=None, region="us-east-1", expires_at_millis=1)


def _rows(*ids: int) -> bytes:
    return encode_arrow_stream(pa.table({"id": pa.array(ids, pa.int64())}))


@pytest.fixture
def estate(governed_catalog: Any) -> Iterator[tuple[Any, _Vendor]]:  # noqa: ANN401 — the conftest's GovernedCatalog
    """alice's `lance_catalog$db$t` holding row 1, its branch `exp`, bob a writer on the table alone, and the recording vendor."""
    catalog, alice = governed_catalog, governed_catalog.bearer("alice")
    vendor = _Vendor()
    catalog.client.app.dependency_overrides[get_vendor] = lambda: vendor
    assert catalog.create_in_the_estate("alice", "db").status_code == 200
    created = catalog.client.post(f"/v1/table/{TABLE}/create", content=_rows(1), headers={**alice, "Content-Type": ARROW_STREAM_MEDIA_TYPE})
    assert created.status_code == 200, created.text
    branched = catalog.client.post(f"/v1/table/{TABLE}/branches/create", json={"name": "exp"}, headers=alice)
    assert branched.status_code == 200, branched.text
    granted = catalog.client.post(f"/management/v1/table/{TABLE}/access/grant", json={"user": "dex~bob", "relation": "writer"}, headers=alice)
    assert granted.status_code == 200, granted.text
    yield catalog, vendor
    catalog.client.app.dependency_overrides.pop(get_vendor, None)


def _ids(catalog: Any, ref: str | None) -> list[int]:  # noqa: ANN401
    described = catalog.client.post(f"/v1/table/{TABLE}/describe", json={"vend_credentials": False}, headers=catalog.bearer("alice"))
    dataset = lance.dataset(described.json()["location"])
    head = dataset if ref is None else dataset.checkout_version((ref, None))
    return sorted(head.to_table(columns=["id"]).column("id").to_pylist())


@pytest.mark.parametrize(
    ("door", "params"),
    [
        pytest.param("insert", {"branch": "exp"}, id="insert"),
        pytest.param("merge_insert", {"branch": "exp", "on": "id", "when_not_matched_insert_all": "true"}, id="merge-insert"),
    ],
)
def test_a_table_writer_writes_rows_on_a_branch(estate: tuple[Any, _Vendor], door: str, params: dict[str, str]) -> None:
    catalog, _ = estate

    written = catalog.client.post(
        f"/v1/table/{TABLE}/{door}", params=params, content=_rows(2), headers={**catalog.bearer("bob"), "Content-Type": ARROW_STREAM_MEDIA_TYPE}
    )

    assert written.status_code == 200, written.text
    assert (_ids(catalog, "exp"), _ids(catalog, None)) == ([1, 2], [1])


def test_a_table_writer_is_vended_a_write_credential_for_a_branch(estate: tuple[Any, _Vendor]) -> None:
    catalog, vendor = estate

    vended = catalog.client.post(f"/management/v1/table/{TABLE}/credentials", params={"tier": "write", "branch": "exp"}, headers=catalog.bearer("bob"))

    assert (vended.status_code, vended.json()["mode"]) == (200, "direct"), vended.text
    assert [(call["tier"], call["branch"]) for call in vendor.calls] == [("write", "exp")]
