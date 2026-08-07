"""The job-retention cron binding — Dapr's sidecar POSTs here on the chart's schedule (#136).

Mirrors the maintenance service's cron shape exactly (`maintenance/api/routes.py`): the route path
IS the binding name, `require_dapr_token` makes it sidecar-only, and OPTIONS acks Dapr's
binding-discovery pre-flight (else Dapr logs the app as not consuming the binding). The schedule
lives in the chart's Component — no scheduler thread in the service.

Env, set by the chart:
  RASK_PRUNE_BINDING     the binding/route name (default ``compute-prune-jobs-cron``)
  RASK_PRUNE_KEEP_JOBS   newest submissions to keep (default 500 — the jobs board pages and no
                         surface renders more than a screenful; Ray's history is otherwise
                         unbounded and reached 81,155 live)
"""

from __future__ import annotations

import logging
import os
from typing import cast

from fastapi import APIRouter, Depends, Response
from fastapi.concurrency import run_in_threadpool

from compute.dependencies import RayClientDep
from ray_kit.prune import JobsClient, PruneResult, prune_jobs
from service_kit.governed.dapr_auth import require_dapr_token


log = logging.getLogger(__name__)

_BINDING = os.environ.get("RASK_PRUNE_BINDING", "compute-prune-jobs-cron")
_KEEP = int(os.environ.get("RASK_PRUNE_KEEP_JOBS", "500"))

router = APIRouter()


async def on_prune_cron(client: RayClientDep) -> PruneResult:
    if client is None:
        # Ray not up yet (fresh cluster ordering) — nothing to prune, and the next tick retries.
        log.info("prune tick skipped: Ray dashboard unreachable")
        return PruneResult(total=0, kept_newest=0)
    # `cast`: JobSubmissionClient satisfies the protocol structurally, but its Pydantic-v1
    # `JobDetails` fields are invisible to the checker — verified against the real signatures
    # (delete_job(self, job_id: str) -> bool; list_jobs(self) -> List[JobDetails]).
    result = await run_in_threadpool(prune_jobs, cast(JobsClient, client), keep_newest=_KEEP)
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


router.add_api_route(f"/{_BINDING}", on_prune_cron, methods=["POST"], tags=["compute"], dependencies=[Depends(require_dapr_token)])
router.add_api_route(f"/{_BINDING}", ack_binding, methods=["OPTIONS"], include_in_schema=False)
