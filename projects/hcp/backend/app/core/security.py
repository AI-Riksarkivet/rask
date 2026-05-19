"""OAuth2 password flow with JWT bearer tokens."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Annotated, NamedTuple

import jwt
from fastapi import Depends, HTTPException, status
from fastapi.security import OAuth2PasswordBearer

from app.core.config import AuthSettings

logger = logging.getLogger(__name__)

oauth2_scheme = OAuth2PasswordBearer(
    tokenUrl="api/v1/auth/token",
    description=(
        "Enter your HCP credentials.\n\n"
        "**Username formats:**\n"
        "- `username` — system-level access (no tenant)\n"
        "- `tenant/username` — tenant-scoped access "
        "(e.g. `dev-ai/admin`)\n\n"
        "**Password:** your HCP password."
    ),
)


class HcpCredentials(NamedTuple):
    """Username/password pair extracted from a JWT."""

    username: str
    password: str
    tenant: str | None = None


def _get_auth_settings() -> AuthSettings:
    return AuthSettings()


def create_access_token(
    username: str,
    password: str,
    *,
    tenant: str | None = None,
    settings: AuthSettings | None = None,
) -> str:
    """Create a JWT access token containing the user's credentials."""
    if settings is None:
        settings = _get_auth_settings()
    expire = datetime.now(timezone.utc) + timedelta(
        minutes=settings.api_token_expire_minutes
    )
    payload: dict = {"sub": username, "pwd": password, "exp": expire}
    if tenant:
        payload["tenant"] = tenant
    return jwt.encode(payload, settings.api_secret_key, algorithm="HS256")


def _decode_token(token: str, settings: AuthSettings | None = None) -> dict:
    """Decode and validate a JWT, returning the full payload."""
    if settings is None:
        settings = _get_auth_settings()
    try:
        payload = jwt.decode(token, settings.api_secret_key, algorithms=["HS256"])
        if payload.get("sub") is None:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid token: no subject",
                headers={"WWW-Authenticate": "Bearer"},
            )
        return payload
    except jwt.ExpiredSignatureError:
        logger.warning("Auth failed: expired token")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token has expired",
            headers={"WWW-Authenticate": "Bearer"},
        )
    except jwt.InvalidTokenError:
        logger.warning("Auth failed: invalid token")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid token",
            headers={"WWW-Authenticate": "Bearer"},
        )


def verify_token_with_credentials(
    token: str, settings: AuthSettings | None = None
) -> HcpCredentials:
    """Verify a JWT token and return the full credential pair."""
    payload = _decode_token(token, settings)
    return HcpCredentials(
        username=payload["sub"],
        password=payload.get("pwd", ""),
        tenant=payload.get("tenant"),
    )


async def get_current_user(
    token: Annotated[str, Depends(oauth2_scheme)],
) -> str:
    """Dependency that extracts and validates the current user from JWT."""
    return _decode_token(token)["sub"]
