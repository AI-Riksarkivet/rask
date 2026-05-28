"""RayJob submission for one chunk.

Reads chunk membership from `batches`, builds the runner entrypoint, submits
via the Ray SDK, and (for the htr/htrflow pipelines) tags `current_rayjob_id`
on every batch in the chunk.

The Ray SDK is sync, so the submit call is wrapped in `anyio.to_thread`.
"""

import logging
import os
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path

from anyio import to_thread
from pydantic import BaseModel
from ray.job_submission import JobSubmissionClient
from sqlmodel import col, select
from sqlmodel.ext.asyncio.session import AsyncSession

from viewer.models.batch import Batch
from viewer.models.enums import ManifestStatus


log = logging.getLogger(__name__)

_ENV_PASSTHROUGH_PREFIXES = ("AWS_", "HCP_", "IIIF_", "RASK_")
_HTR_PIPELINES = frozenset({"htr", "htrflow"})


class SubmitResult(BaseModel):
    chunk_id: int
    chunk_total: int
    pipeline: str
    submission_id: str
    batches: list[str]


class StopResult(BaseModel):
    chunk_id: int
    stopped_submission_id: str
    stopped: bool


def chunk_name(chunk_id: int, chunk_total: int, pipeline: str = "htr") -> str:
    """Submission ID: ``<pipeline>-chunk-NNN-of-MMM-YYYYMMDDTHHMMSS``.

    The timestamp suffix makes every submission unique. Ray's REST API rejects
    duplicate submission_ids (even for previously-completed or -deleted jobs),
    so without it, stopping and re-submitting the same chunk would fail. The
    prefix (`htr-` / `prefetch-` / `htrflow-`) is still parseable by the
    pipeline classifier in `viewer.services.orchestrator._pipeline_for`.
    """
    suffix = datetime.now(UTC).strftime("%Y%m%dT%H%M%S")
    return f"{pipeline}-chunk-{chunk_id:03d}-of-{chunk_total:03d}-{suffix}"


def build_entrypoint(
    batch_ids: list[str],
    *,
    cache_bucket: str,
    output: str,
    iiif_url: str,
    pipeline: str,
) -> str:
    """Build the `runner` invocation that processes all batch_ids in one job."""
    parts = [
        "uv run --project projects/runner runner",
        f"--cache-bucket {cache_bucket}",
        f"--output {output}",
        f"--iiif-url {iiif_url}",
        f"--pipeline {pipeline}",
        *(f"--batch {b}" for b in batch_ids),
    ]
    return " \\\n  ".join(parts)


def _passthrough_env(env: Mapping[str, str]) -> dict[str, str]:
    return {k: v for k, v in env.items() if k.startswith(_ENV_PASSTHROUGH_PREFIXES)}


async def _fetch_chunk_batches(session: AsyncSession, chunk_id: int) -> tuple[int, list[str]]:
    """Returns (chunk_total, [batch_id, ...]) for batches with manifest_status='ok'."""
    rows = list(
        (
            await session.exec(
                select(Batch.chunk_total, Batch.batch_id)
                .where(col(Batch.chunk_id) == chunk_id, col(Batch.manifest_status) == ManifestStatus.OK)
                .order_by(Batch.batch_id)
            )
        ).all()
    )
    if not rows:
        return 0, []
    return int(rows[0][0] or 0), [r[1] for r in rows]


async def submit_chunk(
    session: AsyncSession,
    ray_client: JobSubmissionClient,
    *,
    chunk_id: int,
    repo_root: Path,
    cache_bucket: str = "images-batch",
    output: str = "s3://images-batch-alto",
    iiif_url: str = "https://iiifintern-ai.ra.se",
    pipeline: str = "htr",
    env: Mapping[str, str] | None = None,
) -> SubmitResult:
    """Submit one chunk to Ray. For htr/htrflow, also tag current_rayjob_id on the rows."""
    chunk_total, batch_ids = await _fetch_chunk_batches(session, chunk_id)
    if not batch_ids:
        raise ValueError(f"no batches found for chunk_id={chunk_id}")

    entrypoint = build_entrypoint(
        batch_ids,
        cache_bucket=cache_bucket,
        output=output,
        iiif_url=iiif_url,
        pipeline=pipeline,
    )

    def _submit() -> str:
        return ray_client.submit_job(
            entrypoint=entrypoint,
            submission_id=chunk_name(chunk_id, chunk_total, pipeline),
            runtime_env={
                "working_dir": str(repo_root),
                "env_vars": _passthrough_env(env if env is not None else os.environ),
            },
            metadata={
                "chunk_id": str(chunk_id),
                "chunk_total": str(chunk_total),
                "batches": ",".join(batch_ids),
            },
        )

    submission_id = await to_thread.run_sync(_submit)

    if pipeline in _HTR_PIPELINES:
        now = datetime.now(UTC).isoformat(timespec="seconds")
        result = await session.exec(select(Batch).where(col(Batch.chunk_id) == chunk_id))
        for batch in result.all():
            batch.current_rayjob_id = submission_id
            batch.current_rayjob_submitted_at = now
            session.add(batch)
        await session.commit()

    return SubmitResult(
        chunk_id=chunk_id,
        chunk_total=chunk_total,
        pipeline=pipeline,
        submission_id=submission_id,
        batches=batch_ids,
    )


async def stop_chunk(
    session: AsyncSession,
    ray_client: JobSubmissionClient,
    *,
    chunk_id: int,
) -> StopResult:
    """Stop the Ray job currently bound to a chunk and clear the row markers.

    All batches in the chunk share one `current_rayjob_id` (set by
    `submit_chunk`), so we pick any row to read it from. Ray's `stop_job`
    returns False if the job was already in a terminal state — which we
    still treat as "stopped" from the operator's POV.
    """
    submission_id = (await session.exec(select(Batch.current_rayjob_id).where(col(Batch.chunk_id) == chunk_id).limit(1))).first()
    if not submission_id:
        raise ValueError(f"no running job for chunk_id={chunk_id}")

    stopped = await to_thread.run_sync(ray_client.stop_job, submission_id)

    batches = await session.exec(select(Batch).where(col(Batch.chunk_id) == chunk_id))
    for batch in batches.all():
        batch.current_rayjob_id = None
        batch.current_rayjob_submitted_at = None
        session.add(batch)
    await session.commit()

    return StopResult(chunk_id=chunk_id, stopped_submission_id=submission_id, stopped=stopped)
