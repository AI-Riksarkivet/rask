"""Mock development server — run the real FastAPI app with in-memory fakes.

Start with:  uvicorn mock_server:app --reload          (port 8000)
   or:       uvicorn mock_server:app --reload --port 9000
Login with:  username=admin  password=password

All S3 operations are backed by in-memory dicts (stateful within the session).
All MAPI calls are intercepted by respx and return stateful fixture data.
"""

from __future__ import annotations

import io
import logging
from contextlib import asynccontextmanager
from unittest.mock import patch

import respx
from fastapi import Request
from fastapi.responses import StreamingResponse

from app.api.dependencies import (
    get_mapi_service,
    get_mapi_settings,
    get_query_service,
    get_s3_service,
)
from app.core.config import AuthSettings, MapiSettings
from app.main import app
from app.services.mapi_service import AuthenticatedMapiService, MapiService
from app.services.query_service import QueryService

from .mapi_state import MockMapiState, seed_mapi_state, setup_mapi_routes
from .s3_service import MockS3Service, seed_s3

logger = logging.getLogger("mock_server")

# ── Mock settings ────────────────────────────────────────────────────

MOCK_MAPI_SETTINGS = MapiSettings(
    hcp_host="mock.hcp.example.com",
    hcp_port=9090,
    hcp_domain="hcp.example.com",
    hcp_username="admin",
    hcp_password="password",
    hcp_auth_type="hcp",
    hcp_verify_ssl=False,
    hcp_timeout=30,
)

MOCK_AUTH_SETTINGS = AuthSettings(
    api_secret_key="mock-dev-secret-key-minimum-32-bytes!",
    api_token_expire_minutes=480,
)

HCP_BASE = f"https://{MOCK_MAPI_SETTINGS.hcp_host}:{MOCK_MAPI_SETTINGS.hcp_port}/mapi"


# ── Lifespan ─────────────────────────────────────────────────────────


@asynccontextmanager
async def _mock_lifespan(app_instance):
    """Wrap the FastAPI app with mocked services for development."""
    mock_s3 = MockS3Service()
    state = MockMapiState()

    # Cross-link so namespace ↔ bucket stay in sync
    mock_s3._mapi_state = state
    mock_s3._default_tenant = "mock"
    state._s3_service = mock_s3

    seed_s3(mock_s3)
    seed_mapi_state(state)

    mapi_svc = MapiService(MOCK_MAPI_SETTINGS)
    auth_mapi_svc = AuthenticatedMapiService(
        mapi_svc,
        MOCK_MAPI_SETTINGS.hcp_username,
        MOCK_MAPI_SETTINGS.hcp_password,
    )
    query_svc = QueryService(MOCK_MAPI_SETTINGS)

    async def _override_s3():
        yield mock_s3

    async def _override_mapi():
        yield auth_mapi_svc

    async def _override_query():
        yield query_svc

    def _override_mapi_settings():
        return MOCK_MAPI_SETTINGS

    app_instance.dependency_overrides[get_s3_service] = _override_s3
    app_instance.dependency_overrides[get_mapi_service] = _override_mapi
    app_instance.dependency_overrides[get_query_service] = _override_query
    app_instance.dependency_overrides[get_mapi_settings] = _override_mapi_settings
    app_instance.state.mock_s3 = mock_s3

    # IIIF singleton (uses real IIIF server — respx passes unmatched through)
    from app.core.config import CacheSettings
    from app.services.cached_iiif import CachedIiifService
    from app.services.iiif_service import IiifService
    from app.services.kv import KVCache
    from key_value.aio.stores.null import NullStore

    iiif_inner = IiifService()
    app_instance.state.iiif = CachedIiifService(
        iiif_inner, KVCache(NullStore(), enabled=False), CacheSettings()
    )

    with (
        respx.mock(assert_all_mocked=False, assert_all_called=False) as mock,
        patch("app.core.security._get_auth_settings", return_value=MOCK_AUTH_SETTINGS),
    ):
        setup_mapi_routes(mock, state, HCP_BASE)
        logger.info("Mock MAPI routes registered at %s", HCP_BASE)
        logger.info("Seeded S3 with buckets: %s", list(mock_s3._buckets.keys()))
        logger.info("Login with username=admin password=password")
        yield

    await app_instance.state.iiif.close()
    await query_svc.close()
    await mapi_svc.close()
    app_instance.dependency_overrides.clear()


# ── Public presigned download (no auth) ──────────────────────────────


@app.get("/presigned/{bucket}/{key:path}")
async def presigned_download(bucket: str, key: str, request: Request):
    """Serve mock presigned downloads without authentication."""
    mock_s3: MockS3Service | None = getattr(request.app.state, "mock_s3", None)
    if mock_s3 is None:
        return StreamingResponse(
            content=iter([b"Mock server not initialised"]),
            status_code=503,
        )
    try:
        result = await mock_s3.get_object(bucket, key)
    except Exception:
        return StreamingResponse(
            content=iter([b"Object not found"]),
            status_code=404,
        )
    body: io.BytesIO = result["Body"]
    return StreamingResponse(
        content=body.iter_chunks(),
        media_type=result.get("ContentType", "application/octet-stream"),
        headers={
            "Content-Length": str(result.get("ContentLength", "")),
            "ETag": result.get("ETag", ""),
        },
    )


# Replace the app's lifespan with the mock lifespan
app.router.lifespan_context = _mock_lifespan
