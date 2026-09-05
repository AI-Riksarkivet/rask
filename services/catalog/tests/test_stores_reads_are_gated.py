"""Reading the estate's object stores takes the same privilege as attaching one.

docs/DECISIONS.md "The Python estate audit" `catalog-api-02` (E1, HIGH) — "GET /v1/stores and /v1/stores/tiers disclose the
whole estate's buckets and hosts with no authorization gate, while the sibling POST calls that same
registry estate-admin gated".

THE ASYMMETRY IS THE DEFECT. `attach_store` gates on `can_observe_events` against the root object,
and its own docstring says why: "attaching names a host and a bucket the whole estate will then see,
which is estate-wide disclosure and so takes the estate-wide privilege". Every word of that applies
to READING the list — more so, because the read is the disclosure. The two GETs took only
`UserStateStoreDep`: no token, no client, no gate.

WHY THE ROUTER-LEVEL GUARD CANNOT COVER THEM, and this is the part worth remembering. `fga_deps.
authorize` runs on every v1 route, but it authorizes only paths that carry an `{id}` under one of
`_RESOURCES` (namespace/table/materialized_view/transaction). Its own comment states the fallthrough:
"Other id-less routes (collection lists, health) need only authentication." So an id-less collection
route is authenticated and nothing more, by design — which is correct for a list the endpoint then
FILTERS, and a hole for one it does not.

That distinction is invisible at the call site, so the second test below makes it visible: it
enumerates every route the router guard lets through on authn alone and holds the set against an
allowlist that says, per route, WHY authentication is sufficient. Three answers are legitimate — the
endpoint FGA-filters its result per item (`/v1/model`, `/v1/table`), the document is keyed on the
caller's own subject (`/v1/user-state/*`), or the endpoint gates inside a helper
(`/v1/warehouses/{id}/activate` via `_set_warehouse_status`). `/v1/stores` had no answer.
"""

from __future__ import annotations

import inspect
import logging
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

from catalog.api import fga_deps
from service_kit.lakehouse.ns_errors import install_problem_handlers


# ── the two routes the finding names ─────────────────────────────────────────────────────────────
#
# Driven through the REAL app rather than by calling the handler, so the test does not depend on the
# handler's signature — the signature change IS half the fix, and a test that asserted it would go
# red for the shape rather than for the behaviour. This way the request also traverses the
# router-level `authorize`, which is the guard the finding says cannot cover these routes: it lets
# the call through, and what refuses is the endpoint's own gate.


def _app(*, fga_enabled: bool, allow: bool, subject: str | None = "carol") -> FastAPI:
    from catalog.api.dependencies import get_fga_client, get_settings, get_user_state_store
    from catalog.api.security import authenticate
    from catalog.api.v1.router import api_router
    from service_kit.exceptions import register_handlers

    settings = SimpleNamespace(fga_enabled=fga_enabled, fga_root_object="warehouse:lance_catalog")
    app = FastAPI()
    app.include_router(api_router)
    register_handlers(app)
    install_problem_handlers(app, logging.getLogger(__name__))
    app.state.fga = object() if fga_enabled else None
    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[get_fga_client] = lambda: object() if fga_enabled else None
    app.dependency_overrides[authenticate] = lambda: SimpleNamespace(sub=subject) if subject else None
    app.dependency_overrides[get_user_state_store] = lambda: None
    return app


@pytest.fixture
def decide(monkeypatch: pytest.MonkeyPatch):
    """Make every FGA check answer a fixed verdict."""

    def _set(allowed: bool) -> None:
        async def check(_client: object, *, user: str, relation: str, obj: str) -> bool:
            return allowed

        monkeypatch.setattr(fga_deps.fga, "check", check)

    return _set


@pytest.mark.parametrize("path", ["/v1/stores", "/v1/stores/tiers"])
def test_a_non_estate_admin_cannot_read_the_store_registry(path: str, decide) -> None:
    """The defect, at the wire: an authenticated project member enumerated every bucket and host."""
    decide(False)
    response = TestClient(_app(fga_enabled=True, allow=False), raise_server_exceptions=False).get(path)
    assert response.status_code == 403, f"{path} disclosed the estate's stores to a non-estate-admin ({response.status_code})"


@pytest.mark.parametrize("path", ["/v1/stores", "/v1/stores/tiers"])
def test_an_estate_admin_still_reads_it(path: str, decide) -> None:
    """The gate must refuse the wrong caller, not the route."""
    decide(True)
    response = TestClient(_app(fga_enabled=True, allow=True), raise_server_exceptions=False).get(path)
    assert response.status_code == 200, f"{path} refused an estate admin ({response.status_code}): {response.text[:200]}"


@pytest.mark.parametrize("path", ["/v1/stores", "/v1/stores/tiers"])
def test_dev_is_unchanged_when_fga_is_off(path: str) -> None:
    """`require_relation` is a no-op with FGA off and must stay one: a local run browses as before."""
    response = TestClient(_app(fga_enabled=False, allow=True, subject=None), raise_server_exceptions=False).get(path)
    assert response.status_code == 200, f"{path} broke the FGA-off dev path ({response.status_code}): {response.text[:200]}"


# ── the durable half: which routes may run on authentication alone ───────────────────────────────


