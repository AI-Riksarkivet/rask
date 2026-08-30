"""The history route's declared bound must exist and must not invert.

open_fastapi-audit — "`GET /v1/table/{id}/history` takes an unvalidated `limit: int = 50` that drives
one object-store transaction read per version — and `limit=-1` defeats the bound entirely, returning
all-but-one version".

Two defects, and the finding is careful that only one of them is what it first looks like.

**The bound is absent and unenforceable.** `limit: int = 50` with no `Query(...)` constraints is the
reference's named anti-pattern — "page-size with no upper bound … `Field(le=100)`". The docstring
claims a bound the signature does not have.

**The negative value INVERTS it.** The implementation slices `versions()[:limit]`, so `?limit=-1`
does not mean "no limit" or "one item": Python reads it as all-but-the-last, returning nearly the
whole history. A parameter whose bound can be turned inside out by a minus sign is worse than one
with no bound, because the caller who passes it gets a plausible-looking answer.

WHAT IT IS NOT, and the finding says so plainly: not a caller-controlled amplification. `versions()`
cannot manufacture reads that do not exist, so `?limit=100000` on a five-version table does five
reads. The cost is bounded by the table's real history. That is why this is medium and why the fix is
validation at the boundary rather than a redesign.

`ge=1` alone kills the inversion; `le` makes the docstring's claim true.
"""

from __future__ import annotations

import pytest
from fastapi.routing import APIRoute

from catalog.api.v1.endpoints import versions as versions_ep


def _limit_param():
    for route in versions_ep.router.routes:
        if isinstance(route, APIRoute) and route.path.endswith("/history"):
            for field in route.dependant.query_params:
                if field.name == "limit":
                    return field
            pytest.fail("the history route declares no `limit` query parameter")
    pytest.fail("no history route on the versions router")


def test_the_limit_cannot_be_negative() -> None:
    """`versions()[:limit]` with a negative limit returns all-but-N, not N — the bound inverted."""
    limits = {type(c).__name__: c for c in _limit_param().field_info.metadata}
    assert "Ge" in limits and limits["Ge"].ge >= 1, (
        "history's `limit` accepts a negative value, and the implementation slices `versions()[:limit]` "
        "— so `?limit=-1` returns all-but-one version instead of refusing"
    )


def test_the_limit_has_a_real_ceiling() -> None:
    """The docstring claims a bound; the signature must actually carry one."""
    limits = {type(c).__name__: c for c in _limit_param().field_info.metadata}
    assert "Le" in limits, (
        "history's `limit` has no upper bound — the reference's named anti-pattern, and the route's own "
        "docstring describes a ceiling the signature does not enforce"
    )
    assert limits["Le"].le <= 200, f"the ceiling is {limits['Le'].le}; the sibling annotator route caps at 200"


@pytest.mark.parametrize("bad", [-1, 0])
def test_a_refused_limit_is_a_422_not_a_surprising_page(bad: int) -> None:
    """Validated at the BOUNDARY, so the caller is told — not clamped silently downstream."""
    from fastapi import FastAPI
    from fastapi.routing import APIRoute as _Route
    from fastapi.testclient import TestClient

    app = FastAPI()
    app.include_router(versions_ep.router)

    # The route's own dependencies (namespace, settings, storage options, token, FGA client) cannot
    # resolve in a bare app and raise before anything else — which showed up as a 500 and would have
    # hidden whether validation ran at all. Overridden to nothing, so what this measures is the
    # PARAMETER contract: a refused limit must be a 422 the caller can read, not a surprising page.
    # From the ROUTER, not `app.routes`: this FastAPI version keeps included routers as
    # `_IncludedRouter` wrappers, so scanning the app finds no APIRoute at all.
    route = next(r for r in versions_ep.router.routes if isinstance(r, _Route) and r.path.endswith("/history"))
    for dep in route.dependant.dependencies:
        if dep.call is not None:
            app.dependency_overrides[dep.call] = lambda: None

    response = TestClient(app, raise_server_exceptions=False).get(f"/v1/table/x/history?limit={bad}")
    assert response.status_code == 422, f"limit={bad} was accepted (status {response.status_code})"
