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
import hmac
import os
import threading
from typing import TYPE_CHECKING, Any

import worker
from ray import serve


if TYPE_CHECKING:
    from starlette.responses import JSONResponse


# The estate's standard sizing knobs (open_ray-kernel.md move 3): the NAMES are uniform across
# every runner's Serve deployment so one operator gesture sizes any workload; the DEFAULTS are
# this workload's own — one replica, CPU (the build is LLM-bound over HTTP, not GPU-bound here).
SERVE_REPLICAS = int(os.environ.get("RASK_SERVE_REPLICAS", "1"))
SERVE_GPU_FRAC = float(os.environ.get("RASK_SERVE_GPU_FRAC", "0"))


@serve.deployment(num_replicas=SERVE_REPLICAS, ray_actor_options={"num_gpus": SERVE_GPU_FRAC})
class TopicsDeployment:
    """Build Swedish topic layers on a Lance DB's chunks (Toponymy).

    The database and optional LLM endpoint come from deployment configuration,
    never from the request. Heavy imports (toponymy) stay lazy inside
    :func:`worker.main` so replica start-up is cheap.
    """

    def __init__(self) -> None:
        # ONE build at a time, said out loud. It used to be true by accident — the
        # blocked event loop could not start a second request — and the build writes
        # the whole chunks table, so two of them against one DB would fight. Now that
        # the loop is free, the serialisation has to be deliberate.
        self._building = threading.Lock()
        self._serve_token = os.environ.get("MEDIA_TOPICS_SERVE_TOKEN", "")
        self._db_path = os.environ.get("MEDIA_TOPICS_DB_PATH", "")
        self._llm_url = os.environ.get("MEDIA_TOPICS_LLM_URL", "").strip() or None

    def _is_authorized(self, authorization: str | None) -> bool:
        if not self._serve_token or not self._serve_token.strip():
            return False
        parts = (authorization or "").split()
        return len(parts) == 2 and parts[0].casefold() == "bearer" and hmac.compare_digest(self._serve_token.encode("utf-8"), parts[1].encode("utf-8"))

    async def __call__(self, request: Any) -> dict[str, int] | JSONResponse:
        """Build the topic layers off the event loop.

        `worker.run` is the entire Toponymy build — embedding plus LLM topic-naming
        over a Lance DB's chunks table, minutes to hours. Run inline it froze the
        replica's loop, so Serve's health probe queued behind it and the controller
        declared the replica unhealthy and restarted it MID-BUILD, losing the work.
        That is the failure `runners/htr/scripts/deploy_htr.py` already records:
        "event loop unresponsive -> Serve killed every replica -> restart storm".
        """
        from starlette.responses import JSONResponse

        if not self._serve_token or not self._serve_token.strip():
            return JSONResponse({"detail": "topics Serve authentication is not configured"}, status_code=503)
        if not self._is_authorized(request.headers.get("authorization")):
            return JSONResponse(
                {"detail": "bearer authentication required"},
                status_code=401,
                headers={"WWW-Authenticate": "Bearer"},
            )
        if not self._db_path or not self._db_path.strip():
            return JSONResponse({"detail": "topics database is not configured"}, status_code=503)
        try:
            body = await request.json()
        except (TypeError, ValueError):
            return JSONResponse({"detail": "request body must be an empty JSON object"}, status_code=400)
        if not isinstance(body, dict) or body:
            return JSONResponse({"detail": "request body must be an empty JSON object"}, status_code=400)

        rows = await asyncio.to_thread(self._run_build)
        return {"rows": rows}

    def _run_build(self) -> int:
        with self._building:
            return worker.run(db_path=self._db_path, llm_url=self._llm_url)


app = TopicsDeployment.bind()
