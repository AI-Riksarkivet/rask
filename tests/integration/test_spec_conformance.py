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
    assert len(_op_ids(_spec())) == 54


#: Where the spec is published. Named once so the freshness check below and any future fetch agree.
_UPSTREAM = "https://raw.githubusercontent.com/lance-format/lance-namespace/main/docs/src/spec.yaml"


def _op_ids(doc: dict[str, Any]) -> set[str]:
    return {op["operationId"] for item in doc.get("paths", {}).values() for op in item.values() if isinstance(op, dict) and op.get("operationId")}


def test_the_vendored_spec_still_matches_UPSTREAM() -> None:
    """The half the count above structurally cannot see: whether the SPEC GREW.

    `== 54` catches our vendored copy shrinking and nothing else. If upstream adds a 55th operation, our
    copy stays at 54, this suite stays green, and the estate is silently non-conformant against a spec
    nobody re-read — the conformance gate reporting on a document rather than on the contract.

    NETWORK-GATED and SKIPS without one, the same shape `_helm_template` uses for a missing binary: a
    gate that fails on an offline laptop teaches people to ignore it, and this one is about drift over
    weeks rather than about the commit in front of you.

    Compared by operation ID rather than by count, so "they added one and removed one" cannot pass.
    Measured 2026-09-09: upstream 1.0.0 / 54 ops, vendored 1.0.0 / 54 ops, zero difference either way.
    """
    import urllib.error
    import urllib.request

    import pytest

    try:
        with urllib.request.urlopen(_UPSTREAM, timeout=20) as response:  # noqa: S310 — a pinned https URL, not caller input
            upstream = yaml.safe_load(response.read())
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        pytest.skip(f"upstream spec unreachable ({exc}) — freshness is a drift check, not a commit gate")

    ours, theirs = _op_ids(_spec()), _op_ids(upstream)
    assert theirs, "upstream spec parsed to zero operations — the URL moved; re-point _UPSTREAM"
    assert not theirs - ours, (
        f"upstream defines operations the vendored spec does not: {sorted(theirs - ours)}. The catalog is "
        "non-conformant against the CURRENT spec, and the count guard above cannot see it — re-vendor "
        f"{_UPSTREAM} and wire the new routes"
    )
    assert not ours - theirs, (
        f"the vendored spec carries operations upstream has RETIRED: {sorted(ours - theirs)} — the "
        "conformance check is asserting against a contract that no longer exists"
    )
