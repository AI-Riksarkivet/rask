"""Two producer-side contracts the plane cannot repair downstream, and neither had a test.

`.claude/skills/rask-notifications` states the shape: "a state change that names nobody is not
under-delivered, it is UNDELIVERABLE", and `notifiable()` answers an event it cannot target with a
SUCCESS ack — so a producer that drops a field fails silently and is reported by nothing.

1. TRAP 3 — `lance.project`. `fanout.py:88` skips the watcher loop entirely when `project` is None,
   and the run is still delivered to its AUTHOR, so the event looks completely healthy and simply
   reaches fewer people. Measured 2026-08-22: deleting the mover's stamp left 4,853 tests passing.
2. The `/produce` 503 tail. The route's own docstring makes it load-bearing — the bronze-write emit is
   the cascade head, so "a publish failure surfaces as 503 (not the 202 that would hide it)". Line
   coverage reported the whole branch missing: the `publish_failed` check, the problem+json body and
   the `Retry-After` header never executed.
"""

from __future__ import annotations

import ast
from pathlib import Path
from typing import Any, cast

import pytest
from dapr.aio.clients import DaprClient
from medallion.api import produce as produce_route
from medallion.core.config import MedallionSettings


SRC = Path(__file__).resolve().parents[1] / "src" / "medallion"

#: Stands in for a resolved dependency the code under test never touches.
_UNUSED = object()

#: Emit sites that legitimately carry no `project=`, by the reason they are exempt. An entry here is a
#: claim someone has to justify — which is the whole difference between this and a count.
_PROJECTLESS_EMITS = {
    "services/media_produce.py": (
        "the media head writes to a CONFIGURED platform target (`settings.media_bronze_namespace`), "
        "not a tenant's warehouse. `ingest_media` therefore takes no project, and rask-notifications "
        "records why the door has no `?project=`: 'the media head's target is configured and "
        "authorization scope must equal write scope'. Adding one to reach WATCH would break that "
        "invariant, so this lane reaches its author and no watchers BY CONSTRUCTION."
    ),
}


def _emit_sites() -> list[tuple[str, int, bool]]:
    """(file, line, stamps_project) for every `build_run_event(` call under medallion."""
    out: list[tuple[str, int, bool]] = []
    for py in sorted(SRC.rglob("*.py")):
        for node in ast.walk(ast.parse(py.read_text())):
            if isinstance(node, ast.Call) and getattr(node.func, "id", "") == "build_run_event":
                rel = py.relative_to(SRC).as_posix()
                out.append((rel, node.lineno, any(k.arg == "project" for k in node.keywords)))
    return out


def test_every_lineage_emit_stamps_lance_project() -> None:
    """Trap 3, at the producer — the only place it can be fixed."""
    sites = _emit_sites()
    assert sites, "no build_run_event call sites found — the scan root moved and this gate is vacuous"

    unstamped = sorted({rel for rel, _, stamped in sites if not stamped})
    assert unstamped == sorted(_PROJECTLESS_EMITS), (
        f"these medallion emit sites do not stamp `lance.project`: {unstamped}. fanout.py skips the "
        "watcher loop entirely when it is None, so the run still reaches its author and silently "
        "reaches NO watcher. Stamp it, or justify the omission in _PROJECTLESS_EMITS."
    )


@pytest.mark.asyncio
async def test_a_failed_publish_answers_503_and_not_the_202_that_would_hide_it(monkeypatch: pytest.MonkeyPatch) -> None:
    """The cascade head's failure must be visible to the caller, not swallowed into a 202."""

    async def _publish_failed(*_a: object, **_k: object) -> dict[str, str]:
        return {"status": "publish_failed", "token": "tok"}

    monkeypatch.setattr(produce_route, "run_produce", _publish_failed)
    # Neither dependency is touched on this path — `run_produce` is patched — so a cast is the
    # honest way to satisfy the resolved-dependency signature without loosening it.
    response: Any = await produce_route.produce(dapr=cast(DaprClient, _UNUSED), settings=cast(MedallionSettings, _UNUSED), originator=None)

    assert response.status_code == 503, "a dropped cascade head must not answer 202 — the run silently never happens"
    assert response.headers["Retry-After"] == "5"
    assert response.media_type == "application/problem+json"


@pytest.mark.asyncio
async def test_a_successful_produce_still_answers_the_202_body(monkeypatch: pytest.MonkeyPatch) -> None:
    """The twin, so the 503 assertion above cannot pass by rejecting everything."""

    async def _ok(*_a: object, **_k: object) -> dict[str, str]:
        return {"status": "produced", "token": "tok", "dataset": "bronze$events"}

    monkeypatch.setattr(produce_route, "run_produce", _ok)
    # Neither dependency is touched on this path — `run_produce` is patched — so a cast is the
    # honest way to satisfy the resolved-dependency signature without loosening it.
    response: Any = await produce_route.produce(dapr=cast(DaprClient, _UNUSED), settings=cast(MedallionSettings, _UNUSED), originator=None)

    assert response == {"status": "produced", "token": "tok", "dataset": "bronze$events"}
