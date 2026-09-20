"""The resubmit branch DELETES the dead job before posting a fresh one, and says it resubmitted.

[[XC-034]]. `submit_or_reattach`'s stage contract is the one that retries: a stage trigger is an
at-least-once redelivery, so re-attaching to a job that already FAILED would re-observe the same
failure on every delivery until maxDeliver silently drops the trigger — the deterministic id that
bought idempotency becoming the thing that guarantees the work never completes.

THE DELETE IS WHAT MAKES THE RESUBMIT POSSIBLE, not a tidy-up. Ray's Jobs API refuses to create a
submission id that already exists, so posting again without deleting returns the same 4xx and the
branch would loop back to re-attaching. Only the negative was tested before this: that `report` never
deletes (`test_train_rides_the_shared_kernel.py:116`), and a workflow-level activity count
(`test_a_vanished_stage_is_resubmitted.py:36`). Neither would notice the DELETE going missing.

THE ORDER IS ASSERTED, NOT JUST THE PRESENCE. A POST before the DELETE fails against a live Jobs API
and would leave the branch reporting `"resubmitted"` for a job it never replaced — the wrong-but-
plausible outcome, since the return value is what the caller acts on.
"""

from __future__ import annotations

from typing import Any, cast

import httpx
import pytest

from medallion.services.ray_jobs_api import submit_or_reattach


SUB_ID = "stage-bronze-to-silver-0001"
BODY = {"submission_id": SUB_ID, "entrypoint": "python /home/ray/jobs/ray_stage_job.py"}


class _Resp:
    def __init__(self, status_code: int, payload: dict[str, Any] | None = None) -> None:
        self.status_code = status_code
        self._payload = payload or {}

    def json(self) -> dict[str, Any]:
        return self._payload

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise AssertionError(f"raise_for_status on {self.status_code}")


class _Jobs:
    """A Jobs API double that SCRIPTS successive POST outcomes and records every verb in order.

    A sequence, not one status: the resubmit branch posts TWICE — the first rejected because the id
    exists, the second accepted after the delete — and a double returning one outcome cannot tell the
    two apart, which is exactly the distinction under test.
    """

    def __init__(self, *, posts: list[int], existing: str | None) -> None:
        self._posts = list(posts)
        self._existing = existing
        self.calls: list[tuple[str, str]] = []

    async def post(self, path: str, json: dict[str, Any]) -> _Resp:  # noqa: A002 — httpx's kwarg name
        self.calls.append(("POST", path))
        return _Resp(self._posts.pop(0))

    async def get(self, path: str) -> _Resp:
        self.calls.append(("GET", path))
        return _Resp(200, {"status": self._existing}) if self._existing else _Resp(404)

    async def delete(self, path: str) -> _Resp:
        self.calls.append(("DELETE", path))
        return _Resp(200)


@pytest.mark.asyncio
@pytest.mark.parametrize("terminal", ["FAILED", "STOPPED"])
async def test_a_terminally_failed_job_is_DELETED_then_reposted(terminal: str) -> None:
    """THE DEFECT: without the delete, the fresh POST is refused and the work never runs again.

    Both terminal-bad states, because `TERMINAL_BAD` holds two and a branch keyed on only one would
    leave STOPPED jobs re-attaching forever.
    """
    jobs = _Jobs(posts=[409, 200], existing=terminal)

    outcome = await submit_or_reattach(cast(httpx.AsyncClient, jobs), SUB_ID, BODY)

    assert outcome == "resubmitted"
    assert jobs.calls == [
        ("POST", "/api/jobs/"),
        ("GET", f"/api/jobs/{SUB_ID}"),
        ("DELETE", f"/api/jobs/{SUB_ID}"),
        ("POST", "/api/jobs/"),
    ], f"the delete must precede the fresh post: {jobs.calls}"


@pytest.mark.asyncio
async def test_a_failed_RESUBMIT_raises_rather_than_reporting_success() -> None:
    """`fresh.raise_for_status()` is the line under test. Answering "resubmitted" for a post the Jobs
    API refused would tell the workflow the work is running when nothing is."""
    jobs = _Jobs(posts=[409, 500], existing="FAILED")

    with pytest.raises(Exception):  # noqa: B017 — the kernel wraps the httpx error in its own type
        await submit_or_reattach(cast(httpx.AsyncClient, jobs), SUB_ID, BODY)


@pytest.mark.asyncio
async def test_report_NEVER_deletes(terminal: str = "FAILED") -> None:
    """The train contract's divergence, asserted here beside the branch it diverges from: expensive
    compute is terminal until a human resubmits, so the dead job must survive for them to inspect."""
    jobs = _Jobs(posts=[409], existing=terminal)

    outcome = await submit_or_reattach(cast(httpx.AsyncClient, jobs), SUB_ID, BODY, on_terminal_failure="report")

    assert outcome == "already_failed"
    assert not [c for c in jobs.calls if c[0] == "DELETE"], jobs.calls


@pytest.mark.asyncio
async def test_a_job_that_is_STILL_RUNNING_is_reattached_not_replaced() -> None:
    """The control that stops the branch firing too widely. Deleting a running job would kill work in
    flight and restart it from nothing — strictly worse than the failure this row exists to fix."""
    jobs = _Jobs(posts=[409], existing="RUNNING")

    outcome = await submit_or_reattach(cast(httpx.AsyncClient, jobs), SUB_ID, BODY)

    assert outcome == "reattached"
    assert not [c for c in jobs.calls if c[0] == "DELETE"], jobs.calls
    assert len([c for c in jobs.calls if c[0] == "POST"]) == 1, "a reattach must not post again"
