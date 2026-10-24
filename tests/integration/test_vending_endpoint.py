"""The credential-vending endpoint (Track B): POST /management/v1/table/{id}/credentials.

Exercises our layer — location resolution (describe_table) → vendor.vend → response shape — with the
backend namespace + vendor injected via dependency override. The exchange itself runs on a moto STS in
`services/catalog/tests/test_the_catalog_signs_only_with_sessions_its_token_is_exchanged_for.py`; the live store's
answer is proven by the e2e lanes against RustFS.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from unittest.mock import MagicMock

from botocore.exceptions import ClientError
from fastapi.testclient import TestClient
from lance_namespace import DescribeTableResponse

from catalog.api.dependencies import get_vendor
from catalog.core.vending import Tier, VendedCredentials


def _described(location: str | None) -> DescribeTableResponse:
    return DescribeTableResponse(location=location)


def test_mode_b_returns_server_mediated(client: TestClient, fake_ns: MagicMock) -> None:
    # The lifespan builds a ModeBVendor by default → vend() returns None → no direct credential.
    fake_ns.describe_table.return_value = _described("s3://lance-catalog/db$t")
    resp = client.post("/management/v1/table/db$t/credentials")
    assert resp.status_code == 200
    body = resp.json()
    assert body["mode"] == "server_mediated"
    assert "credentials" not in body  # response_model_exclude_none drops the null


def test_direct_creds_from_the_vendor_carry_scoped_storage_options(client: TestClient, fake_ns: MagicMock, claim_described: Callable[[str], str]) -> None:
    fake_ns.describe_table.return_value = _described("s3://lance-catalog/db$t")
    claim_described("db$t")
    captured: dict[str, object] = {}

    class _FakeVendor:
        def vend(
            self, *, table_location: str, tier: Tier, session_name: str, bases: Sequence[str] = (), data_directories: Sequence[str] = (), branch: str = ""
        ) -> VendedCredentials:
            captured.update(table_location=table_location, tier=tier)
            return VendedCredentials(
                storage_options={"access_key_id": "AK", "secret_access_key": "SK", "session_token": "ST"},
                expires_at_millis=123456,
            )

    client.app.dependency_overrides[get_vendor] = lambda: _FakeVendor()
    resp = client.post("/management/v1/table/db$t/credentials?tier=write")
    assert resp.status_code == 200
    body = resp.json()
    assert body["mode"] == "direct"
    assert body["credentials"]["storage_options"]["session_token"] == "ST"
    assert body["credentials"]["expires_at_millis"] == 123456
    # the table's real object-store location + the requested tier are passed to the vendor
    assert captured == {"table_location": "s3://lance-catalog/db$t", "tier": "write"}


def test_an_exchange_the_store_refuses_answers_unavailable(client: TestClient, fake_ns: MagicMock) -> None:
    """The token the store refused is the catalog's own, so the caller is told to retry (503, code 17), never to sign
    in again: a 401 would send an authorized caller into a re-login loop that cannot succeed."""
    fake_ns.describe_table.return_value = _described("s3://lance-catalog/db$t")

    class _RefusedVendor:
        def vend(
            self, *, table_location: str, tier: Tier, session_name: str, bases: Sequence[str] = (), data_directories: Sequence[str] = (), branch: str = ""
        ) -> VendedCredentials:
            raise ClientError({"Error": {"Code": "AccessDenied", "Message": "token verification failed"}}, "AssumeRoleWithWebIdentity")

    client.app.dependency_overrides[get_vendor] = lambda: _RefusedVendor()
    resp = client.post("/management/v1/table/db$t/credentials")

    assert (resp.status_code, resp.headers["content-type"]) == (503, "application/problem+json"), resp.text
    assert resp.json()["code"] == 17, resp.text


def test_invalid_tier_is_rejected(client: TestClient, fake_ns: MagicMock) -> None:
    fake_ns.describe_table.return_value = _described("s3://lance-catalog/db$t")
    assert client.post("/management/v1/table/db$t/credentials?tier=admin").status_code == 422
