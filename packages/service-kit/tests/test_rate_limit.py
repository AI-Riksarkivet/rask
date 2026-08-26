"""Unmetered expensive routes, and the per-pod assumption that makes in-memory limiting honest.

The audit's sharpest theme: nothing in the estate rate-limits anything — `grep slowapi|Limiter` over
the tree returned zero. `GET /api/explorer/search` drives GPU embedding plus a cross-encoder rerank
per request, with an unbounded query string and a cache a varied `q` always misses.

OWNER RULING 2026-08-26: slowapi, PER ROUTE, in-memory storage. Per-route rather than global is the
reference's own rule and it is not stylistic — one number cannot be right for both `/search` and
`/livez`, and rate-limiting a probe route makes k8s cycle pods under load, turning a limiter into an
outage.

IN-MEMORY IS CORRECT TODAY AND THE REASON IS MEASURABLE: every service carrying these limits runs
`replicas: 1`, so per-pod state IS the global state. It stops being correct the moment one scales —
N pods give N times the limit, silently. That is exactly the failure `rate-limiting.md` files under
"in-process defaultdict rate limiter", and the estate's own values.yaml anticipates scaling
("stateless — scale freely in prod").

So the assumption is GATED rather than commented: if a rate-limited service's replica count goes
above one, the test below fails and names Redis as the fix. A limitation nobody can trip over
silently is a different thing from a limitation written in a docstring.
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from service_kit.rate_limit import RATE_LIMITED_SERVICES, by_subject, make_limiter, register_rate_limiting


def test_the_key_function_falls_back_to_IP_for_an_anonymous_caller() -> None:
    """ "Always include an `ip:` fallback so unauthenticated requests can't bypass the limit by
    omitting the header" — the reference's rule, and the whole point on an anonymous route."""

    class _Client:
        host = "10.0.0.7"

    req = type("R", (), {"state": type("S", (), {})(), "client": _Client(), "headers": {}})()
    assert by_subject(req) == "ip:10.0.0.7"  # ty: ignore[invalid-argument-type]


def test_the_key_function_prefers_the_authenticated_subject() -> None:
    """Keying by IP on an authenticated route is the corporate-NAT defect: thousands of users behind
    one address share a bucket and all get blocked together."""

    class _Client:
        host = "10.0.0.7"

    state = type("S", (), {"subject": "alice@example.com"})()
    req = type("R", (), {"state": state, "client": _Client(), "headers": {}})()
    assert by_subject(req) == "user:alice@example.com"  # ty: ignore[invalid-argument-type]


def test_a_429_is_problem_json_with_Retry_After() -> None:
    """The refusal has to be actionable and shaped like every other error in the estate.

    "Returning a plain 429 with no headers → clients can't self-throttle, retry storms hammer the API
    back into 429."
    """
    limiter = make_limiter()
    app = FastAPI()
    register_rate_limiting(app, limiter)

    @app.get("/expensive")
    @limiter.limit("1/minute")
    async def _expensive(request: Request) -> dict[str, str]:
        return {"ok": "yes"}

    with TestClient(app) as client:
        assert client.get("/expensive").status_code == 200
        resp = client.get("/expensive")

    assert resp.status_code == 429
    assert resp.headers["content-type"].startswith("application/problem+json")
    assert "Retry-After" in resp.headers
    body = resp.json()
    assert body["status"] == 429
    assert body["title"] == "Too Many Requests"


def test_probe_routes_are_never_limited() -> None:
    """ "Rate-limiting health checks → k8s probes fail under load → pods cycle → real outage."

    Asserted because the tempting shape of this change — a global middleware — breaks exactly this,
    and it is the reason the reference forbids one.
    """
    limiter = make_limiter()
    app = FastAPI()
    register_rate_limiting(app, limiter)

    @app.get("/livez")
    async def _livez() -> dict[str, str]:
        return {"status": "ok"}

    with TestClient(app) as client:
        for _ in range(50):
            assert client.get("/livez").status_code == 200


@pytest.mark.parametrize("service", sorted(RATE_LIMITED_SERVICES))
def test_a_rate_limited_service_still_runs_ONE_replica(service: str) -> None:
    """The tripwire that makes in-memory storage honest.

    slowapi's in-memory backend is per PROCESS. At one replica that is the global limit; at N it is
    N times the limit, silently — the caller sees no error and the estate enforces nothing like what it
    declares. Scaling one of these is therefore the moment shared storage (Redis, which this estate
    deliberately does not run) becomes justified, and this test is how that moment announces itself
    instead of passing unnoticed.
    """
    from pathlib import Path

    import yaml

    repo = Path(__file__).resolve().parents[3]
    values = yaml.safe_load((repo / "chart/values.yaml").read_text())

    for section in ("services", "explorer"):
        node = (values.get(section) or {}).get(service)
        if isinstance(node, dict) and "replicas" in node:
            assert node["replicas"] == 1, (
                f"{service} is rate-limited with slowapi's IN-MEMORY backend and now runs "
                f"{node['replicas']} replicas — the limit is silently multiplied by that number. "
                f"Move the limiter to shared storage (Redis) or drop the replica count back to 1."
            )
            return
    # No explicit replicas key means the chart default of 1, which is the assumption holding.
