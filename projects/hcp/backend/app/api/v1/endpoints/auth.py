"""Authentication endpoint — OAuth2 password flow."""

from __future__ import annotations

import logging
import re

from fastapi import APIRouter, Depends, Form, HTTPException, Request, status
from fastapi.security import OAuth2PasswordRequestForm

from app.core.security import create_access_token
from app.schemas.common import TokenResponse

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/auth", tags=["Authentication"])

# Valid HCP tenant names: alphanumeric, may contain hyphens (not leading/trailing).
_TENANT_RE = re.compile(r"^[a-zA-Z0-9]([a-zA-Z0-9-]*[a-zA-Z0-9])?$")


def _parse_username(raw_username: str) -> tuple[str, str | None]:
    """Parse ``tenant/username`` or plain ``username``.

    The Swagger Authorize dialog only shows standard OAuth2 fields
    (username, password).  To include a tenant, use the format
    ``tenant/username`` in the username field.
    """
    if "/" in raw_username:
        tenant, _, username = raw_username.partition("/")
        return username, tenant.strip() or None
    return raw_username, None


@router.post("/token", response_model=TokenResponse)
async def login(
    request: Request,
    form_data: OAuth2PasswordRequestForm = Depends(),
    tenant: str | None = Form(None),
):
    """Authenticate with HCP credentials and receive a JWT bearer token.

    Credentials are stored in the JWT and passed through to HCP on every
    API call.  HCP validates them — login itself always succeeds.

    **Tenant** can be provided in three ways (first match wins):

    1. **``tenant`` form field** — set it directly (used by the frontend).
    2. **``tenant/username`` format** — enter ``acc-ai/my_user`` in the
       username field.  Works in the Swagger **Authorize** dialog (lock icon).
    3. **Omit** — for system-level access (no tenant routing).
    """
    # Explicit tenant field takes priority
    resolved_tenant = tenant.strip() if tenant and tenant.strip() else None

    username = form_data.username
    if resolved_tenant is None:
        username, resolved_tenant = _parse_username(form_data.username)

    client_ip = request.client.host if request.client else None

    if resolved_tenant is not None and not _TENANT_RE.match(resolved_tenant):
        logger.warning(
            "Login failed: invalid tenant name %r user=%s ip=%s",
            resolved_tenant,
            username,
            client_ip,
        )
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="Invalid tenant name",
        )
    token = create_access_token(username, form_data.password, tenant=resolved_tenant)
    logger.info(
        "Login success: user=%s tenant=%s ip=%s",
        username,
        resolved_tenant or "(system)",
        client_ip,
    )
    return {"access_token": token, "token_type": "bearer"}
