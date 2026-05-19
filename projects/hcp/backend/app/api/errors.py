"""Common response helpers for route handlers.

Translates domain exceptions (StorageError, MapiError) into
FastAPI HTTPException responses.  This is the only place where
service-layer errors meet the web framework.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import HTTPException
import httpx

from app.services.mapi_errors import MapiError

logger = logging.getLogger(__name__)


def raise_for_hcp_status(resp: httpx.Response, resource: str = "resource") -> None:
    """Translate HCP status codes into FastAPI HTTP exceptions.

    Delegates to the service-layer ``raise_for_hcp_status`` which raises
    ``MapiResponseError``, then catches and re-raises as ``HTTPException``.
    This keeps the MAPI endpoint code that directly inspects responses
    working unchanged.
    """
    from app.services.mapi_service import (
        raise_for_hcp_status as _svc_raise,
    )
    from app.services.mapi_errors import MapiResponseError

    try:
        _svc_raise(resp, resource)
    except MapiResponseError as exc:
        raise HTTPException(status_code=exc.http_status, detail=exc.message)


def parse_json_response(resp: httpx.Response) -> dict:
    """Parse JSON response body, returning empty dict on empty body."""
    if 200 <= resp.status_code < 300 and resp.content:
        try:
            return resp.json()
        except (ValueError, TypeError):
            return {}
    return {}


async def run_storage(
    coro: Any,
    resource: str,
) -> Any:
    """Await an async storage operation with StorageError handling.

    This is the backend-agnostic version. Storage adapters raise
    StorageError, which is translated to HTTPException here.

    Usage::

        result = await run_storage(s3.list_buckets(), "buckets")
    """
    from app.services.storage.errors import StorageError

    try:
        return await coro
    except StorageError as exc:
        raise HTTPException(
            status_code=exc.http_status, detail=f"{resource}: {exc.message}"
        )


async def run_mapi(
    coro,
    resource: str = "resource",
) -> Any:
    """Await a MAPI coroutine, translating MapiError → HTTPException.

    Usage::

        resp = await run_mapi(mapi.get("/tenants", ...), "tenants")
    """
    try:
        return await coro
    except MapiError as exc:
        raise HTTPException(
            status_code=exc.http_status, detail=f"{resource}: {exc.message}"
        )
