"""#78 format honesty — the create path rejects a client that tries to select a non-Lance file format
instead of silently echoing the ignored property back.

STANDING RULING (owner, 2026-08-15): **rask will only and always only support Lance tables — no other
format, ever.** This is not a current-scope note or a "not yet"; it is permanent, and it makes this
guard a PRODUCT INVARIANT rather than an implementation detail of the create door.

What that settles, so nobody reopens it as a feature request:

* The 400 here is the correct and final answer, not a temporary gap. A future PR adding
  Parquet/Iceberg/Delta support is out of scope by ruling, not by effort.
* The catalog is deliberately format-AWARE — the exact inverse of Lakekeeper's Generic Table
  boundary ("no Lance in the catalog", commit coordination an explicit non-goal). rask imports
  pylance, serves the data plane in-process, and coordinates commits, and it can do all three
  BECAUSE the format is closed. Every one of those becomes unsound the moment a second format
  exists.
* It is also what lets the estate skip a relational database: Iceberg puts the commit pointer in the
  catalog (so every commit is a DB transaction), Lance puts the CAS in the object store. Supporting
  both formats would reintroduce the very requirement the architecture is built to avoid.
* Consequence for the opaque-asset rung (diff2 F9): an `asset` type may govern NON-TABULAR bytes —
  model artefacts are the first and only known consumer — but it must NEVER become a second TABLE
  lane carrying a format tag. Lakekeeper's Generic Table is a format-agnostic table; rask's asset
  rung, if it lands, is a governed blob. Those are different things and this ruling is the line
  between them. (Do not enumerate future consumers by workload: rask is a format-agnostic multimodal
  platform, and HTR/IIIF is one example task, not its identity.)
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest
from catalog.api.v1.endpoints import data as catalog_data
from catalog.core.formats import reject_unsupported_format
from lance_namespace import InvalidInputError
from lance_namespace_urllib3_client import models as ns_models
from pydantic import BaseModel


@pytest.mark.parametrize(
    "props",
    [
        {"write.format.default": "parquet"},
        {"write.format.default": "ORC"},
        {"data_source_format": "avro"},
        {"data_source_format": "DELTA"},
    ],
)
def test_rejects_non_lance_format(props: dict[str, str]) -> None:
    with pytest.raises(InvalidInputError):
        reject_unsupported_format(props)


@pytest.mark.parametrize(
    "props",
    [
        None,
        {},
        {"some.other.prop": "value"},
        {"write.format.default": "lance"},  # explicitly Lance is fine
        {"write.format.default": "LANCE"},
    ],
)
def test_allows_lance_or_absent(props: object) -> None:
    reject_unsupported_format(props)  # no raise


# --------------------------------------------------------------------------------------------------
# The guard must be CALLED. Testing the function proves the rule; it does not wire it to a door.
# --------------------------------------------------------------------------------------------------

_GUARD = "reject_unsupported_format"
_ENDPOINTS = Path(catalog_data.__file__).parent


def _doors_accepting_properties() -> list[tuple[str, str]]:
    """(file:line, handler) for every routed handler whose request body can carry ``properties``.

    Derived, not listed. A hand-written door list is exactly how this gap opened: the guard was wired
    into ``create_table`` — the door it was written for — while ``declare_table`` and
    ``register_table`` took the same ``properties`` field through their spec request models and never
    checked it. Enumerating from the MODELS means the next door that accepts properties fails here on
    the day it lands, rather than being remembered.
    """
    bearing = {
        name for name, model in vars(ns_models).items() if isinstance(model, type) and issubclass(model, BaseModel) and "properties" in model.model_fields
    }
    doors: list[tuple[str, str]] = []
    for py in sorted(_ENDPOINTS.glob("*.py")):
        tree = ast.parse(py.read_text())
        for node in ast.walk(tree):
            if not isinstance(node, ast.AsyncFunctionDef | ast.FunctionDef):
                continue
            if not any("router." in ast.unparse(d) for d in node.decorator_list):
                continue
            annotations = " ".join(ast.unparse(a.annotation) for a in node.args.args if a.annotation)
            # TWO shapes. A door either takes a properties-bearing spec request MODEL as its body, or
            # takes `properties` directly as a parameter — `create_table` does the latter (the spec-0.9
            # JSON-encoded query param), and a model-only scan missed the very door this guard was
            # written for. Found by deleting that call and watching this gate stay green.
            takes_properties = any(a.arg == "properties" for a in (*node.args.args, *node.args.kwonlyargs))
            if takes_properties or any(b in annotations for b in bearing):
                doors.append((f"{py.name}:{node.lineno}", node.name))
    return doors


def test_every_door_that_accepts_properties_calls_the_format_guard() -> None:
    doors = _doors_accepting_properties()
    assert doors, "no routed handler takes a properties-bearing request model — the scan moved and this gate is vacuous"

    unguarded = []
    for loc, handler in doors:
        src = ast.unparse(
            next(
                n
                for n in ast.walk(ast.parse((_ENDPOINTS / loc.split(":")[0]).read_text()))
                if isinstance(n, ast.AsyncFunctionDef | ast.FunctionDef) and n.name == handler
            )
        )
        if _GUARD not in src:
            unguarded.append(f"{loc} {handler}()")

    assert not unguarded, (
        "these create doors accept a `properties` map and never call "
        f"{_GUARD} — a client can select a non-Lance format through them, which the "
        f"2026-08-15 LANCE-ONLY ruling forbids: {unguarded}"
    )
