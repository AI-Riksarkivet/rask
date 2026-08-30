"""CAT-CORE-16 — the data plane's value object is a Pydantic model, and ``StorageOptions`` has ONE home.

Two halves of one drift:

* ``BlobStream`` was a stdlib ``@dataclass``. The estate builds value objects on ``pydantic.BaseModel``
  (CLAUDE.md, and the audit filed the same defect on two siblings) — a dataclass gets no validation, no
  ``model_config`` and cannot be composed into a wire model later without a rewrite.
* ``dataplane.py`` re-declared ``StorageOptions = dict[str, str]`` while importing from the module that
  already defines it (``service_kit.lakehouse.objectfs``). A second declaration of a shared alias is a
  second thing to change when the shape moves, and the two are already spelled differently in the same
  file (the annotation says ``StorageOptions``, ``open_dataset`` takes ``dict[str, str]``).

AST over the catalog source, so an alias or a re-export cannot hide either half.
"""

from __future__ import annotations

import ast
import pathlib

from pydantic import BaseModel

from catalog.services.dataplane import BlobStream


_SRC = pathlib.Path(__file__).resolve().parents[1] / "src" / "catalog"


def _modules() -> list[pathlib.Path]:
    return sorted(p for p in _SRC.rglob("*.py") if "__pycache__" not in p.parts)


def test_the_walk_sees_the_catalog_source() -> None:
    assert len(_modules()) > 30, f"only {len(_modules())} modules — the walk is not seeing the catalog"


def test_the_blob_stream_value_object_is_a_pydantic_model() -> None:
    assert issubclass(BlobStream, BaseModel), "BlobStream is not a pydantic BaseModel"


def test_no_catalog_module_declares_a_dataclass_value_object_in_the_data_plane() -> None:
    offences = [
        f"{path.relative_to(_SRC)}:{node.lineno} @dataclass {node.name}"
        for path in _modules()
        if path.name == "dataplane.py"
        for node in ast.walk(ast.parse(path.read_text()))
        if isinstance(node, ast.ClassDef)
        for deco in node.decorator_list
        if "dataclass" in ast.dump(deco)
    ]
    assert not offences, "stdlib dataclass value objects in the data plane — use pydantic.BaseModel:\n  " + "\n  ".join(offences)


def test_storage_options_is_not_re_declared_inside_the_catalog() -> None:
    offences = [
        f"{path.relative_to(_SRC)}:{node.lineno}"
        for path in _modules()
        for node in ast.walk(ast.parse(path.read_text()))
        if isinstance(node, ast.Assign)
        for target in node.targets
        if isinstance(target, ast.Name) and target.id == "StorageOptions"
    ]
    assert not offences, "StorageOptions re-declared inside the catalog — import the one in service_kit.lakehouse.objectfs instead:\n  " + "\n  ".join(offences)
