"""HCP credential derivation + boto3 S3 client tuned for HCP."""

import base64
import hashlib
import os
from typing import Any


def derive_hcp_creds() -> None:
    """If HCP_USERNAME/HCP_PASSWORD are set and AWS_* are not, derive S3 creds.

    HCP S3 convention: access_key = base64(username), secret_key = md5(password) hex.
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
    """Build a boto3 S3 client tuned for HCP. Reads endpoint/CA/insecure flags from env."""
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
    kwargs: dict = {"endpoint_url": endpoint or os.getenv("HCP_ENDPOINT"), "config": cfg}
    if ca := os.getenv("HCP_CA_BUNDLE"):
        kwargs["verify"] = ca
    elif os.getenv("HCP_INSECURE", "").lower() in ("1", "true", "yes"):
        import urllib3

        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
        kwargs["verify"] = False
    client = boto3.client("s3", **kwargs)
    client.meta.events.unregister("needs-retry.s3")
    return client
