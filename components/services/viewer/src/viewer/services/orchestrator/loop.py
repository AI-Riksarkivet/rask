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

import httpx
from ray.job_submission import JobSubmissionClient
from sqlalchemy.ext.asyncio import async_sessionmaker
from sqlmodel.ext.asyncio.session import AsyncSession

from viewer.core.config import Settings
from viewer.models.enums import Pipeline
from viewer.services.orchestrator.derive import derive_state
from viewer.services.submission import submit_chunk
from viewer.services.sync import reconcile_from_s3


log = logging.getLogger(__name__)


async def tick(
    *,
    settings: Settings,
    sessionmaker: async_sessionmaker[AsyncSession],
    ray_client: JobSubmissionClient | None,
    http: httpx.AsyncClient,
) -> None:
    """One full tick: sync + decide + submit. Idempotent end-to-end."""
    async with sessionmaker() as session:
        if settings.hcp_endpoint:
            await reconcile_from_s3(
                session,
                hcp_endpoint=settings.hcp_endpoint,
                cache_bucket=settings.cache_bucket,
                output_bucket=settings.output_bucket,
            )

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
        if state.prefetch is None or state.htr is None:
            return

        output_uri = f"s3://{settings.output_bucket}"
        if state.prefetch.running is None and state.prefetch.next is not None:
            log.info(f"orchestrator: submitting prefetch for chunk {state.prefetch.next}")
            await submit_chunk(
                session,
                ray_client,
                chunk_id=state.prefetch.next,
                repo_root=settings.repo_root,
                cache_bucket=settings.cache_bucket,
                output=output_uri,
                iiif_url=settings.iiif_url,
                pipeline=Pipeline.PREFETCH.value,
            )
        if state.htr.running is None and state.htr.next is not None:
            log.info(f"orchestrator: submitting {settings.htr_pipeline} for chunk {state.htr.next}")
            await submit_chunk(
                session,
                ray_client,
                chunk_id=state.htr.next,
                repo_root=settings.repo_root,
                cache_bucket=settings.cache_bucket,
                output=output_uri,
                iiif_url=settings.iiif_url,
                pipeline=settings.htr_pipeline,
            )


async def run_loop(
    *,
    settings: Settings,
    sessionmaker: async_sessionmaker[AsyncSession],
    ray_client: JobSubmissionClient | None,
    http: httpx.AsyncClient,
) -> None:
    """Tick forever until cancelled. A failure in one tick is logged and the loop continues."""
    interval = settings.orchestrator_interval_seconds
    log.info(f"orchestrator loop started, interval={interval}s")
    while True:
        try:
            await tick(settings=settings, sessionmaker=sessionmaker, ray_client=ray_client, http=http)
        except Exception:
            log.exception("orchestrator tick failed; continuing")
        try:
            await asyncio.sleep(interval)
        except asyncio.CancelledError:
            log.info("orchestrator loop stopping")
            raise
