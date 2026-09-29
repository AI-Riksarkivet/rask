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

from medallion.services.ray_job_failure import RayJobFailure
from medallion.services.ray_jobs_api import RayJobError, job_failure


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


def test_a_cause_ray_did_not_report_renders_EMPTY_rather_than_a_blank_line() -> None:
    """So a caller can tell "no cause available" from "the cause is blank" — the medallion watcher
    appends nothing at all rather than a dangling em-dash."""
    assert RayJobFailure().summary(800) == ""
    assert RayJobFailure(driver_exit_code=137).summary(800) == "driver exit 137"
