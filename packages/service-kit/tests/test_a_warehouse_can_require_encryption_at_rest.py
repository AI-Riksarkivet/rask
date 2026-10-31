"""Encryption at rest reaches the writer, or it is a setting nobody applies (§ J5).

A lakehouse buyer expects to say "this warehouse is encrypted with my key". rask had it in neither
code nor chart: `lance_storage_options` emitted credentials, endpoint and region and nothing else, so a
client writing DIRECTLY to object storage — the whole point of the vending path — wrote unencrypted no
matter what the warehouse intended.

THE KEY NAMES ARE VERIFIED, NOT GUESSED. `lance_docs/lance/guide/object_store.md:134-136` lists them and their contract:

    aws_server_side_encryption   one of "AES256", "aws:kms", "aws:kms:dsse"
    aws_sse_kms_key_id           "If set, aws_server_side_encryption must be aws:kms or aws:kms:dsse"
    aws_sse_bucket_key_enabled   bucket keys for SSE

That matters more here than usual: this builder's own docstring records that object_store SILENTLY
IGNORES an option it does not recognise (probed 2026-09-03 — an invented key produced no error and no
change to the signed request). So a misspelled encryption field is not a failure, it is plaintext with
a config that claims otherwise — which is the worst outcome available.

THE CONSTRAINT IS ENFORCED HERE FOR THE SAME REASON. A KMS key id paired with "AES256" is a
misconfiguration the store will not reject; refusing it at the builder is the only place it can be
caught before the bytes land.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
from pydantic import SecretStr

from service_kit.governed.store_credential import S3KeyPair
from service_kit.governed.store_session import IssuedSession, WebIdentity
from service_kit.lakehouse.objectfs import StorageOptions, lance_storage_options


def _opts(*, server_side_encryption: str, sse_kms_key_id: str | None = None, sse_bucket_key_enabled: bool | None = None) -> StorageOptions:
    return lance_storage_options(
        "http://s3:9000",
        S3KeyPair(access_key_id="key", secret_access_key=SecretStr("secret")),
        "us-east-1",
        server_side_encryption=server_side_encryption,
        sse_kms_key_id=sse_kms_key_id,
        sse_bucket_key_enabled=sse_bucket_key_enabled,
    )


def test_a_KMS_key_WITHOUT_a_kms_algorithm_is_REFUSED() -> None:
    """`lance_docs/lance/guide/object_store.md:135`, verbatim: "If set, `aws_server_side_encryption` must be `"aws:kms"` or
    `"aws:kms:dsse"`." The store will not reject the pair; it will ignore the key id and encrypt with
    AES256, so the operator believes their KMS key is in use and it never was."""
    with pytest.raises(ValueError, match="aws:kms"):
        _opts(server_side_encryption="AES256", sse_kms_key_id="arn:aws:kms:eu-north-1:1:key/abc")


def test_an_UNKNOWN_algorithm_is_REFUSED() -> None:
    """The guide names exactly three. Anything else is silently dropped by object_store and the data
    lands in plaintext — the failure this builder exists to make impossible."""
    with pytest.raises(ValueError, match="AES256"):
        _opts(server_side_encryption="AES-256")


def test_bucket_keys_ride_along_when_asked() -> None:
    opts = _opts(server_side_encryption="aws:kms", sse_kms_key_id="k", sse_bucket_key_enabled=True)
    assert opts.options["aws_sse_bucket_key_enabled"] == "true", "bucket-key enablement did not reach the writer"


def _identity() -> WebIdentity:
    return WebIdentity(token_file=Path("projected-token"), endpoint="http://s3:9000", region="eu-north-1", role_arn="arn:aws:iam::0:role/r")


def _issued(*_a: object, **_kw: object) -> IssuedSession:
    return IssuedSession(
        key_pair=S3KeyPair(access_key_id="AK", secret_access_key=SecretStr("SK"), session_token=SecretStr("ST")), expiration=datetime(2030, 1, 1, tzinfo=UTC)
    )


def test_the_VENDED_credential_carries_it_to_a_direct_writer() -> None:
    """The end of the chain, and the only part that matters operationally.

    A vended client writes to object storage with the catalog out of the path, so encryption reaches it
    through the storage options or not at all. This drives the real `WebIdentityVendor` with a stubbed
    exchange — the credential is fake, the option assembly is the production one.
    """
    from catalog.core.vending import EncryptionAtRest, WebIdentityVendor

    vendor = WebIdentityVendor(
        identity=_identity(),
        exchange=_issued,
        encryption=EncryptionAtRest(algorithm="aws:kms", kms_key_id="arn:aws:kms:eu-north-1:1:key/abc"),
    )
    vended = vendor.vend(table_location="s3://bucket/ns/t.lance", tier="write", session_name="writer")
    # `vend` answers None on the postures that offer no credential (mode_b); a None here would mean the
    # vendor declined, which is a different failure from the one under test and must not read as it.
    assert vended is not None, "the vendor vended nothing at all"
    options = vended.as_storage_options()
    assert options.options.get("aws_server_side_encryption") == "aws:kms", (
        "the vended credential does not tell the writer to encrypt — the catalog is out of the path, so nothing else can"
    )
    assert options.options.get("aws_sse_kms_key_id") == "arn:aws:kms:eu-north-1:1:key/abc"
