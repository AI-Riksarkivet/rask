"""Error mapping for the REST layer.

Domain exceptions are the ``lance_namespace`` error hierarchy
(``LanceNamespaceError`` with a numeric ``.code``). This module maps those codes
to HTTP statuses and RFC 9457 Problem Details, and translates the native
backend's plain "not implemented" errors into ``UnsupportedOperationError`` so
unsupported operations surface as the spec's 406 (code 0).
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from http import HTTPStatus
from typing import TYPE_CHECKING, Any, Final, cast

from lance_namespace import ErrorCode, LanceNamespaceError, ServiceUnavailableError, UnsupportedOperationError

from lakehouse.problems.envelope import problem_response
from lakehouse.problems.outage import reads_as_store_outage
from lakehouse.problems.spec_routes import SPEC_ERROR_STATUSES, UNSUPPORTED_STATUS, is_spec_route, structural


if TYPE_CHECKING:
    from fastapi import FastAPI
    from starlette.responses import JSONResponse
    from starlette.types import Scope

#: The body and its media type are `lakehouse.problems.envelope`'s, which holds the Lance-free half of the answer and is
#: imported from there by every site that builds a response.
__all__ = [
    "PartiallyAppliedError",
    "ProblemExtensions",
    "as_unsupported_if_stub",
    "error_response",
    "install_problem_handlers",
    "status_for",
    "store_unavailable",
]

#: The ``detail`` every 5xx answers, whatever raised it: the message names paths, URLs and driver text, so it reaches the
#: log only.
_REDACTED: Final = "Internal Server Error"

_STATUS: dict[ErrorCode, int] = {
    # 406, NOT 501, and the spec is explicit about it: `UnsupportedOperationErrorResponse` is status
    # 406 ("Not Acceptable / Unsupported Operation") on every op that lists it, and Lance's own
    # reference server maps `ErrorCode::Unsupported` to NOT_ACCEPTABLE. Owner decision Q3, 2026-09-02.
    #
    # A generated client parses either, because it dispatches on `code` — but a spec-verbatim server
    # answers 406, and the status matters to everything that is NOT that client: a 501 is retried by
    # nothing and alerted on by most things, while 406 says plainly that the request asked for
    # something this backend does not serve. It also stops an unsupported op being counted as a
    # server fault in the RED metrics.
    ErrorCode.UNSUPPORTED: 406,
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
# TypeError on a backed op, and laundering it into an Unsupported answer hides a real bug (the audit caught exactly this —
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


class ProblemExtensions:
    """RFC 9457 extension members a raised error adds to its problem body, never redefining a reserved member.

    A declared carrier, so :func:`error_response` narrows by type and a raising site typechecks. An extension is assembled
    by the raising code from what it already knows about the caller's own objects, which is how a 5xx tells the caller
    something true while its ``detail`` stays redacted. The envelope leaves out one that names a member it owns
    (:func:`lakehouse.problems.envelope.problem_body`).
    """

    #: The members to add, keyed by their wire names.
    extensions: dict[str, object]


class PartiallyAppliedError(ProblemExtensions, ServiceUnavailableError):
    """A destructive operation that landed some of its steps and then failed.

    "Partly done" is a distinct outcome from "refused" and from "done", and the one the caller can least afford to guess
    at, so the extension members name what landed. ``ServiceUnavailable`` is the honest code for it: the steps are
    idempotent, so the recovery is to re-issue the SAME call, which is what a 503 asks of a client. Chain the real
    failure (``raise … from exc``) so the cause survives in the log while the response stays redacted.
    """

    def __init__(self, message: str, *, extensions: dict[str, object]) -> None:
        super().__init__(message)
        self.extensions = extensions


def error_response(exc: LanceNamespaceError) -> JSONResponse:
    """The answer to a raised error: its status and the RFC 9457 problem+json body, titled by the error's class name, with
    the error's extension members where it carries any.

    A 5xx-mapped error answers a GENERIC ``detail``, never ``str(exc)``, so internals (paths, DSNs, driver messages)
    reach the log only. A client (4xx) error keeps its message: it is actionable and self-authored, not an internal
    leak. The app's handler answers with this, and so does a request layer that answers before or outside the app's
    handlers, since an exception raised from a layer passes none of them.
    """
    status = status_for(int(exc.code))
    return problem_response(exc.code, status=status, title=type(exc).__name__, detail=_detail(exc, status), extensions=_extensions(exc))


def _detail(exc: LanceNamespaceError, status: int) -> str:
    return str(exc) if status < 500 else _REDACTED


def _extensions(exc: LanceNamespaceError) -> Mapping[str, object] | None:
    return exc.extensions if isinstance(exc, ProblemExtensions) else None


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


def store_unavailable(exc: BaseException, scope: Scope, *, log: logging.Logger) -> JSONResponse:
    """The spec's answer to a request the object store did not answer (``exc``): 503 with code 17, which a retry may
    clear, after one WARN record under ``log`` that names the request (``scope``). The store's own text reaches the log
    only."""
    log.warning("object_store_unavailable", extra={"method": scope.get("method"), "path": scope.get("path"), "error": str(exc)[:300]})
    return problem_response(ErrorCode.SERVICE_UNAVAILABLE, status=503, title="ServiceUnavailable", detail="Service Unavailable")


def install_problem_handlers(app: FastAPI, log: logging.Logger) -> None:
    """Register the four problem+json exception handlers every service app shares: domain errors, validation, the
    framework's own HTTP errors, and crashes.

    One home keeps the RFC 9457 shapes, the 5xx-generic-detail policy, and the leak rules from drifting per service. FastAPI
    imports stay inside the function so non-HTTP consumers of this module never pay for them.
    """
    from fastapi import Request
    from fastapi.exceptions import RequestValidationError
    from starlette.exceptions import HTTPException as StarletteHTTPException

    @app.exception_handler(LanceNamespaceError)
    async def handle_domain_error(request: Request, exc: LanceNamespaceError) -> JSONResponse:
        # The native backend reports an object store that did not answer as a typed InternalError (500);
        # it is the same outage `handle_unexpected_error` answers 503 for, in the backend's own dialect.
        if int(exc.code) == ErrorCode.INTERNAL and reads_as_store_outage(exc):
            return store_unavailable(exc, request.scope, log=log)
        status = status_for(int(exc.code))
        # Same split as the redaction: Unsupported is the backend answering "I don't do that", so it gets
        # a plain info line. A traceback at ERROR is for faults — spending one on a capability answer is
        # what makes an unsupported op look like an outage on the dashboard.
        if status == status_for(ErrorCode.UNSUPPORTED):
            # A capability answer, not a fault: an info line, never a traceback. Keyed on the mapped
            # status rather than a literal so it follows the map.
            log.info("unsupported_operation", extra={"method": request.method, "path": request.scope["path"], "status": status})
        elif status == status_for(ErrorCode.SERVICE_UNAVAILABLE) and reads_as_store_outage(exc):
            # A store that did not answer, which the door named itself (a guard raises its refusal from the store's own
            # error): one WARN, as every other way that outage arrives is recorded. A 503 for anything else, such as this
            # deployment's view of its IdP, keeps its traceback at ERROR below, the cause an operator acts on.
            log.warning("object_store_unavailable", extra={"method": request.method, "path": request.scope["path"], "error": str(exc)[:300]})
        elif status >= 500:
            log.exception("domain_error", extra={"method": request.method, "path": request.scope["path"], "status": status})
        return error_response(exc)

    @app.exception_handler(RequestValidationError)
    async def handle_validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
        # ONE CODE MUST NOT ARRIVE AT TWO STATUSES. This module maps `INVALID_INPUT` to 400 in both
        # directions (`_STATUS`, `_STATUS_CODE_FALLBACK`), and a generated Lance client dispatches on
        # the CODE — so serving that same code at 422 made one server answer one condition two ways.
        # The vendored spec contains zero 422s (checked against the re-vendored copy, 2026-09-14), so
        # on a spec operation 422 is a status no generated client has a contract for.
        #
        # Narrowed to SPEC routes rather than flipped estate-wide: a rask-only door (`/credentials`,
        # the registries) has no lance-ns contract to honour, and FastAPI's 422 is what its callers —
        # this estate's own clients and tests — already expect. `spec_routes` is committed data because
        # no image carries `lance_docs/`; a test re-derives it from the spec so it cannot drift.
        route = request.scope.get("route")
        status = 400 if is_spec_route(request.method, getattr(route, "path_format", "") or "") else 422
        # The shared envelope PLUS the field list as an extension member.
        errors = [{"field": ".".join(str(p) for p in e["loc"]), "message": e["msg"], "type": e["type"]} for e in exc.errors()]
        return problem_response(
            ErrorCode.INVALID_INPUT, status=status, title="Validation Error", detail="Validation Error", slug="validation", extensions={"errors": errors}
        )

    @app.exception_handler(StarletteHTTPException)
    async def handle_framework_error(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        """FastAPI's OWN 404/405 (and any explicit `HTTPException`), given the spec's `code`.

        Every error body on a `/v1` route is parsed by a generated Lance client whose `ErrorResponse`
        REQUIRES `code`. The other three handlers cover domain errors, validation and crashes; FastAPI's
        default `{"detail": "..."}` for a routing failure is a body the reference client cannot parse, so
        it reports `InternalError 18`: the server is told to be broken when in truth it does not serve
        that route.

        `Unsupported` is the honest default: neither "no such route" nor "wrong method" is a domain
        condition, and code 0 means precisely "this backend does not do that operation". A status the
        spec DOES have a code for keeps it, so an explicit `HTTPException(401)` still dispatches as
        `Unauthenticated`. The exception's own `detail` survives — that is what an operator reads.

        Order of registration does not matter: Starlette picks a handler by walking the raised
        exception's MRO, and `LanceNamespaceError` is not an `HTTPException`, so a `TableNotFound` reaches
        the domain handler and answers code 4 wherever this one is registered.
        """
        code = _STATUS_CODE_FALLBACK.get(exc.status_code, ErrorCode.UNSUPPORTED)
        detail = exc.detail if isinstance(exc.detail, str) else HTTPStatus(exc.status_code).phrase
        phrase = HTTPStatus(exc.status_code).phrase
        # An explicit slug, because the default derives the `type` URI from the title and the phrases
        # carry spaces — `.../problems/method not allowed` is not a URI a client can key on.
        return problem_response(code, status=exc.status_code, title=phrase, detail=detail, slug=phrase.lower().replace(" ", "-"), headers=exc.headers)

    @app.exception_handler(Exception)
    async def handle_unexpected_error(request: Request, exc: Exception) -> JSONResponse:
        # AN OBJECT STORE THAT DID NOT ANSWER IS AN OUTAGE, whichever bare `ValueError`/`OSError` a Lance call raised it
        # as. The app factory's layer (`lakehouse.service_app.outage_answer`) answers it before it reaches this handler,
        # which runs where the server logs it again; one reaches here only once its answer has begun, or raised outside
        # that layer, and is still recorded as the outage it is, never as a fault of this service.
        if reads_as_store_outage(exc):
            return store_unavailable(exc, request.scope, log=log)
        # Internals (native/Arrow/S3 error text, paths) leak via logs only — never the body.
        log.exception("unhandled_error", extra={"method": request.method, "path": request.scope["path"]})
        return problem_response(ErrorCode.INTERNAL, status=500, title="InternalError", detail=_REDACTED)

    _declare_problem_responses(app)


#: How the published schema names each error status a spec operation lists (``spec.yaml``'s own response names).
_SPEC_ERROR_DESCRIPTIONS: Final = {
    "400": "Bad Request",
    "401": "Unauthorized",
    "403": "Forbidden",
    "404": "Not Found",
    "406": "Unsupported Operation",
    "409": "Conflict",
    "503": "Service Unavailable",
    "5XX": "Server Error",
}

#: The problem body every refusal carries (:func:`lakehouse.problems.envelope.problem_body`): RFC 9457's keys, the spec's
#: ``ErrorResponse`` ``code`` and ``error``, the optional ``instance`` (the request's trace id, while a span is active), and
#: the ``errors`` a rask-only door's validation failure lists.
_PROBLEM_SCHEMA: Final = {
    "type": "object",
    "required": ["type", "title", "status", "detail", "code", "error"],
    "properties": {
        "type": {"type": "string"},
        "title": {"type": "string"},
        "status": {"type": "integer"},
        "detail": {"type": "string"},
        "code": {"type": "integer", "description": "The spec's error code, which a client dispatches on"},
        "error": {"type": "string"},
        "instance": {"type": "string", "description": "The W3C trace id of the request, which finds the error's log line"},
        "errors": {
            "type": "array",
            "description": "Each field a request failed validation on",
            "items": {
                "type": "object",
                "required": ["field", "message", "type"],
                "properties": {"field": {"type": "string"}, "message": {"type": "string"}, "type": {"type": "string"}},
            },
        },
    },
}

_PROBLEM_RESPONSE_SCHEMA_NAME: Final = "ProblemDetails"
_PROBLEM_CONTENT: Final = {"application/problem+json": {"schema": {"$ref": f"#/components/schemas/{_PROBLEM_RESPONSE_SCHEMA_NAME}"}}}
#: FastAPI's own validation schemas, which no operation names once every 422 names the problem body.
_FRAMEWORK_VALIDATION_SCHEMAS: Final = ("HTTPValidationError", "ValidationError")


def _declare_problem_responses(app: FastAPI) -> None:
    """Make the published schema say what every operation answers on failure: the problem body.

    A spec operation declares each error status ``spec.yaml`` lists for it and the 406 any of them may answer
    (:data:`lakehouse.problems.spec_routes.SPEC_ERROR_STATUSES`, :data:`UNSUPPORTED_STATUS`), and no 422, which a spec
    operation answers 400. Any other operation's 422 is the problem body with its ``errors``, which
    `handle_validation_error` serves, not FastAPI's validation schema. Every operation declares a ``default`` naming the
    problem body for the statuses it does not list. A status an operation declares itself (a probe's 503) is kept.
    """
    generate = app.openapi
    declared: list[dict[str, Any]] = []

    def openapi() -> dict[str, Any]:
        # The generator caches its schema and this adds to it in place, so the walk runs once a process.
        if declared:
            return declared[0]
        schema = generate()
        for path, item in schema.get("paths", {}).items():
            for method, operation in item.items():
                if isinstance(operation, dict):
                    _declare(method, path, operation.setdefault("responses", {}))
        components = schema.setdefault("components", {}).setdefault("schemas", {})
        for name in _FRAMEWORK_VALIDATION_SCHEMAS:
            components.pop(name, None)
        components[_PROBLEM_RESPONSE_SCHEMA_NAME] = _PROBLEM_SCHEMA
        declared.append(schema)
        return schema

    # FastAPI's own documented override point is this attribute (its `openapi()` is looked up on the
    # INSTANCE, not the class), so assigning it is the supported seam rather than a monkeypatch. The
    # cast is because the DECLARED type is the unbound `openapi(self)` while what is installed is the
    # zero-argument callable FastAPI will actually call — a type-level mismatch the framework's own
    # contract creates, not one this code could narrow away.
    cast(Any, app).openapi = openapi


def _declare(method: str, path: str, responses: dict[str, Any]) -> None:
    """Name the problem body for ``method path``'s errors in its ``responses``."""
    if is_spec_route(method, path):
        responses.pop("422", None)
        for status in sorted(SPEC_ERROR_STATUSES.get((method.upper(), structural(path)), frozenset()) | {UNSUPPORTED_STATUS}):
            responses[status] = {"description": _SPEC_ERROR_DESCRIPTIONS[status], "content": _PROBLEM_CONTENT}
    elif "422" in responses:
        responses["422"] = {"description": "Validation Error", "content": _PROBLEM_CONTENT}
    responses["default"] = {"description": "Error", "content": _PROBLEM_CONTENT}
