"""`DomainError` could not carry response headers, and the handler would have dropped them anyway.

`Retry-After` is part of what a 503 or a 429 MEANS: a refusal that cannot say when to come back is
one the caller can only guess at. Two independent holes stopped it working, and each is invisible on
its own —

  * `DomainError.__init__` took `detail` alone and called `HTTPException.__init__` without headers,
    so writing the correct thing raised a **TypeError at raise time** and the 503 became a 500. On a
    saturation path, which is exactly when nobody is watching.
  * `register_handlers`' handler builds its own `JSONResponse` from the status and the problem body,
    so a header that survived the first hole was dropped on the way out — silently, with a 503 that
    simply says nothing.

Found while bounding the viewer's clip builder (`ServiceUnavailableError(..., headers=...)`), by
checking the constructor rather than trusting it. Needed estate-wide for the rate-limit seam (owner
ruling 2026-08-26), where `Retry-After` on a 429 is the whole point of the status.
"""


def test_a_domain_error_can_carry_RESPONSE_HEADERS() -> None:
    """`Retry-After` is part of what a 503 or a 429 MEANS, and `DomainError` dropped it.

    `DomainError.__init__` took `detail` alone and called `HTTPException.__init__` without headers —
    so a caller writing the correct thing (`ServiceUnavailableError(msg, headers={"Retry-After": ...})`)
    got a TypeError at RAISE time and their 503 became a 500. The failure is invisible until the
    refusal path actually runs, which for a saturation refusal is exactly when nobody is watching.

    Found while bounding the viewer's clip builder; needed estate-wide for the rate-limit seam
    (owner ruling 2026-08-26), where `Retry-After` on a 429 is the whole point of the status.
    """
    from service_kit.exceptions import ServiceUnavailableError

    exc = ServiceUnavailableError("all build slots are busy", headers={"Retry-After": "5"})

    assert exc.status_code == 503
    assert exc.detail == "all build slots are busy"
    assert exc.headers == {"Retry-After": "5"}


def test_a_domain_error_without_headers_is_unchanged() -> None:
    """Every existing raise site passes detail alone; the parameter must stay optional."""
    from service_kit.exceptions import ServiceUnavailableError

    exc = ServiceUnavailableError("plain")
    assert exc.detail == "plain"
    assert exc.headers is None


def test_the_handler_FORWARDS_those_headers_to_the_response() -> None:
    """Carrying the header on the exception is half the job; the handler builds its own response.

    `register_handlers`' `DomainError` handler constructed a `JSONResponse` from the status and the
    problem body ONLY, so a `Retry-After` set at the raise site was dropped silently on the way out.
    Both halves are needed, and each is independently invisible: the first raises, the second says
    nothing.
    """
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from service_kit.exceptions import ServiceUnavailableError, register_handlers

    app = FastAPI()
    register_handlers(app)

    @app.get("/busy")
    async def _busy() -> None:
        raise ServiceUnavailableError("all build slots are busy", headers={"Retry-After": "5"})

    with TestClient(app) as client:
        resp = client.get("/busy")

    assert resp.status_code == 503
    assert resp.headers.get("Retry-After") == "5", (
        "the refusal reached the client without Retry-After — the caller cannot tell a momentary saturation from a dead service"
    )
    assert resp.headers["content-type"].startswith("application/problem+json")
