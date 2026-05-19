"""Tests for app.core.security JWT functions."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import jwt as pyjwt
import pytest
from fastapi import HTTPException

from app.core.config import AuthSettings
from app.core.security import (
    _decode_token,
    create_access_token,
    verify_token_with_credentials,
)


_TEST_KEY = "test-secret-key-for-unit-tests-min32b"


@pytest.fixture
def settings() -> AuthSettings:
    return AuthSettings(api_secret_key=_TEST_KEY, api_token_expire_minutes=60)


def test_create_access_token_returns_valid_jwt(settings: AuthSettings):
    token = create_access_token("alice", "secret", settings=settings)
    payload = pyjwt.decode(token, _TEST_KEY, algorithms=["HS256"])
    assert payload["sub"] == "alice"
    assert payload["pwd"] == "secret"
    assert "exp" in payload


def test_create_access_token_expiry_matches_settings(settings: AuthSettings):
    before = datetime.now(timezone.utc)
    token = create_access_token("alice", "secret", settings=settings)
    after = datetime.now(timezone.utc)

    payload = pyjwt.decode(token, _TEST_KEY, algorithms=["HS256"])
    exp = datetime.fromtimestamp(payload["exp"], tz=timezone.utc)

    assert exp >= before + timedelta(minutes=59)
    assert exp <= after + timedelta(minutes=61)


def test_decode_token_returns_username(settings: AuthSettings):
    token = create_access_token("alice", "secret", settings=settings)
    payload = _decode_token(token, settings=settings)
    assert payload["sub"] == "alice"


def test_verify_token_with_credentials(settings: AuthSettings):
    token = create_access_token("alice", "s3cr3t", settings=settings)
    creds = verify_token_with_credentials(token, settings=settings)
    assert creds.username == "alice"
    assert creds.password == "s3cr3t"


def test_decode_token_rejects_expired_token(settings: AuthSettings):
    expire = datetime.now(timezone.utc) - timedelta(minutes=1)
    payload = {"sub": "alice", "pwd": "secret", "exp": expire}
    token = pyjwt.encode(payload, _TEST_KEY, algorithm="HS256")

    with pytest.raises(HTTPException) as exc_info:
        _decode_token(token, settings=settings)
    assert exc_info.value.status_code == 401
    assert "expired" in exc_info.value.detail.lower()


def test_decode_token_rejects_invalid_signature(settings: AuthSettings):
    token = pyjwt.encode(
        {
            "sub": "alice",
            "pwd": "secret",
            "exp": datetime.now(timezone.utc) + timedelta(hours=1),
        },
        "wrong-key-that-is-at-least-32-bytes!",
        algorithm="HS256",
    )

    with pytest.raises(HTTPException) as exc_info:
        _decode_token(token, settings=settings)
    assert exc_info.value.status_code == 401


def test_decode_token_rejects_missing_subject(settings: AuthSettings):
    payload = {"pwd": "secret", "exp": datetime.now(timezone.utc) + timedelta(hours=1)}
    token = pyjwt.encode(payload, _TEST_KEY, algorithm="HS256")

    with pytest.raises(HTTPException) as exc_info:
        _decode_token(token, settings=settings)
    assert exc_info.value.status_code == 401
    assert "no subject" in exc_info.value.detail.lower()


def test_decode_token_rejects_garbage():
    with pytest.raises(HTTPException) as exc_info:
        _decode_token("not.a.jwt")
    assert exc_info.value.status_code == 401


# ── Tenant in JWT ────────────────────────────────────────────────────


def test_create_access_token_with_tenant(settings: AuthSettings):
    token = create_access_token("alice", "secret", tenant="dev-ai", settings=settings)
    payload = pyjwt.decode(token, _TEST_KEY, algorithms=["HS256"])
    assert payload["tenant"] == "dev-ai"


def test_create_access_token_without_tenant_omits_claim(settings: AuthSettings):
    token = create_access_token("alice", "secret", settings=settings)
    payload = pyjwt.decode(token, _TEST_KEY, algorithms=["HS256"])
    assert "tenant" not in payload


def test_verify_credentials_with_tenant(settings: AuthSettings):
    token = create_access_token("alice", "s3cr3t", tenant="dev-ai", settings=settings)
    creds = verify_token_with_credentials(token, settings=settings)
    assert creds.username == "alice"
    assert creds.password == "s3cr3t"
    assert creds.tenant == "dev-ai"


def test_verify_credentials_without_tenant(settings: AuthSettings):
    token = create_access_token("alice", "s3cr3t", settings=settings)
    creds = verify_token_with_credentials(token, settings=settings)
    assert creds.tenant is None
