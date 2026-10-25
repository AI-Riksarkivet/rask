"""The RFC 9457 problem envelope, with no Lance dependency.

WHY THIS IS NOT IN ``service_kit.lakehouse.ns_errors``, where it used to live. That module imports
``lance_namespace`` at module scope, and ``service_kit.body_limit`` — a middleware every app this
library builds gets — imported ``problem_body`` from it. So ``import service_kit`` transitively
required a package that lives behind the ``[governed]`` / ``[lakehouse]`` extras, and the estate's own
rule is that those stay optional because this library is shared by storeless services too.

The consequence is not theoretical: an image whose member declares ``service-kit`` bare resolves no
``lance_namespace`` and fails ``.docker/import-gate.py`` —

    import gate FAILED — 1/1 modules could not be IMPORTED:
      <member>: ModuleNotFoundError: No module named 'lance_namespace'

A bare consumer that resolves it TRANSITIVELY, through some other dependency, does so by luck and is
one dependency bump from the same failure.

Nothing here is Lance-specific. The envelope is RFC 9457 plus the two spec-0.9 keys, and the code is
an integer on the wire either way — ``ErrorCode`` is an ``IntEnum``, so a caller that HAS the extra
passes its member unchanged and one that does not passes the number. ``ns_errors`` re-exports both
names, so every existing import site is untouched.
"""

from __future__ import annotations

from opentelemetry import trace


#: The RFC 9457 media type every problem body is served with.
PROBLEM_JSON = "application/problem+json"


def occurrence() -> dict[str, str]:
    """The spec's optional ``instance`` for the error being answered: the W3C trace id of the request's span.

    ``instance`` "identifies the specific occurrence of the error", "a request or response ID, or anything that the
    implementation can recognize to trace" it (`lance_docs/ns_catalog/spec.yaml` ErrorResponse). A 5xx detail is
    redacted, so this id is what a caller gives an operator, and it is the trace the request already has, where the
    redacted error's log line sits. Empty when no span is active (tracing off), since no id would then find anything.
    """
    context = trace.get_current_span().get_span_context()
    return {"instance": format(context.trace_id, "032x")} if context.is_valid else {}


def problem_body(code: int, *, status: int, title: str, detail: str, slug: str | None = None) -> dict[str, object]:
    """The RFC 9457 + spec-0.9 envelope, for the sites that must BUILD a response rather than raise.

    Six keys, and the last two are not decoration: ``code`` is a REQUIRED, no-default field on the
    generated Lance-Namespace client's ``ErrorResponse`` model, so a client validating a four-key body
    RAISES rather than seeing a ``None``. Seven places in the estate rebuilt this envelope by hand and
    every one of them emitted four.

    WHY THIS EXISTS INSTEAD OF THOSE SITES SIMPLY RAISING. Two of them are pure-ASGI middleware that
    sit outside ``ExceptionMiddleware`` and must answer before the body is buffered, so they cannot
    raise at all. The rest could — but every one of them sets ``Retry-After`` (5s on a draining
    medallion door, 60s on catalog maintenance), and ``install_problem_handlers``' handler builds a
    bare ``JSONResponse`` with no headers, so raising would trade a missing ``code`` for a missing
    ``Retry-After``. A generic handler also cannot know which window applies. So the SHAPE lives here
    and the STATUS and HEADERS stay with the site that knows them.

    ``detail`` doubles as the spec's ``error`` for the same reason ``problem_detail`` does it: one
    message, so a problem-details client and a spec client cannot be told two different things.

    ``slug`` overrides the ``type`` suffix for a site whose existing URI does not match its title. That
    is not cosmetic: adding a missing key must not silently RENAME a body clients already parse, and
    the 422 handler is exactly that case — its title is "Validation Error" and its type has always
    ended in ``/validation``. Deriving the slug from the title would have put a space in the URI, and
    changing the title to fit the deriver is a wire change dressed up as a fix.
    """
    return {
        "type": f"https://lance.org/problems/{slug or title.lower()}",
        "title": title,
        "status": status,
        "detail": detail,
        "code": int(code),
        "error": detail,
        **occurrence(),
    }
