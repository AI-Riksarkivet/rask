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
(log that instead). A refusal whose substance does not fit one string carries it as RFC 9457 §3.2
EXTENSION MEMBERS — ``raise Refused("2 problem(s)", extensions={"problems": [...]})`` — and a
subclass that wants to NAME its failure sets ``problem_type``. Both exist so a domain never has to
build a problem body beside this module: that is what one route doing it cost
(``FLOWS-422-BYPASSES-HIERARCHY``).

The lakehouse services (catalog, lineage, medallion, compaction) deliberately use a DIFFERENT
taxonomy — the ``lance_namespace`` error hierarchy via
:func:`service_kit.lakehouse.ns_errors.install_problem_handlers` — because the Lance Namespace
REST spec pins a numeric ``code`` in the error body. That is an external contract, not drift.
"""

import logging
from collections.abc import Mapping
from http import HTTPStatus
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse


log = logging.getLogger(__name__)

#: The RFC 9457 media type every problem body is served with.
PROBLEM_JSON = "application/problem+json"


class DomainError(HTTPException):
    status_code: int = HTTPStatus.INTERNAL_SERVER_ERROR
    title: str = "Internal Server Error"

    #: The RFC 9457 `type` URI. `None` means "derive it from the class name", which is what every
    #: refusal in the estate answered before this attribute existed. A subclass sets it when the
    #: DOMAIN has a better name for the failure than the generic status class does — a flow whose
    #: graph does not validate is `about:blank#flow-invalid`, not `#unprocessableentityerror`, and a
    #: client dispatching on `type` wants the specific one. Naming the failure is the taxonomy
    #: working, not a departure from it.
    problem_type: str | None = None

    def __init__(
        self,
        detail: str | None = None,
        *,
        headers: dict[str, str] | None = None,
        extensions: Mapping[str, Any] | None = None,
    ) -> None:
        # Use the class-level status_code (subclasses override it) and the stable message;
        # default the detail to the title so an argument-less raise still yields a
        # meaningful, stable problem body.
        #
        # `headers` exists because `Retry-After` is part of what a 503 or a 429 MEANS — a refusal
        # that cannot say when to come back is a refusal the caller can only guess at. It was
        # missing, and the failure mode was nasty: `HTTPException` accepts headers, this did not
        # forward them, so writing the correct thing raised a TypeError AT RAISE TIME and the 503
        # became a 500. Invisible until the refusal path runs, which for a saturation refusal is
        # precisely when nobody is watching. Keyword-only and optional, so every existing raise site
        # is unchanged.
        #
        # `extensions` are RFC 9457 §3.2 EXTENSION MEMBERS — additional keys on the problem body,
        # which the spec explicitly allows and tells consumers to ignore when unrecognised. Without
        # them a refusal whose substance is STRUCTURED had no way to use this hierarchy at all:
        # `_problem` flattened everything to one `detail` string, so `flows.create_run` hand-built
        # its own body and declared `-> RunState | JSONResponse`, an escape hatch around the estate's
        # single error plane (FLOWS-422-BYPASSES-HIERARCHY). They can never shadow a standard member
        # — see `_problem`.
        super().__init__(status_code=self.status_code, detail=detail or self.title, headers=headers)
        self.extensions: dict[str, Any] = dict(extensions or {})

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


def _problem(exc: DomainError) -> dict[str, Any]:
    body: dict[str, Any] = {
        "type": exc.problem_type or f"about:blank#{exc.__class__.__name__.lower()}",
        "title": exc.title,
        "status": exc.status_code,
        "detail": str(exc) or exc.title,
    }
    # STANDARD MEMBERS FIRST, and extensions may not overwrite them. `type`, `title`, `status` and
    # `detail` are the four keys every client in the estate parses; an extension that could replace
    # one would let a refusal claim a status it does not have. RFC 9457 §3.2 permits the additional
    # members, not a redefinition of the ones it standardises.
    for key, value in exc.extensions.items():
        body.setdefault(key, value)
    return body


def register_handlers(app: FastAPI) -> None:
    """Map every :class:`DomainError` and ``RequestValidationError`` to problem+json.

    Server-class errors (>=500) are logged with the traceback; client-class are not
    (they're expected).
    """

    @app.exception_handler(DomainError)
    async def _domain(_: Request, exc: DomainError) -> JSONResponse:
        if exc.status_code >= HTTPStatus.INTERNAL_SERVER_ERROR:
            log.exception("domain error", exc_info=exc)
        # `headers=` is not optional politeness. This handler builds its own response, so a
        # `Retry-After` set at the raise site was dropped here silently — the exception carried it
        # and the client never saw it. Both halves of that are independently invisible: without the
        # constructor change the raise TypeErrors, without this one the refusal simply says nothing.
        # `exc.headers` is None for every existing raise site, which JSONResponse accepts.
        return JSONResponse(
            status_code=exc.status_code,
            content=_problem(exc),
            media_type=PROBLEM_JSON,
            headers=exc.headers,
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

    @app.exception_handler(Exception)
    async def _unexpected(request: Request, exc: Exception) -> JSONResponse:
        """The CATCH-ALL. Without it anything that is not a `DomainError` fell through to starlette's
        default — a `text/plain` 500 reading "Internal Server Error" — which is a third error envelope
        on services whose every other answer is RFC 9457, and one no client parsing problem+json can
        read.

        A FIXED body, never the exception. The reference is explicit that internals reach logs only:
        the traceback goes to `log.exception` (inside the active OTel span), and the caller gets a
        stable title and detail. Leaking `str(exc)` here would put native/Arrow/S3 error text and
        filesystem paths on the wire for any unhandled fault.
        """
        log.exception("unhandled error", extra={"method": request.method, "path": request.url.path})
        return JSONResponse(
            status_code=HTTPStatus.INTERNAL_SERVER_ERROR,
            media_type=PROBLEM_JSON,
            content={
                "type": "about:blank#internalerror",
                "title": "Internal Server Error",
                "status": HTTPStatus.INTERNAL_SERVER_ERROR,
                "detail": "Internal Server Error",
            },
        )
