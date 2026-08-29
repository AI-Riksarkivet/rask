"""A catalog door no code opens is worse than no door: three suites were stubbing one.

`register_stage_output` lost its last production caller when the mover started ASKING the catalog
where to write (`ensure_stage_output`) instead of writing first and telling it afterwards — the
reordering `test_mover_writes_where_the_catalog_says.py` pins. The function stayed, and with it a
whole authenticated register/verify path, its `MEDALLION_CATALOG_ROOT` setting, and its own suites.

The register form is BACK — for the cascade head, whose write location is a deployment contract rather
than the catalog's to vend (`test_produce_governs_its_bronze.py`). That changes nothing about the rule
below: a door has to be opened by the service, and a setting has to be read by it.

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


def test_the_catalog_root_setting_has_a_reader() -> None:
    """The property is A READER, not an absence — and this test used to assert the absence.

    `MEDALLION_CATALOG_ROOT` expresses a location RELATIVE to the catalog's connection root, which only
    a REGISTER call needs. When the movers stopped registering (they ask, and write to the absolute
    location the catalog vends) the field had no reader left, and a settings field with no reader is how
    a chart goes on rendering an env var nobody can act on — so it was deleted, and this test pinned the
    deletion.

    The cascade HEAD registers now: `POST /produce` owns where it writes (the chart renders that URI and
    the bronze->silver mover's `MEDALLION_FROM_URI` from one expression), so it tells the catalog rather
    than asking, and `register_table` accepts only a relative location. The field is back WITH a reader,
    which is the state this asserts. Written as "some medallion source reads it" rather than naming
    `produce.py`, because the defect is an unread setting, not which module reads it.
    """
    from medallion.core.config import MedallionSettings

    assert "catalog_root" in MedallionSettings.model_fields, "the chart renders MEDALLION_CATALOG_ROOT onto the producer and both movers"

    readers = [path.name for path in _medallion_sources() if "catalog_root" in _identifiers(path.read_text(encoding="utf-8"))]

    assert readers, "MEDALLION_CATALOG_ROOT is a settings field nothing in services/medallion/src reads"
