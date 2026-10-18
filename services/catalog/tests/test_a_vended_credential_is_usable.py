"""A vended credential must be USABLE by the client it is handed to, not merely correct.

Measured on the deployed estate 2026-09-03, once STS vending could sign at all: the vended
`storage_options` carried the key pair, the session token, the region and the endpoint — and Lance
refused to build a client from them, failing in 13µs with `HTTP error: builder error`. Not a network
error, a CONSTRUCTION error: object_store will not build an S3 client for an `http://` endpoint unless
`allow_http` says so. Adding it made the identical credential read the table.

So the credential was correct and unusable, which is the worst shape — the vend succeeds, the caller
gets a 200 with a full triple, and every attempt to use it fails somewhere else entirely.

The cause is drift the estate already has a guard against: the vendor hand-rolls the options dict
instead of going through `lance_storage_options`, whose own docstring says it exists because "one
omitted key in a hand-rolled copy is exactly the drift this builder exists to prevent". Two omitted
keys, in the copy that ships credentials to clients.

`allow_http` IS A PERMIT, NOT A REQUEST, and it follows the endpoint's scheme ([[LH-238]]). The
catalog's own connection derives it that way; a vend is the same rule on the options that LEAVE the
catalog, so an `https://` store must never be handed a credential that also permits plaintext. Measured
on pylance 12.0.0 against a closed port: `http://` with `allow_http=false` dies at construction
(`builder error`), `https://` with `allow_http=false` proceeds to a TLS request — so the scheme alone
decides both halves and there is nothing a caller could need to override.

What the exchange sends and the triple it answers are driven on a moto STS by
`test_the_catalog_signs_only_with_sessions_its_token_is_exchanged_for.py`; this file is about whether a client can
BUILD from what it was handed.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from lance_namespace import DescribeNamespaceResponse, DescribeTableResponse
from pydantic import SecretStr

from catalog.api.dependencies import get_namespace, get_settings
from catalog.api.v1.endpoints import tables
from catalog.core.config import Settings
from catalog.core.vending import VendFacts, WebIdentityVendor
from service_kit.governed.store_credential import S3KeyPair
from service_kit.governed.store_session import IssuedSession, WebIdentity
from service_kit.lakehouse.ns_errors import install_problem_handlers


def _fake_exchange(*_: Any, **__: Any) -> IssuedSession:
    return IssuedSession(
        key_pair=S3KeyPair(access_key_id="AK", secret_access_key=SecretStr("SK"), session_token=SecretStr("TOK")), expiration=datetime(2030, 1, 1, tzinfo=UTC)
    )


def _vendor(endpoint: str | None) -> WebIdentityVendor:
    identity = WebIdentity(token_file=Path("projected-token"), endpoint=endpoint, region="us-east-1", role_arn="arn:aws:iam::000000000000:role/vend")
    return WebIdentityVendor(identity=identity, exchange=_fake_exchange)


def _vend(endpoint: str | None) -> dict[str, str]:
    vended = _vendor(endpoint).vend(table_location="s3://bucket/tbl", tier="read", session_name="reader")
    assert vended is not None
    return vended.storage_options


def test_a_vended_credential_carries_what_a_lance_client_needs_to_build() -> None:
    """`allow_http` and path-style addressing are not optional extras.

    Without `allow_http`, object_store refuses to construct a client for an `http://` endpoint at all.
    Without path-style, RustFS rejects virtual-hosted signing with 403 `SignatureDoesNotMatch` —
    the same reason `lance_storage_options` defaults `virtual_hosted=False`.
    """
    opts = _vend("http://rustfs:9000")
    assert opts["allow_http"] == "true"
    assert opts["virtual_hosted_style_request"] == "false"


def test_an_https_endpoint_is_not_downgraded() -> None:
    """A TLS endpoint keeps TLS AND is not handed a plaintext permit beside it.

    The endpoint alone was never enough: object_store consults `allow_http` only when a request would
    otherwise be refused, so a credential carrying `https://` with `allow_http=true` works identically
    until something downgrades — and then permits it. The same trap `s3_filesystem` documents, where
    hardcoding `http` once silently downgraded a secured connection.
    """
    opts = _vend("https://s3.example.com")
    assert opts["endpoint"] == "https://s3.example.com"
    assert opts["allow_http"] == "false", "an https store was vended a credential that also permits plaintext"


def test_a_vend_against_aws_proper_permits_no_plaintext() -> None:
    """No configured endpoint means botocore's regional AWS endpoint, which is `https://`.

    The `endpoint` key is REMOVED for that case, so the scheme the permit follows is AWS's own — and a
    permit derived from an empty endpoint must read as "no plaintext", since the store it names is TLS.
    """
    opts = _vend(None)
    assert "endpoint" not in opts
    assert opts["allow_http"] == "false", "a credential for AWS proper permits plaintext"


class _Namespace:
    """A backend whose `describe_table` answers an `s3://` location, the only kind a vend scopes."""

    def describe_table(self, request: Any) -> DescribeTableResponse:
        return DescribeTableResponse(location="s3://lakehouse/abc12345_ns$t")

    def describe_namespace(self, request: Any) -> DescribeNamespaceResponse:
        """The namespace declares no tier; the tier judge reads it before a write-tier vend."""
        return DescribeNamespaceResponse(properties={})


@pytest.fixture
def describe_client(monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    application = FastAPI()
    install_problem_handlers(application, logging.getLogger(__name__))
    application.include_router(tables.router)
    application.state.vendor = _vendor("https://s3.example.com")
    # The manifest read is the catalog's own root-credential open, not the vend, and needs a live store.
    monkeypatch.setattr(tables, "dataset_facts", lambda location, storage_options: VendFacts(read_version=1))
    # The catalog's OWN connection is plaintext and root-keyed, so the production `get_storage_options`
    # answers `allow_http=true` and the root pair: a door that merged it into the vend, either way round,
    # would hand a reader the root secret or hand the https store a plaintext permit.
    settings = Settings(LANCE_S3_ENDPOINT="http://rustfs:9000", LANCE_S3_ACCESS_KEY_ID="ROOTKEY", LANCE_S3_SECRET_ACCESS_KEY="ROOTSECRET")
    application.dependency_overrides[get_settings] = lambda: settings
    application.dependency_overrides[get_namespace] = _Namespace
    with TestClient(application) as client:
        yield client


def test_the_describe_door_hands_an_https_vend_on_without_a_plaintext_permit(describe_client: TestClient) -> None:
    """The HTTP door, not only the vendor: `describe_table` answers the vend and nothing of its own connection."""
    response = describe_client.post("/v1/table/ns%24t/describe", params={"vend_credentials": "true"})

    assert response.status_code == 200, response.text
    options = response.json()["storage_options"]
    assert options["endpoint"] == "https://s3.example.com"
    assert options["aws_session_token"] == "TOK", "the door answered without the vend — this proves nothing about it"
    assert options["aws_access_key_id"] == "AK"
    assert not {"ROOTKEY", "ROOTSECRET"} & set(options.values()), "the describe door handed a reader the catalog's root credential"
    assert options["allow_http"] == "false", "the describe door handed an https store a credential that also permits plaintext"