#: Every route `authorize` lets through on authn alone, with the reason authn IS the right gate.
#: A new entry here is a claim someone has to justify; a new route NOT here fails the test.
_AUTHN_IS_ENOUGH: dict[str, str] = {
    "/v1/me": "describes the caller's OWN identity, and 401s an anonymous call itself",
    "/v1/model": "FGA-filtered per model (can_get_metadata), like the governed table listing",
    "/v1/table": "FGA-filtered per table (can_read_data)",
    "/v1/warehouses": "FGA-filtered per warehouse (can_get_metadata), and reports X-Authorization-Truncated",
    "/v1/warehouses/-/bindings": "FGA-filtered per warehouse (can_get_metadata) — a binding names another tenant's bucket",
    "/v1/user-state/dock-layout": "keyed on the caller's own subject",
    "/v1/user-state/dock-layout-library": "keyed on the caller's own subject",
    "/v1/user-state/saved-views": "keyed on the caller's own subject",
    "/v1/user-state/workflow-graph": "keyed on the caller's own subject",
    "/v1/warehouses/{warehouse_id}/activate": "gated inside _set_warehouse_status on the warehouse's own project",
    "/v1/warehouses/{warehouse_id}/deactivate": "gated inside _set_warehouse_status on the warehouse's own project",
}


def _mounted_routes() -> list[APIRoute]:
    """Every APIRoute under the v1 router.

    DESCENDS `original_router`. `api_router.routes` holds `_IncludedRouter` WRAPPERS in this FastAPI
    version, not APIRoutes — a walk that reads it directly finds zero routes and passes vacuously,
    which is exactly how an earlier version of this kind of gate reported an app with two dozen open
    routes as clean.
    """
    from catalog.api.v1.router import api_router

    def descend(router: object) -> list[APIRoute]:
        found: list[APIRoute] = []
        for route in getattr(router, "routes", []):
            if isinstance(route, APIRoute):
                found.append(route)
            inner = getattr(route, "original_router", None)
            if inner is not None:
                found.extend(descend(inner))
        return found

    routes = descend(api_router)
    assert len(routes) > 100, f"only {len(routes)} routes found — the walk is not seeing the catalog, so it would pass vacuously"
    return routes


def _declares_a_gate(route: APIRoute) -> bool:
    names: list[str] = []

    def walk(dependant: object) -> None:
        call = getattr(dependant, "call", None)
        if call is not None:
            names.append(getattr(call, "__name__", ""))
        for sub in getattr(dependant, "dependencies", []):
            walk(sub)

    walk(route.dependant)
    if any(n.startswith("require_") or "estate_gate" in n for n in names):
        return True
    try:
        source = inspect.getsource(route.endpoint)
    except (OSError, TypeError):
        return False
    return "require_relation" in source or "fga_deps.require_" in source


def test_every_route_the_router_guard_waves_through_has_a_reason() -> None:
    """`authorize` authorizes only `{id}` routes under `_RESOURCES`; everything else is authn-only.
    That fallthrough is fine where the endpoint filters or owns its subject, and a hole otherwise."""
    unexplained: list[str] = []
    for route in _mounted_routes():
        if "{id}" in route.path and fga_deps._resource_for(route.path) is not None:
            continue  # the router guard authorizes these
        if route.path.rstrip("/") in fga_deps._BATCH_PATHS:
            continue  # body-keyed batch, authorized by _authorize_batch
        if _declares_a_gate(route):
            continue
        if route.path in _AUTHN_IS_ENOUGH:
            continue
        method = sorted((route.methods or set()) - {"HEAD", "OPTIONS"})
        unexplained.append(f"{method} {route.path} ({getattr(route.endpoint, '__name__', route.endpoint)})")

    assert not unexplained, (
        "these routes run on authentication alone with no gate and no recorded reason — any "
        "authenticated principal reaches them:\n  " + "\n  ".join(sorted(unexplained))
    )


# ── the error ENVELOPE: rask-extension routes still speak the spec's problem dialect ─────────────
#
# docs/DECISIONS.md "The Python estate audit" `catalog-api-01` + `RV-03` — one defect, two modules: stores.py and members.py
# imported the FLEET taxonomy (`service_kit.exceptions`) instead of `lance_namespace`, so their
# bodies rendered as problem+json (that half was fixed by `745af135` installing `register_handlers`)
# but carried only four keys — no `code`, no `error`, and a `type` in `about:blank#` rather than
# `https://lance.org/problems/`. The estate's own invariant (`test_problem_bodies_carry_a_code`)
# pins six keys for every body the catalog emits; these routes were the exceptions.


def test_the_attach_refusal_carries_the_spec_code() -> None:
    """Witnessed RED: this exact request answered a 4-key `about:blank#serviceunavailableerror`."""
    response = TestClient(_app(fga_enabled=False, allow=True, subject=None), raise_server_exceptions=False).post(
        "/v1/stores",
        json={"name": "x", "bucket": "b", "role": "bronze", "description": "", "read_only": False},
    )
    assert response.status_code == 503
    assert response.headers["content-type"].startswith("application/problem+json")
    body = response.json()
    assert set(body) >= {"type", "title", "status", "detail", "code", "error"}, f"the envelope omits the spec keys — got {sorted(body)}"
    assert body["type"].startswith("https://lance.org/problems/"), f"the type is off-namespace: {body['type']}"


def test_membership_without_authz_is_a_503_in_the_spec_envelope() -> None:
    """RV-03's site — and the ONE deliberate status change in this closure: 409 -> 503. Membership
    IS tuples, so authz-off is the service being UNAVAILABLE for this operation, not a conflict with
    anything; `access.py` already answers 503 for the identical condition, and two doors giving two
    statuses for one condition is how a client learns the wrong lesson."""
    response = TestClient(_app(fga_enabled=False, allow=True, subject="carol"), raise_server_exceptions=False).put(
        "/v1/projects/proj1/members",
        json={"user": "user:bob", "relation": "member"},
    )
    assert response.status_code == 503, f"authz-off membership answered {response.status_code}"
    body = response.json()
    assert set(body) >= {"type", "title", "status", "detail", "code", "error"}, f"the envelope omits the spec keys — got {sorted(body)}"
