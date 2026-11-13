"""A maintainer holding no reader grant is vended no read credential ([[LH-560]]), and the maintainer doors are a
maintainer's and no writer's ([[LH-575]]).

The model keeps a maintainer from reading (`model.fga`: a table's ``can_read_data`` is ``reader``), and a vend is a read
of the whole table: the read tier's session policy gets every object under its prefix. So the vend door asks the reader
rung at the read tier, and admits a maintainer only at the maintain tier, whose own rung (``can_vend_maintain``) the
maintenance identity alone holds. A read handed to a maintainer would be a data read no ``reader`` grant wrote, and
``can_grant_maintainer`` (``maintainer and pass_grants``) would let any maintainer hand it on.

The disarm and the floor raise are maintenance's two commits, which the catalog makes under its own credential at doors
on the maintainer rung (``can_maintain``), since the maintain tier puts nothing of a table: a writer, who may change the
table's rows, may not make them.

Every decision is the real model's, on a store of this test's own: alice owns ``lance_catalog$db`` and ``lance_catalog$db$t`` and grants bob
``maintainer`` on ``lance_catalog$db$t`` through its own door. The vendor records every call, since a credential minted and then
discarded is still a grant at the store.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pyarrow as pa
import pytest
from pydantic import SecretStr

from catalog.api.dependencies import get_vendor
from catalog.core.vending import VendedSession
from lakehouse.arrow_ipc import ARROW_STREAM_MEDIA_TYPE, encode_arrow_stream
from lakehouse.governed.store_credential import S3KeyPair


#: The Lance Namespace spec's PermissionDenied code (`spec.yaml`).
PERMISSION_DENIED = 15


class _Vendor:
    """Records every credential it is asked for: the claim is that it is never asked."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def vend(self, **kwargs: Any) -> VendedSession:  # noqa: ANN401 — the vendor protocol's keyword arguments
        self.calls.append(kwargs)
        return VendedSession(key_pair=S3KeyPair(access_key_id="AK", secret_access_key=SecretStr("SK")), endpoint=None, region="us-east-1", expires_at_millis=1)


@pytest.fixture
def maintained(governed_catalog: Any) -> Iterator[tuple[Any, _Vendor]]:  # noqa: ANN401 — the conftest's GovernedCatalog
    """alice's ``lance_catalog$db$t``, bob a maintainer on it and nothing else, and the recording vendor."""
    catalog = governed_catalog
    vendor = _Vendor()
    catalog.client.app.dependency_overrides[get_vendor] = lambda: vendor
    alice = catalog.bearer("alice")
    assert catalog.create_in_the_estate("alice", "db").status_code == 200
    rows = encode_arrow_stream(pa.table({"id": pa.array([1, 2], pa.int64())}))
    created = catalog.client.post("/v1/table/lance_catalog$db$t/create", content=rows, headers={**alice, "Content-Type": ARROW_STREAM_MEDIA_TYPE})
    assert created.status_code == 200, created.text
    granted = catalog.client.post("/management/v1/table/lance_catalog$db$t/access/grant", json={"user": "dex~bob", "relation": "maintainer"}, headers=alice)
    assert granted.status_code == 200, granted.text
    yield catalog, vendor
    catalog.client.app.dependency_overrides.pop(get_vendor, None)


def test_a_maintainer_holding_no_reader_grant_is_refused_a_read_vend(maintained: tuple[Any, _Vendor]) -> None:
    catalog, vendor = maintained

    refused = catalog.client.post("/management/v1/table/lance_catalog$db$t/credentials", headers=catalog.bearer("bob"))

    assert (refused.status_code, refused.json().get("code")) == (403, PERMISSION_DENIED), refused.text
    assert vendor.calls == []


def test_the_maintainer_doors_answer_a_maintainer_and_refuse_a_writer(maintained: tuple[Any, _Vendor]) -> None:
    catalog, _ = maintained
    granted = catalog.client.post(
        "/management/v1/table/lance_catalog$db$t/access/grant", json={"user": "dex~carol", "relation": "writer"}, headers=catalog.bearer("alice")
    )
    assert granted.status_code == 200, granted.text
    doors = {"maintenance/disarm": None, "maintenance/floor": {"dry_run": True}}

    answered = {
        (who, door): catalog.client.post(f"/management/v1/table/lance_catalog$db$t/{door}", json=body, headers=catalog.bearer(who)).status_code
        for who in ("bob", "carol")
        for door, body in doors.items()
    }

    assert answered == {
        ("bob", "maintenance/disarm"): 200,
        ("bob", "maintenance/floor"): 200,
        ("carol", "maintenance/disarm"): 403,
        ("carol", "maintenance/floor"): 403,
    }
