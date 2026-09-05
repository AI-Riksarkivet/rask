"""`collect` reads every task's actor concurrently, and still refuses deterministically.

docs/DECISIONS.md "The Python estate audit" `ANN-03`, the publish-path half the send fix (SendMany) did not cover. `collect`
loops the project's task ids and awaits `handle.get()` then `handle.get_draft()` on EACH task's OWN
actor — one per task id, so they are DIFFERENT actor ids and genuinely parallelise (unlike the send
path, whose per-task calls all hit ONE project actor and queue on its turn lock, which is why that
half needed SendMany rather than a fan-out). The actor docstring says this step "must not be slow or
flaky"; awaited sequentially it is O(tasks) sidecar round-trips on the publish critical path.

THE ORDERING TRAP the re-verification flagged: `gather` yields in COMPLETION order, so the refusal
for a task whose actor lost its state must be decided AFTER the gather, on the LOWEST-INDEX missing
task — otherwise the `PublishRefusal` message names whichever call happened to finish first and
flaps between runs.
"""

from __future__ import annotations

import asyncio
from typing import Any, cast

import pytest

from annotator.projects.publish import PublishRefusal
from annotator.projects.saga import ProjectHandle, collect


class _CountingProject:
    async def get(self) -> dict[str, Any] | None:
        return None


class _SlowTask:
    """Records concurrency so a sequential await is visible; each `get` yields the loop once."""

    in_flight = 0
    peak = 0

    def __init__(self, task_id: str, present: bool) -> None:
        self.task_id = task_id
        self.present = present

    async def get(self) -> dict[str, Any] | None:
        type(self).in_flight += 1
        type(self).peak = max(type(self).peak, type(self).in_flight)
        try:
            await asyncio.sleep(0.02)
            if not self.present:
                return None
            return {
                "task_id": self.task_id,
                "project_id": "p1",
                "state": "accepted",
                "submitted_by": "g",
                "reviewed_by": "c",
                "review_action": "accepted",
                "source": {"kind": "chunks", "keys": [self.task_id]},
                "media": {"kind": "image", "image_url": "s3://b/x.jpg"},
            }
        finally:
            type(self).in_flight -= 1

    async def get_draft(self) -> dict[str, Any] | None:
        return None


@pytest.mark.asyncio
async def test_collect_reads_the_task_actors_concurrently() -> None:
    _SlowTask.peak = 0
    handles = {t: _SlowTask(t, present=True) for t in ("t0", "t1", "t2", "t3")}
    out = await collect(cast("ProjectHandle", _CountingProject()), lambda t: handles[t], ["t0", "t1", "t2", "t3"])
    assert len(out) == 4
    assert _SlowTask.peak > 1, "the task actors were read one at a time — the publish path still serialises collect"


@pytest.mark.asyncio
async def test_a_missing_actor_refuses_and_names_the_LOWEST_index_deterministically() -> None:
    """t1 and t3 both lost their state; the refusal must always name t1 (lowest index), never t3,
    however the concurrent gets happen to finish."""
    handles = {t: _SlowTask(t, present=(t not in ("t1", "t3"))) for t in ("t0", "t1", "t2", "t3")}
    for _ in range(5):
        with pytest.raises(PublishRefusal, match="t1"):
            await collect(cast("ProjectHandle", _CountingProject()), lambda t: handles[t], ["t0", "t1", "t2", "t3"])
