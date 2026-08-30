"""CAT-CORE-14 — module privacy holds across the WHOLE catalog package, not just the endpoint plane.

``test_endpoints_do_not_reach_into_siblings`` already pins this for ``api/v1/endpoints``; the service
layer had the same defect and nothing was watching it. The project registry imported
``warehouses._read_json`` / ``warehouses._write_json`` — two underscore-private helpers that are not
about warehouses at all (they are "read/write one JSON document on the control root"), so "private"
was a lie and a change to either one silently reached a second registry.

Anything two catalog modules genuinely share gets a PUBLIC home. AST, so an alias cannot slip through.
"""

from __future__ import annotations

import ast
import pathlib


_SRC = pathlib.Path(__file__).resolve().parents[1] / "src" / "catalog"


def _modules() -> list[pathlib.Path]:
    return sorted(p for p in _SRC.rglob("*.py") if "__pycache__" not in p.parts)


def _module_name(path: pathlib.Path) -> str:
    return ".".join(path.relative_to(_SRC.parent).with_suffix("").parts)


def test_the_walk_sees_the_whole_package() -> None:
    assert len(_modules()) > 40, f"only {len(_modules())} modules — the walk is not seeing the catalog package"


def test_no_catalog_module_imports_a_private_name_from_another() -> None:
    offences = [
        f"{path.relative_to(_SRC)}:{node.lineno} imports private {private} from {node.module}"
        for path in _modules()
        for node in ast.walk(ast.parse(path.read_text()))
        if isinstance(node, ast.ImportFrom)
        and node.module
        and node.module.startswith("catalog.")
        and node.module != _module_name(path)
        and (private := [alias.name for alias in node.names if alias.name.startswith("_")])
    ]
    assert not offences, "catalog modules reaching for a sibling's private name — give the shared helper a public home:\n  " + "\n  ".join(offences)
