"""Collaborators with a declared Protocol are typed as it, not `Any`.

docs/DECISIONS.md "The Python estate audit" `ANN-16`: nine seams typed a collaborator as `Any` while a declared type for it
already existed — `FgaChecker` (`api/security.py`, re-exported from `service_kit.governed.deps`) for
the checker params, `AnnotationTaskActorInterface` for `_task_proxy`, the saga's own `TaskHandle`
Protocol for its task-actor factory, `AppState` for the dataset-registry state, and the proxy seam's
wire contract. `Any` suppresses `ty`'s ability to catch a mistyped relation call or a wrong actor
method, which is exactly what those seams guard.

This guard sweeps ALL NINE cited sites. Its first version covered five, and the re-audit found the
other four regressed precisely because nothing pinned them — a partial guard reads as a full one.
"""

from __future__ import annotations

import ast
from pathlib import Path


_ANNOTATOR = Path(__file__).resolve().parents[1] / "src" / "annotator"
_ENDPOINTS = _ANNOTATOR / "api" / "v1" / "endpoints"
_PROJECTS = _ANNOTATOR / "projects"


def _functions(path: Path) -> dict[str, ast.AsyncFunctionDef | ast.FunctionDef]:
    tree = ast.parse(path.read_text())
    return {node.name: node for node in ast.walk(tree) if isinstance(node, ast.AsyncFunctionDef | ast.FunctionDef)}


def _param_annotation(path: Path, func: str, param: str) -> str | None:
    node = _functions(path).get(func)
    if node is None:
        return None
    for arg in [*node.args.posonlyargs, *node.args.args, *node.args.kwonlyargs]:
        if arg.arg == param and arg.annotation is not None:
            return ast.unparse(arg.annotation)
    return None


def _method_param_annotation(path: Path, cls: str, method: str, param: str) -> str | None:
    tree = ast.parse(path.read_text())
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == cls:
            for item in node.body:
                if isinstance(item, ast.AsyncFunctionDef | ast.FunctionDef) and item.name == method:
                    for arg in [*item.args.posonlyargs, *item.args.args, *item.args.kwonlyargs]:
                        if arg.arg == param and arg.annotation is not None:
                            return ast.unparse(arg.annotation)
    return None


def _return_annotation(path: Path, func: str) -> str | None:
    node = _functions(path).get(func)
    return ast.unparse(node.returns) if node is not None and node.returns is not None else None


def test_every_ann16_site_names_its_collaborators_type() -> None:
    """All nine ANN-16 sites, in one sweep — a partial list is how four of them regressed."""
    project_events = _ENDPOINTS / "project_events.py"
    tasks = _ENDPOINTS / "tasks.py"
    members = _ENDPOINTS / "members.py"
    saga = _PROJECTS / "saga.py"
    proxies = _PROJECTS / "proxies.py"

    sites = {
        "project_events._check(checker)": _param_annotation(project_events, "_check", "checker"),
        "project_events._authorize_publish(checker)": _param_annotation(project_events, "_authorize_publish", "checker"),
        "project_events._task_proxy() return": _return_annotation(project_events, "_task_proxy"),
        "project_events._refuse_unknown_datasets(state)": _param_annotation(project_events, "_refuse_unknown_datasets", "state"),
        "tasks._authorize(checker)": _param_annotation(tasks, "_authorize", "checker"),
        "members._require_manage(checker)": _param_annotation(members, "_require_manage", "checker"),
        "saga.collect(task_handle)": _param_annotation(saga, "collect", "task_handle"),
        "saga.run_publish(task_handle)": _param_annotation(saga, "run_publish", "task_handle"),
        "proxies.TypedActorProxy.__init__(proxy)": _method_param_annotation(proxies, "TypedActorProxy", "__init__", "proxy"),
    }

    missing = {name: ann for name, ann in sites.items() if ann is None}
    assert not missing, f"guard could not find these sites — the sweep drifted from the code: {missing}"

    anys = {name: ann for name, ann in sites.items() if ann in {"Any", "typing.Any"}}
    assert not anys, f"sites still typing a collaborator as Any despite a declared type: {anys}"
