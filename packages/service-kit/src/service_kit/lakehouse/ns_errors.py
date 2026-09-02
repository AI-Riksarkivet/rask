"""Error mapping for the REST layer.

Domain exceptions are the ``lance_namespace`` error hierarchy
(``LanceNamespaceError`` with a numeric ``.code``). This module maps those codes
to HTTP statuses and RFC 9457 Problem Details, and translates the native
backend's plain "not implemented" errors into ``UnsupportedOperationError`` so
unsupported operations surface as a spec-correct 501.
"""

from __future__ import annotations

import logging
from http import HTTPStatus
from typing import TYPE_CHECKING

from lance_namespace import ErrorCode, LanceNamespaceError, UnsupportedOperationError

from service_kit.problem import PROBLEM_JSON, problem_body


if TYPE_CHECKING:
    from fastapi import FastAPI

#: Re-exported from `service_kit.problem`, which owns the Lance-free half of this module. Both names
#: stay importable from here so no call site moved: `problem_body` had seven of them and the point of
#: the split was packaging, not API churn. See that module for why it exists.
__all__ = ["PROBLEM_JSON", "install_problem_handlers", "problem_body", "problem_detail", "status_for"]

_STATUS: dict[ErrorCode, int] = {
    ErrorCode.UNSUPPORTED: 501,
    ErrorCode.NAMESPACE_NOT_FOUND: 404,
    ErrorCode.NAMESPACE_ALREADY_EXISTS: 409,
    ErrorCode.NAMESPACE_NOT_EMPTY: 409,
    ErrorCode.TABLE_NOT_FOUND: 404,
    ErrorCode.TABLE_ALREADY_EXISTS: 409,
    ErrorCode.TABLE_INDEX_NOT_FOUND: 404,
    ErrorCode.TABLE_INDEX_ALREADY_EXISTS: 409,
    ErrorCode.TABLE_TAG_NOT_FOUND: 404,
    ErrorCode.TABLE_TAG_ALREADY_EXISTS: 409,
    ErrorCode.TRANSACTION_NOT_FOUND: 404,
    ErrorCode.TABLE_VERSION_NOT_FOUND: 404,
    ErrorCode.TABLE_COLUMN_NOT_FOUND: 404,
    ErrorCode.INVALID_INPUT: 400,
    ErrorCode.CONCURRENT_MODIFICATION: 409,
    ErrorCode.PERMISSION_DENIED: 403,
    ErrorCode.UNAUTHENTICATED: 401,
    ErrorCode.SERVICE_UNAVAILABLE: 503,
    ErrorCode.INTERNAL: 500,
    ErrorCode.INVALID_TABLE_STATE: 409,
    ErrorCode.TABLE_SCHEMA_VALIDATION_ERROR: 400,
    ErrorCode.THROTTLING: 429,
    # Codes 22/23 landed with the branch ops and were MISSING here, so a missing branch answered
    # 500 ("Internal Server Error") instead of 404 — on endpoints rask actually ships
    # (`catalog/api/v1/endpoints/branches.py`). Found by the 2026-08-04 skills audit, which caught
    # the skill claiming "22 codes, all mapped" while the SDK's ErrorCode has 24. The completeness
    # test now pins the whole enum, so the NEXT spec-added code fails a test instead of a client.
    ErrorCode.TABLE_BRANCH_NOT_FOUND: 404,
    ErrorCode.TABLE_BRANCH_ALREADY_EXISTS: 409,
}

# The native dir backend raises plain (untyped) errors when an op is a genuine stub. We deliberately do
# NOT include marshalling hints like "is not an instance" here: that phrase is raised by a pydantic-vs-dict
# TypeError on a backed op, and laundering it into a 501 hides a real bug (the audit caught exactly this —
# create/describe/batch-delete versions were backed but appeared unsupported). Such errors now surface as 500.
_UNSUPPORTED_HINTS = ("not implemented", "not supported")


def status_for(code: int) -> int:
    """Map a numeric lance ``ErrorCode`` to an HTTP status, defaulting to 500."""
    try:
        return _STATUS.get(ErrorCode(code), 500)
    except ValueError:
        return 500


def as_unsupported_if_stub(exc: Exception) -> Exception:
    """Return an ``UnsupportedOperationError`` if ``exc`` signals a backend stub."""
    if isinstance(exc, LanceNamespaceError):
        return exc
    if any(hint in str(exc).lower() for hint in _UNSUPPORTED_HINTS):
        return UnsupportedOperationError(str(exc))
    return exc


#: 5xx statuses whose message is a CAPABILITY STATEMENT, not a fault report, and so is not redacted.
#:
#: 501 is the whole set. "alter_table_backfill_columns not implemented" names an operation the caller asked
#: for and nothing else — no path, no DSN, no driver internals — and it is the only thing that tells them to
#: stop asking. Under the blanket ``>= 500`` rule it was replaced with "Internal Server Error", so a user who
#: pressed a button the UI ships (backfill) read that the server had broken rather than that the backend does
#: not implement the op (#101). Every other 5xx stays redacted: those ARE faults, and their text leaks.
_UNREDACTED_5XX = frozenset({501})


def problem_detail(exc: LanceNamespaceError) -> tuple[int, dict[str, object]]:
    """Build (status, RFC 9457 problem+json body) for a domain error.

    A 5xx-mapped error uses a GENERIC ``detail`` — never ``str(exc)`` — so internals (paths, DSNs, driver
    messages) leak via logs only, not the response body. Client (4xx) errors keep their message: it is
    actionable and self-authored, not an internal leak. The one 5xx exception is ``_UNREDACTED_5XX``.
    """
    status = status_for(int(exc.code))
    detail = str(exc) if status < 500 or status in _UNREDACTED_5XX else "Internal Server Error"
    body: dict[str, object] = {
        "type": f"https://lance.org/problems/{exc.__class__.__name__.lower()}",
        "title": exc.__class__.__name__,
        "status": status,
        "detail": detail,
        "code": int(exc.code),
        # Spec-0.9 ErrorResponse compatibility: `code` (required) + `error` (brief message). Kept
        # alongside the RFC 9457 fields so both problem-details and spec clients can parse us.
        "error": detail,
    }
    return status, body


