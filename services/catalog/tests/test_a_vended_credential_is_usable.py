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
decides both halves and there is nothing a caller could need to override. What both doors answer for an `https://`
store is pinned by `test_a_vended_secret_leaves_the_catalog_only_on_the_wire.py`.

What the exchange sends and the triple it answers are driven on a moto STS by
`test_the_catalog_signs_only_with_sessions_its_token_is_exchanged_for.py`; this file is about whether a client can
BUILD from what it was handed.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pydantic import SecretStr

from catalog.core.vending import WebIdentityVendor
from service_kit.governed.store_credential import S3KeyPair
from service_kit.governed.store_session import IssuedSession, WebIdentity


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
    return vended.as_storage_options().in_clear()


def test_a_vended_credential_carries_what_a_lance_client_needs_to_build() -> None:
    """`allow_http` and path-style addressing are not optional extras.

    Without `allow_http`, object_store refuses to construct a client for an `http://` endpoint at all.
    Without path-style, RustFS rejects virtual-hosted signing with 403 `SignatureDoesNotMatch` —
    the same reason `lance_storage_options` defaults `virtual_hosted=False`.
    """
    opts = _vend("http://rustfs:9000")
    assert opts["allow_http"] == "true"
    assert opts["virtual_hosted_style_request"] == "false"


def test_a_vend_against_aws_proper_permits_no_plaintext() -> None:
    """No configured endpoint means botocore's regional AWS endpoint, which is `https://`.

    The `endpoint` key is REMOVED for that case, so the scheme the permit follows is AWS's own — and a
    permit derived from an empty endpoint must read as "no plaintext", since the store it names is TLS.
    """
    opts = _vend(None)
    assert "endpoint" not in opts
    assert opts["allow_http"] == "false", "a credential for AWS proper permits plaintext"
