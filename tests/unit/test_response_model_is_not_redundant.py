"""`response_model=` is for when the wire shape DIFFERS from the return type.

open_fastapi-audit — "21 routes across 7 services declare `response_model=` identical to the return
annotation — the redundant form the reference reserves for when the two DIFFER".

`core-conventions.md`: include a return type when possible, and use `response_model` instead "if the
return type differs from what should be sent over the wire (filtering, sensitive fields)".
`anti-patterns.md` names the redundant pair directly: "Return a Pydantic model AND set
`response_model=` to the same class".

The audit is careful about the cost, and so is this gate: it is COSMETIC. FastAPI infers the identical
response field from the return annotation alone and runs the identical dump-and-revalidate either way,
so the claimed per-row validation saving on the bounded listings does not exist. What is real is that
a reader cannot tell, at 21 sites, whether the two were meant to differ — which is exactly the signal
`response_model=` is supposed to carry.

WHAT STAYS. `response_model=None` is a different statement — it tells FastAPI to serialize nothing
from the annotation — and the sanctioned widening cases stay too. This gate only refuses the pair that
says the same thing twice.
"""

from __future__ import annotations

import ast
import pathlib


REPO = pathlib.Path(__file__).resolve().parents[2]


def _redundant_sites() -> list[str]:
    """Every route whose `response_model=` restates its own return annotation."""
    found: list[str] = []
    for path in (REPO / "services").rglob("*.py"):
        if "/tests/" in str(path):
            continue
        try:
            tree = ast.parse(path.read_text())
        except SyntaxError:  # pragma: no cover - not our files to parse
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.AsyncFunctionDef | ast.FunctionDef) or node.returns is None:
                continue
            returns = ast.unparse(node.returns)
            for deco in node.decorator_list:
                if not isinstance(deco, ast.Call):
                    continue
                for kw in deco.keywords:
                    if kw.arg != "response_model":
                        continue
                    declared = ast.unparse(kw.value)
                    # `response_model=None` is a DIFFERENT statement — it suppresses serialization from
                    # the annotation rather than restating it — and is sanctioned.
                    if declared != "None" and declared == returns:
                        found.append(f"{path.relative_to(REPO)}:{node.lineno} {node.name} -> {declared}")
    return sorted(found)


def test_no_route_restates_its_own_return_type() -> None:
    sites = _redundant_sites()
    assert not sites, (
        "these routes declare `response_model=` identical to their return annotation, so the argument "
        "carries no information — the reference reserves it for when the wire shape DIFFERS, and a "
        "reader cannot tell these were not meant to:\n  " + "\n  ".join(sites)
    )
