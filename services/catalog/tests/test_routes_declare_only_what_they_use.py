"""catalog-api-20 — a route's signature lists the dependencies it uses, and nothing else.

Two task routes declared ``token: CurrentToken`` and never referenced it; nineteen parameters across
the plane were the same shape (an unused ``settings``, an unused FGA client, an unused ``Request``).
A declared-but-unused dependency is not free: it is a signature that misstates what the handler needs,
and on the authorization surface it reads as "this route consults the token" when it does not.

Removing them does NOT weaken the door — ``api_router = APIRouter(dependencies=[Depends(authorize)])``
and ``authorize`` itself takes ``CurrentToken``, so ``authenticate`` runs for every route in the plane
regardless of what any single handler names. ``test_the_task_routes_are_still_authenticated`` below
pins that, because "delete the unused auth dependency" is exactly the cleanup that can silently open a
door.
"""

from __future__ import annotations

import ast
import pathlib

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from catalog.api.v1.router import api_router


_ENDPOINTS = pathlib.Path(__file__).resolve().parents[1] / "src" / "catalog" / "api" / "v1" / "endpoints"

#: The injected parameter types a route may declare — every one of them is resolved by FastAPI, and
#: every one of them is dead weight when the body never reads it.
_INJECTED = {
    "CurrentToken",
    "SettingsDep",
    "NamespaceDep",
    "StorageOptionsDep",
    "FgaClientDep",
    "LineageEmitterDep",
    "ControlEmitterDep",
    "VendorDep",
    "UserStateStoreDep",
    "EstateFgaClient",
    "Request",
}


def _offences(path: pathlib.Path) -> list[str]:
    tree = ast.parse(path.read_text())
    found: list[str] = []
    for fn in ast.walk(tree):
        if not isinstance(fn, ast.FunctionDef | ast.AsyncFunctionDef):
            continue
        if "router" not in ast.dump(ast.Module(body=[ast.Expr(d) for d in fn.decorator_list], type_ignores=[])):
            continue
        used = {n.id for n in ast.walk(ast.Module(body=fn.body, type_ignores=[])) if isinstance(n, ast.Name)}
        found += [
            f"{path.name}:{fn.lineno} {fn.name}({a.arg}: {a.annotation.id})"
            for a in [*fn.args.args, *fn.args.kwonlyargs]
            if isinstance(a.annotation, ast.Name) and a.annotation.id in _INJECTED and a.arg not in used
        ]
    return found


def test_the_walk_sees_the_endpoint_plane() -> None:
    modules = [p for p in _ENDPOINTS.glob("*.py") if p.name != "__init__.py"]
    assert len(modules) > 10, f"only {len(modules)} endpoint modules — the walk is not seeing the plane"


def test_no_route_declares_a_dependency_its_body_never_uses() -> None:
    offences = [o for path in sorted(_ENDPOINTS.glob("*.py")) for o in _offences(path)]
    assert not offences, "route parameters nothing in the handler reads — drop them:\n  " + "\n  ".join(offences)


@pytest.mark.parametrize("path", ["/v1/table/db1$t/tasks", "/v1/namespace/db1/tasks"])
def test_the_task_routes_are_still_authenticated(path: str) -> None:
    """The removal must not be the thing that opens the door: with the router's gate refusing, the
    route must never run its body. A stand-in `authorize` proves the ROUTER dependency is what
    enforces it, independently of any parameter a handler happens to declare."""
    app = FastAPI()
    ran: list[str] = []

    def _refuse() -> None:
        ran.append(path)
        raise RuntimeError("authorize ran")

    from catalog.api import fga_deps

    app.dependency_overrides[fga_deps.authorize] = _refuse
    app.include_router(api_router)
    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.get(path)
    # The CALL is the assertion — a 500 alone could come from anywhere, including a handler that ran
    # ungated and then failed on something else.
    assert ran == [path], f"the router-level gate never ran for {path} (status {response.status_code})"
    assert response.status_code == 500, "the request was answered despite the gate refusing"
