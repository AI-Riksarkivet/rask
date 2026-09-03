"""Credential vending builds its STS clients through `storage.sts_client`, never `boto3` directly.

STS is not S3, so this is not the path-style/s3v4 argument the warehouse registry makes — it is the
dependency one: `boto3` is declared and imported by `packages/storage` alone, so every client the
estate builds gets the same connect/read timeouts. botocore's defaults have NONE, and the vending call
sits on the synchronous path of a data-plane request: an STS endpoint that accepts the connection and
never answers would hang the catalog worker instead of failing the vend.
"""

from __future__ import annotations

import ast
import pathlib
from typing import Any

import pytest

from catalog.core import vending


_VENDING_PY = pathlib.Path(vending.__file__)


def test_vending_never_imports_boto3() -> None:
    """No `import boto3` — top-level or inline. `botocore` stays allowed (`UNSIGNED` is a sentinel,
    and `ClientError` is an error shape); building the client is what belongs to `storage`."""
    offences: list[str] = []
    for node in ast.walk(ast.parse(_VENDING_PY.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import) and any(alias.name.split(".")[0] == "boto3" for alias in node.names):
            offences.append(f"vending.py:{node.lineno} imports boto3")
        if isinstance(node, ast.ImportFrom) and node.module and node.module.split(".")[0] == "boto3":
            offences.append(f"vending.py:{node.lineno} imports from boto3")
    assert not offences, "vending hand-rolls its STS client instead of storage.sts_client:\n  " + "\n  ".join(offences)


class _FakeSts:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def assume_role(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(("assume_role", kwargs))
        return {"Credentials": {"AccessKeyId": "ak", "SecretAccessKey": "sk", "SessionToken": "tok"}}

    def assume_role_with_web_identity(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(("assume_role_with_web_identity", kwargs))
        return {"Credentials": {"AccessKeyId": "ak", "SecretAccessKey": "sk", "SessionToken": "tok"}}


@pytest.fixture
def seam(monkeypatch: pytest.MonkeyPatch) -> tuple[_FakeSts, list[dict[str, Any]]]:
    built: list[dict[str, Any]] = []
    client = _FakeSts()

    def fake_sts_client(**kwargs: Any) -> _FakeSts:
        built.append(kwargs)
        return client

    import storage

    monkeypatch.setattr(storage, "sts_client", fake_sts_client)
    return client, built


def test_sts_vendor_builds_a_signed_client_through_the_storage_seam(seam: tuple[_FakeSts, list[dict[str, Any]]]) -> None:
    """`AssumeRole` is authenticated by the catalog's own SigV4 signature — RustFS refuses an
    unsigned STS request with `InvalidRequest`, so this client must NOT ask for unsigned.

    The KEY PAIR is asserted alongside the endpoint because "not unsigned" was never sufficient: the
    client was built without credentials and botocore's default chain finds none in the pod (the S3
    secret arrives from the Dapr secret store into `Settings`, never the env), so every AssumeRole
    raised `NoCredentialsError` and the vending door answered 503 — measured on the deployed estate
    2026-09-03. Signing needs the credential, not merely the intent to sign.
    """
    _client, built = seam

    vendor = vending.StsVendor(
        role_arn="arn:aws:iam::123456789012:role/vend",
        region="eu-north-1",
        endpoint="http://rustfs:9000",
        access_key="ROOTKEY",
        secret_key="ROOTSECRET",
    )
    creds = vendor.vend(table_location="s3://b/db$t", tier="read")

    assert built == [{"region": "eu-north-1", "endpoint": "http://rustfs:9000", "access_key": "ROOTKEY", "secret_key": "ROOTSECRET"}]
    assert creds is not None and creds.storage_options["aws_session_token"] == "tok"


def test_web_identity_vendor_builds_an_unsigned_client_through_the_storage_seam(seam: tuple[_FakeSts, list[dict[str, Any]]]) -> None:
    """The JWT in the body is the authentication; there is no credential to sign with, so the
    unsigned flag is load-bearing and asserted by name."""
    _client, built = seam

    vendor = vending.WebIdentityVendor(region="eu-north-1", endpoint="http://rustfs:9000")
    creds = vendor.vend(table_location="s3://b/db$t", tier="read", web_identity_token="a.jwt.here")

    assert built == [{"region": "eu-north-1", "endpoint": "http://rustfs:9000", "unsigned": True}]
    assert creds is not None and creds.storage_options["aws_session_token"] == "tok"


def test_the_sts_client_is_built_once_per_vendor(seam: tuple[_FakeSts, list[dict[str, Any]]]) -> None:
    """The vendor is a lifespan singleton and the client owns a connection pool; rebuilding it per
    vend would re-resolve the endpoint on every data-plane request."""
    _client, built = seam

    vendor = vending.StsVendor(role_arn="arn:aws:iam::123456789012:role/vend", region="eu-north-1")
    for _ in range(3):
        vendor.vend(table_location="s3://b/db$t", tier="read")

    assert len(built) == 1
