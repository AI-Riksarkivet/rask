"""The read paths that OOMKilled `rask-compute` must stay BOUNDED as the cluster grows.

Measured on the live k3s cluster 2026-08-06, before the fix: `/api/ray/jobs` returned 81,155 jobs
in 164.7 MB, one call peaked at 1179 MiB of RSS against a 1536 MiB container limit, and two
concurrent calls reached 1488 MiB — 48 MiB under the cap. That is the OOMKill.

The defect was an ORDERING one: `list_jobs` validated every job into a `RayJob` and sorted
afterwards, so the cost scaled with the cluster's entire history rather than with what the UI
renders. Ray cannot help — `GET /api/jobs` is specified (v4.0.0) with no `parameters` at all, so it
always returns everything and the bound has to be ours.

These tests are written to FAIL on the old code: `test_list_jobs_caps_the_materialised_list` fails
because the old path returned all 5,000, and `test_list_jobs_validates_only_the_kept_jobs` fails
because the old path called `.dict()` on every one of them.
"""

from typing import cast

import pytest
from ray.job_submission import JobSubmissionClient

from ray_kit import dashboard
from ray_kit.schemas import RayJob


_DASH = "http://ray-head:8265"


class _FakeJobDetails:
    """Structural stand-in for Ray's `JobDetails` (a Pydantic V1 model, hence `.dict()`).

    `dict_calls` counts materialisations so a test can prove the expensive step is paid per KEPT
    job rather than per job in the cluster — the actual regression, which a length assertion alone
    would not catch (a fix that validated everything and then sliced would still pass that).
    """

    dict_calls = 0

    def __init__(self, submission_id: str, start_time: int, *, bulky: dict | None = None) -> None:
        self.submission_id = submission_id
        self.start_time = start_time
        self.entrypoint = f"runner --chunk {submission_id}"
        self.status = "SUCCEEDED"
        self._bulky = bulky or {}

    def dict(self) -> dict[str, object]:
        type(self).dict_calls += 1
        return {
            "submission_id": self.submission_id,
            "start_time": self.start_time,
            "entrypoint": self.entrypoint,
            "status": self.status,
            **self._bulky,
        }


class _FakeClient:
    def __init__(self, jobs: list[_FakeJobDetails]) -> None:
        self._jobs = jobs

    def list_jobs(self) -> list[_FakeJobDetails]:
        return self._jobs


def _client(jobs: list[_FakeJobDetails]) -> JobSubmissionClient:
    return cast(JobSubmissionClient, _FakeClient(jobs))


@pytest.mark.asyncio
async def test_list_jobs_caps_the_materialised_list() -> None:
    """5,000 jobs in the cluster must not become 5,000 jobs in the response."""
    jobs = [_FakeJobDetails(f"job-{i}", start_time=i) for i in range(5_000)]

    payload = await dashboard.list_jobs(_client(jobs), _DASH)

    assert payload.ok
    assert len(payload.jobs) == dashboard.MAX_JOBS
    assert payload.total == 5_000
    assert payload.truncated is True


@pytest.mark.asyncio
async def test_list_jobs_validates_only_the_kept_jobs() -> None:
    """The EXPENSIVE step is paid per kept job, not per job in the cluster.

    This is the memory property. Without it the cap is cosmetic: peak allocation would still scale
    with cluster history even though the response is short.
    """
    jobs = [_FakeJobDetails(f"job-{i}", start_time=i) for i in range(5_000)]
    _FakeJobDetails.dict_calls = 0

    await dashboard.list_jobs(_client(jobs), _DASH)

    assert _FakeJobDetails.dict_calls == dashboard.MAX_JOBS


@pytest.mark.asyncio
async def test_list_jobs_keeps_the_NEWEST_jobs() -> None:
    """Capping must not silently discard the runs an operator is actually watching."""
    jobs = [_FakeJobDetails(f"job-{i}", start_time=i) for i in range(5_000)]

    payload = await dashboard.list_jobs(_client(jobs), _DASH)

    assert payload.jobs[0].submission_id == "job-4999"
    assert payload.jobs[0].start_time == 4_999
    returned = {j.start_time for j in payload.jobs if j.start_time is not None}
    assert min(returned) == 5_000 - dashboard.MAX_JOBS


