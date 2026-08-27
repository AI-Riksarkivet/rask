"""A sealed runner's probes still have to answer the two questions probes exist to answer.

open_fastapi-audit — "The assist runner's `/livez` is a sync `def` on the blocking threadpool — the
exact shape `service_kit.probes` was created to delete — and its `/readyz` never reports draining".

THE FINDING IS HALF SCOPE ERROR, and this gate keeps only the half that survives. `runners/` is
deliberately outside the platform's conventions (CLAUDE.md: "Matched by no glob, which is the
point"), probes.py's sync-`def` complaint was aimed at a service-plane copy, and a plain `def` route
is normally CORRECT here because FastAPI threadpools it. So this file does not import
`service_kit.probes` into a runner or ask a runner to look like a fleet service.

What survives is narrow and real, and it is the readiness half: `readyz` tested
`app.state.models is None`, and the lifespan assigns `models` BEFORE it yields — so after boot the
branch is unreachable and the route is a constant 200. A readiness check that cannot fail is not a
readiness check.

The liveness half survives in the weaker form the reference actually states: "/livez … Cheap — return
200 if the event loop is responsive". A handler that returns a literal has nothing to yield for, and
routing it through the threadpool means liveness queues behind whatever the workload is doing — which
on a runner is model inference. `async def` is free here and it is what makes the probe answer the
question it claims to.

READ WITH `ast`, NEVER IMPORTED. A runner has its own `pyproject.toml` and its own lock (torch,
ultralytics, transformers …) precisely so that stack never enters the fleet's resolution; importing
`server.py` from the root suite would either fail or drag the whole thing in. Parsing keeps the seal
intact — the platform learns nothing about the workload, and the workload owes the platform nothing.
"""

from __future__ import annotations

import ast
import pathlib

import pytest


RUNNERS = sorted((pathlib.Path(__file__).resolve().parents[2] / "runners").glob("*/server.py"))


def _route_handlers(tree: ast.Module) -> dict[str, ast.FunctionDef | ast.AsyncFunctionDef]:
    """Every handler in the module, keyed by the literal path its decorator declares."""
    handlers: dict[str, ast.FunctionDef | ast.AsyncFunctionDef] = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            continue
        for decorator in node.decorator_list:
            if isinstance(decorator, ast.Call) and decorator.args and isinstance(decorator.args[0], ast.Constant):
                path = decorator.args[0].value
                if isinstance(path, str):
                    handlers[path] = node
    return handlers


def _state_attrs_assigned(fn: ast.AsyncFunctionDef | ast.FunctionDef, *, after_yield: bool) -> set[str]:
    """`app.state.X = ...` names on one side of the lifespan's `yield`.

    The side matters and is the whole point of this file's second gate. An attribute set BEFORE the
    yield is true for every request the app ever serves — uvicorn does not bind until the lifespan
    reaches the yield — so gating readiness on it asks a question whose answer is fixed. Only the
    `finally` side (the drain) can distinguish one post-boot moment from another.
    """
    yields = [n.lineno for n in ast.walk(fn) if isinstance(n, ast.Yield)]
    if not yields:
        return set()
    boundary = min(yields)
    found = set()
    for node in ast.walk(fn):
        if not isinstance(node, ast.Assign):
            continue
        on_far_side = node.lineno > boundary
        if on_far_side != after_yield:
            continue
        for target in node.targets:
            if isinstance(target, ast.Attribute) and isinstance(target.value, ast.Attribute) and target.value.attr == "state":
                found.add(target.attr)
    return found


def _state_attrs_read(fn: ast.FunctionDef | ast.AsyncFunctionDef) -> set[str]:
    """`app.state.X` reads plus `getattr(app.state, "X", ...)` — the two spellings in use."""
    found = set()
    for node in ast.walk(fn):
        if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Attribute) and node.value.attr == "state":
            found.add(node.attr)
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "getattr"
            and len(node.args) >= 2
            and isinstance(node.args[1], ast.Constant)
            and isinstance(node.args[1].value, str)
        ):
            found.add(node.args[1].value)
    return found


def _lifespan(tree: ast.Module) -> ast.AsyncFunctionDef | ast.FunctionDef | None:
    return next(
        (n for n in ast.walk(tree) if isinstance(n, ast.AsyncFunctionDef | ast.FunctionDef) and n.name == "lifespan"),
        None,
    )


assert RUNNERS, "no runner server.py was found — this gate would pass vacuously"


@pytest.mark.parametrize("path", RUNNERS, ids=[p.parent.name for p in RUNNERS])
def test_liveness_answers_on_the_event_loop(path: pathlib.Path) -> None:
    """A literal return has nothing to yield for, and the threadpool is where the workload lives."""
    handler = _route_handlers(ast.parse(path.read_text())).get("/livez")
    if handler is None:
        pytest.skip(f"{path.parent.name} serves no /livez")
    assert isinstance(handler, ast.AsyncFunctionDef), (
        f"{path.parent.name}'s /livez is a sync `def`, so liveness is answered on the blocking "
        "threadpool — it queues behind model inference and fails exactly when the pod is busiest"
    )


@pytest.mark.parametrize("path", RUNNERS, ids=[p.parent.name for p in RUNNERS])
def test_readiness_can_actually_FAIL_after_boot(path: pathlib.Path) -> None:
    """The surviving half of the finding, stated as the property rather than as the symptom.

    A `/readyz` that reads only attributes the lifespan sets before yielding is a constant 200 dressed
    as a check. It may legitimately read NOTHING from `app.state` — insid3 calls its model loader, a
    real dependency check — but if it gates on lifecycle state, that state has to be able to change.
    """
    tree = ast.parse(path.read_text())
    handler = _route_handlers(tree).get("/readyz")
    if handler is None:
        pytest.skip(f"{path.parent.name} serves no /readyz")

    read = _state_attrs_read(handler)
    if not read:
        return  # gates on a real dependency call instead — a different, valid answer

    lifespan = _lifespan(tree)
    assert lifespan is not None, f"{path.parent.name}'s /readyz gates on app.state {sorted(read)} but the module has no lifespan to set it"
    always_true = _state_attrs_assigned(lifespan, after_yield=False)
    changeable = _state_attrs_assigned(lifespan, after_yield=True)

    assert read & changeable, (
        f"{path.parent.name}'s /readyz gates only on {sorted(read & always_true)}, which the lifespan assigns "
        f"BEFORE it yields — uvicorn does not bind until then, so the branch is unreachable for every request "
        f"the pod ever serves and readiness is a constant 200. Nothing it reads is set after the yield "
        f"(available: {sorted(changeable)})"
    )
