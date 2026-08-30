"""catalog-api-14 — the repeated get/put/delete triples share ONE body each, not one per document.

``user_state.py`` shipped FOUR get/put/delete triples (workflow-graph, saved-views, dock-layout,
dock-layout-library) and ``policies.py`` THREE set/describe/delete triples (table, namespace,
project). The routes themselves have to stay explicit — FastAPI resolves each PUT's body model and
each route's ``response_model_exclude_none`` posture from the declaration, and two of the four user-
state documents deliberately differ on that flag — but their BODIES were copied verbatim: the same
eight-line envelope construction four times over, the same "read it, 404 if absent" three times, the
same "delete it, log it, emit the control event" three times.

Copied bodies drift silently. The measurable property is therefore the number of PLACES that build an
envelope, delete a policy, or mint the not-found refusal — not the number of routes.

The behavioural half is asserted alongside: the routes, their paths and their per-document postures
must all survive the collapse.
"""

from __future__ import annotations

import ast
import pathlib

from fastapi import APIRouter
from fastapi.routing import APIRoute

from catalog.api.v1.endpoints import policies as p_ep
from catalog.api.v1.endpoints import user_state as us_ep


_ENDPOINTS = pathlib.Path(__file__).resolve().parents[1] / "src" / "catalog" / "api" / "v1" / "endpoints"


def _functions_naming(module: str, dotted: str) -> list[str]:
    """Every function in ``module`` whose body REFERENCES ``a.b`` — called or handed to a threadpool."""
    owner, attr = dotted.split(".")
    tree = ast.parse((_ENDPOINTS / module).read_text())
    return [
        f"{fn.name}:{fn.lineno}"
        for fn in ast.walk(tree)
        if isinstance(fn, ast.FunctionDef | ast.AsyncFunctionDef)
        for node in ast.walk(fn)
        if isinstance(node, ast.Attribute) and node.attr == attr and isinstance(node.value, ast.Name) and node.value.id == owner
    ]


def _envelope_constructions() -> list[str]:
    """Every ``UserStateEnvelope[...](...)`` construction site."""
    tree = ast.parse((_ENDPOINTS / "user_state.py").read_text())
    return [
        f"{fn.name}:{node.lineno}"
        for fn in ast.walk(tree)
        if isinstance(fn, ast.FunctionDef | ast.AsyncFunctionDef)
        for node in ast.walk(fn)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Subscript)
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "UserStateEnvelope"
    ]


# ── the shape ─────────────────────────────────────────────────────────────────────────────────────


def test_the_walk_sees_both_modules() -> None:
    assert (_ENDPOINTS / "user_state.py").is_file() and (_ENDPOINTS / "policies.py").is_file()
    assert _envelope_constructions(), "no envelope construction found — the walk is not seeing user_state.py"


def test_the_user_state_envelope_is_built_in_at_most_two_places() -> None:
    """One for a READ (which may find nothing) and one for a WRITE (which always has a value)."""
    sites = _envelope_constructions()
    assert len(sites) <= 2, f"the response envelope is hand-built {len(sites)}x — one per document: {sites}"


def test_a_policy_is_deleted_from_one_place() -> None:
    sites = _functions_naming("policies.py", "policies.delete_policy")
    assert sites, "no delete_policy reference found — the walk is not seeing policies.py"
    assert len(sites) == 1, f"the policy-delete body is copied per rung: {sites}"


def test_a_missing_policy_is_reported_from_one_place() -> None:
    """The three describe handlers each carried their own read-then-404 — and each picked its own
    exception class for it, which is exactly the drift a copied body produces."""
    tree = ast.parse((_ENDPOINTS / "policies.py").read_text())
    sites = [
        f"{fn.name}:{node.lineno}"
        for fn in ast.walk(tree)
        if isinstance(fn, ast.FunctionDef | ast.AsyncFunctionDef)
        for node in ast.walk(fn)
        # `if record is None: raise …` — the read-then-404 the three describe handlers each carried.
        if isinstance(node, ast.If)
        and isinstance(node.test, ast.Compare)
        and isinstance(node.test.left, ast.Name)
        and node.test.left.id == "record"
        and any(isinstance(n, ast.Raise) for n in node.body)
    ]
    assert len(sites) <= 1, f"the 'no policy set' refusal is minted per rung: {sites}"


# ── the behaviour the collapse must not change ────────────────────────────────────────────────────


def _routes(*routers: APIRouter) -> dict[tuple[str, str], APIRoute]:
    """Read off the LEAF routers: this FastAPI defers ``include_router``, so an aggregator's
    ``.routes`` holds ``_IncludedRouter`` placeholders until the app is set up."""
    return {(r.path, method): r for router in routers for r in router.routes if isinstance(r, APIRoute) for method in sorted(r.methods or [])}


def test_every_user_state_route_survives_with_its_own_posture() -> None:
    routes = _routes(us_ep.router)
    for path in ("/v1/user-state/workflow-graph", "/v1/user-state/saved-views", "/v1/user-state/dock-layout", "/v1/user-state/dock-layout-library"):
        assert {(path, "GET"), (path, "PUT"), (path, "DELETE")} <= set(routes), f"{path} lost a verb"
    # The two dock documents hand back an OPAQUE dockview payload, so stripping nulls would MUTATE a
    # document these routes exist to return unchanged — the flag differs on purpose and must survive.
    assert routes[("/v1/user-state/workflow-graph", "GET")].response_model_exclude_none is True
    assert routes[("/v1/user-state/saved-views", "GET")].response_model_exclude_none is True
    assert routes[("/v1/user-state/dock-layout", "GET")].response_model_exclude_none is False
    assert routes[("/v1/user-state/dock-layout-library", "GET")].response_model_exclude_none is False


def test_every_policy_route_survives() -> None:
    paths = {path for path, _ in _routes(p_ep.table_router, p_ep.namespace_router, p_ep.project_router, p_ep.projects_router)}
    for prefix in ("/v1/table/{id}", "/v1/namespace/{id}", "/v1/project/{id}"):
        assert {f"{prefix}/policy/set", f"{prefix}/policy/describe", f"{prefix}/policy/delete"} <= paths, f"{prefix} lost a policy route"
