"""Every production process that takes SIGTERM takes it through the one seam that hands it on ([[LH-183]]).

`arm_drain_on_sigterm` flips the drain flag and then calls the handler it displaced — under uvicorn,
`Server.handle_exit`, which is what stops the server. That holds for every service only while two
properties of the TREE hold, and neither is visible from the seam's own tests:

* each service that arms the drain arms THAT function, not a copy of it; and
* nothing else in production installs a SIGTERM handler. One installed after the drain displaces the
  drain and uvicorn together, and `getsignal` would hand it asyncio's no-op, so the chain ends there.

Walked from the source, so an eighth caller or a second handler is seen the day it lands. The seven
named in `_ARMED` are a floor: a service that stops arming loses the SIGTERM-time flag and nothing
else notices. The seam's behaviour is proven against a real uvicorn in
`packages/service-kit/tests/test_sigterm_still_stops_the_server.py`.
"""

from __future__ import annotations

import ast
from collections.abc import Iterator
from pathlib import Path


REPO = Path(__file__).resolve().parents[2]
_SEAM = "packages/service-kit/src/service_kit/draining.py"

#: The lifespans that arm the drain. The media lifespan serves viewer, search and annotator, and the
#: maintenance planner and worker both run `maintenance.service:app`.
_ARMED = frozenset(
    {
        "packages/service-kit/src/service_kit/media/lifespan.py",
        "services/gateway/src/gateway/__init__.py",
        "services/lineage/src/lineage/main.py",
        "services/maintenance/src/maintenance/service.py",
        "services/medallion/src/medallion/producer.py",
        "services/medallion/src/medallion/stage_runner.py",
        "services/notifications/src/notifications/lifespan.py",
    }
)


def _production_trees() -> Iterator[tuple[str, ast.Module]]:
    for pattern in ("packages/*/src/**/*.py", "services/*/src/**/*.py"):
        for path in sorted(REPO.glob(pattern)):
            yield str(path.relative_to(REPO)), ast.parse(path.read_text(encoding="utf-8"))


def _callee(call: ast.Call) -> str | None:
    if isinstance(call.func, ast.Name):
        return call.func.id
    if isinstance(call.func, ast.Attribute):
        return call.func.attr
    return None


def _calls(tree: ast.Module) -> Iterator[ast.Call]:
    return (node for node in ast.walk(tree) if isinstance(node, ast.Call))


def test_every_service_that_arms_the_drain_arms_THE_seam() -> None:
    armed: set[str] = set()
    copies: set[str] = set()
    for module, tree in _production_trees():
        if module != _SEAM and any(isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef) and node.name == "arm_drain_on_sigterm" for node in ast.walk(tree)):
            copies.add(module)
        if any(_callee(call) == "arm_drain_on_sigterm" for call in _calls(tree)):
            imported = any(
                isinstance(node, ast.ImportFrom) and node.module == "service_kit.draining" and any(alias.name == "arm_drain_on_sigterm" for alias in node.names)
                for node in ast.walk(tree)
            )
            if not imported:
                copies.add(module)
            armed.add(module)

    assert not copies, f"{sorted(copies)} arm a drain that is not `service_kit.draining.arm_drain_on_sigterm`, so the hand-on to uvicorn is not theirs"
    assert armed >= _ARMED, f"{sorted(_ARMED - armed)} no longer arm the drain: their flag flips only after uvicorn has stopped serving"
