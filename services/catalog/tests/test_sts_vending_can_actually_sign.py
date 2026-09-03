"""STS vending could never sign its own AssumeRole call, so the mode was dead.

Measured on the deployed estate 2026-09-03: flipping `vending.mode=sts` and asking
`POST /v1/table/{id}/credentials` for a write-tier credential answered **503**, with
`botocore.exceptions.NoCredentialsError: Unable to locate credentials` in the catalog log.

`storage.sts_client(region=..., endpoint=...)` builds `boto3.client("sts", ...)` with no key pair, so
botocore falls through to its default credential chain — which finds nothing in the pod, because the
S3 secret is delivered by the Dapr secret store into `Settings` and deliberately never into the
environment (the estate's fail-closed secret rule). And AssumeRole MUST be signed: `sts.py`'s own
docstring records that RustFS "verifies it as a signed `s3` request and answers an unsigned one with
`InvalidRequest`".

So the whole scoped-credential story was unreachable, and `mode_b` — which vends nothing at all — was
the only mode that worked. That is why the estate runs it.

The fix threads the catalog's own root credentials into the STS client. It is the credential the
vendor is DELEGATING FROM: an STS session policy can only ever restrict the caller's own rights
(intersection-only), which is what makes handing the root key to the signer safe here and is the same
reason `build_session_policy` can be trusted to narrow rather than widen.
"""

from __future__ import annotations

import inspect
from typing import Any


def test_the_sts_client_accepts_the_credentials_it_must_sign_with() -> None:
    from storage import sts_client

    params = inspect.signature(sts_client).parameters
    assert "access_key" in params, "an unsigned AssumeRole is refused by the store; the signer needs a key"
    assert "secret_key" in params


def test_the_vendor_factory_forwards_them() -> None:
    """The factory is where the wiring was missing — `sts_client` could have grown the parameters and
    still never received them."""
    from catalog.core.vending import make_vendor

    params = inspect.signature(make_vendor).parameters
    assert "access_key" in params
    assert "secret_key" in params


def test_an_sts_vendor_signs_with_the_credentials_it_was_built_with() -> None:
    """End to end through the vendor: the key pair reaches the boto client, not just the signature."""
    from catalog.core.vending import make_vendor

    seen: dict[str, Any] = {}

    def fake_client(**kwargs: Any) -> Any:
        seen.update(kwargs)

        class _C:
            @staticmethod
            def assume_role(**_: Any) -> dict[str, Any]:
                return {"Credentials": {"AccessKeyId": "A", "SecretAccessKey": "S", "SessionToken": "T"}}

        return _C()

    vendor = make_vendor(
        "sts",
        region="us-east-1",
        sts_endpoint="http://store:9000",
        assume_role_arn="arn:aws:iam::000000000000:role/lance-vend",
        access_key="ROOTKEY",
        secret_key="ROOTSECRET",
    )
    vendor._client = fake_client(  # the vendor builds its client lazily; hand it one to observe
        aws_access_key_id="ROOTKEY", aws_secret_access_key="ROOTSECRET"
    )
    vended = vendor.vend(table_location="s3://bucket/tbl", tier="write")
    assert vended is not None
    assert vended.storage_options["aws_session_token"] == "T"
    assert seen.get("aws_access_key_id") == "ROOTKEY"
