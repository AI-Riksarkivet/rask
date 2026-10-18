"""STS clients for the token exchange — the estate's second boto3 client family.

STS is not S3, and this module shares nothing with :mod:`storage.client` but the rule that puts it
here: ``boto3`` is declared and imported by ``packages/storage`` alone, so every AWS-protocol client
the estate builds gets an explicit timeout and retry policy rather than botocore's defaults. Those
defaults carry NO connect or read timeout, and an STS exchange sits on the synchronous path of a
data-plane request — an endpoint that accepts the connection and never answers would hang the worker
instead of failing the vend.
"""

from typing import Any


# Public alias for the boto3 STS client, mirroring `storage.S3Client`: boto3 ships no public stubs,
# so this resolves to `Any` at the type level and callers get a name without reaching into
# `mypy_boto3_sts`.
type STSClient = Any


def sts_client(*, region: str, endpoint: str | None = None) -> Any:  # noqa: ANN401 — boto3 client has no public stub
    """Build an unsigned boto3 STS client for AWS or an S3-compatible store's STS endpoint (RustFS/Ceph).

    ``endpoint`` ``None`` leaves botocore to resolve the regional AWS endpoint; a self-hosted store
    passes its own. ``region`` is required rather than env-resolved because the caller exchanging a
    token always knows which store it is exchanging at, and a wrong region is a failure that reports
    itself as a credential problem.

    UNSIGNED, because the one call the estate makes is ``AssumeRoleWithWebIdentity`` (ADR 0170): the
    projected ServiceAccount token in the request body is the credential, and the caller holds no key
    to sign with. A signed client would look for a key in botocore's default chain and fail with
    ``NoCredentialsError`` in a pod that holds none.
    """
    import boto3
    from botocore import UNSIGNED
    from botocore.config import Config

    # `standard` rather than the S3 client's `adaptive`: adaptive adds client-side rate limiting sized
    # for object-store throttling, which an STS exchange neither provokes nor benefits from. The
    # timeouts are tight because the call is one small round trip on a request's critical path and a
    # vended credential's TTL (900s) leaves no value in waiting minutes for one.
    cfg = Config(
        connect_timeout=5,
        read_timeout=15,
        retries={"max_attempts": 3, "mode": "standard"},
        signature_version=UNSIGNED,
    )
    return boto3.client("sts", region_name=region, endpoint_url=endpoint, config=cfg)
