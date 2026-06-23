"""A generic boto3 S3 client (MinIO / AWS / Ceph / HCP) + optional HCP creds.

rask is storage-agnostic: the client below speaks only standard S3, so the
backend is a runtime choice (endpoint + credentials), never a code one.
`derive_hcp_creds` is an opt-in no-op kept for the one HCP deployment.
"""

import base64
import hashlib
import os
from typing import Any


# Public alias for the boto3 S3 client. boto3 ships no public stubs, so this
# resolves to `Any` at the type level; callers get a usable name without
# reaching past storage's boundary into `mypy_boto3_s3`.
type S3Client = Any

# Env names for the S3 endpoint/insecure/CA, canonical-first. HCP_* are the
# legacy names, honored for back-compat (mirrors the AliasChoices on Settings).
_ENDPOINT_ENVS = ("RASK_S3_ENDPOINT_URL", "S3_ENDPOINT_URL", "HCP_ENDPOINT")
_INSECURE_ENVS = ("RASK_S3_INSECURE", "S3_INSECURE", "HCP_INSECURE")
_CA_BUNDLE_ENVS = ("RASK_S3_CA_BUNDLE", "S3_CA_BUNDLE", "HCP_CA_BUNDLE")


def _env_first(names: tuple[str, ...]) -> str | None:
    """First non-empty value among `names` (canonical-first env resolution)."""
    for name in names:
        if value := os.getenv(name):
            return value
    return None


def derive_hcp_creds() -> None:
    """Opt-in HCP credential derivation; a no-op unless HCP_USERNAME/PASSWORD are set.

    For the one HCP deployment whose S3 keys follow the convention
    access_key = base64(username), secret_key = md5(password) hex. Never
    overrides already-set AWS_* keys, so MinIO/AWS (keys set directly) is untouched.
    """
    user = os.getenv("HCP_USERNAME")
    pwd = os.getenv("HCP_PASSWORD")
    if not (user and pwd):
        return
    if not os.getenv("AWS_ACCESS_KEY_ID"):
        os.environ["AWS_ACCESS_KEY_ID"] = base64.b64encode(user.encode()).decode()
    if not os.getenv("AWS_SECRET_ACCESS_KEY"):
        os.environ["AWS_SECRET_ACCESS_KEY"] = hashlib.md5(pwd.encode()).hexdigest()  # noqa: S324


def s3_client(endpoint: str | None = None) -> Any:  # noqa: ANN401 — boto3 client has no public stub
    """Build a boto3 S3 client for any S3-compatible backend (MinIO / AWS / Ceph / HCP).

    Endpoint/CA/insecure flags resolve from env (RASK_S3_*/S3_*, HCP_* honored for
    back-compat). Path-style + s3v4 are the MinIO-safe, AWS-accepted defaults; region
    comes from AWS_REGION so it isn't left solely to the boto3 env chain.
    """
    import boto3
    from botocore.config import Config

    cfg = Config(
        signature_version="s3v4",
        s3={"addressing_style": "path"},
        request_checksum_calculation="when_required",
        response_checksum_validation="when_required",
        retries={"max_attempts": 3, "mode": "adaptive"},
        connect_timeout=10,
        read_timeout=60,
    )
    kwargs: dict = {
        "endpoint_url": endpoint or _env_first(_ENDPOINT_ENVS),
        "region_name": os.getenv("AWS_REGION", "us-east-1"),
        "config": cfg,
    }
    if ca := _env_first(_CA_BUNDLE_ENVS):
        kwargs["verify"] = ca
    elif (_env_first(_INSECURE_ENVS) or "").lower() in ("1", "true", "yes"):
        import urllib3

        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
        kwargs["verify"] = False
    client = boto3.client("s3", **kwargs)
    client.meta.events.unregister("needs-retry.s3")
    return client
