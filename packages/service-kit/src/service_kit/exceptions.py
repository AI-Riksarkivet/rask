"""The ONE domain exception hierarchy + RFC 9457 Problem Details handlers.

Routes raise these; never ``HTTPException`` directly. Each subclass maps to a status
code via the registered handler — one place to change the response shape.

Until this gate the estate carried **two** copies of this module: this one (bound into
``make_service_app`` for gateway/compute/controlplane, and never actually raised from
anywhere) and the media pair ``media.exceptions`` + ``media.handlers`` (raised
throughout viewer/search/annotator/lancekit). The ``_problem`` builder and both handler
bodies were identical; only the member set and the base class differed. They are now one
module — the unification ``service_kit.media.__init__`` flagged as "a later gate".

``DomainError`` subclasses :class:`fastapi.HTTPException` (the media lineage's choice, kept
because it is load-bearing): importing the exception class does NOT pull in the FastAPI
app/router/DI machinery, so non-web service/client modules import these freely, and a
service-layer raise stays catchable as an ``HTTPException`` while call sites read only the
domain class names.

Construct as ``ValidationError("stable detail msg")``; the message becomes ``exc.detail`` AND
``str(exc)`` (both the bare string — ``__str__`` is overridden so it is NOT Starlette's
``"400: msg"``). Keep the message STABLE; never interpolate a raw upstream exception into it
(log that instead).

The lakehouse services (catalog, lineage, medallion, compaction) deliberately use a DIFFERENT
taxonomy — the ``lance_namespace`` error hierarchy via
:func:`service_kit.lakehouse.ns_errors.install_problem_handlers` — because the Lance Namespace
REST spec pins a numeric ``code`` in the error body. That is an external contract, not drift.
"""

import logging
from http import HTTPStatus

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse


log = logging.getLogger(__name__)

#: The RFC 9457 media type every problem body is served with.
PROBLEM_JSON = "application/problem+json"


class DomainError(HTTPException):
    status_code: int = HTTPStatus.INTERNAL_SERVER_ERROR
    title: str = "Internal Server Error"

    def __init__(self, detail: str | None = None) -> None:
        # Use the class-level status_code (subclasses override it) and the stable message;
        # default the detail to the title so an argument-less raise still yields a
        # meaningful, stable problem body.
        super().__init__(status_code=self.status_code, detail=detail or self.title)

    def __str__(self) -> str:
        # Starlette's default __str__ is "<status>: <detail>"; we want the bare stable
        # message so _problem()'s `str(exc)` == the detail.
        return self.detail


class ValidationError(DomainError):
    status_code = HTTPStatus.BAD_REQUEST
    title = "Bad Request"


class UnauthorizedError(DomainError):
    status_code = HTTPStatus.UNAUTHORIZED
    title = "Unauthorized"


class ForbiddenError(DomainError):
    status_code = HTTPStatus.FORBIDDEN
    title = "Forbidden"


class NotFoundError(DomainError):
    status_code = HTTPStatus.NOT_FOUND
    title = "Not Found"


class ConflictError(DomainError):
    status_code = HTTPStatus.CONFLICT
    title = "Conflict"


class UnprocessableEntityError(DomainError):
    status_code = HTTPStatus.UNPROCESSABLE_ENTITY
    title = "Unprocessable Entity"


class ServiceUnavailableError(DomainError):
    status_code = HTTPStatus.SERVICE_UNAVAILABLE
    title = "Service Unavailable"


def _problem(exc: DomainError) -> dict[str, str | int]:
    return {
        "type": f"about:blank#{exc.__class__.__name__.lower()}",
        "title": exc.title,
        "status": exc.status_code,
        "detail": str(exc) or exc.title,
    }


def register_handlers(app: FastAPI) -> None:
    """Map every :class:`DomainError` and ``RequestValidationError`` to problem+json.

    Server-class errors (>=500) are logged with the traceback; client-class are not
    (they're expected).
    """

    @app.exception_handler(DomainError)
    async def _domain(_: Request, exc: DomainError) -> JSONResponse:
        if exc.status_code >= HTTPStatus.INTERNAL_SERVER_ERROR:
            log.exception("domain error", exc_info=exc)
        return JSONResponse(
            status_code=exc.status_code,
            content=_problem(exc),
            media_type=PROBLEM_JSON,
        )

    @app.exception_handler(RequestValidationError)
    async def _validation(_: Request, exc: RequestValidationError) -> JSONResponse:
        return JSONResponse(
            status_code=HTTPStatus.UNPROCESSABLE_ENTITY,
            media_type=PROBLEM_JSON,
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
