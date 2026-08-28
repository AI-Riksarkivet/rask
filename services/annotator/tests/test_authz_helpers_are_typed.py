"""The internal authz helpers name the real checker type, not `Any`.

open_python-audit `ANN-16`: a declared `FgaChecker` Protocol exists (`api/security.py`, re-exported
from `service_kit.governed.deps`), yet `_check`/`_authorize_publish` (project_events) and `_authorize`
(tasks) typed their `checker` parameter as `Any`, and `_task_proxy` returned `Any` while the sibling
`_project_proxy` returns the typed `AnnotationProjectActorInterface`. `Any` suppresses `ty`'s ability
to catch a mistyped relation call or a wrong actor method, which is exactly what those seams guard.
"""

from __future__ import annotations

import ast
from pathlib import Path


_SRC = Path(__file__).resolve().parents[1] / "src" / "annotator" / "api" / "v1" / "endpoints"


def _param_annotation(path: Path, func: str, param: str) -> str | None:
    tree = ast.parse(path.read_text())
    for node in ast.walk(tree):
        if isinstance(node, ast.AsyncFunctionDef | ast.FunctionDef) and node.name == func:
            for arg in node.args.args:
                if arg.arg == param and arg.annotation is not None:
                    return ast.unparse(arg.annotation)
    return None


def _return_annotation(path: Path, func: str) -> str | None:
    tree = ast.parse(path.read_text())
    for node in ast.walk(tree):
        if isinstance(node, ast.AsyncFunctionDef | ast.FunctionDef) and node.name == func and node.returns is not None:
            return ast.unparse(node.returns)
    return None


def test_checker_helpers_type_checker_as_the_fga_protocol() -> None:
    project_events = _SRC / "project_events.py"
    tasks = _SRC / "tasks.py"

    offenders = {
        "project_events._check(checker)": _param_annotation(project_events, "_check", "checker"),
        "project_events._authorize_publish(checker)": _param_annotation(project_events, "_authorize_publish", "checker"),
        "tasks._authorize(checker)": _param_annotation(tasks, "_authorize", "checker"),
    }
    anys = {name: ann for name, ann in offenders.items() if ann == "Any"}
    assert not anys, f"authz helpers still type checker as Any (should be FgaChecker): {anys}"


def test_task_proxy_returns_the_typed_actor_interface() -> None:
    ann = _return_annotation(_SRC / "project_events.py", "_task_proxy")
    assert ann != "Any", "_task_proxy returns Any while AnnotationTaskActorInterface exists (the sibling _project_proxy returns its typed interface)"
