"""Domain exception hierarchy + RFC 9457 Problem Details handlers.

Routes raise these; never `HTTPException` directly. Each subclass maps to a
status code via the registered handler — one place to change the response shape.
"""

import logging
from http import HTTPStatus

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse


log = logging.getLogger(__name__)


class DomainError(Exception):
    status_code: int = HTTPStatus.INTERNAL_SERVER_ERROR
    title: str = "Internal Server Error"


class NotFoundError(DomainError):
    status_code = HTTPStatus.NOT_FOUND
    title = "Not Found"


class ValidationError(DomainError):
    status_code = HTTPStatus.BAD_REQUEST
    title = "Bad Request"


class ServiceUnavailableError(DomainError):
    status_code = HTTPStatus.SERVICE_UNAVAILABLE
    title = "Service Unavailable"


class UpstreamUnavailableError(DomainError):
    status_code = HTTPStatus.BAD_GATEWAY
    title = "Bad Gateway"


class UpstreamTimeoutError(DomainError):
    """504 — use for `asyncio.TimeoutError` / `httpx.TimeoutException` boundaries."""

    status_code = HTTPStatus.GATEWAY_TIMEOUT
    title = "Gateway Timeout"


def _problem(exc: DomainError) -> dict[str, str | int]:
    return {
        "type": f"about:blank#{exc.__class__.__name__.lower()}",
        "title": exc.title,
        "status": exc.status_code,
        "detail": str(exc) or exc.title,
    }


def register_handlers(app: FastAPI) -> None:
    @app.exception_handler(DomainError)
    async def _domain(_: Request, exc: DomainError) -> JSONResponse:
        if exc.status_code >= HTTPStatus.INTERNAL_SERVER_ERROR:
            log.exception("domain error", exc_info=exc)
        return JSONResponse(
            status_code=exc.status_code,
            content=_problem(exc),
            media_type="application/problem+json",
        )

    @app.exception_handler(RequestValidationError)
    async def _validation(_: Request, exc: RequestValidationError) -> JSONResponse:
        return JSONResponse(
            status_code=HTTPStatus.UNPROCESSABLE_ENTITY,
            content={
                "type": "about:blank#validation",
                "title": "Validation Error",
                "status": HTTPStatus.UNPROCESSABLE_ENTITY,
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
