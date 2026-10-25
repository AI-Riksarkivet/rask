"""`spawn_publish` guards against a concurrent publish with a module-level `_RUNNING` set.

Two ways that guard turned into a permanent lock, and both leave a project unable to publish again
with nothing naming why -- `spawn_publish` simply logs "already running" and stands down, forever.

1. `_RUNNING.add(project_id)` ran BEFORE `create_task`. A `create_task` that raises then leaves the
   id claimed by a task that does not exist.
2. The task was returned but never REFERENCED. asyncio keeps only a weak reference to a running
   task, so an unreferenced one can be garbage-collected mid-flight -- which loses the publish and
   leaks `_RUNNING`, because `_drive`'s `finally` never runs. The caller's handle is not enough:
   `run_watchdog` discards it.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from annotator.projects import lakehouse


@pytest.fixture(autouse=True)
def _clean_state() -> Any:
    lakehouse._RUNNING.clear()
    lakehouse._TASKS.clear()
    yield
    lakehouse._RUNNING.clear()
    lakehouse._TASKS.clear()


@pytest.mark.asyncio
async def test_a_spawn_that_CANNOT_START_does_not_claim_the_project_forever(monkeypatch: pytest.MonkeyPatch) -> None:
    """THE LOCK. Claiming before the task exists means a failed spawn is indistinguishable from a
    running publish, and every later watchdog tick stands down."""
    loop = asyncio.get_running_loop()

    def _refuse(_coro: Any) -> Any:
        # Close the coroutine so the failure under test is the spawn, not a "never awaited" warning.
        _coro.close()
        raise RuntimeError("cannot schedule")

    monkeypatch.setattr(loop, "create_task", _refuse)

    with pytest.raises(RuntimeError):
        lakehouse.spawn_publish("p1")

    assert "p1" not in lakehouse._RUNNING, "a spawn that never started claimed the project permanently"


@pytest.mark.asyncio
async def test_a_second_spawn_while_one_runs_STILL_stands_down(monkeypatch: pytest.MonkeyPatch) -> None:
    """The behaviour the guard exists for, which the fix must not weaken."""
    release = asyncio.Event()
    started = asyncio.Event()

    async def _slow(_project_id: str) -> None:
        started.set()
        await release.wait()
        return

    monkeypatch.setattr(lakehouse, "run_publish_for", _slow)

    first = lakehouse.spawn_publish("p1")
    await started.wait()

    assert lakehouse.spawn_publish("p1") is None, "a concurrent publish was allowed to start"

    release.set()
    assert first is not None
    await first
