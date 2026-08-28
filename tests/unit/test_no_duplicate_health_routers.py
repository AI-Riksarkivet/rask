"""DUP-06: the ``/health`` liveness badge router has ONE definition, in service_kit.

Five services (compute, controlplane, flows, ingest, notifications) each carried a
line-identical ``health.py`` defining its own ``async def health() -> Liveness`` behind an
``APIRouter``. That body is the estate-wide liveness badge; it belongs to
``service_kit.health.make_health_router`` and every service's ``health.py`` re-exports it.
This gate fails if any service re-grows a local handler.
"""

from __future__ import annotations

import ast
from pathlib import Path


_REPO_ROOT = Path(__file__).resolve().parents[2]


def _service_health_modules() -> list[Path]:
    return sorted(_REPO_ROOT.glob("services/*/src/*/health.py"))


def _defines_local_health_handler(source: str) -> bool:
    tree = ast.parse(source)
    return any(isinstance(node, ast.AsyncFunctionDef) and node.name == "health" for node in ast.walk(tree))


def test_service_health_modules_exist() -> None:
    # Guard the guard: if the glob matched nothing the assertion below would pass vacuously.
    assert _service_health_modules(), "expected per-service health.py re-export modules"


def test_no_service_defines_its_own_health_handler() -> None:
    offenders = [p for p in _service_health_modules() if _defines_local_health_handler(p.read_text())]
    assert not offenders, f"health handler must live only in service_kit; local copies: {[str(p) for p in offenders]}"
