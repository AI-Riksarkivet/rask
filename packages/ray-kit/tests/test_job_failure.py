"""`job_failure` reads the CAUSE off the same response `job_status` reads the status off.

The defect this file exists for: `job_status` did `response.json().get("status")` and threw the rest
away, so every Ray failure the medallion watcher reported said only "ended FAILED after N poll(s)".
`RayJob` had declared `error_type`, `message` and `driver_exit_code` since the OOM work — nothing
asked for them. `driver_exit_code` 137 is SIGKILL, i.e. a host-RAM OOM, and telling that apart from
an ordinary exception is the difference between an actionable failure and a ticket.
"""

from typing import Any

import httpx
import pytest

from ray_kit.schemas import RayJobFailure
from ray_kit.submit import RayJobError, job_failure


def _client(handler: Any) -> httpx.AsyncClient:
    return httpx.AsyncClient(base_url="http://ray", transport=httpx.MockTransport(handler))


@pytest.mark.asyncio
async def test_it_lifts_rays_three_cause_fields_off_the_job_detail() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        # The shape Ray's `GET /api/jobs/<id>` actually answers with — `status` alongside the cause,
        # which is why this needs no second endpoint.
        return httpx.Response(
            200,
            json={
                "submission_id": "ray-silver-tok-1",
                "status": "FAILED",
                "error_type": "RuntimeError",
                "message": "Job entrypoint command failed with exit code 137",
                "driver_exit_code": 137,
            },
        )

    async with _client(handler) as client:
        failure = await job_failure(client, "ray-silver-tok-1")

    assert failure is not None
    assert failure.error_type == "RuntimeError"
    assert failure.driver_exit_code == 137
    assert "exit code 137" in (failure.message or "")


@pytest.mark.asyncio
async def test_an_unknown_job_is_NONE_not_a_raise() -> None:
    """Ray prunes terminal jobs by recency, so a failure CAN outlive its own record. A missing cause
    is an absence of enrichment, never evidence about the job — same contract as `job_status`."""

    async with _client(lambda _r: httpx.Response(404)) as client:
        assert await job_failure(client, "long-gone") is None


@pytest.mark.asyncio
async def test_an_unreachable_dashboard_RAISES() -> None:
    """The other half of `job_status`'s contract: a transport or 5xx failure is not an answer about
    the job, and silently returning None would let a dashboard outage read as "no cause reported"."""

    async with _client(lambda _r: httpx.Response(503)) as client:
        with pytest.raises(RayJobError):
            await job_failure(client, "sub")


@pytest.mark.asyncio
async def test_rays_runtime_env_and_metadata_do_not_ride_along() -> None:
    """`extra="ignore"`, and it is load-bearing. Ray's JobDetails carries the job's full `runtime_env`
    — pip list, working-dir refs and EVERY env var, which on this estate includes S3_SECRET and the
    lineage service token — plus an arbitrary user `metadata` dict. Retaining either would put them
    into whatever this is rendered into, and this is rendered into a lineage event."""

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "status": "FAILED",
                "driver_exit_code": 1,
                "runtime_env": {"env_vars": {"S3_SECRET": "hunter2", "LINEAGE_SERVICE_TOKEN": "tok"}},
                "metadata": {"rask.originator": "alice"},
            },
        )

    async with _client(handler) as client:
        failure = await job_failure(client, "sub")

    assert failure is not None
    assert "hunter2" not in failure.model_dump_json()
    assert "runtime_env" not in failure.model_dump()
    # The test's NAME says "and metadata", and until 2026-08-22 nothing here checked it — so half the
    # claim in the title was unasserted. It matters as much as the other half: the medallion's own
    # submitter puts `rask.token` into that dict beside `rask.originator` (`ray_submit.py:166`), and
    # this model is rendered into a lineage event.
    assert "metadata" not in failure.model_dump()
    assert "alice" not in failure.model_dump_json()


def test_the_summary_keeps_the_TYPE_and_the_CODE_when_it_truncates() -> None:
    """Truncation may only ever eat the free-text tail. The error type classifies the failure and the
    exit code says whether it was an OOM; a cap that swallowed either would leave a longer string
    saying less than the short one it replaced."""
    long = RayJobFailure(error_type="RuntimeError", message="X" * 5_000, driver_exit_code=137)
    rendered = long.summary(80)

    assert rendered.startswith("RuntimeError:")
    assert rendered.endswith("(driver exit 137)")
    assert "truncated" in rendered
    assert len(rendered) < 200


def test_a_cause_ray_did_not_report_renders_EMPTY_rather_than_a_blank_line() -> None:
    """So a caller can tell "no cause available" from "the cause is blank" — the medallion watcher
    appends nothing at all rather than a dangling em-dash."""
    assert RayJobFailure().summary(800) == ""
    assert RayJobFailure(driver_exit_code=137).summary(800) == "driver exit 137"
