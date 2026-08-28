"""Ray Serve deployment for the topics model service (the merge-time online form).

This is the ONLINE form. The batch form is the same :func:`worker.main` compute
run directly in this runner's sealed env, or submitted as a Ray Job. At merge
this ``@serve.deployment`` serves query-time callers; the batch path keeps going
through the job seam.

Runs ONLY in this runner's env (``ray[serve]`` + toponymy, the ``serve`` extra),
never in the platform's — the runner is sealed, which is why ``runners/`` is
excluded from the root type-check.
"""

from __future__ import annotations

import asyncio
import os
from typing import Any

import worker
from ray import serve  # type: ignore[import-not-found]


# The estate's standard sizing knobs (open_ray-kernel.md move 3): the NAMES are uniform across
# every runner's Serve deployment so one operator gesture sizes any workload; the DEFAULTS are
# this workload's own — one replica, CPU (the build is LLM-bound over HTTP, not GPU-bound here).
SERVE_REPLICAS = int(os.environ.get("RASK_SERVE_REPLICAS", "1"))
SERVE_GPU_FRAC = float(os.environ.get("RASK_SERVE_GPU_FRAC", "0"))


@serve.deployment(num_replicas=SERVE_REPLICAS, ray_actor_options={"num_gpus": SERVE_GPU_FRAC})
class TopicsDeployment:
    """Build Swedish topic layers on a Lance DB's chunks (Toponymy).

    The request carries the DB path (S3/local URI) + the optional LLM endpoint;
    the deployment runs the same compute as the sealed CLI worker and returns the
    row count. Heavy imports (toponymy) stay lazy inside :func:`worker.main` so
    replica start-up is cheap.
    """

    def __init__(self) -> None:
        # ONE build at a time, said out loud. It used to be true by accident — the
        # blocked event loop could not start a second request — and the build writes
        # the whole chunks table, so two of them against one DB would fight. Now that
        # the loop is free, the serialisation has to be deliberate.
        self._building = asyncio.Lock()

    async def __call__(self, request: Any) -> dict[str, int]:
        """Build the topic layers off the event loop.

        `worker.run` is the entire Toponymy build — embedding plus LLM topic-naming
        over a Lance DB's chunks table, minutes to hours. Run inline it froze the
        replica's loop, so Serve's health probe queued behind it and the controller
        declared the replica unhealthy and restarted it MID-BUILD, losing the work.
        That is the failure `runners/htr/scripts/deploy_htr.py` already records:
        "event loop unresponsive -> Serve killed every replica -> restart storm".
        """
        body = await request.json()
        async with self._building:
            rows = await asyncio.to_thread(worker.run, db_path=body["db"], llm_url=body.get("llm_url"))
        return {"rows": rows}


app = TopicsDeployment.bind()
