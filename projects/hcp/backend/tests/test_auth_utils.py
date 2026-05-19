"""Tests for app.core.auth_utils credential derivation functions."""

from __future__ import annotations

import base64
import hashlib

import pytest

from app.core.auth_utils import (
    build_hcp_auth_token,
    derive_s3_keys,
    get_hcp_auth_header,
)


# ── build_hcp_auth_token ────────────────────────────────────────────


def test_build_hcp_auth_token_format():
    token = build_hcp_auth_token("admin", "secret")
    user_part, pass_part = token.split(":")
    assert user_part == base64.b64encode(b"admin").decode()
    assert pass_part == hashlib.md5(b"secret").hexdigest()


def test_build_hcp_auth_token_special_characters():
    token = build_hcp_auth_token("user@domain.com", "p@ss!w0rd#")
    user_part, pass_part = token.split(":")
    assert user_part == base64.b64encode(b"user@domain.com").decode()
    assert pass_part == hashlib.md5(b"p@ss!w0rd#").hexdigest()


def test_build_hcp_auth_token_empty_password():
    token = build_hcp_auth_token("admin", "")
    _, pass_part = token.split(":")
    assert pass_part == hashlib.md5(b"").hexdigest()


# ── get_hcp_auth_header ─────────────────────────────────────────────


def test_get_hcp_auth_header_default_hcp_type():
    header = get_hcp_auth_header("admin", "secret")
    assert header.startswith("HCP ")
    token = header[4:]
    assert token == build_hcp_auth_token("admin", "secret")


def test_get_hcp_auth_header_ad_type():
    header = get_hcp_auth_header("admin", "secret", auth_type="ad")
    assert header == "AD admin:secret"


def test_get_hcp_auth_header_ad_preserves_raw_credentials():
    """AD auth sends raw credentials, not base64/md5."""
    header = get_hcp_auth_header("user@corp.com", "MyP@ss", auth_type="ad")
    assert header == "AD user@corp.com:MyP@ss"


# ── derive_s3_keys ──────────────────────────────────────────────────


def test_derive_s3_keys_returns_tuple():
    access_key, secret_key = derive_s3_keys("admin", "secret")
    assert isinstance(access_key, str)
    assert isinstance(secret_key, str)


def test_derive_s3_keys_access_key_is_base64_username():
    access_key, _ = derive_s3_keys("admin", "secret")
    assert access_key == base64.b64encode(b"admin").decode()


def test_derive_s3_keys_secret_key_is_md5_password():
    _, secret_key = derive_s3_keys("admin", "secret")
    assert secret_key == hashlib.md5(b"secret").hexdigest()


def test_derive_s3_keys_matches_build_hcp_auth_token():
    """derive_s3_keys and build_hcp_auth_token use the same derivation."""
    access_key, secret_key = derive_s3_keys("testuser", "testpass")
    token = build_hcp_auth_token("testuser", "testpass")
    assert token == f"{access_key}:{secret_key}"


@pytest.mark.parametrize(
    "username,password",
    [
        ("admin", "secret"),
        ("user@domain.com", "p@ss!w0rd"),
        ("", "empty-user"),
        ("empty-pass", ""),
    ],
)
def test_derive_s3_keys_deterministic(username: str, password: str):
    """Same inputs always produce same outputs."""
    keys1 = derive_s3_keys(username, password)
    keys2 = derive_s3_keys(username, password)
    assert keys1 == keys2
