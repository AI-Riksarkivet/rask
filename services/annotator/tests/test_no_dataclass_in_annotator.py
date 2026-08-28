"""The annotator models with Pydantic, never `@dataclass`.

open_python-audit `ANN-15`: `saga.PublishOutcome` was a `@dataclass(frozen=True)` — the only
dataclass in the service. The house rule is Pydantic `BaseModel` (MEMORY: 'Pydantic not @dataclass';
the writing-python skill). This guards the whole `src` tree so a future dataclass cannot slip back in.
"""

from __future__ import annotations

import ast
from pathlib import Path


_SRC = Path(__file__).resolve().parents[1] / "src" / "annotator"


def _dataclass_decorated(path: Path) -> list[str]:
    tree = ast.parse(path.read_text())
    hits: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.ClassDef):
            continue
        for dec in node.decorator_list:
            target = dec.func if isinstance(dec, ast.Call) else dec
            name = target.attr if isinstance(target, ast.Attribute) else getattr(target, "id", "")
            if name == "dataclass":
                hits.append(f"{path.name}:{node.lineno} {node.name}")
    return hits


def test_no_dataclass_decorated_class_in_annotator_src() -> None:
    offenders = [hit for path in _SRC.rglob("*.py") for hit in _dataclass_decorated(path)]
    assert offenders == [], f"@dataclass found in annotator src (house rule is Pydantic BaseModel): {offenders}"
