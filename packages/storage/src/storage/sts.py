"""STS clients for credential vending — the estate's second boto3 client family.

STS is not S3, and this module shares nothing with :mod:`storage.client` but the rule that puts it
here: ``boto3`` is declared and imported by ``packages/storage`` alone, so every AWS-protocol client
the estate builds gets an explicit timeout and retry policy rather than botocore's defaults. Those
defaults carry NO connect or read timeout, and an STS vend sits on the synchronous path of a
data-plane request — an endpoint that accepts the connection and never answers would hang the worker
instead of failing the vend.
"""

from typing import Any


# Public alias for the boto3 STS client, mirroring `storage.S3Client`: boto3 ships no public stubs,
# so this resolves to `Any` at the type level and callers get a name without reaching into
# `mypy_boto3_sts`.
type STSClient = Any


def sts_client(*, region: str, endpoint: str | None = None, unsigned: bool = False, access_key: str | None = None, secret_key: str | None = None) -> Any:  # noqa: ANN401 — boto3 client has no public stub
    """Build a boto3 STS client for AWS or an S3-compatible store's STS endpoint (RustFS/MinIO/Ceph).

    ``endpoint`` ``None`` leaves botocore to resolve the regional AWS endpoint; a self-hosted store
    passes its own. ``region`` is required rather than env-resolved because the caller vending a
    credential always knows which store it is vending for, and a wrong region is a signature failure
    that reports itself as a credential problem.

    ``unsigned`` drops SigV4. ``AssumeRoleWithWebIdentity`` is authenticated by the JWT in the request
    body, and the caller making that exchange holds no key to sign with; ``AssumeRole`` is the
    opposite — RustFS verifies it as a signed ``s3`` request and answers an unsigned one with
    ``InvalidRequest`` — so signing stays the default and unsigned is opted into by name.

    ``access_key``/``secret_key`` are the credential the caller is DELEGATING FROM, and a signed call
    needs them EXPLICITLY: botocore's default chain reads the environment, and this estate delivers its
    S3 secret through the Dapr secret store into ``Settings`` and deliberately never into the process
    env (the fail-closed secret rule). Omitting them therefore does not fall back to anything — it
    raises ``NoCredentialsError``, which is what made ``sts`` vending mode unreachable in practice
    (measured 2026-09-03: the vending door answered 503). Handing the root key to the signer is safe
    because an STS session policy is intersection-only: it can restrict the caller's rights, never
    widen them.
    """
    import boto3
    from botocore.config import Config

    # `standard` rather than the S3 client's `adaptive`: adaptive adds client-side rate limiting sized
    # for object-store throttling, which an AssumeRole call neither provokes nor benefits from. The
    # timeouts are tight because the call is one small round trip on a request's critical path and a
    # vended credential's TTL (900s) leaves no value in waiting minutes for one.
    cfg = Config(
        connect_timeout=5,
        read_timeout=15,
        retries={"max_attempts": 3, "mode": "standard"},
    )
    if unsigned:
        from botocore import UNSIGNED

        cfg = cfg.merge(Config(signature_version=UNSIGNED))
    return boto3.client(
        "sts",
        region_name=region,
        endpoint_url=endpoint,
        config=cfg,
        aws_access_key_id=access_key,
        aws_secret_access_key=secret_key,
    )
