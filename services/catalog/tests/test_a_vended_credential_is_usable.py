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
"""

from __future__ import annotations

from typing import Any


def _vend(**kw: Any) -> dict[str, str]:
    from catalog.core.vending import StsVendor

    def fake_assume_role(**_: Any) -> dict[str, Any]:
        return {"Credentials": {"AccessKeyId": "AK", "SecretAccessKey": "SK", "SessionToken": "TOK"}}

    vendor = StsVendor(
        role_arn="arn:aws:iam::000000000000:role/vend",
        region="us-east-1",
        assume_role=fake_assume_role,
        **kw,
    )
    vended = vendor.vend(table_location="s3://bucket/tbl", tier="read")
    assert vended is not None
    return vended.storage_options


def test_a_vended_credential_carries_what_a_lance_client_needs_to_build() -> None:
    """`allow_http` and path-style addressing are not optional extras.

    Without `allow_http`, object_store refuses to construct a client for an `http://` endpoint at all.
    Without path-style, RustFS/MinIO reject virtual-hosted signing with 403 `SignatureDoesNotMatch` —
    the same reason `lance_storage_options` defaults `virtual_hosted=False`.
    """
    opts = _vend(endpoint="http://rustfs:9000")
    assert opts["allow_http"] == "true"
    assert opts["virtual_hosted_style_request"] == "false"


def test_the_credential_itself_still_rides() -> None:
    opts = _vend(endpoint="http://rustfs:9000")
    assert (opts["access_key_id"], opts["secret_access_key"], opts["session_token"]) == ("AK", "SK", "TOK")
    assert opts["region"] == "us-east-1"


def test_an_https_endpoint_is_not_downgraded() -> None:
    """`allow_http` PERMITS plaintext; it must not be read as requesting it. A TLS endpoint keeps TLS —
    the same trap `s3_filesystem` documents, where hardcoding `http` once silently downgraded a secured
    connection."""
    opts = _vend(endpoint="https://s3.example.com")
    assert opts["endpoint"] == "https://s3.example.com"
