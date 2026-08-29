"""ING-16 — an entry point with no caller is deleted, not left carrying a docstring that says it matters.

Three of them survived here, and two carried docstrings asserting they were load-bearing:

* `lineage._delimiter()` — "the catalog's table-id separator … read from env so it cannot drift from
  the catalog client's". Nothing called it, so the drift it claimed to prevent was prevented by
  `naming.delimiter()` and by an import-frozen `catalog_service.DELIMITER` disagreeing with each
  other (see `test_settings_are_declared_not_scattered.py`).
* the `sources.register(fetcher=...)` hook — documented as the escape hatch for a kind whose keys are
  not scheme-resolvable, and unwired, so `lance-append` enumerated its units and failed EVERY fetch.

A dead helper is not neutral: the reader is told a rule is enforced and it is not. So this is a GATE
rather than a one-off deletion — the next unreferenced entry point fails here.
"""

from __future__ import annotations

import ast
import collections
import re
from pathlib import Path

from ingest import config as config_mod


SRC = Path(config_mod.__file__).parent
_ROOT = SRC.parents[3]

#: Everywhere that could legitimately name an ingest symbol. Not just Python: `create_app` is named
#: by a dockerfile's uvicorn target and by the chart, so a Python-only scan would report the app
#: factory itself as dead — which is how a dead-code gate teaches people to ignore it.
#: SCOPED TO WHAT CAN ACTUALLY REACH AN INGEST SYMBOL. A repo-wide scan is worse than none here: a
#: private helper called `_delimiter` is "referenced" by `service_kit.lancekit.reader`'s own unrelated
#: `self._delimiter`, so the gate reports the dead one as live. `tests/unit` is in because the estate's
#: shared suite imports `ingest.naming`; the chart and dockerfiles are in because they name
#: `create_app` as a literal string.
_SEARCHED = ("services/ingest", "tests", "scripts", "chart", ".docker")
_SUFFIXES = (".py", ".toml", ".yaml", ".yml", ".dockerfile", ".sh", ".md")


def _code_names(source: str) -> list[str]:
    """Identifiers a Python file actually USES — never the ones it merely talks about.

    A word-level scan counts a symbol named in a docstring, which is the hole that matters here: this
    very file names `lineage._delimiter` in its own header, and a regex gate would therefore have
    reported the dead helper it was written about as referenced. So Python files are read through the
    AST and prose is invisible to them.
    """
    tree = ast.parse(source)
    names: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            names.append(node.id)
        elif isinstance(node, ast.Attribute):
            names.append(node.attr)
        elif isinstance(node, ast.alias):
            names.extend(part for part in (node.name.split(".")[-1], node.asname) if part)
    return names


def _reference_counts() -> collections.Counter[str]:
    counts: collections.Counter[str] = collections.Counter()
    for name in _SEARCHED:
        root = _ROOT / name
        if not root.is_dir():
            continue
        for path in sorted(root.rglob("*")):
            if not path.is_file() or path.suffix not in _SUFFIXES or "__pycache__" in str(path) or ".venv" in str(path):
                continue
            text = path.read_text(encoding="utf-8", errors="ignore")
            # Non-Python files reach a symbol only as a literal string — a dockerfile's uvicorn target,
            # a chart value — so there is nothing to parse and a word scan is exactly right.
            counts.update(_code_names(text) if path.suffix == ".py" else re.findall(r"[A-Za-z_][A-Za-z0-9_]*", text))
    return counts


def _module_entry_points(path: Path) -> list[tuple[str, int]]:
    """Module-scope functions and classes, as `(name, line)`.

    Decorated definitions are excluded: a route handler or a pydantic validator is CALLED by its
    decorator, so a name-reference count cannot see its caller and would report every one as dead.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"))
    return [(n.name, n.lineno) for n in tree.body if isinstance(n, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef) and not n.decorator_list]


def test_every_module_level_entry_point_has_a_caller() -> None:
    counts = _reference_counts()
    dead: dict[str, list[str]] = {}
    for path in sorted(SRC.glob("*.py")):
        # ZERO, not one: a definition's own `def`/`class` statement is not a Name node, so a symbol
        # nothing uses is counted exactly zero times.
        orphans = [f"{name} ({path.name}:{line})" for name, line in _module_entry_points(path) if counts[name] == 0]
        if orphans:
            dead[path.name] = orphans
    assert dead == {}, (
        f"{dead} are defined and never referenced. Delete them in the change that killed them — a dead "
        f"helper whose docstring asserts it is load-bearing tells the reader a rule is enforced when it is not."
    )
