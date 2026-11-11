"""The credential-vending endpoint (Track B): POST /management/v1/table/{id}/credentials.

Exercises our layer — location resolution (describe_table) → vendor.vend → response shape — with the
backend namespace + vendor injected via dependency override. The exchange itself runs on a moto STS in
`services/catalog/tests/test_the_catalog_signs_only_with_sessions_its_token_is_exchanged_for.py`; the live store's
answer is proven by the e2e lanes against RustFS.
"""

from __future__ import annotations

import socket
from collections.abc import Callable, Sequence
from unittest.mock import MagicMock

import pytest
from botocore.exceptions import ClientError
from fastapi.testclient import TestClient
from lance_namespace import DescribeTableResponse
from pydantic import SecretStr

from catalog.api.dependencies import get_vendor
from catalog.core.config import get_settings
from catalog.core.vending import TableRecords, Tier, VendedSession
from lakehouse.governed.store_credential import S3KeyPair


def test_mode_b_returns_server_mediated(client: TestClient, claim_described: Callable[[str], str]) -> None:
    # The lifespan builds a ModeBVendor by default → vend() returns None → no direct credential.
    claim_described("db$t")
    resp = client.post("/management/v1/table/db$t/credentials")
    assert resp.status_code == 200
    body = resp.json()
    assert body["mode"] == "server_mediated"
    assert "credentials" not in body  # response_model_exclude_none drops the null


def test_direct_creds_from_the_vendor_carry_scoped_storage_options(client: TestClient, fake_ns: MagicMock, claim_described: Callable[[str], str]) -> None:
    claim_described("db$t")
    captured: dict[str, object] = {}

    class _FakeVendor:
        def vend(
            self,
            *,
            table_location: str,
            tier: Tier,
            session_name: str,
            bases: Sequence[str] = (),
            data_directories: Sequence[str] = (),
            branch: str = "",
            records: TableRecords | None = None,
        ) -> VendedSession:
            captured.update(table_location=table_location, tier=tier)
            key_pair = S3KeyPair(access_key_id="AK", secret_access_key=SecretStr("SK"), session_token=SecretStr("ST"))
            return VendedSession(key_pair=key_pair, endpoint=None, region="us-east-1", expires_at_millis=123456)

    client.app.dependency_overrides[get_vendor] = lambda: _FakeVendor()
    resp = client.post("/management/v1/table/db$t/credentials?tier=write")
    assert resp.status_code == 200
    body = resp.json()
    assert body["mode"] == "direct"
    assert body["credentials"]["storage_options"]["aws_session_token"] == "ST"
    assert body["credentials"]["expires_at_millis"] == 123456
    # the table's real object-store location + the requested tier are passed to the vendor
    assert captured == {"table_location": fake_ns.describe_table.return_value.location, "tier": "write"}


def test_an_exchange_the_store_refuses_answers_unavailable(client: TestClient, claim_described: Callable[[str], str]) -> None:
    """The token the store refused is the catalog's own, so the caller is told to retry (503, code 17), never to sign
    in again: a 401 would send an authorized caller into a re-login loop that cannot succeed."""
    claim_described("db$t")

    class _RefusedVendor:
        def vend(
            self,
            *,
            table_location: str,
            tier: Tier,
            session_name: str,
            bases: Sequence[str] = (),
            data_directories: Sequence[str] = (),
            branch: str = "",
            records: TableRecords | None = None,
        ) -> VendedSession:
            raise ClientError({"Error": {"Code": "AccessDenied", "Message": "token verification failed"}}, "AssumeRoleWithWebIdentity")

    client.app.dependency_overrides[get_vendor] = lambda: _RefusedVendor()
    resp = client.post("/management/v1/table/db$t/credentials")

    assert (resp.status_code, resp.headers["content-type"]) == (503, "application/problem+json"), resp.text
    assert resp.json()["code"] == 17, resp.text


def test_a_vend_whose_store_does_not_answer_the_open_is_refused_unavailable(
    client: TestClient, fake_ns: MagicMock, claim_described: Callable[[str], str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """What the table holds is read off its open before anything is vended, so an open the store did not answer says
    nothing about it: the caller is told to retry (503, code 17), and no credential is asked for ([[LH-572]]). The store
    is an endpoint nothing listens on, which pylance reports as a request that could not be sent (measured on pylance
    13.0.0: a ValueError, after retries that cost this test a few seconds)."""
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    monkeypatch.setenv("LANCE_S3_ENDPOINT", f"http://127.0.0.1:{port}")
    get_settings.cache_clear()
    fake_ns.describe_table.return_value = DescribeTableResponse(location="s3://lance-catalog/db$t")
    claim_described("db$t")
    asked: list[str] = []

    class _RecordingVendor:
        def vend(
            self,
            *,
            table_location: str,
            tier: Tier,
            session_name: str,
            bases: Sequence[str] = (),
            data_directories: Sequence[str] = (),
            branch: str = "",
            records: TableRecords | None = None,
        ) -> VendedSession:
            asked.append(table_location)
            return VendedSession(
                key_pair=S3KeyPair(access_key_id="AK", secret_access_key=SecretStr("SK")), endpoint=None, region="us-east-1", expires_at_millis=1
            )

    client.app.dependency_overrides[get_vendor] = lambda: _RecordingVendor()
    resp = client.post("/management/v1/table/db$t/credentials")

    assert (resp.status_code, resp.headers["content-type"], resp.json().get("code")) == (503, "application/problem+json", 17), resp.text
    assert asked == []


def test_invalid_tier_is_rejected(client: TestClient) -> None:
    assert client.post("/management/v1/table/db$t/credentials?tier=admin").status_code == 422
