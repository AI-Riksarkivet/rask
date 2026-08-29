"""catalog-api-04 — endpoint modules are LEAVES: none reaches into a sibling, none takes a private name.

An underscore helper is a module-internal contract; importing one across modules makes "private" a lie
and welds two routing modules together (namespaces↔tables, tables↔credentials, stores↔user_state each
did it). Anything two endpoint modules genuinely share has a shared home — ``catalog.api.pagination``,
``catalog.api.dependencies``, ``catalog.core.*``, ``catalog.schemas`` — under a public name.

AST, not grep, so aliases and multi-name imports cannot slip through.
"""

from __future__ import annotations

import ast
import pathlib


_ENDPOINTS = pathlib.Path(__file__).resolve().parents[1] / "src" / "catalog" / "api" / "v1" / "endpoints"
_ENDPOINTS_PKG = "catalog.api.v1.endpoints"


def _offences(path: pathlib.Path) -> list[str]:
    found: list[str] = []
    for node in ast.walk(ast.parse(path.read_text())):
        if not isinstance(node, ast.ImportFrom) or node.module is None:
            continue
        # A sibling endpoint module (the package's own __init__ re-exporting routers is not a sibling reach).
        if node.module.startswith(f"{_ENDPOINTS_PKG}.") and path.name != "__init__.py":
            names = [alias.name for alias in node.names]
            found.append(f"{path.name}:{node.lineno} imports {names} from sibling {node.module.rsplit('.', 1)[-1]}.py")
        # A private name out of any non-sibling catalog module (dependencies.py was the offender).
        elif node.module.startswith("catalog."):
            private = [alias.name for alias in node.names if alias.name.startswith("_")]
            if private:
                found.append(f"{path.name}:{node.lineno} imports private {private} from {node.module}")
    return found


def test_the_walk_sees_the_endpoint_plane() -> None:
    modules = [p for p in _ENDPOINTS.glob("*.py") if p.name != "__init__.py"]
    assert len(modules) > 10, f"only {len(modules)} endpoint modules — the walk is not seeing the plane"


def test_no_endpoint_module_reaches_into_a_sibling_or_a_private_helper() -> None:
    offences = [o for path in sorted(_ENDPOINTS.glob("*.py")) for o in _offences(path)]
    assert not offences, (
        "endpoint modules reaching across module-privacy boundaries — promote the helper to a shared "
        "public home (catalog.api.pagination / catalog.api.dependencies / catalog.core.*) instead:\n  " + "\n  ".join(offences)
    )
