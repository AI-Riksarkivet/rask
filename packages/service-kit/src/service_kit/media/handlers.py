"""RFC 9457 problem+json error handlers.

``register_handlers(app)`` is the ONE place that owns the error response shape:
it maps every :class:`~service_kit.media.exceptions.DomainError` and FastAPI's
``RequestValidationError`` to ``application/problem+json``. Server-class errors
(>=500) are logged with the traceback; client-class are not (they're expected).
"""

from __future__ import annotations

import logging
from http import HTTPStatus

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from service_kit.media.exceptions import DomainError


logger = logging.getLogger(__name__)


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
            logger.exception("domain error", exc_info=exc)
        return JSONResponse(
            status_code=exc.status_code,
            content=_problem(exc),
            media_type="application/problem+json",
        )

    @app.exception_handler(RequestValidationError)
    async def _validation(_: Request, exc: RequestValidationError) -> JSONResponse:
        return JSONResponse(
            status_code=HTTPStatus.UNPROCESSABLE_ENTITY,
            media_type="application/problem+json",
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
        )
