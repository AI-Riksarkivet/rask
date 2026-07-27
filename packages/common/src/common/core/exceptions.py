"""Domain exceptions — raised by routes AND service/clients layers.

These subclass :class:`fastapi.HTTPException` (itself a thin subclass of
Starlette's): importing this exception class does NOT pull in the FastAPI
app/router/DI machinery — it is just the exception type — so the non-web
service/clients modules import these freely. Basing on ``fastapi.HTTPException``
is load-bearing: the unmodifiable ``tests/test_backend_service.py`` catches
these via ``pytest.raises(HTTPException)`` (imported ``from fastapi``) and reads
``.status_code`` / ``.detail``. So the service layer raises a *domain* exception
that stays catchable as an ``HTTPException`` while the call sites read only the
domain class names.

Each class carries a ``status_code`` (drives the HTTP status) and a ``title``
(RFC 9457 title). Construct as ``ValidationError("stable detail msg")``; the
message becomes ``exc.detail`` AND ``str(exc)`` (both the stable string — we
override ``__str__`` so it is NOT the Starlette default ``"400: msg"``). Keep the
message STABLE; never interpolate a raw upstream exception into it (log that
instead). The handler that turns these into problem+json responses lives in
:mod:`common.core.handlers`.
"""

from __future__ import annotations

from http import HTTPStatus

from fastapi import HTTPException


class DomainError(HTTPException):
    status_code: int = HTTPStatus.INTERNAL_SERVER_ERROR
    title: str = "Internal Server Error"

    def __init__(self, detail: str | None = None) -> None:
        # Use the class-level status_code (subclasses override it) and the stable
        # message; default the detail to the title so an argument-less raise still
        # yields a meaningful, stable problem body.
        super().__init__(status_code=self.status_code, detail=detail or self.title)

    def __str__(self) -> str:
        # Starlette's default __str__ is "<status>: <detail>"; we want the bare
        # stable message so handlers._problem()'s `str(exc)` == the detail.
        return self.detail


class ValidationError(DomainError):
    status_code = HTTPStatus.BAD_REQUEST
    title = "Bad Request"


class NotFoundError(DomainError):
    status_code = HTTPStatus.NOT_FOUND
    title = "Not Found"


class ConflictError(DomainError):
    status_code = HTTPStatus.CONFLICT
    title = "Conflict"


class ForbiddenError(DomainError):
    status_code = HTTPStatus.FORBIDDEN
    title = "Forbidden"


class ServiceUnavailableError(DomainError):
    status_code = HTTPStatus.SERVICE_UNAVAILABLE
    title = "Service Unavailable"
