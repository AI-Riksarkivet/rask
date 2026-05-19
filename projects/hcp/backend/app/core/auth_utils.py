"""Shared HCP authentication and credential utilities.

Centralises the base64/MD5 credential derivation used by MAPI, S3,
and the Metadata Query API so there is a single source of truth.
"""

from __future__ import annotations

import base64
import hashlib


def build_hcp_auth_token(username: str, password: str) -> str:
    """Build HCP authentication token: ``base64(user):md5(password)``."""
    user_b64 = base64.b64encode(username.encode()).decode()
    pass_md5 = hashlib.md5(password.encode()).hexdigest()  # noqa: S324 — HCP protocol requires MD5
    return f"{user_b64}:{pass_md5}"


def get_hcp_auth_header(
    username: str,
    password: str,
    auth_type: str = "hcp",
) -> str:
    """Return the ``Authorization`` header value for an HCP request."""
    if auth_type == "ad":
        return f"AD {username}:{password}"
    return f"HCP {build_hcp_auth_token(username, password)}"


def derive_s3_keys(username: str, password: str) -> tuple[str, str]:
    """Derive HCP S3 access_key / secret_key from plain credentials.

    - **access_key** = base64(username)
    - **secret_key** = md5(password)
    """
    access_key = base64.b64encode(username.encode()).decode()
    secret_key = hashlib.md5(password.encode()).hexdigest()  # noqa: S324 — HCP protocol requires MD5
    return access_key, secret_key
