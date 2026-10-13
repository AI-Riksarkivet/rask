"""Conformance: every lance-namespace spec operation has a served catalog route (todo P1 #9).

Locks the catalog as a FAITHFUL REST surface over the spec — a spec op never wired, or a route renamed
away from the spec, turns this red. Routes are read from ``app.openapi()`` (NOT ``app.routes``): starlette
1.3's lazy ``include_router`` keeps included routes off ``app.routes``, so ``openapi()`` is the authoritative
served set. The app may serve MORE than the spec (the ``/credentials`` vending extension + health probes) —
we assert only that the spec is a SUBSET of what's served, on (method, structural-path).

THE OTHER DIRECTION ([[LH-021]], LH-476): every route under ``/v1`` must be a spec operation, because a
spec client meets it there; rask's own routes live under ``/management/v1``, the way Lakekeeper separates
``/management`` from ``/catalog``. The spec side of that check is
``service_kit.lakehouse.spec_routes.SPEC_ROUTES``, committed because no image carries ``lance_docs/``.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, cast

import yaml
from fastapi import FastAPI
from fastapi.testclient import TestClient

from service_kit.lakehouse.spec_routes import SPEC_ROUTES


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


#: The spec owns the whole of ``/v1``. rask's own surface is ``/management/v1``, and the probes sit at the root.
_SPEC_PREFIX = "/v1/"

#: PERMANENT, JUSTIFIED DEVIATIONS: spec operations reached by the method the shipped client sends.
#:
#: The Lance namespace spec defines `count_rows` and `tags/list` as POST at every tag from v0.9.0 to
#: v0.12.0. But the REST client pylance BUNDLES (`rust/lance-namespace-impls/src/rest.rs`) calls
#: `get_json` for exactly those two, and the reference SERVER it bundles mounts them as GET — the lance
#: repo disagrees with its own document on both sides of the wire, and `lance_namespace.connect("rest",
#: …)` resolves to that class. rask dual-mounts so the stock client works; mounting POST only gave it
#: FastAPI's 405, which carries no `code` and surfaced as `InternalError 18`. Moving them to
#: `/management/v1` would break the stock client, and deleting them would too.
#: `tests/integration/test_spec_method_and_status_conformance.py` (A3) holds the behaviour; the real
#: fix is upstream, a one-line change in lance, and this set shrinks when that lands.
_METHOD_ALIASES_THE_STOCK_CLIENT_SENDS: frozenset[tuple[str, str]] = frozenset(
    {
        ("GET", "/v1/table/{}/count_rows"),  # spec: POST
        ("GET", "/v1/table/{}/tags/list"),  # spec: POST
    }
)


def _on_the_spec_surface(routes: set[tuple[str, str]]) -> set[tuple[str, str]]:
    return {(method, path) for method, path in routes if path.startswith(_SPEC_PREFIX)}


def test_the_v1_surface_holds_only_spec_operations(client: TestClient) -> None:
    """Every route the catalog serves under ``/v1`` is a spec operation; rask-only routes are served only under ``/management/v1``.

    `client.app` is typed as the ASGI protocol, which has no `openapi`; the cast says what the fixture
    actually builds rather than silencing the checker.
    """
    served = _ops(cast(FastAPI, client.app).openapi().get("paths", {}))
    intruders = _on_the_spec_surface(served) - SPEC_ROUTES - _METHOD_ALIASES_THE_STOCK_CLIENT_SENDS

    assert not intruders, (
        f"{len(intruders)} rask-only operation(s) are served on the SPEC surface, where a spec client "
        f"meets them:\n  " + "\n  ".join(f"{m} {p}" for m, p in sorted(intruders)) + "\n"
        "Mount them on the management prefix instead."
    )
