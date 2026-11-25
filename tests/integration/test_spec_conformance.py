"""Conformance: every lance-namespace spec operation has a served catalog route (todo P1 #9).

Locks the catalog as a FAITHFUL REST surface over the spec — a spec op never wired, or a route renamed
away from the spec, turns this red. Routes are read from ``app.openapi()`` (NOT ``app.routes``): starlette
1.3's lazy ``include_router`` keeps included routes off ``app.routes``, so ``openapi()`` is the authoritative
served set. The app may serve MORE than the spec (the ``/credentials`` vending extension + health probes) —
we assert only that the spec is a SUBSET of what's served, on (method, structural-path).

THE OTHER DIRECTION ([[LH-021]], LH-476): every route under ``/v1`` must be a spec operation, because a
spec client meets it there; rask's own routes live under ``/management/v1``, apart from the spec's. The spec side
of that check is
``lakehouse.problems.spec_routes.SPEC_ROUTES``, committed because no image carries ``lance_docs/``.
"""

import re
from pathlib import Path
from typing import Any, cast

import yaml
from fastapi import FastAPI
from fastapi.testclient import TestClient

from lakehouse.problems.spec_routes import SPEC_ROUTES


_SPEC = Path(__file__).resolve().parents[2] / "lance_docs" / "ns_catalog" / "spec.yaml"
_METHODS = frozenset({"get", "put", "post", "delete", "patch"})


def _ops(paths: dict[str, Any]) -> set[tuple[str, str]]:
    """(METHOD, structural-path) per operation — path-param NAMES collapsed so ``{id}`` matches ``{x}``."""
    out: set[tuple[str, str]] = set()
    for path, item in paths.items():
        structural = re.sub(r"\{[^}]+\}", "{}", path)
        out.update((m.upper(), structural) for m in item if m.lower() in _METHODS)
    return out


def _spec() -> dict[str, Any]:
    return yaml.safe_load(_SPEC.read_text())


def test_every_spec_operation_is_served(client: TestClient) -> None:
    spec_ops = _ops(_spec().get("paths", {}))
    served = _ops(client.app.openapi().get("paths", {}))
    missing = spec_ops - served
    assert not missing, f"spec operations with NO served route: {sorted(missing)}"


def _error_statuses(paths: dict[str, Any]) -> dict[tuple[str, str], frozenset[str]]:
    """(METHOD, structural path) -> the 4xx and 5xx statuses each operation declares."""
    return {
        (method.upper(), re.sub(r"\{[^}]+\}", "{}", path)): frozenset(str(status) for status in operation.get("responses", {}) if str(status)[0] in "45")
        for path, item in paths.items()
        for method, operation in item.items()
        if method.lower() in _METHODS
    }


def test_every_spec_operation_publishes_the_error_statuses_the_spec_lists(client: TestClient) -> None:
    """The catalog's OpenAPI declares on each spec operation exactly the error statuses ``spec.yaml`` lists for it
    (``lakehouse.problems.spec_routes.SPEC_ERROR_STATUSES``, committed because no image carries ``lance_docs/``) and the 406 any of
    them may answer, the spec's status for code 0 (ADR 0020 row 11)."""
    listed = _error_statuses(_spec().get("paths", {}))
    served = _error_statuses(client.app.openapi().get("paths", {}))

    assert {operation: served.get(operation) for operation in listed} == {operation: statuses | {"406"} for operation, statuses in listed.items()}


#: The spec owns the whole of ``/v1``. rask's own surface is ``/management/v1``, and the probes sit at the root.
_SPEC_PREFIX = "/v1/"


def _on_the_spec_surface(routes: set[tuple[str, str]]) -> set[tuple[str, str]]:
    return {(method, path) for method, path in routes if path.startswith(_SPEC_PREFIX)}


def test_the_v1_surface_holds_only_spec_operations(client: TestClient) -> None:
    """Every route the catalog serves under ``/v1`` is a spec operation; rask-only routes are served only under ``/management/v1``.

    `client.app` is typed as the ASGI protocol, which has no `openapi`; the cast says what the fixture
    actually builds rather than silencing the checker.
    """
    served = _ops(cast(FastAPI, client.app).openapi().get("paths", {}))
    intruders = _on_the_spec_surface(served) - SPEC_ROUTES

    assert not intruders, (
        f"{len(intruders)} rask-only operation(s) are served on the SPEC surface, where a spec client "
        f"meets them:\n  " + "\n  ".join(f"{m} {p}" for m, p in sorted(intruders)) + "\n"
        "Mount them on the management prefix instead."
    )


_PROBLEM_BODY = {"application/problem+json": {"schema": {"$ref": "#/components/schemas/ProblemDetails"}}}
#: The probes answer 503 with their own model: a probe's body is the process's answer, not an error's.
_PROBES = frozenset({"/livez", "/readyz"})


def _undeclared_problems(schema: dict[str, Any]) -> list[str]:
    """Each operation whose published errors do not name the problem body: a 4xx or 5xx carrying another body (but a
    probe's 503), or no ``default`` for the statuses the operation does not list."""
    offenders: list[str] = []
    for path, item in schema.get("paths", {}).items():
        for method, operation in item.items():
            if method.lower() not in _METHODS:
                continue
            responses = operation.get("responses", {})
            offenders += [
                f"{method.upper()} {path} {status}: {sorted(response['content'])}"
                for status, response in responses.items()
                if (str(status)[0] in "45" or status == "default")
                and response.get("content")
                and response["content"] != _PROBLEM_BODY
                and not (path in _PROBES and status == "503")
            ]
            if "default" not in responses:
                offenders.append(f"{method.upper()} {path}: no default")
    return offenders


def test_every_error_both_documents_publish_is_the_problem_body(client: TestClient) -> None:
    """Every door of the catalog and lineage answers a failure in the one body (`install_problem_handlers`), so the
    published documents say so: a rask-only door's 422 is the problem body with its ``errors``, and every operation's
    ``default`` names it."""
    from lineage.main import app as lineage

    documents = {"catalog": cast(FastAPI, client.app).openapi(), "lineage": lineage.openapi()}

    assert {name: _undeclared_problems(schema)[:3] for name, schema in documents.items()} == {"catalog": [], "lineage": []}
    assert "instance" in documents["lineage"]["components"]["schemas"]["ProblemDetails"]["properties"]