#: The spec code to stamp on a status the framework produced, where the spec HAS one for it.
#:
#: The inverse of the `ErrorCode -> status` map above, and deliberately partial: a status with no
#: domain meaning (404 on an unknown route, 405, 409 from app code) falls through to `Unsupported`,
#: which is the truthful answer for "this backend does not do that". Guessing `TableNotFound` for a
#: routing 404 would tell a client a table is missing when the OPERATION is.
_STATUS_CODE_FALLBACK: dict[int, ErrorCode] = {
    400: ErrorCode.INVALID_INPUT,
    401: ErrorCode.UNAUTHENTICATED,
    403: ErrorCode.PERMISSION_DENIED,
    429: ErrorCode.THROTTLING,
    500: ErrorCode.INTERNAL,
    501: ErrorCode.UNSUPPORTED,
    503: ErrorCode.SERVICE_UNAVAILABLE,
}


def install_problem_handlers(app: FastAPI, log: logging.Logger) -> None:
    """Register the three problem+json exception handlers every service app shares.

    Byte-identical blocks lived in the catalog and lineage mains (audit 2026-07-15) — one home keeps the
    RFC 9457 shapes, the 5xx-generic-detail policy, and the leak rules from drifting per service. FastAPI
    imports stay inside the function so non-HTTP consumers of this module never pay for them.
    """
    from fastapi import Request
    from fastapi.exceptions import RequestValidationError
    from fastapi.responses import JSONResponse
    from starlette.exceptions import HTTPException as StarletteHTTPException

    @app.exception_handler(LanceNamespaceError)
    async def handle_domain_error(request: Request, exc: LanceNamespaceError) -> JSONResponse:
        status, body = problem_detail(exc)
        # Same split as the redaction: a 501 is the backend answering "I don't do that", so it gets a plain
        # info line. A traceback at ERROR is for faults — spending one on a capability answer is what makes
        # an unsupported op look like an outage on the dashboard.
        if status in _UNREDACTED_5XX:
            log.info("unsupported_operation", extra={"method": request.method, "path": request.url.path, "status": status})
        elif status >= 500:
            log.exception("domain_error", extra={"method": request.method, "path": request.url.path, "status": status})
        return JSONResponse(status_code=status, content=body, media_type=PROBLEM_JSON)

    @app.exception_handler(RequestValidationError)
    async def handle_validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
        return JSONResponse(
            status_code=422,
            # The shared envelope PLUS the field list. A 422 is served on the same `/v1` routes a
            # generated client calls, and `code` is required on its `ErrorResponse` — so this body
            # made the client raise just as the hand-built ones did, at the status a client is most
            # likely to hit.
            content=problem_body(ErrorCode.INVALID_INPUT, status=422, title="Validation Error", detail="Validation Error", slug="validation")
            | {"errors": [{"field": ".".join(str(p) for p in e["loc"]), "message": e["msg"], "type": e["type"]} for e in exc.errors()]},
            media_type=PROBLEM_JSON,
        )

    @app.exception_handler(StarletteHTTPException)
    async def handle_framework_error(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        """FastAPI's OWN 404/405 (and any explicit `HTTPException`), given the spec's `code`.

        Every error body on a `/v1` route is parsed by a generated Lance client whose `ErrorResponse`
        REQUIRES `code`. The three handlers above cover domain errors, validation and crashes; routing
        failures went out as FastAPI's default `{"detail": "..."}`, which the reference client cannot
        parse, so it reported `InternalError 18` — the server is told to be broken when in truth it
        does not serve that route. A3's GET form of `count_rows` is exactly how that surfaced.

        `Unsupported` is the honest default: neither "no such route" nor "wrong method" is a domain
        condition, and code 0 means precisely "this backend does not do that operation". A status the
        spec DOES have a code for keeps it, so an explicit `HTTPException(401)` still dispatches as
        `Unauthenticated`. The exception's own `detail` survives — that is what an operator reads.

        Registered BELOW the domain handler, which is what keeps a `TableNotFound` answering code 4
        rather than being swallowed into a generic 404 by this catch-all.
        """
        code = _STATUS_CODE_FALLBACK.get(exc.status_code, ErrorCode.UNSUPPORTED)
        detail = exc.detail if isinstance(exc.detail, str) else HTTPStatus(exc.status_code).phrase
        phrase = HTTPStatus(exc.status_code).phrase
        # An explicit slug, because the default derives the `type` URI from the title and the phrases
        # carry spaces — `.../problems/method not allowed` is not a URI a client can key on.
        body = problem_body(code, status=exc.status_code, title=phrase, detail=detail, slug=phrase.lower().replace(" ", "-"))
        return JSONResponse(status_code=exc.status_code, content=body, headers=dict(exc.headers or {}), media_type=PROBLEM_JSON)

    @app.exception_handler(Exception)
    async def handle_unexpected_error(request: Request, exc: Exception) -> JSONResponse:
        # Internals (native/Arrow/S3 error text, paths) leak via logs only — never the body.
        log.exception("unhandled_error", extra={"method": request.method, "path": request.url.path})
        return JSONResponse(
            status_code=500,
            content=problem_body(ErrorCode.INTERNAL, status=500, title="InternalError", detail="Internal Server Error"),
            media_type=PROBLEM_JSON,
        )
