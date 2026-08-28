"""A Ray Serve deployment's `async def` must not run its blocking work on the event loop.

Found by the Ray design-patterns audit (2026-08-28) against ray-project's own
`doc/source/ray-core/patterns/concurrent-operations-async-actor.rst`: *"a long running method call
blocks all the following ones ... use `await` to yield control from the long running method call so
other method calls can run concurrently"*.

THE ESTATE HAS ALREADY PAID FOR THIS ONCE, and wrote it down:
`runners/htr/scripts/deploy_htr.py` records *"Running it inline froze the loop -> event loop
unresponsive -> Serve killed every replica -> restart storm"*. That is the real cost — not slow
requests. Serve's controller health-probes each replica over the same loop the blocked coroutine is
sitting on, so a long inline call is not merely serialised, it gets the replica declared unhealthy
and RESTARTED mid-work. `htrflow_service.py` took the lesson (`asyncio.to_thread`); three other
deployments did not, in three different runners, which is why this is a gate rather than three fixes.

WHY A SOURCE GATE AND NOT AN IMPORT. A runner is SEALED — its deps live in its own lock and its own
venv, and the root interpreter cannot import `toponymy` or a voiceprint encoder by design. Reading
the AST needs neither, so one root-suite test can hold every runner to the rule. It is also the
estate's existing idiom for a cross-cutting coroutine invariant (`test_invariants.py::
test_no_coroutine_verifies_a_bearer_on_the_event_loop`).

THE RULE, stated so a future reader can argue with it: inside a `@serve.deployment` class, an
`async def` that does substantive work must yield the loop at least once for that work — either by
awaiting something other than reading the request, or by handing the work to `asyncio.to_thread` /
`run_in_executor`. A coroutine that awaits only `request.body()`/`.json()` and then calls into a
model, a builder or a subprocess is async in name only.

Both halves are load-bearing. Awaiting only the request read is what `topics` and `voiceprint` do;
having no await at all while doing GPU work is what `TranscribeService.transcribe` did — and its
sibling `__call__` awaits *it*, so a rule that only looked at the entry point would have called that
deployment clean.
"""

from __future__ import annotations

import ast
import pathlib

import pytest


REPO = pathlib.Path(__file__).resolve().parents[2]

#: Calls that do no real work, so their presence alone does not make a coroutine "substantive".
_CHEAP = frozenset(
    {
        "len",
        "list",
        "dict",
        "set",
        "tuple",
        "str",
        "int",
        "float",
        "bool",
        "range",
        "enumerate",
        "zip",
        "max",
        "min",
        "sum",
        "sorted",
        "getattr",
        "isinstance",
        "print",
        "repr",
        "abs",
        "any",
        "all",
        "reversed",
        "map",
        "filter",
        "next",
        "iter",
    }
)

#: Reading the request is not "yielding for the work" — it is the work's INPUT. A coroutine whose
#: only await is `await request.json()` has not yielded for anything it then goes on to do.
_REQUEST_READS = frozenset({"json", "body", "form", "read", "text"})

#: The sanctioned ways to run blocking work from a coroutine.
_OFFLOAD = frozenset({"to_thread", "run_in_executor", "run_sync"})


def _serve_deployment_classes(tree: ast.Module) -> list[ast.ClassDef]:
    out = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.ClassDef):
            continue
        for dec in node.decorator_list:
            func = dec.func if isinstance(dec, ast.Call) else dec
            if isinstance(func, ast.Attribute) and func.attr == "deployment":
                out.append(node)
                break
    return out


def _yields_for_work(fn: ast.AsyncFunctionDef) -> bool:
    """Does this coroutine hand the loop back for anything other than reading the request?"""
    for node in ast.walk(fn):
        if isinstance(node, ast.AsyncFor | ast.AsyncWith):
            return True
        if not isinstance(node, ast.Await):
            continue
        value = node.value
        if isinstance(value, ast.Call) and isinstance(value.func, ast.Attribute) and value.func.attr in _REQUEST_READS:
            continue
        return True
    return False


def _blocking_calls(fn: ast.AsyncFunctionDef) -> list[tuple[int, str]]:
    """Substantive calls NOT already handed to a thread."""
    offloaded: set[int] = set()
    for node in ast.walk(fn):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr in _OFFLOAD:
            offloaded.update(id(sub) for sub in ast.walk(node))
    found = []
    for node in ast.walk(fn):
        if not isinstance(node, ast.Call) or id(node) in offloaded:
            continue
        func = node.func
        name = func.id if isinstance(func, ast.Name) else func.attr if isinstance(func, ast.Attribute) else "?"
        if name in _CHEAP or name in _OFFLOAD or name in _REQUEST_READS:
            continue
        found.append((node.lineno, name))
    return found


def _runner_sources() -> list[pathlib.Path]:
    """Every runner's own Python — never a vendored dependency inside its venv."""
    return [p for p in sorted((REPO / "runners").rglob("*.py")) if ".venv" not in p.parts and "site-packages" not in p.parts]


def test_the_gate_can_see_a_serve_deployment_at_all() -> None:
    """A guard on the guard: a rename of the decorator would silently empty this suite."""
    seen = [p for p in _runner_sources() if _serve_deployment_classes(ast.parse(p.read_text()))]
    assert len(seen) >= 3, f"the gate found almost no Serve deployments to check ({seen}) — it is probably looking at nothing"


def test_no_deployment_coroutine_runs_its_work_on_the_event_loop() -> None:
    offenders: list[str] = []
    for path in _runner_sources():
        tree = ast.parse(path.read_text())
        for cls in _serve_deployment_classes(tree):
            for fn in [n for n in cls.body if isinstance(n, ast.AsyncFunctionDef)]:
                if _yields_for_work(fn):
                    continue
                blocking = _blocking_calls(fn)
                if blocking:
                    where = f"{path.relative_to(REPO)}:{fn.lineno} {cls.name}.{fn.name}"
                    offenders.append(f"{where} — blocking: {', '.join(f'{n}() at :{ln}' for ln, n in blocking[:3])}")
    assert not offenders, (
        "these Serve coroutines do their work on the replica's event loop, so Serve's health probe "
        "queues behind it and the controller can restart the replica mid-work — wrap the blocking "
        "call in asyncio.to_thread():\n  " + "\n  ".join(offenders)
    )


@pytest.mark.parametrize(
    "path",
    ["runners/htr/src/runner/htrflow_service.py", "runners/htr/scripts/deploy_htr.py"],
)
def test_the_deployments_that_already_got_this_right_still_pass(path: str) -> None:
    """The two that took the lesson. If the rule ever starts flagging these it has gone wrong."""
    tree = ast.parse((REPO / path).read_text())
    for cls in _serve_deployment_classes(tree):
        for fn in [n for n in cls.body if isinstance(n, ast.AsyncFunctionDef)]:
            assert _yields_for_work(fn) or not _blocking_calls(fn), f"{path} {cls.name}.{fn.name} was flagged — it uses asyncio.to_thread and must not be"
