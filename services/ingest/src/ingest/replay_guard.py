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
#:
#: `settings` joined them when the plane's knobs moved onto `ingest.config.IngestSettings`. That model
#: is deliberately UNCACHED — it reads `os.environ` per call, which is the whole point — so
#: `settings().max_units` inside a workflow body is `os.getenv("RASK_INGEST_MAX_UNITS")` with one more
#: frame in front of it. A gate that stopped at the syntax would have been silently retired by a
#: refactor that changed nothing about the hazard.
_ENV_ATTRS = frozenset({"getenv", "environ", "settings"})


def _reads_env(node: ast.AST) -> bool:
    for child in ast.walk(node):
        if isinstance(child, ast.Attribute) and child.attr in _ENV_ATTRS:
            return True
        if isinstance(child, ast.Name) and child.id in _ENV_ATTRS:
            return True
    return False


def _module_functions(tree: ast.Module) -> dict[str, ast.FunctionDef | ast.AsyncFunctionDef]:
    return {n.name: n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef | ast.AsyncFunctionDef)}


def _called_names(node: ast.AST) -> set[str]:
    """Bare-name call targets inside ``node``. Attribute calls (`ctx.call_activity`, `wf.when_all`)
    are deliberately ignored: they are the runtime's surface, not this module's helpers."""
    return {c.func.id for c in ast.walk(node) if isinstance(c, ast.Call) and isinstance(c.func, ast.Name)}


def workflow_scope(source: str, workflow_names: set[str], activity_names: set[str] | None = None) -> list[str]:
    """Every function that runs in WORKFLOW SCOPE: the named bodies plus the helpers they call.

    The Diagrid determinism checklist defines scope as "the body of any function decorated with
    `@wfr.workflow` … **or any helper called from such a function**", and this gate covered only the
    first half — which is the same shape of hole its own docstring criticises in the suite it replaced.

    Transitive, because a helper's helper is still workflow scope. ACTIVITY names are excluded: an
    activity reading the environment is the sanctioned asymmetry this whole module depends on, and a
    body that dispatches one via `ctx.call_activity` is not calling it in workflow scope anyway.
    """
    tree = ast.parse(source)
    functions = _module_functions(tree)
    excluded = activity_names or set()
    seen: set[str] = set()
    queue = [n for n in workflow_names if n in functions]
    while queue:
        name = queue.pop()
        if name in seen:
            continue
        seen.add(name)
        queue.extend(c for c in _called_names(functions[name]) if c in functions and c not in seen and c not in excluded)
    # Definition order, so a caller's report reads like the file rather than like a set.
    return [name for name in functions if name in seen]


def env_reads_in_workflow_bodies(source: str, workflow_names: set[str], activity_names: set[str] | None = None) -> list[str]:
    """Names of functions in WORKFLOW SCOPE that read the environment, in definition order.

    ``workflow_names`` should be derived from the module's own registration tuple rather than
    hard-coded — a gate that names its targets silently stops covering the next workflow somebody adds.
    ``activity_names`` likewise, and passing it is what keeps the sanctioned activity-side env reads
    out of the answer.

    Raises ``SyntaxError`` on unparseable input rather than returning an empty list: a detector that
    answers "clean" for source it could not read reports every file as clean, which is the failure
    mode this module exists to prevent rather than reproduce.
    """
    tree = ast.parse(source)
    functions = _module_functions(tree)
    in_scope = workflow_scope(source, workflow_names, activity_names)
    return [name for name in in_scope if _reads_env(functions[name])]
