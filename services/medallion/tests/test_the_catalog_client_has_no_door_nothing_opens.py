"""A catalog door no code opens is worse than no door: three suites were stubbing one.

`register_stage_output` lost its last production caller when the mover started ASKING the catalog
where to write (`ensure_stage_output`) instead of writing first and telling it afterwards — the
reordering `test_mover_writes_where_the_catalog_says.py` pins. The function stayed, and with it a
whole authenticated register/verify path, its `MEDALLION_CATALOG_ROOT` setting, and its own suites.

That is not merely dead weight. `test_cascade_via_publish.py`, `test_a_same_tier_transform_is_legal.py`
and `test_mover_writes_where_the_catalog_says.py` all `monkeypatch.setattr(transform.catalog_register,
"register_stage_output", ...)`, so each carried a line that stubbed a seam the code under test cannot
reach — a stub that looks like coverage and buys none, and would go on looking like coverage if the
LIVE seams beside it were ever removed.

The rule this pins is the estate's: dead code is deleted in the change that kills it, cascading
through the module. So the assertion is not "that one name is gone" (which a re-added orphan would
pass by simply choosing another name) — it is that every function this module defines is reachable
from the service's own source.
"""

from __future__ import annotations

import ast
import inspect
from pathlib import Path

from medallion.services import catalog_register


def _medallion_sources() -> list[Path]:
    root = Path(inspect.getfile(catalog_register)).resolve().parents[2]  # .../src/medallion
    return sorted(root.rglob("*.py"))


def _identifiers(source: str) -> set[str]:
    """Every name this source USES, read from the syntax tree.

    Not a text search, and the difference is the whole test: `transform.py` names
    `register_stage_output` in a comment explaining why it must NOT be called, so a grep reads the
    tombstone as a caller and reports the orphan as live.
    """
    used: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Name):
            used.add(node.id)
        elif isinstance(node, ast.Attribute):
            used.add(node.attr)
    return used


def test_every_function_this_module_defines_is_called_from_the_service() -> None:
    """A door only tests open is not a door. It is a fixture with an import path."""
    module_path = Path(inspect.getfile(catalog_register)).resolve()
    own_source = module_path.read_text(encoding="utf-8")
    defined = [node.name for node in ast.parse(own_source).body if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)]
    assert defined, "the module defines no functions — the parse, not the estate, is what broke"

    used = _identifiers(own_source)
    for path in _medallion_sources():
        if path != module_path:
            used |= _identifiers(path.read_text(encoding="utf-8"))

    orphans = [name for name in defined if name not in used]

    assert orphans == [], (
        f"catalog_register defines {orphans}, which nothing in services/medallion/src calls — "
        "a seam only reachable from tests, so every suite that stubs it proves nothing"
    )


def test_the_settings_carry_no_env_var_only_a_deleted_door_read() -> None:
    """`MEDALLION_CATALOG_ROOT` existed to express a location RELATIVE to the catalog's connection
    root, which only the register call ever needed. The vended location the mover writes to now comes
    from the catalog itself and is absolute, so a second root is a value with no reader — and a
    settings field with no reader is how a chart keeps rendering an env var nobody can act on.
    """
    from medallion.core.config import MedallionSettings

    assert "catalog_root" not in MedallionSettings.model_fields, (
        "MEDALLION_CATALOG_ROOT survives its only reader; the chart still renders it into two mover templates"
    )
