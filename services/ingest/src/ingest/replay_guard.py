"""Static replay-safety checks over the workflow module — the half a module-scope gate cannot reach.

A Dapr workflow BODY is re-executed from scratch on every replay, so anything it reads must come from
its input or from an activity's recorded result. Reading the environment there is the defect this
plane has already paid for once: `RunLimits` records the exact break — `if max_run_hours > 0` decides
whether a durable timer exists, so a rolling deploy that changed the variable between a run's first
execution and its replay produced an action stream the history does not match. `resolve_limits` pins
those numbers in history instead.

The existing gate (`tests/test_replay_hygiene.py`) refuses env reads at MODULE scope, which is a real
but weaker defect: a module constant is fixed per POD rather than per RUN. An `os.getenv` inside a
workflow body passed it untouched, so the estate had a gate that would have let the very defect it was
written about come straight back.

AN ACTIVITY MAY READ ENV, and that asymmetry is the whole design: an activity's result is recorded, so
every replay sees the value the first execution saw. Banning env reads everywhere would ban the fix.
"""

from __future__ import annotations

import ast


#: Reads that resolve against the live process environment. `environ` covers both subscript
#: (`os.environ['X']`) and method (`os.environ.get('X')`) forms, because they are the same hazard
#: wearing two syntaxes and a gate that caught only one would be trivially worked around by accident.
_ENV_ATTRS = frozenset({"getenv", "environ"})


def _reads_env(node: ast.AST) -> bool:
    for child in ast.walk(node):
        if isinstance(child, ast.Attribute) and child.attr in _ENV_ATTRS:
            return True
        if isinstance(child, ast.Name) and child.id in _ENV_ATTRS:
            return True
    return False


def env_reads_in_workflow_bodies(source: str, workflow_names: set[str]) -> list[str]:
    """Names of workflow bodies in ``source`` that read the environment, in definition order.

    ``workflow_names`` should be derived from the module's own registration tuple rather than
    hard-coded — a gate that names its targets silently stops covering the next workflow somebody adds.

    Raises ``SyntaxError`` on unparseable input rather than returning an empty list: a detector that
    answers "clean" for source it could not read reports every file as clean, which is the failure
    mode this module exists to prevent rather than reproduce.
    """
    tree = ast.parse(source)
    return [
        node.name for node in ast.walk(tree) if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef) and node.name in workflow_names and _reads_env(node)
    ]
