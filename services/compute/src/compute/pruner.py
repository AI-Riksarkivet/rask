"""The job-retention cron binding — Dapr's sidecar POSTs here on the chart's schedule (#136).

Mirrors the maintenance service's cron shape exactly (`maintenance/api/routes.py`): the route path
IS the binding name, `require_dapr_token` makes it sidecar-only, and OPTIONS acks Dapr's
binding-discovery pre-flight (else Dapr logs the app as not consuming the binding). The schedule
lives in the chart's Component — no scheduler thread in the service.

**The router is BUILT, not declared** (`make_pruner_router`), and the retention bounds are read per
call off `ComputeSettings`. All three knobs used to be `os.environ.get()` at module import, two of
them wrapped in a bare `int()` — so a typo raised `ValueError: invalid literal for int()` out of an
import naming no field, and the bounds were frozen at first import where nothing could inject or
exercise them (FLEET-ENV-SCATTER). The PATH still has to be decided when the app is built, because a
binding is delivered to `POST /<name>`; it is now decided from the same validated model as
everything else. Its documentation lives on the fields — see `compute.config`.
"""

from __future__ import annotations

import logging
from typing import cast

from fastapi import APIRouter, Depends, Response
from fastapi.concurrency import run_in_threadpool

from compute.config import ComputeSettings
from compute.dependencies import ComputeSettingsDep, RayClientDep
from ray_kit.prune import JobsClient, PruneResult, prune_jobs
from service_kit.governed.dapr_auth import require_dapr_token


log = logging.getLogger(__name__)


async def on_prune_cron(client: RayClientDep, settings: ComputeSettingsDep) -> PruneResult:
    if client is None:
        # Ray not up yet (fresh cluster ordering) — nothing to prune, and the next tick retries.
        log.info("prune tick skipped: Ray dashboard unreachable")
        return PruneResult(total=0, kept_newest=0)
    # `cast`: JobSubmissionClient satisfies the protocol structurally, but its Pydantic-v1
    # `JobDetails` fields are invisible to the checker — verified against the real signatures
    # (delete_job(self, job_id: str) -> bool; list_jobs(self) -> List[JobDetails]).
    result = await run_in_threadpool(
        prune_jobs,
        cast(JobsClient, client),
        keep_newest=settings.prune_keep_jobs,
        keep_newest_failed=settings.prune_keep_failed_jobs,
    )
    log.info(
        "job retention: total=%d kept=%d deleted=%d failed=%d active=%d",
        result.total,
        result.kept_newest,
        result.deleted,
        result.failed,
        result.skipped_active,
    )
    return result


async def ack_binding() -> Response:
    return Response(status_code=200)


def make_pruner_router(settings: ComputeSettings) -> APIRouter:
    """The cron router, mounted at the configured binding name.

    A factory rather than a module-level `router`, because the path is configuration: a module
    constant made the route unbuildable for any name but the one the process happened to import
    with, and untestable without reloading the module.
    """
    router = APIRouter()
    path = f"/{settings.prune_binding}"
    router.add_api_route(path, on_prune_cron, methods=["POST"], tags=["compute"], dependencies=[Depends(require_dapr_token)])
    router.add_api_route(path, ack_binding, methods=["OPTIONS"], include_in_schema=False)
    return router
