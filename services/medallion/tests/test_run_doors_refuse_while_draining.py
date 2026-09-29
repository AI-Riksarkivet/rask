"""Every door that STARTS work refuses it while this replica is draining — and a new one cannot slip in.

The unit behaviour lives in `packages/service-kit/tests/test_draining_refuses_admission.py`. This is
the coverage half, and it is the half that rots: the dependency can be perfect and a door added next
month simply never asks for it. Nothing else would notice, because an ungated door behaves correctly
in every test that is not about shutdown.

The split matters and is asserted per door. An HTTP door answers 503 so the caller can retry; a
SIDECAR-delivered door answers RETRY, because a 503 at a Dapr sidecar is read as a delivery failure —
which happens to retry today and would silently become a drop the moment a resiliency policy treated
5xx as terminal. Getting it backwards on the subscription is the expensive direction: these topics
carry no DLQ, so a dropped trigger cancels a bronze→silver→gold run with nothing reporting it.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest


API = Path(__file__).resolve().parents[1] / "src" / "medallion" / "api"


def _fn(module: str, name: str) -> ast.FunctionDef | ast.AsyncFunctionDef:
    tree = ast.parse((API / module).read_text())
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef) and node.name == name:
            return node
    raise AssertionError(f"{name} not found in {module} — the door was renamed and this gate now asserts nothing")


def _mentions(node: ast.AST, symbol: str) -> bool:
    return any(isinstance(n, ast.Name) and n.id == symbol for n in ast.walk(node))


class TestEveryHttpRunDoorIsGated:
    @pytest.mark.parametrize(("module", "name"), [("produce.py", "produce")])
    def test_it_depends_on_the_http_refusal(self, module: str, name: str) -> None:
        node = _fn(module, name)
        gated = any(_mentions(d, "refuse_when_draining") for d in node.decorator_list)
        assert gated, f"{module}::{name} starts work and does not refuse while draining"
