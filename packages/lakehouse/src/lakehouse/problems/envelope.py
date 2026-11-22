"""The RFC 9457 problem envelope every refusal the lakehouse answers carries.

The envelope is RFC 9457 plus the two spec-0.9 keys, and the code is an integer on the wire: ``ErrorCode`` is an
``IntEnum``, so a caller passes its member unchanged. ``lakehouse.problems.ns_errors`` maps the Lance Namespace errors
onto it; the sites that must BUILD a response rather than raise (a request layer that answers before the app's handlers,
a refusal that sets ``Retry-After``) build it here, with :func:`problem_response`, or :func:`send_problem` for a pure-ASGI
layer, and every problem response names its media type by ``PROBLEM_JSON``.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from typing import Final

from starlette.responses import JSONResponse
from starlette.types import Send

from lakehouse.process.correlation import current_trace_id


__all__ = ["PROBLEM_JSON", "occurrence", "problem_body", "problem_response", "send_problem"]

#: The RFC 9457 media type every problem body is served with.
PROBLEM_JSON = "application/problem+json"

#: The members the builder owns: RFC 9457's, the spec's ``ErrorResponse`` ``code`` and ``error``, and the ``instance`` a
#: span puts there. Fixed rather than read off the body built, so an extension naming a member absent from one body
#: (``instance`` with no span active) is left out as surely as one naming a member present.
_RESERVED_MEMBERS: Final = frozenset({"type", "title", "status", "detail", "code", "error", "instance"})


def occurrence() -> dict[str, str]:
    """The spec's optional ``instance`` for the error being answered: the W3C trace id of the request's span.

    ``instance`` "identifies the specific occurrence of the error", "a request or response ID, or anything that the
    implementation can recognize to trace" it (`lance_docs/ns_catalog/spec.yaml` ErrorResponse). A 5xx detail is
    redacted, so this id is what a caller gives an operator, and it is the trace the request already has, where the
    redacted error's log line sits. Empty when no span is active (tracing off), since no id would then find anything.
    """
    trace_id = current_trace_id()
    return {"instance": trace_id} if trace_id is not None else {}


def problem_body(
    code: int,
    *,
    status: int,
    title: str,
    detail: str,
    slug: str | None = None,
    extensions: Mapping[str, object] | None = None,
) -> dict[str, object]:
    """The RFC 9457 + spec-0.9 envelope, for the sites that must BUILD a response rather than raise.

    Six keys, and the last two are not decoration: ``code`` is a REQUIRED, no-default field on the
    generated Lance-Namespace client's ``ErrorResponse`` model, so a client validating a four-key body
    RAISES rather than seeing a ``None``. Seven places in the estate rebuilt this envelope by hand and
    every one of them emitted four.

    WHY THIS EXISTS INSTEAD OF THOSE SITES SIMPLY RAISING. Some are pure-ASGI layers that sit outside
    ``ExceptionMiddleware`` and must answer before the body is buffered (the body cap, the deadline, the write
    shed), so they cannot raise at all. The others answer before the app's handlers run (the escaped-slash refusal)
    or set ``Retry-After`` (catalog maintenance, the signing gate), which no raised error carries and whose window
    no generic handler can know. So the SHAPE lives here and the STATUS and HEADERS stay with the site that knows
    them.

    ``detail`` doubles as the spec's ``error``: one message, so a problem-details client and a spec
    client cannot be told two different things. ``ns_errors.error_response`` builds a raised domain
    error's body here too.

    ``slug`` overrides the ``type`` suffix for a site whose existing URI does not match its title. That
    is not cosmetic: adding a missing key must not silently RENAME a body clients already parse, and
    the 422 handler is exactly that case — its title is "Validation Error" and its type has always
    ended in ``/validation``. Deriving the slug from the title would have put a space in the URI, and
    changing the title to fit the deriver is a wire change dressed up as a fix.

    ``extensions`` are RFC 9457 §3.2 extension members, which may ADD to the body and never redefine a member the builder
    owns, so a site can neither claim another status nor un-redact ``detail``.
    """
    body: dict[str, object] = {
        "type": f"https://lance.org/problems/{slug or title.lower()}",
        "title": title,
        "status": status,
        "detail": detail,
        "code": int(code),
        "error": detail,
        **occurrence(),
    }
    return body | {key: value for key, value in (extensions or {}).items() if key not in _RESERVED_MEMBERS}


def problem_response(
    code: int,
    *,
    status: int,
    title: str,
    detail: str,
    slug: str | None = None,
    headers: Mapping[str, str] | None = None,
    extensions: Mapping[str, object] | None = None,
) -> JSONResponse:
    """:func:`problem_body` at ``status`` as a response, for every handler and every layer that builds its answer.

    ``headers`` carry what only the building site knows, ``Retry-After`` above all: no raised error carries a header, so a
    refusal that says when to come back is built, never raised (`spec.yaml`: a 503 "may additionally send a Retry-After
    header").
    """
    body = problem_body(code, status=status, title=title, detail=detail, slug=slug, extensions=extensions)
    return JSONResponse(status_code=status, content=body, media_type=PROBLEM_JSON, headers=dict(headers) if headers else None)


async def send_problem(
    send: Send,
    code: int,
    *,
    status: int,
    title: str,
    detail: str,
    slug: str | None = None,
    headers: Sequence[tuple[bytes, bytes]] = (),
) -> None:
    """Answer an ASGI request with :func:`problem_body` at ``status``, for a pure-ASGI layer that answers before, or
    outside, the app's handlers. ``headers`` go out beside the content type and length."""
    payload = json.dumps(problem_body(code, status=status, title=title, detail=detail, slug=slug)).encode()
    framing = [(b"content-type", PROBLEM_JSON.encode()), (b"content-length", str(len(payload)).encode())]
    await send({"type": "http.response.start", "status": status, "headers": [*framing, *headers]})
    await send({"type": "http.response.body", "body": payload})