@pytest.mark.asyncio
async def test_a_small_cluster_is_not_reported_as_truncated() -> None:
    """`truncated` is the honesty signal — it must not cry wolf when nothing was dropped."""
    jobs = [_FakeJobDetails(f"job-{i}", start_time=i) for i in range(3)]

    payload = await dashboard.list_jobs(_client(jobs), _DASH)

    assert len(payload.jobs) == 3
    assert payload.total == 3
    assert payload.truncated is False


@pytest.mark.asyncio
async def test_bulky_ray_fields_are_dropped_not_retained() -> None:
    """`runtime_env`/`driver_info` are fat and nothing renders them; `metadata` is fat but its
    `rask.` keys ARE rendered, so it is PROJECTED rather than dropped whole.

    `RayJob` was `extra="allow"`, so every one of these rode along on every row. Fails on the old
    model config.

    The metadata assertion CHANGED when the job page began naming a run's lane. The invariant is
    unchanged and is what is still asserted here — an arbitrary user dict must not ride along, or
    the unbounded growth `MAX_JOBS` closed comes back through a different door. What changed is the
    mechanism: dropping the field whole also destroyed `rask.originator`/`project`/`token`/`stage`/
    `lane`, the identity the medallion deliberately puts in `metadata` because that is what survives
    to `GET /api/jobs/<id>` and stays readable after a job fails. So the projection keeps the `rask.`
    prefix and discards everything else, and this test now pins BOTH halves.
    """
    bulky = {
        "runtime_env": {"pip": ["torch"] * 200, "env_vars": {f"K{i}": "v" * 100 for i in range(50)}},
        "metadata": {f"m{i}": "x" * 100 for i in range(50)},
        "driver_info": {"id": "d", "node_ip_address": "10.0.0.1", "pid": "1"},
    }
    jobs = [_FakeJobDetails("job-1", start_time=1, bulky=bulky)]

    payload = await dashboard.list_jobs(_client(jobs), _DASH)

    kept = payload.jobs[0].model_dump()
    assert "runtime_env" not in kept
    assert "driver_info" not in kept
    # The fifty foreign keys do not survive — the bulk half of the invariant, unchanged.
    assert kept["metadata"] == {}
    # …while the fields the SPA actually reads survive.
    assert kept["submission_id"] == "job-1"
    assert kept["entrypoint"] == "runner --chunk job-1"


def test_declared_fields_cover_every_field_the_frontend_reads() -> None:
    """Narrowing to `extra="ignore"` is only safe while this holds.

    Grepped from `frontend/microfrontends/compute` and `@rask/api` at the time of the change. If a
    zone starts reading a new Ray field, this fails and points at the model instead of shipping a
    silently-absent value to the UI.
    """
    read_by_frontend = {
        "submission_id",
        "job_id",
        "status",
        "entrypoint",
        "start_time",
        "end_time",
        "batches",
        "logs_url",
        "error_type",
        "message",
        "driver_exit_code",
    }
    assert read_by_frontend <= set(RayJob.model_fields)


def test_task_limit_is_below_rays_api_ceiling() -> None:
    """`limit=10000` was EXACTLY `RAY_MAX_LIMIT_FROM_API_SERVER` — the largest page Ray will serve.

    On an endpoint polled every 5 s by two pages, with `detail=1` attaching `runtime_env_info`,
    `events` and `profiling_data` to every row.
    """
    assert dashboard.MAX_TASKS < 10_000


@pytest.mark.asyncio
async def test_metadata_projection_keeps_identity_and_discards_the_rest() -> None:
    """The other half of the projection: `rask.` survives, a workload's own keys do not.

    Without this, someone restoring the old whole-field drop would still see a green suite — the
    bulk assertion alone passes when metadata is dropped entirely, which is exactly how the lane
    identity was lost the first time.
    """
    bulky = {
        "metadata": {
            "rask.lane": "dummy",
            "rask.stage": "silver",
            **{f"workload{i}": "x" * 100 for i in range(50)},
        },
    }
    jobs = [_FakeJobDetails("job-1", start_time=1, bulky=bulky)]

    payload = await dashboard.list_jobs(_client(jobs), _DASH)

    assert payload.jobs[0].metadata == {"rask.lane": "dummy", "rask.stage": "silver"}
