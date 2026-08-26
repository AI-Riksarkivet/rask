"""Per-route rate limiting for the estate's expensive surfaces.

Nothing in this estate rate-limited anything until 2026-08-26 — `grep 'slowapi|Limiter|@limiter'`
over the tree returned zero — while `GET /api/explorer/search` drove GPU embedding plus a
cross-encoder rerank per request, unauthenticated, with an unbounded query string and a cache that a
varied `q` always missed.

**PER ROUTE, NEVER A GLOBAL MIDDLEWARE**, and that is not a style preference. One number cannot be
right for both `/search` and `/livez`, and rate-limiting a probe route makes the kubelet's checks fail
under load — pods cycle, and a limiter becomes an outage. Probe routes carry no decorator, full stop.

**IN-MEMORY STORAGE, and the reason it is honest here is measurable rather than hopeful.** slowapi's
in-memory backend is per PROCESS. Every service carrying these limits runs `replicas: 1`, so per-pod
state *is* the global state. It stops being true the moment one scales: N pods give N times the declared
limit, silently, with no error anywhere — the failure `rate-limiting.md` files under "in-process
defaultdict rate limiter". This estate deliberately runs no Redis (`CLAUDE.md`: events ride Dapr
pub/sub on NATS JetStream), so shared storage is a real infrastructure addition and is not justified
while the assumption holds.

So the assumption is GATED, not commented: `test_rate_limit.py` fails if any service in
`RATE_LIMITED_SERVICES` has a replica count above one, and names Redis as the fix. That is the moment
shared storage becomes justified, and this is how it announces itself instead of passing unnoticed.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from slowapi import Limiter
from slowapi.errors import RateLimitExceeded


if TYPE_CHECKING:
    from fastapi import FastAPI, Request
    from fastapi.responses import JSONResponse


#: Services whose routes carry a limiter. Read by the replica tripwire — a service added here without
#: staying at one replica fails the gate, which is the point.
RATE_LIMITED_SERVICES: frozenset[str] = frozenset({"search"})


def by_subject(request: Request) -> str:
    """Key on the authenticated subject, falling back to the client IP.

    Two rules from the reference, both load-bearing. Keying by IP alone on an authenticated route is
    the corporate-NAT defect — thousands of users behind one address share a bucket and are blocked
    together. And the `ip:` fallback is mandatory, or an anonymous caller bypasses the limit simply by
    presenting no credential, which on these routes is precisely the caller to meter.
    """
    subject = getattr(request.state, "subject", None)
    if subject:
        return f"user:{subject}"
    client = request.client
    return f"ip:{client.host}" if client else "ip:unknown"


def make_limiter() -> Limiter:
    """The module-level limiter.

    Module-level because `@limiter.limit(...)` decorators are evaluated at IMPORT time — a limiter
    built inside a lifespan cannot back them. `storage_uri` is left unset, which is slowapi's
    in-memory backend; see the module docstring for why that is honest here and what would change it.
    """
    return Limiter(key_func=by_subject)


def register_rate_limiting(app: FastAPI, limiter: Limiter) -> None:
    """Attach the limiter and a problem+json 429 handler.

    `app.state.limiter` is the exact attribute slowapi's own header injection reads — the name is not
    ours to choose.

    The 429 is shaped like every other error in this estate (RFC 9457 problem+json) rather than
    slowapi's default, and it keeps `Retry-After`: "returning a plain 429 with no headers → clients
    can't self-throttle, retry storms hammer the API back into 429". slowapi also attaches
    `X-RateLimit-*` to successful responses, so a well-behaved client can pace itself before ever
    being refused.
    """
    from fastapi.responses import JSONResponse

    app.state.limiter = limiter

    async def _rate_limited(_request: Request, exc: Exception) -> JSONResponse:
        retry_after = "60"
        detail = "rate limit exceeded"
        if isinstance(exc, RateLimitExceeded):
            detail = f"rate limit exceeded: {exc.detail}"
            headers = getattr(exc, "headers", None) or {}
            retry_after = str(headers.get("Retry-After", retry_after))
        return JSONResponse(
            status_code=429,
            content={
                "type": "about:blank#ratelimitexceeded",
                "title": "Too Many Requests",
                "status": 429,
                "detail": detail,
            },
            headers={"Retry-After": retry_after},
            media_type="application/problem+json",
        )

    app.add_exception_handler(RateLimitExceeded, _rate_limited)
