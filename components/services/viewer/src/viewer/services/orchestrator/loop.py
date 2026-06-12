"""Periodic orchestrator tick — runs inside the viewer process via lifespan.

Replaces the cron-driven `scripts/orchestrator.py`. On each tick:

  1. Sync the batches table with S3 (idempotent)
  2. If the prefetch slot is free and a chunk needs prefetch → submit a prefetch job
  3. If the htr slot is free and a chunk has cached ≥95% → submit an htr job

# TODO(post-NATS): this asyncio.Task pattern is the transitional shape while
# rask runs single-replica. Once NATS JetStream lands, replace with a JetStream
# consumer so the orchestrator survives viewer restarts and scales horizontally.
# See python-infrastructure/References/background-jobs.md.
"""

import asyncio
import logging
import time

import httpx
from anyio import to_thread
from ray.job_submission import JobSubmissionClient
from sqlalchemy.ext.asyncio import async_sessionmaker
from sqlmodel.ext.asyncio.session import AsyncSession

from storage import S3Client
from viewer.core.config import PIPELINE_DISABLED, Settings
from viewer.models.pipelines import PIPELINE_SPECS
from viewer.services.orchestrator.derive import derive_state
from viewer.services.ray_dashboard import build_client
from viewer.services.submission import submit_chunk
from viewer.services.sync import reconcile_from_s3


log = logging.getLogger(__name__)


async def tick(
    *,
    settings: Settings,
    sessionmaker: async_sessionmaker[AsyncSession],
    ray_client: JobSubmissionClient | None,
    http: httpx.AsyncClient,
    s3: S3Client | None,
    reconcile: bool = True,
) -> None:
    """One full tick: (optionally) sync + decide + submit. Idempotent end-to-end.

    `reconcile=False` skips the slow full-bucket S3 walk and decides from existing
    DB state — the run_loop throttles reconcile so it doesn't block every tick.
    """
    async with sessionmaker() as session:
        if reconcile and s3 is not None:
            await reconcile_from_s3(session, s3, cache_bucket=settings.cache_bucket, output_bucket=settings.output_bucket)

        state = await derive_state(
            http=http,
            client=ray_client,
            dashboard_url=settings.ray_dashboard_url,
            session=session,
        )
        if not state.ok:
            return
        if ray_client is None:
            return
        if state.prefetch is None:
            return
        if state.htr is None:
            return

        # Submit EVERY eligible chunk per lane; Ray/Kueue queue what doesn't fit.
        # derive_state already excludes in-flight + cooled-down chunks, so this
        # can't re-submit a running chunk.
        params = settings.runner_params()
        # Prefetch lane is optional — `RASK_PREFETCH_PIPELINE=none` runs HTR-only.
        if settings.prefetch_pipeline.lower() not in PIPELINE_DISABLED:
            prefetch_spec = PIPELINE_SPECS[settings.prefetch_pipeline]
            for cid in state.prefetch.eligible:
                log.info(f"orchestrator: submitting {settings.prefetch_pipeline} for chunk {cid}")
                await submit_chunk(session, ray_client, chunk_id=cid, params=params, spec=prefetch_spec)
        htr_spec = PIPELINE_SPECS[settings.htr_pipeline]
        # Concurrency cap: only top up to `htr_max_inflight` running jobs so a
        # batch run can't flood the head with driver subprocesses. 0 = unlimited.
        htr_eligible = state.htr.eligible
        if settings.htr_max_inflight > 0:
            free = max(0, settings.htr_max_inflight - len(state.htr.running))
            if free < len(htr_eligible):
                log.info(f"orchestrator: htr cap {settings.htr_max_inflight} ({len(state.htr.running)} running) — submitting {free} of {len(htr_eligible)} eligible")
            htr_eligible = htr_eligible[:free]
        for cid in htr_eligible:
            log.info(f"orchestrator: submitting {settings.htr_pipeline} for chunk {cid}")
            await submit_chunk(session, ray_client, chunk_id=cid, params=params, spec=htr_spec)


async def run_loop(
    *,
    settings: Settings,
    sessionmaker: async_sessionmaker[AsyncSession],
    ray_client: JobSubmissionClient | None,
    http: httpx.AsyncClient,
    s3: S3Client | None,
) -> None:
    """Tick forever until cancelled. A failure in one tick is logged and the loop continues.

    If Ray was unreachable at viewer boot, `ray_client` is None and stays that way for
    the task's life — so we re-attempt the (cheap) build each tick while it's None.
    A client that connected once is reused: it's a stateless HTTP wrapper that
    reconnects per request, so a Ray restart needs no rebuild.
    """
    interval = settings.orchestrator_interval_seconds
    recon_secs = settings.orchestrator_reconcile_seconds
    log.info(f"orchestrator loop started, interval={interval}s, reconcile_every={recon_secs}s")
    client = ray_client
    # Skip reconcile on the first tick so submission starts immediately from the
    # existing DB; the slow S3 walk then runs at most every `recon_secs`.
    last_reconcile = time.monotonic()
    first = True
    while True:
        if client is None:
            client = await to_thread.run_sync(build_client, settings.ray_dashboard_url)
            if client is not None:
                log.info("orchestrator: connected to Ray dashboard")
        do_reconcile = recon_secs == 0 or (not first and time.monotonic() - last_reconcile >= recon_secs)
        first = False
        try:
            await tick(settings=settings, sessionmaker=sessionmaker, ray_client=client, http=http, s3=s3, reconcile=do_reconcile)
            if do_reconcile:
                last_reconcile = time.monotonic()
        except Exception:
            log.exception("orchestrator tick failed; continuing")
        try:
            await asyncio.sleep(interval)
        except asyncio.CancelledError:
            log.info("orchestrator loop stopping")
            raise
