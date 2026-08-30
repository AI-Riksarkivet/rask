"""The credential-vending endpoint (Track B): POST /v1/table/{id}/credentials.

Exercises our layer — location resolution (describe_table) → vendor.vend → response shape — with the
backend namespace + vendor injected via dependency override. The vendor classes themselves are unit-tested
in test_vending.py; the live STS path is proven by the e2e against RustFS.
"""

from __future__ import annotations

from unittest.mock import MagicMock

from fastapi.testclient import TestClient
from lance_namespace import DescribeTableResponse

from catalog.api.dependencies import get_vendor
from catalog.core.vending import Tier, VendedCredentials


def _described(location: str | None) -> DescribeTableResponse:
    return DescribeTableResponse(location=location)


def test_mode_b_returns_server_mediated(client: TestClient, fake_ns: MagicMock) -> None:
    # The lifespan builds a ModeBVendor by default → vend() returns None → no direct credential.
    fake_ns.describe_table.return_value = _described("s3://lance-catalog/db$t")
    resp = client.post("/v1/table/db$t/credentials")
    assert resp.status_code == 200
    body = resp.json()
    assert body["mode"] == "server_mediated"
    assert "credentials" not in body  # response_model_exclude_none drops the null


def test_direct_creds_from_the_vendor_carry_scoped_storage_options(client: TestClient, fake_ns: MagicMock) -> None:
    fake_ns.describe_table.return_value = _described("s3://lance-catalog/db$t")
    captured: dict[str, object] = {}

    class _FakeVendor:
        def vend(self, *, table_location: str, tier: Tier, web_identity_token: str | None = None) -> VendedCredentials:
            captured.update(table_location=table_location, tier=tier)
            return VendedCredentials(
                storage_options={"access_key_id": "AK", "secret_access_key": "SK", "session_token": "ST"},
                expires_at_millis=123456,
            )

    client.app.dependency_overrides[get_vendor] = lambda: _FakeVendor()
    resp = client.post("/v1/table/db$t/credentials?tier=write")
    assert resp.status_code == 200
    body = resp.json()
    assert body["mode"] == "direct"
    assert body["credentials"]["storage_options"]["session_token"] == "ST"
    assert body["credentials"]["expires_at_millis"] == 123456
    # the table's real object-store location + the requested tier are passed to the vendor
    assert captured == {"table_location": "s3://lance-catalog/db$t", "tier": "write"}


def test_missing_location_falls_back_to_server_mediated(client: TestClient, fake_ns: MagicMock) -> None:
    fake_ns.describe_table.return_value = _described(None)
    resp = client.post("/v1/table/db$t/credentials")
    assert resp.status_code == 200
    assert resp.json()["mode"] == "server_mediated"


def test_invalid_tier_is_rejected(client: TestClient, fake_ns: MagicMock) -> None:
    fake_ns.describe_table.return_value = _described("s3://lance-catalog/db$t")
    assert client.post("/v1/table/db$t/credentials?tier=admin").status_code == 422


# --------------------------------------------------------------------------------------------------
# The SPEC's own credential path: DescribeTable(vend_credentials=true) -> response.storage_options.
# We previously DROPPED this field, so a generic Lance client (incl. medallion-producer in REST mode) got no
# credentials and had no way to discover our bespoke /credentials endpoint. The one confirmed
# reinvention in the 2026-07-14 audit — interop-breaking.
# --------------------------------------------------------------------------------------------------


def test_describe_vends_storage_options_when_asked(client: TestClient, fake_ns: MagicMock) -> None:
    fake_ns.describe_table.return_value = _described("s3://lance-catalog/db$t")
    vendor = MagicMock()
    vendor.vend.return_value = VendedCredentials(storage_options={"aws_access_key_id": "AK"})
    client.app.dependency_overrides[get_vendor] = lambda: vendor

    resp = client.post("/v1/table/db$t/describe?vend_credentials=true")
    assert resp.status_code == 200
    assert resp.json()["storage_options"] == {"aws_access_key_id": "AK"}

    # READ tier ONLY: describe is gated on the reader rung, so vending a WRITE credential from it would be
    # a privilege escalation. A write credential must still go through /credentials?tier=write.
    assert vendor.vend.call_args.kwargs["tier"] == "read"
    client.app.dependency_overrides.pop(get_vendor, None)


def test_describe_does_not_vend_unless_asked(client: TestClient, fake_ns: MagicMock) -> None:
    # Backward-compatible: the field is opt-in. A plain describe must not hand out credentials.
    fake_ns.describe_table.return_value = _described("s3://lance-catalog/db$t")
    vendor = MagicMock()
    client.app.dependency_overrides[get_vendor] = lambda: vendor

    resp = client.post("/v1/table/db$t/describe")
    assert resp.status_code == 200
    assert "storage_options" not in resp.json()  # response_model_exclude_none drops the null
    vendor.vend.assert_not_called()
    client.app.dependency_overrides.pop(get_vendor, None)
