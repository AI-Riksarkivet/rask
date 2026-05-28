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
from ray.job_submission import JobSubmissionClient
from sqlmodel import col, select
from sqlmodel.ext.asyncio.session import AsyncSession

from viewer.core.exceptions import ServiceUnavailableError
from viewer.models.batch import Batch
from viewer.models.enums import ManifestStatus
from viewer.schemas.chunk import ChunkBatches, StopResult, SubmitResult
from viewer.services.ray_dashboard import RAY_TRANSIENT_ERRORS


log = logging.getLogger(__name__)

_ENV_PASSTHROUGH_PREFIXES = ("AWS_", "HCP_", "IIIF_", "RASK_")
_HTR_PIPELINES = frozenset({"htr", "htrflow"})


def chunk_name(chunk_id: int, chunk_total: int, pipeline: str = "htr") -> str:
    """Submission ID: ``<pipeline>-chunk-NNN-of-MMM-YYYYMMDDTHHMMSS``.

    The timestamp suffix makes every submission unique. Ray's REST API rejects
    duplicate submission_ids (even for previously-completed or -deleted jobs),
    so without it, stopping and re-submitting the same chunk would fail. The
    prefix (`htr-` / `prefetch-` / `htrflow-`) is still parseable by the
    pipeline classifier in `viewer.services.orchestrator.derive._pipeline_for`.
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


async def _fetch_chunk_batches(session: AsyncSession, chunk_id: int) -> ChunkBatches:
    """Membership of one chunk: `chunk_total` + ordered batch_ids with manifest_status='ok'."""
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
        return ChunkBatches(chunk_total=0, batch_ids=[])
    return ChunkBatches(chunk_total=int(rows[0][0] or 0), batch_ids=[r[1] for r in rows])


async def submit_chunk(
    session: AsyncSession,
    ray_client: JobSubmissionClient,
    *,
    chunk_id: int,
    repo_root: Path,
    cache_bucket: str,
    output: str,
    iiif_url: str,
    pipeline: str = "htr",
    env: Mapping[str, str] | None = None,
) -> SubmitResult:
    """Submit one chunk to Ray. For htr/htrflow, also tag current_rayjob_id on the rows."""
    membership = await _fetch_chunk_batches(session, chunk_id)
    if not membership.batch_ids:
        raise ValueError(f"no batches found for chunk_id={chunk_id}")

    entrypoint = build_entrypoint(
        membership.batch_ids,
        cache_bucket=cache_bucket,
        output=output,
        iiif_url=iiif_url,
        pipeline=pipeline,
    )

    def _submit() -> str:
        return ray_client.submit_job(
            entrypoint=entrypoint,
            submission_id=chunk_name(chunk_id, membership.chunk_total, pipeline),
            runtime_env={
                "working_dir": str(repo_root),
                "env_vars": _passthrough_env(env if env is not None else os.environ),
            },
            metadata={
                "chunk_id": str(chunk_id),
                "chunk_total": str(membership.chunk_total),
                "batches": ",".join(membership.batch_ids),
            },
        )

    try:
        submission_id = await to_thread.run_sync(_submit)
    except RAY_TRANSIENT_ERRORS as exc:
        raise ServiceUnavailableError(f"Ray submit failed for chunk {chunk_id}: {exc}") from exc

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
        chunk_total=membership.chunk_total,
        pipeline=pipeline,
        submission_id=submission_id,
        batches=membership.batch_ids,
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
    returns False if the job was already terminal; if the job no longer
    exists on the cluster (pruned / cluster restarted) the SDK raises, which
    we treat as already-stopped so the row markers still get cleared.
    """
    submission_id = (await session.exec(select(Batch.current_rayjob_id).where(col(Batch.chunk_id) == chunk_id).limit(1))).first()
    if not submission_id:
        raise ValueError(f"no running job for chunk_id={chunk_id}")

    try:
        stopped = await to_thread.run_sync(ray_client.stop_job, submission_id)
    except RAY_TRANSIENT_ERRORS as exc:
        log.warning(f"stop_job failed for {submission_id} (treating as already stopped): {exc}")
        stopped = False

    batches = await session.exec(select(Batch).where(col(Batch.chunk_id) == chunk_id))
    for batch in batches.all():
        batch.current_rayjob_id = None
        batch.current_rayjob_submitted_at = None
        session.add(batch)
    await session.commit()

    return StopResult(chunk_id=chunk_id, stopped_submission_id=submission_id, stopped=stopped)
