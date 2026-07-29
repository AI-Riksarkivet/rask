"""Conformance: every lance-namespace spec operation has a served catalog route (todo P1 #9).

Locks the catalog as a FAITHFUL REST surface over the spec — a spec op never wired, or a route renamed
away from the spec, turns this red. Routes are read from ``app.openapi()`` (NOT ``app.routes``): starlette
1.3's lazy ``include_router`` keeps included routes off ``app.routes``, so ``openapi()`` is the authoritative
served set. The app may serve MORE than the spec (the ``/credentials`` vending extension + health probes) —
we assert only that the spec is a SUBSET of what's served, on (method, structural-path).
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import yaml
from fastapi.testclient import TestClient


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


def test_spec_has_54_operations() -> None:
    # Guards against a stale/shrunken vendored spec silently weakening the conformance check above.
    op_ids = [op.get("operationId") for item in _spec().get("paths", {}).values() for op in item.values() if isinstance(op, dict) and op.get("operationId")]
    assert len(op_ids) == 54
