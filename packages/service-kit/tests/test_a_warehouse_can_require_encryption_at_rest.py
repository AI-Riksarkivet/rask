"""Encryption at rest reaches the writer, or it is a setting nobody applies (§ J5).

A lakehouse buyer expects to say "this warehouse is encrypted with my key". rask had it in neither
code nor chart: `lance_storage_options` emitted credentials, endpoint and region and nothing else, so a
client writing DIRECTLY to object storage — the whole point of the vending path — wrote unencrypted no
matter what the warehouse intended.

THE KEY NAMES ARE VERIFIED, NOT GUESSED. `lance_docs/guide.md:2417-2419` lists them and their contract:

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

import pytest

from service_kit.lakehouse.objectfs import lance_storage_options


def _opts(**kw: object) -> dict[str, str]:
    return lance_storage_options("http://s3:9000", "key", "secret", "us-east-1", **kw)  # ty: ignore[invalid-argument-type]


def test_no_encryption_asked_for_emits_no_encryption_keys() -> None:
    """Every existing caller is untouched: an option the store would read must not appear uninvited."""
    opts = _opts()
    assert not [k for k in opts if "encryption" in k or "sse" in k], f"encryption keys appeared unasked: {sorted(opts)}"


def test_AES256_reaches_the_writer_under_the_documented_name() -> None:
    opts = _opts(server_side_encryption="AES256")
    assert opts.get("aws_server_side_encryption") == "AES256", (
        "the algorithm did not reach the storage options — object_store ignores what it does not recognise, so this writes plaintext silently"
    )


def test_a_KMS_key_carries_its_algorithm() -> None:
    """Both halves, under the names the guide documents."""
    opts = _opts(server_side_encryption="aws:kms", sse_kms_key_id="arn:aws:kms:eu-north-1:1:key/abc")
    assert opts["aws_server_side_encryption"] == "aws:kms"
    assert opts["aws_sse_kms_key_id"] == "arn:aws:kms:eu-north-1:1:key/abc"


def test_a_KMS_key_WITHOUT_a_kms_algorithm_is_REFUSED() -> None:
    """`lance_docs/guide.md:2418`, verbatim: "If set, `aws_server_side_encryption` must be `aws:kms` or
    `aws:kms:dsse`." The store will not reject the pair; it will ignore the key id and encrypt with
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
    assert opts["aws_sse_bucket_key_enabled"] == "true", "bucket-key enablement did not reach the writer"


def test_the_VENDED_credential_carries_it_to_a_direct_writer() -> None:
    """The end of the chain, and the only part that matters operationally.

    A vended client writes to object storage with the catalog out of the path, so encryption reaches it
    through the storage options or not at all. This drives the real `StsVendor` with a stubbed
    AssumeRole — the credential is fake, the option assembly is the production one.
    """
    from catalog.core.vending import EncryptionAtRest, StsVendor

    def _assume(**_kw: object) -> dict[str, object]:
        return {
            "Credentials": {
                "AccessKeyId": "AK",
                "SecretAccessKey": "SK",
                "SessionToken": "ST",
                "Expiration": None,
            }
        }

    vendor = StsVendor(
        role_arn="arn:aws:iam::0:role/r",
        region="eu-north-1",
        endpoint="http://s3:9000",
        assume_role=_assume,
        access_key="a",
        secret_key="b",
        encryption=EncryptionAtRest(algorithm="aws:kms", kms_key_id="arn:aws:kms:eu-north-1:1:key/abc"),
    )
    vended = vendor.vend(table_location="s3://bucket/ns/t.lance", tier="write")
    # `vend` answers None on the postures that offer no credential (mode_b); a None here would mean the
    # STS vendor declined, which is a different failure from the one under test and must not read as it.
    assert vended is not None, "the STS vendor vended nothing at all"
    assert vended.storage_options.get("aws_server_side_encryption") == "aws:kms", (
        "the vended credential does not tell the writer to encrypt — the catalog is out of the path, so nothing else can"
    )
    assert vended.storage_options.get("aws_sse_kms_key_id") == "arn:aws:kms:eu-north-1:1:key/abc"


def test_an_UNCONFIGURED_vendor_vends_exactly_what_it_did_before() -> None:
    """Every deployment that asks for nothing is untouched — no uninvited option reaches the writer."""
    from catalog.core.vending import StsVendor

    vendor = StsVendor(
        role_arn="arn:aws:iam::0:role/r",
        region="eu-north-1",
        endpoint="http://s3:9000",
        assume_role=lambda **_k: {"Credentials": {"AccessKeyId": "AK", "SecretAccessKey": "SK", "SessionToken": "ST", "Expiration": None}},
        access_key="a",
        secret_key="b",
    )
    vended = vendor.vend(table_location="s3://bucket/ns/t.lance", tier="write")
    assert vended is not None, "the STS vendor vended nothing at all"
    opts = vended.storage_options
    assert not [k for k in opts if "encryption" in k or "sse" in k], f"encryption keys appeared unasked: {sorted(opts)}"
