"""Domain exception hierarchy + RFC 9457 Problem Details handlers.

Routes raise these; never `HTTPException` directly. Each subclass maps to a
status code via the registered handler — one place to change the response shape.
"""

import logging
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse


log = logging.getLogger(__name__)


class DomainError(Exception):
    status_code = 500
    title = "Internal Server Error"


class NotFoundError(DomainError):
    status_code = 404
    title = "Not Found"


class ValidationError(DomainError):
    status_code = 400
    title = "Bad Request"


class ServiceUnavailableError(DomainError):
    status_code = 503
    title = "Service Unavailable"


class UpstreamUnavailableError(DomainError):
    status_code = 502
    title = "Bad Gateway"


class UpstreamTimeoutError(DomainError):
    """504 — use for `asyncio.TimeoutError` / `httpx.TimeoutException` boundaries."""

    status_code = 504
    title = "Gateway Timeout"


def _problem(exc: DomainError) -> dict[str, Any]:
    return {
        "type": f"about:blank#{exc.__class__.__name__.lower()}",
        "title": exc.title,
        "status": exc.status_code,
        "detail": str(exc) or exc.title,
    }


def register_handlers(app: FastAPI) -> None:
    @app.exception_handler(DomainError)
    async def _domain(_: Request, exc: DomainError) -> JSONResponse:
        if exc.status_code >= 500:
            log.exception("domain error", exc_info=exc)
        return JSONResponse(
            status_code=exc.status_code,
            content=_problem(exc),
            media_type="application/problem+json",
        )

    @app.exception_handler(RequestValidationError)
    async def _validation(_: Request, exc: RequestValidationError) -> JSONResponse:
        return JSONResponse(
            status_code=422,
            content={
                "type": "about:blank#validation",
                "title": "Validation Error",
                "status": 422,
                "errors": [
                    {
                        "field": ".".join(str(p) for p in e["loc"]),
                        "message": e["msg"],
                        "type": e["type"],
                    }
                    for e in exc.errors()
                ],
            },
            media_type="application/problem+json",
        )
