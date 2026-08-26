"""Error mapping for the REST layer.

Domain exceptions are the ``lance_namespace`` error hierarchy
(``LanceNamespaceError`` with a numeric ``.code``). This module maps those codes
to HTTP statuses and RFC 9457 Problem Details, and translates the native
backend's plain "not implemented" errors into ``UnsupportedOperationError`` so
unsupported operations surface as a spec-correct 501.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from lance_namespace import ErrorCode, LanceNamespaceError, UnsupportedOperationError


if TYPE_CHECKING:
    from fastapi import FastAPI

#: The RFC 9457 media type every problem body is served with.
PROBLEM_JSON = "application/problem+json"

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


def problem_body(code: ErrorCode | int, *, status: int, title: str, detail: str, slug: str | None = None) -> dict[str, object]:
    """The RFC 9457 + spec-0.9 envelope, for the sites that must BUILD a response rather than raise.

    Six keys, and the last two are not decoration: `code` is a REQUIRED, no-default field on the
    generated Lance-Namespace client's `ErrorResponse` model, so a client validating a four-key body
    RAISES rather than seeing a `None`. Seven places in the estate rebuilt this envelope by hand and
    every one of them emitted four.

    WHY THIS EXISTS INSTEAD OF THOSE SITES SIMPLY RAISING. Two of them are pure-ASGI middleware that
    sit outside `ExceptionMiddleware` and must answer before the body is buffered, so they cannot
    raise at all. The rest could — but every one of them sets `Retry-After` (5s on a draining
    medallion door, 60s on catalog maintenance), and `install_problem_handlers`' handler builds a
    bare `JSONResponse` with no headers, so raising would trade a missing `code` for a missing
    `Retry-After`. A generic handler also cannot know which window applies. So the SHAPE lives here
    and the STATUS and HEADERS stay with the site that knows them.

    `detail` doubles as the spec's `error` for the same reason `problem_detail` does it: one message,
    so a problem-details client and a spec client cannot be told two different things.

    `slug` overrides the `type` suffix for a site whose existing URI does not match its title. That is
    not cosmetic: adding a missing key must not silently RENAME a body clients already parse, and the
    422 handler is exactly that case — its title is "Validation Error" and its type has always ended
    in `/validation`. Deriving the slug from the title would have put a space in the URI, and changing
    the title to fit the deriver is a wire change dressed up as a fix.
    """
    return {
        "type": f"https://lance.org/problems/{slug or title.lower()}",
        "title": title,
        "status": status,
        "detail": detail,
        "code": int(code),
        "error": detail,
    }


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


def install_problem_handlers(app: FastAPI, log: logging.Logger) -> None:
    """Register the three problem+json exception handlers every service app shares.

    Byte-identical blocks lived in the catalog and lineage mains (audit 2026-07-15) — one home keeps the
    RFC 9457 shapes, the 5xx-generic-detail policy, and the leak rules from drifting per service. FastAPI
    imports stay inside the function so non-HTTP consumers of this module never pay for them.
    """
    from fastapi import Request
    from fastapi.exceptions import RequestValidationError
    from fastapi.responses import JSONResponse

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

    @app.exception_handler(Exception)
    async def handle_unexpected_error(request: Request, exc: Exception) -> JSONResponse:
        # Internals (native/Arrow/S3 error text, paths) leak via logs only — never the body.
        log.exception("unhandled_error", extra={"method": request.method, "path": request.url.path})
        return JSONResponse(
            status_code=500,
            content=problem_body(ErrorCode.INTERNAL, status=500, title="InternalError", detail="Internal Server Error"),
            media_type=PROBLEM_JSON,
        )
